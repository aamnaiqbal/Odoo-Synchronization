from ast import literal_eval

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)

# Never copied from the remote
EXCLUDED_FIELDS = {
    "id", "display_name", "create_uid", "create_date", "write_uid", "write_date",
    "__last_update", "parent_path", "message_main_attachment_id",
}
# Field types that are never copied
EXCLUDED_TYPES = {
    "one2many", "many2one_reference", "reference", "properties",
    "properties_definition", "json",
}

# (model, match fields). Models that are not installed locally are skipped.
SUGGESTED_MODELS = [
    ("res.partner.category", "name"),
    ("uom.uom", "name"),
    ("product.category", "name"),
    ("product.attribute", "name"),
    ("product.attribute.value", "name"),
    ("account.payment.term", "name"),
    ("account.payment.term.line", ""),
    ("product.template", "default_code"),
    ("product.pricelist", "name"),
    ("product.pricelist.item", ""),
    ("res.partner", "name,email"),
]


class SyncModelConfig(models.Model):
    _name = "sync.model.config"
    _description = "Model to fetch from the remote Odoo"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    model_id = fields.Many2one(
        "ir.model",
        string="Model",
        required=True,
        ondelete="cascade",
        index=True,
        domain=[("transient", "=", False)],
    )
    model_name = fields.Char(related="model_id.model", store=True, string="Technical Name")
    mode = fields.Selection(
        [("import", "Import records"), ("lookup", "Reference only")],
        default="import",
        required=True,
        help="Import: all records are fetched and created locally.\n"
        "Reference only: nothing is imported. When a fetched record points to a record of "
        "this model that has no mapping/xmlid, the remote record is read and the LOCAL "
        "record with the same 'match fields' is used (and mapped). Use it for records that "
        "already exist locally, e.g. accounts (match 'code'), product variants "
        "(match 'default_code').",
    )
    remote_domain = fields.Char(
        string="Remote Domain",
        default="[]",
        help="Domain applied on the REMOTE server, e.g. [('type', '=', 'consu')]. "
        "Archived records are included automatically.",
    )
    match_fields = fields.Char(
        help="Comma separated fields used to find an existing LOCAL record when it is "
        "not mapped yet and has no xmlid (e.g. 'default_code'). Empty = the model's "
        "record name. If one of the fields is empty on a record, no matching is tried.",
    )
    excluded_fields = fields.Char(
        help="Comma separated fields that must NOT be copied from the remote.",
    )
    update_existing = fields.Boolean(
        default=True,
        help="Update local records that already exist (mapped, same xmlid or matched). "
        "If unchecked they are only mapped, never overwritten.",
    )
    include_binary = fields.Boolean(
        string="Include Binary Fields",
        help="Images and attachments. Makes the fetch much heavier.",
    )
    note = fields.Text()

    _model_uniq = models.Constraint(
        "UNIQUE(model_id)", "This model is already configured."
    )

    @api.constrains("remote_domain")
    def _check_remote_domain(self):
        for config in self:
            try:
                domain = literal_eval(config.remote_domain or "[]")
            except (ValueError, SyntaxError) as error:
                raise ValidationError(
                    _("Remote domain must be a valid Python literal list.")
                ) from error
            if not isinstance(domain, list):
                raise ValidationError(_("Remote domain must be a list."))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _split(value):
        return [item.strip() for item in (value or "").split(",") if item.strip()]

    def _get_domain(self):
        self.ensure_one()
        return literal_eval(self.remote_domain or "[]")

    def _get_match_fields(self):
        self.ensure_one()
        Model = self.env[self.model_name]
        names = [n for n in self._split(self.match_fields) if n in Model._fields]
        if not names and Model._rec_name in Model._fields:
            names = [Model._rec_name]
        return names

    def _get_fetch_fields(self, connection):
        """Fields that exist on both sides, same type, stored and writable."""
        self.ensure_one()
        Model = self.env[self.model_name]
        remote_fields = connection._json2_call(
            "sync.registry", "get_fields", {"model_name": self.model_name}
        )
        excluded = EXCLUDED_FIELDS | set(self._split(self.excluded_fields))
        result = []
        for name, field in Model._fields.items():
            if name in excluded or field.type in EXCLUDED_TYPES:
                continue
            if not field.store or (field.compute and not field.inverse and field.readonly):
                continue
            if field.type == "binary" and not self.include_binary:
                continue
            remote = remote_fields.get(name)
            if not remote or remote["type"] != field.type:
                continue
            if not remote["store"] or not remote["writable"]:
                continue
            if self.model_name == "sale.order.line":
                _logger.info(
                    "[SYNC DEBUG] Including sale.order.line.name | "
                    "local_type=%s | remote=%s",
                    field.type,
                    remote,
                )
            result.append(name)
        return result

    @api.model
    def _get_ordered_configs(self):
        """Active configs sorted so a model comes after the models it points to.
        Ties and cycles fall back to `sequence`."""
        configs = self.search([("mode", "=", "import")], order="sequence, id")
        names = set(configs.mapped("model_name"))
        deps = {}
        for config in configs:
            Model = self.env[config.model_name]
            deps[config.model_name] = {
                f.comodel_name
                for f in Model._fields.values()
                if f.type in ("many2one", "many2many")
                and f.store
                and f.comodel_name in names
                and f.comodel_name != config.model_name
            }
        ordered, done, remaining = [], set(), list(configs)
        while remaining:
            ready = next((c for c in remaining if deps[c.model_name] <= done), None)
            if ready is None:  # circular dependency: take the lowest sequence
                ready = remaining[0]
            ordered.append(ready.id)
            done.add(ready.model_name)
            remaining.remove(ready)
        return self.browse(ordered)

    @api.model
    def _load_suggested(self):
        sequence = 10
        for model_name, match in SUGGESTED_MODELS:
            model = self.env["ir.model"].search([("model", "=", model_name)], limit=1)
            if not model:
                continue
            if self.with_context(active_test=False).search_count([("model_id", "=", model.id)]):
                continue
            self.create(
                {"model_id": model.id, "match_fields": match, "sequence": sequence}
            )
            sequence += 10