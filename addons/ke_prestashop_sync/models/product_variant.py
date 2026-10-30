from odoo import fields, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    ps_combination_id = fields.Integer(
        string='ID Combinazione PS',
        index=True,
        copy=False,
        default=0,
        help='ID della combinazione (variante) in PrestaShop. 0 = non ancora sincronizzata.',
    )
