from odoo import models


class SaleOrder(models.Model):
    
    _inherit = [
        "sale.order",
        "sync.mixin",
    ]

    _sync_enabled = True


class SaleOrderLine(models.Model):
    
    _inherit = [
        "sale.order.line",
        "sync.mixin",
    ]

    _sync_enabled = True


class StockMove(models.Model):
    
    _inherit = [
        "stock.move",
        "sync.mixin",
    ]

    _sync_enabled = True

class StockPicking(models.Model):
    
    _inherit = [
        "stock.picking",
        "sync.mixin",
    ]

    _sync_enabled = True
