from odoo import models


class ResPartner(models.Model):
    _inherit = [
        "res.partner",
        "sync.mixin",
    ]

    _sync_enabled = True