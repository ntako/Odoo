from odoo import fields, models


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
