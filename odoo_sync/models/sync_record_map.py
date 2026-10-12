from odoo import api, fields, models


class SyncRecordMap(models.Model):
    """remote id <-> local id for every record fetched from the remote.
    This is what the future local -> remote push will use."""

    _name = "sync.record.map"
    _description = "Remote / Local Record Mapping"
    _order = "model, remote_id"
    _rec_name = "model"

    connection_id = fields.Many2one(
        "sync.connection", required=True, ondelete="cascade", index=True
    )
    model = fields.Char(required=True, index=True)
    local_id = fields.Integer(required=True, index=True)
    remote_id = fields.Integer(required=True, index=True)
    remote_xmlid = fields.Char()
    remote_write_date = fields.Datetime(
        help="write_date of the remote record when it was last fetched. "
        "Used to skip unchanged records and, later, to detect conflicts."
    )
    last_synced = fields.Datetime(default=fields.Datetime.now)

    _remote_uniq = models.Constraint(
        "UNIQUE(connection_id, model, remote_id)",
        "This remote record is already mapped.",
    )
    _local_uniq = models.Constraint(
        "UNIQUE(connection_id, model, local_id)",
        "This local record is already mapped.",
    )

    @api.model
    def _load_by_remote(self, connection, model, remote_ids):
        maps = self.search(
            [
                ("connection_id", "=", connection.id),
                ("model", "=", model),
                ("remote_id", "in", remote_ids),
            ]
        )
        return {m.remote_id: m for m in maps}

    @api.model
    def _get_local_id(self, connection, model, remote_id):
        mapping = self.search(
            [
                ("connection_id", "=", connection.id),
                ("model", "=", model),
                ("remote_id", "=", remote_id),
            ],
            limit=1,
        )
        if mapping and self.env[model].browse(mapping.local_id).exists():
            return mapping.local_id
        return False

    @api.model
    def _get_remote_id(self, connection, model, local_id):
        """For the future push: which remote record does this local record map to?"""
        mapping = self.search(
            [
                ("connection_id", "=", connection.id),
                ("model", "=", model),
                ("local_id", "=", local_id),
            ],
            limit=1,
        )
        return mapping.remote_id or False

    @api.model
    def _set(self, connection, model, local_id, remote_id, xmlid=False, remote_write_date=False):
        base = [("connection_id", "=", connection.id), ("model", "=", model)]
        mapping = self.search(base + [("remote_id", "=", remote_id)], limit=1) or self.search(
            base + [("local_id", "=", local_id)], limit=1
        )
        vals = {
            "connection_id": connection.id,
            "model": model,
            "local_id": local_id,
            "remote_id": remote_id,
            "remote_xmlid": xmlid or False,
            "remote_write_date": remote_write_date or False,
            "last_synced": fields.Datetime.now(),
        }
        if mapping:
            mapping.write(vals)
            return mapping
        return self.create(vals)
