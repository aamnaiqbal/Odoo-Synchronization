from odoo import models


class ProductTemplate(models.Model):
    
    _inherit = [
        "product.template",
        "sync.mixin",
    ]

    _sync_enabled = True

