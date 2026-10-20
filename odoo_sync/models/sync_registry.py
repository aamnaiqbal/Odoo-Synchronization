import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SyncRegistry(models.Model):
    """REMOTE side of the push.

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
