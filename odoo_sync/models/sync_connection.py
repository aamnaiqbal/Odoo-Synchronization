import logging

import requests

from odoo import _, api, fields, models
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

    push_requires_fetch = fields.Boolean(
        string="Fetch Before Push",
        default=True,
        help="When checked, the queue worker does not push local records until the "
        "configuration has been fetched from the remote at least once. The fetch "
        "stores the remote ids that the push needs to resolve relations "
        "(country, categories, parent partner, ...). Uncheck to push without a fetch.",
    )
    config_fetched = fields.Boolean(
        string="Configuration Fetched",
        readonly=True,
        copy=False,
        help="Set when a configuration fetch run finished without errors.",
    )
    last_fetch_date = fields.Datetime(readonly=True, copy=False)

    run_ids = fields.One2many("sync.run", "connection_id", string="Runs")
    run_count = fields.Integer(compute="_compute_run_count")
    pending_count = fields.Integer(string="Pending Jobs", compute="_compute_pending_count")

    def _compute_run_count(self):
        data = self.env["sync.run"]._read_group(
            [("connection_id", "in", self.ids)], ["connection_id"], ["__count"]
        )
        counts = {connection.id: count for connection, count in data}
        for connection in self:
            connection.run_count = counts.get(connection.id, 0)

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

    def action_load_suggested_models(self):
        self.ensure_one()
        self.env["sync.model.config"]._load_suggested()
        return {
            "type": "ir.actions.act_window",
            "name": _("Models to Sync"),
            "res_model": "sync.model.config",
            "view_mode": "list,form",
        }

    def _create_fetch_run(self, push_after_fetch=False):
        self.ensure_one()
        if self.env["sync.run"].search_count(
            [("connection_id", "=", self.id), ("state", "in", ("queued", "running"))]
        ):
            raise UserError(_("A fetch is already queued or running for this connection."))
        if not self.env["sync.model.config"].search_count([]):
            raise UserError(
                _("No models configured. Use 'Load Suggested Models' or add some first.")
            )
        run = self.env["sync.run"].create(
            {"connection_id": self.id, "push_after_fetch": push_after_fetch}
        )
        self.env.ref("odoo_sync.ir_cron_process_sync_runs").sudo()._trigger()
        return {
            "type": "ir.actions.act_window",
            "res_model": "sync.run",
            "res_id": run.id,
            "view_mode": "form",
        }

    def action_fetch_configuration(self):
        """Queue a run. The heavy work happens in a cron, not in the HTTP request."""
        self.ensure_one()
        return self._create_fetch_run(push_after_fetch=False)

    def action_fetch_and_push(self):
        """Fetch the remote configuration, then (when the fetch succeeded) queue every
        local record that does not exist on the remote yet for the push."""
        self.ensure_one()
        return self._create_fetch_run(push_after_fetch=True)

    def action_push_local_records(self):
        self.ensure_one()
        if self.push_requires_fetch and not self.config_fetched:
            raise UserError(
                _("Fetch the configuration from the remote first (or uncheck 'Fetch Before Push').")
            )
        count = self._enqueue_local_records()
        self.env.ref("odoo_sync.ir_cron_sync_queue_worker").sudo()._trigger()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Local records queued"),
                "message": _("%s record(s) were added to the push queue.", count),
                "type": "success",
                "sticky": False,
            },
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

    def action_view_queue(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Synchronization Queue"),
            "res_model": "sync.queue",
            "view_mode": "list,form",
            "domain": [("connection_id", "=", self.id)],
        }

    # ------------------------------------------------------------------
    # Local records -> push queue
    # ------------------------------------------------------------------
    def _get_sync_models(self):
        """Models that carry the sync mixin and are enabled for the push."""
        names = []
        for name in self.env.registry:
            model = self.env[name]
            if getattr(model, "_sync_enabled", False) and "sync_uuid" in model._fields:
                names.append(name)
        return names

    def _enqueue_local_records(self):
        """Queue a 'create' job for every local record of the sync-enabled models that
        was NOT fetched from the remote (no sync.record.map row) and has no job yet.
        Records fetched from the remote already exist there, so they are skipped."""
        self.ensure_one()
        Map = self.env["sync.record.map"].sudo()
        Queue = self.env["sync.queue"].sudo()
        total = 0
        for model_name in self._get_sync_models():
            mapped = Map.search(
                [("connection_id", "=", self.id), ("model", "=", model_name)]
            ).mapped("local_id")
            queued = Queue.search(
                [
                    ("model_name", "=", model_name),
                    ("state", "in", ("pending", "processing", "done")),
                ]
            ).mapped("record_id")
            records = (
                self.env[model_name]
                .sudo()
                .with_context(active_test=False)
                .search([("id", "not in", list(set(mapped) | set(queued)))], order="id")
            )
            if records:
                records._create_sync_queue("create")
                total += len(records)
            _logger.info("[SYNC] %s: %s local record(s) queued for push", model_name, len(records))
        return total
