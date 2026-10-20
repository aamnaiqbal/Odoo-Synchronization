from odoo import models


class ProductTemplate(models.Model):
    
    _inherit = [
        "product.template",
        "sync.mixin",
    ]

    _sync_enabled = True

class ProductProduct(models.Model):
    
    _inherit = [
        "product.product",
        "sync.mixin",
    ]

    _sync_enabled = True

