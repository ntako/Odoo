from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Tutte le immagini del prodotto, comprese quelle delle singole varianti.
    ke_media_ids = fields.One2many('ke.product.media', 'product_tmpl_id', string='Galleria immagini')
    ke_brand_id = fields.Many2one('ke.product.brand', string='Marca', index=True, ondelete='set null')


class ProductProduct(models.Model):
    _inherit = 'product.product'

    # Solo le immagini proprie di questa variante (il campo ha lo stesso nome
    # di quello del template perché la scheda variante eredita la vista del
    # template: la stessa scheda "Galleria" mostra qui le sole righe della
    # variante).
    ke_media_ids = fields.One2many('ke.product.media', 'product_id', string='Galleria immagini')
