from odoo import fields, models


class ServerConnection(models.Model):
    _inherit = "server.connection"

    sync_partners = fields.Boolean(
        string="Sync new contacts",
        help="Every contact created on this database is also created on this server.",
    )
