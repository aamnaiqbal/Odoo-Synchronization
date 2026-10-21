from odoo import models


class ResPartner(models.Model):
    """Partners created locally are pushed to the remote Odoo.

    To push another model (sale.order, purchase.order, ...) add a file like this one:
    inherit the model together with `sync.mixin` and set `_sync_enabled = True`.
    A model with lines needs the same on the line model (e.g. sale.order.line): lines
    are pushed on their own, and wait until the order they point to is on the remote.
    """

    _inherit = [
        "res.partner",
        "sync.mixin",
    ]

    _sync_enabled = True
    # Push-only for now. Pulling partners needs a conflict policy (both sides edit them).

class ResPartnerCategory(models.Model):
    
    _inherit = [
        "res.partner.category",
        "sync.mixin",
    ]

    _sync_enabled = True
    # Master data owned by the remote: pulled, never pushed. Create/edit it on the remote only.
    _sync_operations = ()
    _sync_pull = True
    _sync_pull_sequence = 5
