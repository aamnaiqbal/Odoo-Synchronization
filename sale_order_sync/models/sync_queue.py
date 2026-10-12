import logging

import requests

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


class SyncQueue(models.Model):
    _name = "sync.queue"
    _description = "Sync Queue (sale orders to remote server)"
    _order = "id desc"

    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade",
                               index=True)
    connection_id = fields.Many2one("server.connection", required=True,
                                    ondelete="cascade")
    state = fields.Selection(
        [("pending", "Pending"), ("done", "Synced"), ("failed", "Failed")],
        default="pending", required=True, index=True,
    )
    attempts = fields.Integer(readonly=True)
    last_error = fields.Text(readonly=True)
    remote_id = fields.Integer(readonly=True)
    done_date = fields.Datetime(readonly=True)

    # ------------------------------------------------------------------
    # Enqueue (called when a sale order is created)
    # ------------------------------------------------------------------
    def _enqueue(self, orders):
        connections = self.env["server.connection"].sudo().search(
            [("sync_sale_orders", "=", True)]
        )
        created = False
        for conn in connections:
            for order in orders:
                if self.search_count([("order_id", "=", order.id),
                                      ("connection_id", "=", conn.id)]):
                    continue
                self.create({"order_id": order.id, "connection_id": conn.id})
                created = True
        if created:
            # Run the worker as soon as this transaction commits.
            cron = self.env.ref("sale_order_sync.ir_cron_sync_queue",
                                raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _cron_process(self, limit=50):
        for rec in self.search([("state", "=", "pending")], order="id",
                               limit=limit):
            try:
                with self.env.cr.savepoint():
                    rec._process()
            except requests.RequestException as e:
                # Server/network down: not the record's fault. Keep it pending,
                # stop this run, the periodic cron will try again.
                rec.write({"last_error": f"Server unreachable: {e}"[:1000]})
                self.env.cr.commit()
                _logger.warning("Sync paused, server unreachable: %s", e)
                break
            except Exception as e:
                attempts = rec.attempts + 1
                rec.write({
                    "attempts": attempts,
                    "last_error": str(e)[:1000],
                    "state": "failed" if attempts >= MAX_ATTEMPTS else "pending",
                })
                _logger.exception("Sync of %s failed", rec.order_id.name)
            self.env.cr.commit()

    def _process(self):
        self.ensure_one()
        order, conn = self.order_id, self.connection_id
        key = f"{self.env.cr.dbname}/{order.name}"

        # Idempotency: if a previous attempt created it but the answer got lost.
        existing = conn._json2_call(
            "sale.order", "search", {"domain": [["origin", "=", key]], "limit": 1}
        )
        if existing:
            remote_id = existing[0]
        else:
            res = conn._json2_call(
                "sale.order", "create", {"vals_list": [self._build_vals(order, conn, key)]}
            )
            remote_id = res[0] if isinstance(res, list) else res

        self.write({
            "state": "done",
            "remote_id": remote_id,
            "last_error": False,
            "done_date": fields.Datetime.now(),
        })
        order.sudo().write({"remote_order_id": remote_id})

    # ------------------------------------------------------------------
    # Payload building / mapping
    # ------------------------------------------------------------------
    def _build_vals(self, order, conn, key):
        vals = {
            "partner_id": self._resolve_partner(conn, order.partner_id),
            "origin": key,
            "date_order": fields.Datetime.to_string(order.date_order),
            "order_line": [[0, 0, self._line_vals(conn, line)]
                           for line in order.order_line],
        }
        if order.client_order_ref:
            vals["client_order_ref"] = order.client_order_ref
        return vals

    def _line_vals(self, conn, line):
        if line.display_type:  # section / note
            return {"display_type": line.display_type, "name": line.name,
                    "sequence": line.sequence}
        return {
            "product_id": self._resolve_product(conn, line.product_id),
            "name": line.name,
            "product_uom_qty": line.product_uom_qty,
            "price_unit": line.price_unit,
            "discount": line.discount,
            "sequence": line.sequence,
        }

    def _resolve_partner(self, conn, partner):
        if partner.email:
            domain = [["email", "=", partner.email]]
        else:
            domain = [["name", "=", partner.name]]
        ids = conn._json2_call("res.partner", "search",
                               {"domain": domain, "limit": 1})
        if ids:
            return ids[0]
        vals = {
            "name": partner.name,
            "email": partner.email,
            "phone": partner.phone,
            "street": partner.street,
            "city": partner.city,
            "zip": partner.zip,
            "is_company": partner.is_company,
        }
        vals = {k: v for k, v in vals.items() if v}
        res = conn._json2_call("res.partner", "create", {"vals_list": [vals]})
        return res[0] if isinstance(res, list) else res

    def _resolve_product(self, conn, product):
        if not product:
            raise UserError("Order line has no product.")
        if product.default_code:
            domain = [["default_code", "=", product.default_code]]
        else:
            domain = [["name", "=", product.name]]
        ids = conn._json2_call("product.product", "search",
                               {"domain": domain, "limit": 1})
        if not ids:
            raise UserError(
                f"Product '{product.display_name}' not found on the target "
                f"(matched by {'internal reference' if product.default_code else 'name'})."
            )
        return ids[0]

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_retry(self):
        self.filtered(lambda r: r.state != "done").write(
            {"state": "pending", "attempts": 0, "last_error": False}
        )
        cron = self.env.ref("sale_order_sync.ir_cron_sync_queue")
        cron.sudo()._trigger()
