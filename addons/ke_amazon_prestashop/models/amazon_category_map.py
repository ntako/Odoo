from odoo import fields, models


class AmazonCategoryMap(models.Model):
    _inherit = 'ke.amazon.category.map'

    ps_category_id = fields.Many2one(
        'ke.prestashop.category', string='Categoria PrestaShop', ondelete='set null',
        help='Categoria PS dei prodotti con questa categoria Amazon. Vuota: quella mappata sulla '
             'categoria Odoo del prodotto (PrestaShop → Mappatura categorie).')
