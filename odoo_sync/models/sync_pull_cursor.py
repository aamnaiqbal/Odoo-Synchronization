from odoo import api, fields, models
from odoo.tools import SQL

PULL_DT_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
PULL_EPOCH = "1970-01-01 00:00:00.000000"


class SyncPullCursor(models.Model):
    """Where the pull of one model stopped: the (write_date, id) of the last remote
    record that was applied. Only records after this position are fetched next time.

    The first position of a model is the newest write_date among the records that
    came with the imported database, so everything the remote changed after its
    export is fetched. If that guess is wrong an administrator can edit the cursor.
    """

    _name = "sync.pull.cursor"
    _description = "Remote -> Local Pull Cursor"
    _order = "connection_id, model"
    _rec_name = "model"

    connection_id = fields.Many2one(
        "sync.connection", required=True, ondelete="cascade", index=True
    )
    model = fields.Char(required=True, index=True)
    last_write_date = fields.Char(
        required=True,
        default=PULL_EPOCH,
        help="write_date of the last applied remote record, as text with microseconds "
        "(YYYY-MM-DD HH:MM:SS.ffffff, UTC).",
    )
    last_id = fields.Integer(help="id of the last applied remote record (tie-breaker).")
    last_run = fields.Datetime(readonly=True)
    created_count = fields.Integer(readonly=True, help="Total created locally by the pull.")
    updated_count = fields.Integer(readonly=True, help="Total updated locally by the pull.")
    skipped_count = fields.Integer(
        readonly=True, help="Total left untouched because the local record has an unsent change."
    )
    last_error = fields.Text(
        readonly=True,
        help="The pull of this model stops at the record that failed and retries it on the "
        "next run, so nothing is lost. It is cleared by the next successful run.",
    )

    _connection_model_uniq = models.Constraint(
        "UNIQUE(connection_id, model)",
        "There is already a pull cursor for this model.",
    )

    @api.model
    def _initial_write_date(self, connection, model_name):
        base = connection._get_baseline_max_id(model_name)
        if base:
            Model = self.env[model_name]
            self.env.cr.execute(
                SQL(
                    "SELECT MAX(write_date) FROM %s WHERE id <= %s",
                    SQL.identifier(Model._table), base,
                )
            )
            value = self.env.cr.fetchone()[0]
            if value:
                return value.strftime(PULL_DT_FORMAT)
        return PULL_EPOCH

    @api.model
    def _get_or_create(self, connection, model_name):
        cursor = self.search(
            [("connection_id", "=", connection.id), ("model", "=", model_name)], limit=1
        )
        if cursor:
            return cursor
        return self.create(
            {
                "connection_id": connection.id,
                "model": model_name,
                "last_write_date": self._initial_write_date(connection, model_name),
                "last_id": 0,
            }
        )
