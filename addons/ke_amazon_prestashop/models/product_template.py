from odoo import models

from odoo.addons.ke_amazon_sync.models.amazon_category_map import category_key


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def _ke_ps_category(self):
        """Prima la categoria PS della corrispondenza Amazon (più dettagliata), poi quella della categoria Odoo."""
        self.ensure_one()
        if self.x_amazon_category:
            mapping = self.env['ke.amazon.category.map'].sudo().search(
                [('key', '=', category_key(self.x_amazon_category))], limit=1)
            if mapping.ps_category_id:
                return mapping.ps_category_id
        return super()._ke_ps_category()
