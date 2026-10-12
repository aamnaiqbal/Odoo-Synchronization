import xmlrpc.client

import requests

from odoo import fields, models
from odoo.exceptions import UserError

TIMEOUT = 10


class _TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = TIMEOUT
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = TIMEOUT
        return conn


class ServerConnection(models.Model):
    _name = "server.connection"
    _description = "Cloud Server Connection"

    name = fields.Char(required=True, default="Cloud Server")
    url = fields.Char(required=True, help="e.g. https://your-server.com")
    db = fields.Char(string="Database", required=True)
    login = fields.Char(required=True, help="Only used by the XML-RPC fallback.")
    api_key = fields.Char(required=True, groups="base.group_system")
    last_status = fields.Char(readonly=True)
    last_checked = fields.Datetime(readonly=True)

    # ------------------------------------------------------------------
    # Helpers (reusable later by the sync code)
    # ------------------------------------------------------------------
    def _base_url(self):
        return (self.url or "").strip().rstrip("/")

    def _json2_call(self, model, method, payload=None):
        """Call any model method through the Odoo 19 JSON-2 API."""
        self.ensure_one()
        r = requests.post(
            f"{self._base_url()}/json/2/{model}/{method}",
            headers={
                "Authorization": f"bearer {self.api_key}",
                "X-Odoo-Database": self.db,
            },
            json=payload or {},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise UserError(f"JSON-2 error {r.status_code}: {r.text[:300]}")
        return r.json()

    def _xmlrpc_proxy(self, path):
        url = f"{self._base_url()}/xmlrpc/2/{path}"
        transport = (
            _TimeoutSafeTransport() if url.startswith("https")
            else _TimeoutTransport()
        )
        return xmlrpc.client.ServerProxy(url, transport=transport)

    # ------------------------------------------------------------------
    # Connection tests
    # ------------------------------------------------------------------
    def _test_json2(self):
        count = self._json2_call("res.partner", "search_count", {"domain": []})
        return f"JSON-2 OK. Partners on server: {count}"

    def _test_xmlrpc(self):
        uid = self._xmlrpc_proxy("common").authenticate(
            self.db, self.login, self.api_key, {}
        )
        if not uid:
            raise UserError(
                "Authentication failed: check database, login and API key."
            )
        count = self._xmlrpc_proxy("object").execute_kw(
            self.db, uid, self.api_key, "res.partner", "search_count", [[]]
        )
        return f"XML-RPC OK (uid {uid}). Partners on server: {count}"

    def _notify(self, title, message, ntype, sticky=False):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": ntype,
                "sticky": sticky,
            },
        }

    def action_test_connection(self):
        self.ensure_one()
        now = fields.Datetime.now()

        # 1) Is the server reachable?
        try:
            requests.get(
                f"{self._base_url()}/web/login", timeout=TIMEOUT
            ).raise_for_status()
        except requests.RequestException as e:
            msg = f"FAILED: server unreachable ({e})"
            self.write({"last_status": msg[:255], "last_checked": now})
            return self._notify("Connection failed", msg, "danger", sticky=True)

        # 2) Authenticate + read: JSON-2 first, XML-RPC as fallback
        errors = []
        for label, test in (("JSON-2", self._test_json2),
                            ("XML-RPC", self._test_xmlrpc)):
            try:
                status = test()
            except Exception as e:
                errors.append(f"{label}: {e}")
                continue
            self.write({"last_status": status, "last_checked": now})
            return self._notify("Connection successful", status, "success")

        # Return (don't raise) so the failure status is committed, not rolled back.
        msg = "FAILED: " + " | ".join(errors)
        self.write({"last_status": msg[:255], "last_checked": now})
        return self._notify("Connection failed", "\n".join(errors),
                            "danger", sticky=True)
