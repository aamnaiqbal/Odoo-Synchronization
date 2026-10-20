from odoo import models


class SaleOrder(models.Model):
    
    _inherit = [
        "sale.order",
        "sync.mixin",
    ]

    _sync_enabled = True
