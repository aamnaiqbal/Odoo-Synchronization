import logging
from datetime import date, datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

MAX_BATCH = 500


class SyncRegistry(models.Model):
    """REMOTE side. Public methods are callable through /json/2/sync.registry/<method>.
    Methods starting with an underscore are NOT exposed by JSON-2.

    Also stores the sync_uuid -> remote id link of the records pushed from a local Odoo."""

    _name = "sync.registry"
    _description = "Synchronization Registry"
    _order = "id desc"

    name = fields.Char(required=True)
    model_name = fields.Char(required=True, index=True)
    sync_key = fields.Char(required=True, index=True)
    remote_id = fields.Integer(required=True)
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

    @api.model
    def _serialize(self, value):
        if isinstance(value, datetime):  # check before date (it is a subclass)
            return fields.Datetime.to_string(value)
        if isinstance(value, date):
            return fields.Date.to_string(value)
        if isinstance(value, bytes):
            return value.decode()
        return value

    @api.model
    def _get_xmlid_map(self, model_name, ids):
        data = self.env["ir.model.data"].sudo().search_read(
            [
                ("model", "=", model_name),
                ("res_id", "in", ids),
                ("module", "!=", "__export__"),
            ],
            ["res_id", "module", "name"],
        )
        return {d["res_id"]: f"{d['module']}.{d['name']}" for d in data}

    # ------------------------------------------------------------------
    # API used by the local side: configuration fetch
    # ------------------------------------------------------------------
    @api.model
    def get_fields(self, model_name):
        """Describe the fields of a model so the local side can build its field list."""
        Model = self._get_sync_model(model_name)
        return {
            name: {
                "type": field.type,
                "relation": field.comodel_name or False,
                "store": field.store,
                # writable = plain stored field, or computed field with an inverse
                "writable": (not field.compute) or bool(field.inverse) or (not field.readonly),
                "required": field.required,
            }
            for name, field in Model._fields.items()
        }

    @api.model
    def fetch_batch(self, model_name, field_names, domain=None, after_id=0, limit=50):
        """Keyset-paginated read. Relational fields come back as plain ids
        (load=False) and every row carries its xmlid (if any) in `_sync_xmlid`."""
        Model = self._get_sync_model(model_name).with_context(active_test=False)
        limit = max(1, min(int(limit), MAX_BATCH))
        domain = list(domain or []) + [("id", ">", int(after_id))]
        records = Model.search(domain, order="id asc", limit=limit)

        accessible = set(Model.fields_get(list(field_names), attributes=["type"]))
        to_read = [name for name in field_names if name in accessible]
        if "write_date" not in to_read:
            to_read.append("write_date")

        rows = records.read(to_read, load=False)
        xmlids = self._get_xmlid_map(model_name, records.ids)
        for row in rows:
            for key, value in list(row.items()):
                row[key] = self._serialize(value)
            row["_sync_xmlid"] = xmlids.get(row["id"], False)
        return {"records": rows}

    @api.model
    def get_xmlids(self, model_name, record_ids):
        """Return {str(id): 'module.name'} for the given ids."""
        self._get_sync_model(model_name)
        mapping = self._get_xmlid_map(model_name, list(record_ids))
        return {str(res_id): xmlid for res_id, xmlid in mapping.items()}

    # ------------------------------------------------------------------
    # API used by the local side: push
    # ------------------------------------------------------------------
    @api.model
    def get_remote_id(self, model_name, sync_key):
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
    def apply(self, model_name, sync_uuid, operation, values=None, remote_id=None):
        """Create / update / delete one record pushed by the local Odoo.

        `remote_id` is the id this record already has on this server (known by the
        local side because the record was fetched from here, or pushed before). It
        lets an update reach the right record even if no registry row exists yet."""
        if not model_name:
            raise UserError(_("Model name is required."))
        if not sync_uuid:
            raise UserError(_("Sync UUID is required."))

        Model = self._get_sync_model(model_name)
        _logger.debug(
            "[SYNC APPLY] model=%s uuid=%s op=%s remote_id=%s", model_name, sync_uuid, operation, remote_id
        )

        registry = self.search(
            [("model_name", "=", model_name), ("sync_key", "=", sync_uuid)], limit=1
        )

        if operation == "delete":
            return self._apply_delete(registry, model_name, sync_uuid)

        warnings = []
        values = self._resolve_relations(values or {}, warnings)
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
                self.create(
                    {
                        "name": f"{model_name}:{sync_uuid}",
                        "model_name": model_name,
                        "sync_key": sync_uuid,
                        "remote_id": record.id,
                    }
                )
            return {
                "success": True,
                "remote_id": record.id,
                "operation": "update",
                "warnings": warnings,
            }

        record = Model.create(values)
        self.create(
            {
                "name": f"{model_name}:{sync_uuid}",
                "model_name": model_name,
                "sync_key": sync_uuid,
                "remote_id": record.id,
            }
        )
        return {
            "success": True,
            "remote_id": record.id,
            "operation": "create",
            "warnings": warnings,
        }

    def _apply_delete(self, registry, model_name, sync_uuid):
        if not registry:
            return {"success": True, "remote_id": False, "operation": "delete"}

        record = self.env[model_name].browse(registry.remote_id)
        if record.exists():
            record.with_context(skip_sync=True).unlink()
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

        The local side sends every hint it has; they are tried from the most to the
        least reliable:
          1. remote_id  - the remote id stored in the local sync.record.map
                          (record fetched from here, or pushed before)
          2. xmlid      - standard data (countries, companies, ...) that has the same
                          xmlid on both sides
          3. sync_uuid  - record pushed earlier by the registry (sync.registry)
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
            if self._is_relation_key(value):
                remote_id = self._resolve_one_key(field_name, value, warnings)
                if remote_id:
                    result[field_name] = remote_id
                continue

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
