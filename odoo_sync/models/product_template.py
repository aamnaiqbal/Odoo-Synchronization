from odoo import models


class ProductTemplate(models.Model):
    
    _inherit = [
        "product.template",
        "sync.mixin",
    ]

    _sync_enabled = True
    # Master data owned by the remote: pulled, never pushed. Create/edit it on the remote only.
    _sync_operations = ()
    _sync_pull = True
    _sync_pull_sequence = 10

class ProductProduct(models.Model):
    
    _inherit = [
        "product.product",
        "sync.mixin",
    ]

    _sync_enabled = True
    # Master data owned by the remote: pulled, never pushed. Create/edit it on the remote only.
    _sync_operations = ()
    _sync_pull = True
    _sync_pull_sequence = 20  # after product.template
    # A variant exists because its template exists: it is never created on its own.
    _sync_pull_allow_create = False

    def _sync_pull_locate(self, connection, data, state):
        """Variants are off (one variant per product): the local counterpart of a remote
        variant is the single variant of the local counterpart of its template. The
        template is pulled first if it is not here yet."""
        key = (data.get("values") or {}).get("product_tmpl_id")
        if not key:
            return self.browse()
        template_id = connection._pull_resolve_key(key, state, "product.product.product_tmpl_id")
        if not template_id:
            return self.browse()
        template = self.env["product.template"].with_context(active_test=False).browse(template_id)
        return template.product_variant_ids[:1]

