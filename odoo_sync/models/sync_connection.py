import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL
from odoo.tools.sql import existing_tables

_logger = logging.getLogger(__name__)


class SyncConnection(models.Model):
    """Connection from this (local) Odoo to the remote (server) Odoo.

    How the one-way flow (local -> remote) starts:

      1. The remote Odoo is configured, then its database is exported.
      2. The dump is imported into the local Odoo. Every record that existed at
         export time therefore has the SAME id on both sides.
      3. The *baseline* is stored on this connection: the highest id of every
         table at that moment. It separates the records that came with the dump
         (same id on the remote, nothing to map) from the records created
         locally afterwards (they get a sync.record.map row once pushed).
      4. Records created locally are queued (sync.queue) and pushed by a cron.
    """

    _name = "sync.connection"
    _description = "Remote Odoo Synchronization Connection"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    base_url = fields.Char(
        string="Remote URL",
        required=True,
        help="Example: https://my-odoo.example.com",
    )
    database = fields.Char(required=True)
    api_key = fields.Char(required=True, copy=False)
    timeout = fields.Integer(default=30, required=True)
    batch_size = fields.Integer(default=50, required=True)

    # ------------------------------------------------------------------
    # Baseline of the imported database
    # ------------------------------------------------------------------
    baseline = fields.Json(
        readonly=True,
        copy=False,
        help="{model: highest record id found right after the database import}. "
        "A record with an id up to this value came with the imported database, so it "
        "has the same id on the remote. Higher ids were created locally afterwards.",
    )
    baseline_date = fields.Datetime(
        string="Baseline Captured On",
        readonly=True,
        copy=False,
        help="Nothing is pushed until the baseline is captured.",
    )
    baseline_model_count = fields.Integer(compute="_compute_baseline_model_count")

    pending_count = fields.Integer(string="Pending Jobs", compute="_compute_pending_count")

    @api.depends("baseline")
    def _compute_baseline_model_count(self):
        for connection in self:
            connection.baseline_model_count = len(connection.baseline or {})

    def _compute_pending_count(self):
        data = self.env["sync.queue"]._read_group(
            [("connection_id", "in", self.ids), ("state", "in", ("pending", "processing"))],
            ["connection_id"],
            ["__count"],
        )
        counts = {connection.id: count for connection, count in data}
        for connection in self:
            connection.pending_count = counts.get(connection.id, 0)

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        # The first connection is created right after the database import, before
        # any user creates records, so this is the right moment to take the
        # baseline. Later connections never re-capture it automatically: that would
        # wrongly treat the records created locally in the meantime as "imported".
        is_first_connection = not self.with_context(active_test=False).search_count([])
        connections = super().create(vals_list)
        if is_first_connection:
            connections[:1]._capture_baseline()
        return connections

    # ------------------------------------------------------------------
    # Baseline
    # ------------------------------------------------------------------
    def _capture_baseline(self):
        """Store the highest id of every table of the local database."""
        self.ensure_one()
        cr = self.env.cr

        # Real tables only: no abstract / transient models, no SQL views.
        models_by_table = {}
        for model_name in self.env.registry:
            model = self.env[model_name]
            if model._abstract or model._transient or not model._auto:
                continue
            models_by_table[model._table] = model_name
        tables = existing_tables(cr, models_by_table)

        baseline = {}
        for table in tables:
            cr.execute(SQL("SELECT COALESCE(MAX(id), 0) FROM %s", SQL.identifier(table)))
            baseline[models_by_table[table]] = cr.fetchone()[0]

        self.write({"baseline": baseline, "baseline_date": fields.Datetime.now()})
        _logger.info("[SYNC] baseline captured for %s model(s)", len(baseline))
        return len(baseline)

    def _get_baseline_max_id(self, model_name):
        """Highest id that came with the imported database (0 = none / unknown model)."""
        self.ensure_one()
        return (self.baseline or {}).get(model_name, 0)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _get_headers(self):
        self.ensure_one()
        return {
            "Authorization": f"bearer {self.api_key}",
            "X-Odoo-Database": self.database,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _json2_call(self, model, method, payload):
        self.ensure_one()
        url = self.base_url.rstrip("/") + "/json/2/" + model + "/" + method
        _logger.debug("JSON-2 request %s payload=%s", url, payload)
        response = requests.post(
            url,
            headers=self._get_headers(),
            json=payload,
            timeout=self.timeout,
        )
        _logger.debug("JSON-2 response %s: %s", response.status_code, response.text[:5000])

        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            _logger.exception("JSON-2 HTTP ERROR: %s", error)
            raise UserError(
                _(
                    "Remote JSON-2 request failed.\n\n"
                    "Status: %s\n"
                    "URL: %s\n\n"
                    "Remote response:\n%s"
                )
                % (response.status_code, url, response.text[:3000])
            ) from error
        return response.json()

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        try:
            result = self._json2_call("sync.registry", "ping", {})
        except requests.RequestException as error:
            raise UserError(_("Connection failed:\n%s", error)) from error
        raise UserError(_("Connection successful.\n\nRemote response:\n%s", result))

    def action_capture_baseline(self):
        """(Re)capture the baseline. Only allowed while nothing was queued or pushed,
        otherwise the records created since the import would be taken for imported ones."""
        self.ensure_one()
        domain = [("connection_id", "=", self.id)]
        already_used = self.env["sync.queue"].search_count(domain) or self.env[
            "sync.record.map"
        ].search_count(domain)
        if already_used:
            raise UserError(
                _(
                    "The baseline cannot be captured again: records were already queued or "
                    "pushed with the current one."
                )
            )
        count = self._capture_baseline()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Baseline captured"),
                "message": _("%s models recorded.", count),
                "type": "success",
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_view_queue(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Synchronization Queue"),
            "res_model": "sync.queue",
            "view_mode": "list,form",
            "domain": [("connection_id", "=", self.id)],
        }
