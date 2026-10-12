import logging

import requests

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SyncConnection(models.Model):
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

    run_ids = fields.One2many("sync.run", "connection_id", string="Runs")
    run_count = fields.Integer(compute="_compute_run_count")

    def _compute_run_count(self):
        data = self.env["sync.run"]._read_group(
            [("connection_id", "in", self.ids)], ["connection_id"], ["__count"]
        )
        counts = {connection.id: count for connection, count in data}
        for connection in self:
            connection.run_count = counts.get(connection.id, 0)

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
        response = requests.post(
            url,
            headers=self._get_headers(),
            json=payload,
            timeout=self.timeout,
        )
        
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

    def action_load_suggested_models(self):
        self.ensure_one()
        self.env["sync.model.config"]._load_suggested()
        return {
            "type": "ir.actions.act_window",
            "name": _("Models to Sync"),
            "res_model": "sync.model.config",
            "view_mode": "list,form",
        }

    def action_fetch_configuration(self):
        """Queue a run. The heavy work happens in a cron, not in the HTTP request."""
        self.ensure_one()
        if self.env["sync.run"].search_count(
            [("connection_id", "=", self.id), ("state", "in", ("queued", "running"))]
        ):
            raise UserError(_("A fetch is already queued or running for this connection."))
        if not self.env["sync.model.config"].search_count([]):
            raise UserError(
                _("No models configured. Use 'Load Suggested Models' or add some first.")
            )
        run = self.env["sync.run"].create({"connection_id": self.id})
        self.env.ref("odoo_sync.ir_cron_process_sync_runs").sudo()._trigger()
        return {
            "type": "ir.actions.act_window",
            "res_model": "sync.run",
            "res_id": run.id,
            "view_mode": "form",
        }

    def action_view_runs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Runs"),
            "res_model": "sync.run",
            "view_mode": "list,form",
            "domain": [("connection_id", "=", self.id)],
        }
