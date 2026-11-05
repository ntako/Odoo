from collections import Counter

from odoo import api, fields, models


class ProductBrand(models.Model):
    _inherit = 'ke.product.brand'

    amazon_name = fields.Char(
        'Nome su Amazon',
        help="Valore esatto del campo marca da inviare ad Amazon per i prodotti NUOVI di questa "
             "marca (deve coincidere con il catalogo Amazon / Brand Registry, maiuscole comprese). "
             "Proposto in automatico con la scrittura più usata da Amazon sui prodotti della marca. "
             "I prodotti già su Amazon usano sempre la marca che Amazon ha per loro (Brand Amazon).",
    )

    def _propose_amazon_name(self):
        """Nome su Amazon = scrittura più frequente di x_amazon_brand fra i prodotti della marca (se vuoto)."""
        for brand in self.filtered(lambda b: not b.amazon_name):
            values = Counter(v for v in brand.with_context(active_test=False).product_tmpl_ids.mapped('x_amazon_brand') if v)
            if values:
                brand.amazon_name = values.most_common(1)[0][0]

    @api.model
    def _reassign_products_for_name(self, name, old_brand, new_brand):
        """Prodotti con marca Amazon uguale a 'name' (confronto normalizzato) passano a 'new_brand'."""
        res = super()._reassign_products_for_name(name, old_brand, new_brand)
        from odoo.addons.ke_product_catalog.models.product_brand import brand_key
        key = brand_key(name)
        products = self.env['product.template'].with_context(active_test=False).search([
            ('ke_brand_id', '=', old_brand.id), ('x_amazon_brand', '!=', False),
        ]).filtered(lambda t: brand_key(t.x_amazon_brand) == key)
        products.write({'ke_brand_id': new_brand.id or False})
        return res
