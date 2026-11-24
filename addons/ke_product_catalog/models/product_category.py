from odoo import fields, models


class ProductCategory(models.Model):
    """Nome della categoria per i canali di vendita, tradotto (2026-11-17): il nome Odoo non è traducibile."""
    _inherit = 'product.category'

    ke_web_name = fields.Char(
        'Nome sul sito', translate=True,
        help='Nome della categoria nei canali di vendita (menu del sito), in ogni lingua. '
             'Vuoto: il nome della categoria.')

    def _ke_web_name_own(self, lang):
        """Nome sul sito scritto in questa lingua (senza il ripiego di Odoo), o ''."""
        self.ensure_one()
        self.env.flush_all()
        self.env.cr.execute('SELECT ke_web_name FROM product_category WHERE id = %s', (self.id,))
        row = self.env.cr.fetchone()
        return ((row and row[0]) or {}).get(lang) or ''
