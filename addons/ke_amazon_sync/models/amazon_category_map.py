import re
import unicodedata

from odoo import api, fields, models


def category_key(text):
    """
    Chiave di confronto di una categoria Amazon: senza maiuscole, accenti e
    spazi doppi ("Cebos de calamar", "cebos  de calamár" → stessa chiave).
    """
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', text.casefold()).strip()


class AmazonCategoryMap(models.Model):
    """
    Corrispondenza categoria Amazon → categoria Odoo (2026-11-10, deciso con
    l'utente). Il Full Import non crea più una categoria Odoo per ogni nome
    Amazon: i nomi arrivano nella lingua del marketplace ("Esche da calamaro",
    "Cebos de calamar", "Squid Lures" = 3 categorie, 440 su odoo_2) e il nome
    di product.category non è traducibile. Una riga per nome Amazon, in
    qualunque lingua; senza categoria Odoo vale la categoria predefinita
    ("Da classificare", Impostazioni → Amazon). Le righe dei nomi nuovi le
    crea l'import, da completare a mano o con "Importa corrispondenze".
    """
    _name = 'ke.amazon.category.map'
    _description = 'Corrispondenza categoria Amazon'
    _order = 'name'

    name = fields.Char('Categoria Amazon', required=True)
    key = fields.Char('Chiave di confronto', compute='_compute_key', store=True, index=True)
    categ_id = fields.Many2one(
        'product.category', string='Categoria Odoo', ondelete='set null', index=True,
        help='Vuota: i prodotti vanno nella categoria predefinita (Impostazioni → Amazon).')
    product_count = fields.Integer('Prodotti', compute='_compute_product_count')

    _key_uniq = models.Constraint('unique(key)', 'Questa categoria Amazon ha già una corrispondenza.')

    @api.depends('name')
    def _compute_key(self):
        for rec in self:
            rec.key = category_key(rec.name)

    def _compute_product_count(self):
        counts = {}
        groups = self.env['product.template']._read_group(
            [('x_amazon_category', '!=', False)], ['x_amazon_category'], ['__count'])
        for name, count in groups:
            key = category_key(name)
            counts[key] = counts.get(key, 0) + count
        for rec in self:
            rec.product_count = counts.get(rec.key, 0)

    def action_open_products(self):
        self.ensure_one()
        names = [n for n, in self.env['product.template']._read_group(
            [('x_amazon_category', '!=', False)], ['x_amazon_category']) if category_key(n) == self.key]
        return {
            'type': 'ir.actions.act_window', 'name': self.name, 'res_model': 'product.template',
            'view_mode': 'list,form', 'domain': [('x_amazon_category', 'in', names)],
        }
