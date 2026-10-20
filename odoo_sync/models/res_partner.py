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
