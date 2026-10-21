from odoo import models


class AccountPaymentTerm(models.Model):
    
    _inherit = [
        "account.payment.term",
        "sync.mixin",
    ]

    _sync_enabled = True
    # Master data owned by the remote: pulled, never pushed. Create/edit it on the remote only.
    _sync_operations = ()
    _sync_pull = True
    _sync_pull_sequence = 5
