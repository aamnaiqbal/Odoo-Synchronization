from odoo import models


class AccountMove(models.Model):
    
    _inherit = [
        "account.move",
        "sync.mixin",
    ]

    _sync_enabled = True


class AccountMoveLine(models.Model):
    
    _inherit = [
        "account.move.line",
        "sync.mixin",
    ]

    _sync_enabled = True
