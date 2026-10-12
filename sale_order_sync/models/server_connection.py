from odoo import fields, models


class ServerConnection(models.Model):
    _inherit = "server.connection"

    sync_sale_orders = fields.Boolean(
        string="Sync new sale orders",
        help="Every sale order created on this database is pushed to this server.",
    )
