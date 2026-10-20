from odoo import models


class AccountPaymentTerm(models.Model):
    
    _inherit = [
        "account.payment.term",
        "sync.mixin",
    ]

    _sync_enabled = True
