from odoo import fields, models

from .product_template import compute_amazon_sale_price


class ProductProduct(models.Model):
    _inherit = 'product.product'

    x_amazon_asin = fields.Char(
        'Amazon ASIN (variante)', index=True, copy=False,
        help='ASIN del child ASIN Amazon per questa specifica variante.',
    )
    x_amazon_sku = fields.Char(
        'Amazon SKU (variante)', index=True, copy=False,
        help='SKU Amazon per questa specifica variante.',
    )
    x_amazon_sku_previous = fields.Char(
        'SKU Amazon precedente (variante)', copy=False, readonly=True,
        help='Ultimo SKU sostituito dalla riconciliazione quando Amazon riassegna lo '
             'SKU di questa variante (es. rifornimento). Solo indicazione informativa.',
    )

    def get_amazon_sale_price(self):
        """
        Prezzo IVA inclusa da comunicare ad Amazon per questa variante: lst_price
        (prezzo template + extra variante) con il markup del listino 'Amazon' e
        l'IVA reintegrata — aliquota e convenzione netto/lordo lette dalle tasse
        di vendita configurate sul prodotto.
        """
        self.ensure_one()
        return compute_amazon_sale_price(self, self.lst_price)
