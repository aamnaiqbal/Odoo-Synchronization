from odoo import api, fields, models


class SyncRecordMap(models.Model):
    """local id <-> remote id of the records created locally and pushed to the remote.

    This is what resolves the relational fields of the records pushed later.

    Records that came with the imported database are NOT stored here: they have the
    same id on both sides, which is known from the connection baseline
    (see `sync.connection._get_baseline_max_id`).
    """

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
    # Reserved for the future two-way synchronization: not filled by the one-way push.
    remote_write_date = fields.Datetime(
        help="write_date of the remote record when it was last synchronized. "
        "Will be used to detect conflicts once the synchronization is two-way."
    )
    # Exact write_date (with microseconds) of the remote record when it was last pulled.
    # A record fetched again with the same stamp (the pull re-reads a short overlap)
    # is already up to date and is skipped.
    remote_stamp = fields.Char(readonly=True)
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
    def _get_remote_id(self, connection, model, local_id):
        """Which record of the remote does this local record correspond to?

        1. a mapping row           -> record created locally and pushed
        2. id <= baseline of model -> record that came with the imported database,
                                      it has the same id on the remote
        3. otherwise False         -> created locally, not pushed yet"""
        mapping = self.search(
            [
                ("connection_id", "=", connection.id),
                ("model", "=", model),
                ("local_id", "=", local_id),
            ],
            limit=1,
        )
        if mapping:
            return mapping.remote_id
        if local_id <= connection._get_baseline_max_id(model):
            return local_id
        return False

    @api.model
    def _get_mapping_by_remote(self, connection, model, remote_id):
        """The mapping row of a remote record (empty if it has none)."""
        return self.search(
            [
                ("connection_id", "=", connection.id),
                ("model", "=", model),
                ("remote_id", "=", remote_id),
            ],
            limit=1,
        )

    @api.model
    def _get_local_id(self, connection, model, remote_id):
        """Reverse of `_get_remote_id`: which local record is this remote record?
        Used by the pull (remote -> local).

        1. a mapping row            -> record pulled earlier
        2. id <= baseline of model  -> record that came with the imported database,
                                       it has the same id locally
        3. otherwise False          -> a record this database has not seen yet.
        A record above the baseline is never matched by id: the local sequence has
        moved on and that id may belong to a different local record."""
        mapping = self._get_mapping_by_remote(connection, model, remote_id)
        local_id = mapping.local_id if mapping else (
            remote_id if remote_id <= connection._get_baseline_max_id(model) else False
        )
        if local_id and self.env[model].browse(local_id).exists():
            return local_id
        return False

    @api.model
    def _set(self, connection, model, local_id, remote_id, remote_write_date=False, remote_stamp=False):
        """Create or update the mapping of one record."""
        base = [("connection_id", "=", connection.id), ("model", "=", model)]
        mapping = self.search(base + [("remote_id", "=", remote_id)], limit=1) or self.search(
            base + [("local_id", "=", local_id)], limit=1
        )
        vals = {
            "connection_id": connection.id,
            "model": model,
            "local_id": local_id,
            "remote_id": remote_id,
            "remote_write_date": remote_write_date or False,
            "remote_stamp": remote_stamp or False,
            "last_synced": fields.Datetime.now(),
        }
        if mapping:
            mapping.write(vals)
            return mapping
        return self.create(vals)
