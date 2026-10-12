from datetime import date, datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

MAX_BATCH = 500


class SyncRegistry(models.Model):
    """REMOTE side. Public methods are callable through /json/2/sync.registry/<method>.
    Methods starting with an underscore are NOT exposed by JSON-2."""

    _name = "sync.registry"
    _description = "Synchronization Registry"
    _order = "id desc"

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
    # API used by the local side
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
                "writable": (not field.compute) or bool(field.inverse),
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
    def get_xmlids(self, model_name, ids):
        """Return {str(id): 'module.name'} for the given ids."""
        self._get_sync_model(model_name)
        mapping = self._get_xmlid_map(model_name, list(ids))
        return {str(res_id): xmlid for res_id, xmlid in mapping.items()}
