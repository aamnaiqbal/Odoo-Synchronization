from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    remote_order_id = fields.Integer(
        string="Remote Order ID", readonly=True, copy=False,
        help="ID of this order on the target server once synced.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        # Only queues a row; never talks to the network inside the user's save.
        self.env["sync.queue"].sudo()._enqueue(orders)
        return orders
