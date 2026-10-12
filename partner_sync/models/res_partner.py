import logging

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

SIMPLE_FIELDS = [
    "name", "is_company", "email", "phone", "street", "street2",
    "city", "zip", "vat", "website", "function",
]


class ResPartner(models.Model):
    _inherit = "res.partner"

    sync_pending = fields.Boolean(copy=False, index=True)
    remote_partner_id = fields.Integer(copy=False, readonly=True)
    sync_error = fields.Char(copy=False, readonly=True)

    # ------------------------------------------------------------------
    # 1) On create: only flag the record. No network call while saving.
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        enabled = self.env["server.connection"].sudo().search_count(
            [("sync_partners", "=", True)]
        )
        if enabled:
            for vals in vals_list:
                vals["sync_pending"] = True
        partners = super().create(vals_list)
        if enabled:
            cron = self.env.ref("partner_sync.ir_cron_partner_sync",
                                raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()  # run as soon as this transaction commits
        return partners

    # ------------------------------------------------------------------
    # 2) Worker: push flagged contacts to the target
    # ------------------------------------------------------------------
    @api.model
    def _cron_push_partners(self, limit=50):
        conn = self.env["server.connection"].sudo().search(
            [("sync_partners", "=", True)], limit=1
        )
        if not conn:
            return
        for partner in self.search([("sync_pending", "=", True)],
                                   order="id", limit=limit):
            try:
                remote_id = partner._push_to_remote(conn)
            except requests.RequestException as e:
                # Server/network down: keep everything pending, retry next run.
                _logger.warning("Partner sync paused, server unreachable: %s", e)
                break
            except Exception as e:
                partner.write({"sync_error": str(e)[:255]})
                _logger.exception("Partner sync failed for %s", partner.display_name)
            else:
                partner.write({
                    "sync_pending": False,
                    "remote_partner_id": remote_id,
                    "sync_error": False,
                })
            self.env.cr.commit()

    def _push_to_remote(self, conn):
        self.ensure_one()
        #Build the data to send
        vals = {f: self[f] for f in SIMPLE_FIELDS if self[f]}
        if self.country_id:
            ids = conn._json2_call("res.country", "search", {
                "domain": [["code", "=", self.country_id.code]], "limit": 1})
            if ids:
                vals["country_id"] = ids[0]
                if self.state_id:
                    sids = conn._json2_call("res.country.state", "search", {
                        "domain": [["code", "=", self.state_id.code],
                            ["country_id", "=", ids[0]]],
                        "limit": 1})
                    if sids:
                        vals["state_id"] = sids[0]
        res = conn._json2_call("res.partner", "create", {"vals_list": [vals]})
        return res[0] if isinstance(res, list) else res
