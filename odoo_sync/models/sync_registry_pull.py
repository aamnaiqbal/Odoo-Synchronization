from collections import defaultdict
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

# write_date is exchanged as text WITH microseconds: the pull cursor is a
# (write_date, id) pair and a rounded date would re-deliver or skip records.
PULL_DT_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
PULL_MAX_LIMIT = 500


class SyncRegistryPull(models.Model):
    """REMOTE side of the pull (remote -> local). Read-only API.

    Public methods are callable through /json/2/sync.registry/<method>.

      pull_changes  records written after a (write_date, id) cursor, oldest first
      pull_records  records by id (used by the local side to fetch a missing dependency)

    Only models that opted in to the synchronization (`_sync_enabled`) can be read.
    The values use the same field rules as the push (`_sync_is_syncable_field`);
    relations are sent as keys the local side resolves (see `_pull_key`).
    """

    _inherit = "sync.registry"

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @api.model
    def _pull_get_model(self, model_name):
        Model = self._get_sync_model(model_name)
        if not getattr(Model, "_sync_enabled", False):
            raise UserError(_("Model %s is not enabled for synchronization.", model_name))
        if not Model._log_access:
            raise UserError(_("Model %s has no write_date, it cannot be pulled.", model_name))
        return Model.with_context(active_test=False)

    @staticmethod
    def _pull_parse_dt(value):
        for fmt in (PULL_DT_FORMAT, "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except (TypeError, ValueError):
                continue
        raise UserError(_("Invalid cursor date: %s", value))

    @staticmethod
    def _pull_format_dt(value):
        return value.strftime(PULL_DT_FORMAT) if value else False

    @api.model
    def _pull_write_dates(self, Model, ids):
        """{id: write_date with microseconds}, straight from the table (the ORM may
        round the value)."""
        if not ids:
            return {}
        self.env.cr.execute(
            SQL("SELECT id, write_date FROM %s WHERE id IN %s", SQL.identifier(Model._table), tuple(ids))
        )
        return dict(self.env.cr.fetchall())

    @api.model
    def _pull_key(self, record, cache):
        """Describe a related record so the local side can find it. The hints are
        tried in this order by `sync.connection._pull_lookup_key`:
          remote_id  id on this server (same id locally for imported records, or a
                     sync.record.map row for records created later)
          xmlid      standard data present on both sides
          sync_keys  sync_uuid(s) of the local instance(s) that pushed this record"""
        ck = (record._name, record.id)
        if ck not in cache:
            xmlid = record.get_external_id().get(record.id) or False
            if xmlid and xmlid.startswith("__export__."):
                xmlid = False
            sync_keys = self.search(
                [("model_name", "=", record._name), ("remote_id", "=", record.id)]
            ).mapped("sync_key")
            cache[ck] = {
                "model": record._name,
                "remote_id": record.id,
                "xmlid": xmlid,
                "sync_keys": sync_keys,
            }
        return cache[ck]

    @api.model
    def _pull_serialize(self, Model, records, write_dates):
        names = [name for name in Model._fields if Model._sync_is_syncable_field(name)]
        rows = self.search([("model_name", "=", Model._name), ("remote_id", "in", records.ids)])
        cache = {}
        out = []
        for record in records:
            values = {}
            for name in names:
                field = Model._fields[name]
                value = record[name]
                if field.type == "many2one":
                    values[name] = self._pull_key(value, cache) if value else False
                elif field.type == "many2many":
                    values[name] = [self._pull_key(item, cache) for item in value]
                elif field.type == "date":
                    values[name] = fields.Date.to_string(value) if value else False
                elif field.type == "datetime":
                    values[name] = fields.Datetime.to_string(value) if value else False
                else:
                    values[name] = value
            out.append(
                {
                    "id": record.id,
                    "write_date": self._pull_format_dt(write_dates.get(record.id)),
                    "key": self._pull_key(record, cache),
                    "values": values,
                }
            )
        return out

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    @api.model
    def pull_changes(self, model_name, write_date, last_id=0, limit=100):
        """Records with (write_date, id) greater than the cursor, oldest first.

        Returns {"records": [...], "cursor": {"write_date", "id"}, "has_more": bool}.
        `cursor` is the last row SCANNED (records hidden from the API user by access
        rules advance it too), so the caller can always continue from it."""
        Model = self._pull_get_model(model_name)
        limit = max(1, min(int(limit or 100), PULL_MAX_LIMIT))
        since = self._pull_parse_dt(write_date)

        self.env.cr.execute(
            SQL(
                "SELECT id, write_date FROM %s WHERE (write_date, id) > (%s, %s) "
                "ORDER BY write_date, id LIMIT %s",
                SQL.identifier(Model._table), since, int(last_id or 0), limit + 1,
            )
        )
        rows = self.env.cr.fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        if not rows:
            return {"records": [], "cursor": False, "has_more": False}

        ids = [row[0] for row in rows]
        visible = set(Model.search([("id", "in", ids)]).ids)
        records = Model.browse([rid for rid in ids if rid in visible])
        last = rows[-1]
        return {
            "records": self._pull_serialize(Model, records, dict(rows)),
            "cursor": {"write_date": self._pull_format_dt(last[1]), "id": last[0]},
            "has_more": has_more,
        }

    @api.model
    def pull_records(self, model_name, ids):
        """Records by id (a missing dependency of a record being pulled)."""
        Model = self._pull_get_model(model_name)
        ids = [int(rid) for rid in (ids or [])][:PULL_MAX_LIMIT]
        records = Model.search([("id", "in", ids)])
        return self._pull_serialize(Model, records, self._pull_write_dates(Model, records.ids))
