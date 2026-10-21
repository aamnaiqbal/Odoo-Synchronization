import logging
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)


class SyncRegistry(models.Model):
    """REMOTE side of the synchronization.

    * push (local -> remote): `apply` and `get_remote_id`
    * fetch (remote -> local): `fetch_changes` and `fetch_ids`

    Public methods are callable through /json/2/sync.registry/<method>.
    Methods starting with an underscore are NOT exposed by JSON-2.

    Besides applying the pushed records, it stores the sync_uuid -> remote id link of
    every record created by a push. That makes a retried push idempotent: if the
    remote created the record but the local side never received the answer, the next
    attempt finds the row and updates the record instead of creating a duplicate.
    """

    _name = "sync.registry"
    _description = "Synchronization Registry"
    _order = "id desc"

    name = fields.Char(required=True)
    model_name = fields.Char(required=True, index=True)
    sync_key = fields.Char(required=True, index=True)
    remote_id = fields.Integer(required=True)
    source = fields.Char(
        index=True,
        readonly=True,
        help="Name of the local instance (its connection name) that first pushed this record.",
    )
    active = fields.Boolean(default=True)

    _model_sync_key_unique = models.Constraint(
        "UNIQUE(model_name, sync_key)",
        "A synchronization key must be unique per model.",
    )

    @api.model
    def ping(self):
        return {
            "success": True,
            "message": "Sync server is reachable.",
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @api.model
    def _get_sync_model(self, model_name):
        if model_name not in self.env:
            raise UserError(_("Model %s does not exist on this server.", model_name))
        return self.env[model_name]

    # ------------------------------------------------------------------
    # API used by the local side: push
    # ------------------------------------------------------------------
    @api.model
    def get_remote_id(self, model_name, sync_key):
        """Id on this server of the record pushed with this sync_uuid (False if none)."""
        registry = self.search(
            [
                ("model_name", "=", model_name),
                ("sync_key", "=", sync_key),
                ("active", "=", True),
            ],
            limit=1,
        )
        return registry.remote_id if registry else False

    @api.model
    def apply(self, model_name, sync_uuid, operation, values=None, remote_id=None, source=None):
        """Create / update / delete one record pushed by the local Odoo.

        `source` is the name of the pushing local instance, kept on the registry row
        to trace where a record came from when several local instances push here.

        `remote_id` is the id this record already has on this server, as far as the
        local side knows it (the record came with the imported database, or was pushed
        before). It lets an update reach the right record even if no registry row
        exists for it."""
        if not model_name:
            raise UserError(_("Model name is required."))
        if not sync_uuid:
            raise UserError(_("Sync UUID is required."))

        Model = self._get_sync_model(model_name)
        _logger.debug(
            "[SYNC APPLY] model=%s uuid=%s op=%s remote_id=%s",
            model_name, sync_uuid, operation, remote_id,
        )

        registry = self.search(
            [("model_name", "=", model_name), ("sync_key", "=", sync_uuid)], limit=1
        )

        if operation == "delete":
            return self._apply_delete(registry, model_name, remote_id)

        warnings = []
        values = self._resolve_relations(values or {}, warnings)
        # skip_sync: what is applied here must never be queued again on this server.
        Model = Model.with_context(skip_sync=True)

        record = Model.browse()
        if registry:
            record = Model.browse(registry.remote_id).exists()
            if not record:
                registry.unlink()
                registry = self.browse()
        if not record and remote_id:
            record = Model.browse(int(remote_id)).exists()

        if record:
            if values:
                record.write(values)
            if not registry:
                self._create_registry_row(model_name, sync_uuid, record.id, source)
            return {
                "success": True,
                "remote_id": record.id,
                "operation": "update",
                "warnings": warnings,
            }

        record = Model.create(values)
        self._create_registry_row(model_name, sync_uuid, record.id, source)
        return {
            "success": True,
            "remote_id": record.id,
            "operation": "create",
            "warnings": warnings,
        }

    @api.model
    def _create_registry_row(self, model_name, sync_uuid, remote_id, source=None):
        return self.create(
            {
                "name": f"{model_name}:{sync_uuid}",
                "model_name": model_name,
                "sync_key": sync_uuid,
                "remote_id": remote_id,
                "source": source or False,
            }
        )

    def _apply_delete(self, registry, model_name, remote_id=None):
        target = registry.remote_id if registry else remote_id
        if target:
            record = self.env[model_name].browse(int(target))
            if record.exists():
                record.with_context(skip_sync=True).unlink()
            # Several local instances may each have a row for the same remote record
            # (an imported record gets its own sync_uuid on every instance): drop them all.
            self.search([("model_name", "=", model_name), ("remote_id", "=", int(target))]).unlink()
        if registry:
            registry.unlink()
        return {"success": True, "remote_id": False, "operation": "delete"}

    # ------------------------------------------------------------------
    # Relation resolution (key sent by the local side -> id on THIS server)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_relation_key(value):
        return isinstance(value, dict) and "model" in value

    @api.model
    def _resolve_relation_key(self, key):
        """Return the id on this server for a relation key, or False.

        The local side sends the hints it has (see sync.mixin._sync_get_relation_key);
        they are tried from the most to the least reliable:
          1. remote_id  - id of the record on this server: the record came with the
                          imported database (same id) or was pushed before
          2. xmlid      - standard data that has the same xmlid on both sides
          3. sync_uuid  - record created locally and pushed earlier (registry row)
        """
        model = key.get("model")
        if not model or model not in self.env:
            return False
        Model = self.env[model].with_context(active_test=False)

        if key.get("remote_id"):
            record = Model.browse(int(key["remote_id"])).exists()
            if record:
                return record.id
        if key.get("xmlid"):
            record = self.env.ref(key["xmlid"], raise_if_not_found=False)
            if record and record._name == model:
                return record.id
        if key.get("sync_uuid"):
            remote_id = self.get_remote_id(model, key["sync_uuid"])
            if remote_id and Model.browse(remote_id).exists():
                return remote_id
        return False

    @api.model
    def _resolve_one_key(self, field_name, key, warnings):
        """Resolve a single relation key. Raises (so the job is retried later) only
        when the target is a syncable record (it has a sync_uuid) that has not been
        pushed yet; any other unresolved target is skipped and reported."""
        remote_id = self._resolve_relation_key(key)
        if remote_id:
            return remote_id
        if key.get("sync_uuid"):
            raise UserError(
                _("Related record not synchronized yet: %s / %s", key["model"], key["sync_uuid"])
            )
        warnings.append(
            f"{field_name}: {key.get('model')} could not be resolved on the remote "
            f"(hints: {sorted(k for k in key if k != 'model')}), skipped."
        )
        return False

    def _resolve_relations(self, values, warnings=None):
        warnings = warnings if warnings is not None else []
        result = {}
        for field_name, value in values.items():
            # many2one
            if self._is_relation_key(value):
                remote_id = self._resolve_one_key(field_name, value, warnings)
                if remote_id:
                    result[field_name] = remote_id
                continue

            # many2many
            if isinstance(value, list):
                resolved_list = []
                for item in value:
                    if self._is_relation_key(item):
                        remote_id = self._resolve_one_key(field_name, item, warnings)
                        if remote_id:
                            resolved_list.append(remote_id)
                    else:
                        resolved_list.append(item)
                result[field_name] = [(6, 0, resolved_list)]
                continue

            result[field_name] = value
        return result

    # ------------------------------------------------------------------
    # API used by the local side: fetch (remote -> local)
    # ------------------------------------------------------------------
    # Never sent to the local side.
    _FETCH_SKIPPED_FIELDS = frozenset({
        "id",
        "create_uid",
        "create_date",
        "write_uid",
        "write_date",
        "__last_update",
        "sync_uuid",
        "parent_path",
        "message_main_attachment_id",
    })
    # Field types that are sent (binary / image only when asked for). one2many are
    # left out: the records on the "many" side are fetched as models of their own.
    _FETCH_FIELD_TYPES = (
        "char", "text", "html", "selection", "boolean", "integer", "float", "monetary",
        "date", "datetime", "many2one", "many2many",
    )

    @api.model
    def fetch_changes(
        self, model_name, since_write_date=False, since_id=0, domain=None,
        limit=200, include_binary=False, excluded_fields=None,
    ):
        """One page of the records of `model_name` written after a cursor.

        The cursor is the pair (write_date, id) of the last record the caller has
        applied; `since_write_date` is that write_date as text WITH its microseconds
        (all the records of one transaction share the same write_date, the id breaks
        the tie). Archived records are included, so that an archiving reaches the
        local side.

        Returns {"records": [...], "has_more": bool}. Each record is
        {"id", "write_date", "xmlid", "values": {...}} where relations are sent as
        keys {"model", "remote_id", "xmlid"}; a product.template also carries the ids
        of its variants (`variant_ids`)."""
        Model = self._get_sync_model(model_name).with_context(active_test=False)
        limit = max(1, min(int(limit or 200), 1000))

        terms = list(domain or [])
        if since_write_date:
            # A datetime object (not text) keeps the microseconds in the comparison.
            since = datetime.fromisoformat(since_write_date)
            terms += [
                "|", ("write_date", ">", since),
                "&", ("write_date", "=", since), ("id", ">", int(since_id or 0)),
            ]

        records = Model.search(terms, order="write_date asc, id asc", limit=limit + 1)
        has_more = len(records) > limit
        records = records[:limit]
        return {
            "records": self._serialize_records(records, include_binary, excluded_fields or []),
            "has_more": has_more,
        }

    @api.model
    def fetch_ids(self, model_name, domain=None):
        """Ids of every record of `model_name` that matches the domain (archived
        ones included). The local side compares them with its own records to find
        out which ones were deleted here."""
        Model = self._get_sync_model(model_name).with_context(active_test=False)
        return Model.search(list(domain or [])).ids

    @api.model
    def _get_fetch_fields(self, records, include_binary=False, excluded_fields=()):
        """Names of the fields of `records` that are sent to the local side."""
        types = self._FETCH_FIELD_TYPES + (("binary", "image") if include_binary else ())
        skipped = self._FETCH_SKIPPED_FIELDS | set(excluded_fields or ())
        names = []
        for name, field in records._fields.items():
            if name in skipped or not field.store or field.type not in types:
                continue
            # A computed field nobody can edit is computed again by the local Odoo.
            # An editable one (compute + store + readonly=False) holds a user choice.
            if (field.compute or field.related) and field.readonly:
                continue
            names.append(name)
        return names

    @api.model
    def _read_fetch_fields(self, records, names):
        """records.read() that leaves out the fields the API user may not read."""
        try:
            return names, records.read(names + ["write_date"], load=None)
        except AccessError:
            readable = []
            for name in names:
                try:
                    records[:1].read([name], load=None)
                except AccessError:
                    continue
                readable.append(name)
            return readable, records.read(readable + ["write_date"], load=None)

    @api.model
    def _get_xmlid_map(self, model_name, ids):
        """{res_id: "module.name"} of the standard data among `ids` (not __export__)."""
        if not ids:
            return {}
        data = self.env["ir.model.data"].sudo().search_read(
            [("model", "=", model_name), ("res_id", "in", list(ids)), ("module", "!=", "__export__")],
            ["module", "name", "res_id"],
        )
        result = {}
        for item in data:
            result.setdefault(item["res_id"], f"{item['module']}.{item['name']}")
        return result

    @api.model
    def _serialize_records(self, records, include_binary=False, excluded_fields=()):
        if not records:
            return []
        names = self._get_fetch_fields(records, include_binary, excluded_fields)
        names, rows = self._read_fetch_fields(records, names)
        rows_by_id = {row["id"]: row for row in rows}

        relation_keys = []      # every relation key, to attach the xmlids in one query per model
        result = []
        for record in records:
            row = rows_by_id[record.id]
            values = {}
            for name in names:
                field = records._fields[name]
                value = row.get(name)
                if field.type == "many2one":
                    if value:
                        key = {"model": field.comodel_name, "remote_id": value}
                        relation_keys.append(key)
                        values[name] = key
                    else:
                        values[name] = False
                elif field.type == "many2many":
                    keys = [{"model": field.comodel_name, "remote_id": rid} for rid in value or []]
                    relation_keys.extend(keys)
                    values[name] = keys
                elif field.type == "datetime":
                    values[name] = fields.Datetime.to_string(value) if value else False
                elif field.type == "date":
                    values[name] = fields.Date.to_string(value) if value else False
                elif field.type in ("binary", "image"):
                    values[name] = value.decode() if isinstance(value, bytes) else (value or False)
                else:
                    values[name] = value

            write_date = row["write_date"]
            if not isinstance(write_date, datetime):
                write_date = fields.Datetime.to_datetime(write_date)
            item = {
                "id": record.id,
                # exact text, microseconds included: it is the pull cursor
                "write_date": write_date.isoformat(sep=" "),
                "values": values,
            }
            if record._name == "product.template":
                # Lets the local side pair the variants (only one per template is supported).
                item["variant_ids"] = record.with_context(active_test=False).product_variant_ids.ids
            result.append(item)

        # xmlids: of the records themselves and of everything they point to
        ids_by_model = {}
        for key in relation_keys:
            ids_by_model.setdefault(key["model"], set()).add(key["remote_id"])
        xmlids = {model: self._get_xmlid_map(model, ids) for model, ids in ids_by_model.items()}
        for key in relation_keys:
            xmlid = xmlids[key["model"]].get(key["remote_id"])
            if xmlid:
                key["xmlid"] = xmlid
        own_xmlids = self._get_xmlid_map(records._name, records.ids)
        for item in result:
            if own_xmlids.get(item["id"]):
                item["xmlid"] = own_xmlids[item["id"]]
        return result
