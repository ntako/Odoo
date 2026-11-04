from collections import Counter

from odoo import fields, models


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
