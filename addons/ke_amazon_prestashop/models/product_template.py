from odoo import models

from odoo.addons.ke_amazon_sync.models.amazon_category_map import category_key


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def _ke_ps_category_auto(self):
        """
        Prima la categoria PS della corrispondenza Amazon (più dettagliata),
        tranne dopo "Sincronizza categorie da Odoo" (albero del sito = Odoo),
        ma solo finché il prodotto è nella categoria Odoo in cui l'ha messo
        quella corrispondenza (o nella predefinita, se la corrispondenza non
        ne indica una): spostato a mano in un'altra categoria Odoo, vale la
        mappatura di quella (2026-11-11, deciso con l'utente). Poi la
        categoria Odoo e i padri.
        """
        # Albero del sito sincronizzato da Odoo (2026-11-17): vale solo la categoria Odoo.
        tree_from_odoo = self.env['ir.config_parameter'].sudo().get_param(
            'ke_prestashop_sync.category_tree_from_odoo') in ('True', '1')
        if self.x_amazon_category and not tree_from_odoo:
            mapping = self.env['ke.amazon.category.map'].sudo().search(
                [('key', '=', category_key(self.x_amazon_category))], limit=1)
            if mapping.ps_category_id:
                from odoo.addons.ke_amazon_sync.services.category_map import CategoryMapper
                expected = mapping.categ_id or CategoryMapper(self.env).default_category()
                if self.categ_id == expected:
                    return mapping.ps_category_id, 'Corrispondenza categoria Amazon'
        return super()._ke_ps_category_auto()
