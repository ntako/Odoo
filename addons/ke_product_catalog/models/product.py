from odoo import api, fields, models


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

    # Prezzo di vendita della singola variante (2026-11-05): Odoo ha un solo
    # prezzo per famiglia più i sovrapprezzi sui valori degli attributi, che
    # non bastano quando ogni variante ha il suo prezzo (276 famiglie Amazon
    # IT con varianti a prezzi diversi; in produzione ogni articolo ha il suo
    # prezzo base). Se compilato vale al posto del prezzo della famiglia
    # ovunque Odoo usi il prezzo di vendita della variante (lst_price, listini,
    # ordini, canali): diventa il sovrapprezzo della variante (price_extra).
    ke_list_price = fields.Float(
        'Prezzo di vendita variante', digits='Product Price',
        help='Prezzo base di questa variante. Vuoto (0): vale il prezzo di vendita della '
             'famiglia più gli eventuali sovrapprezzi degli attributi.',
    )

    @api.depends('product_template_attribute_value_ids.price_extra', 'ke_list_price', 'list_price')
    def _compute_product_price_extra(self):
        super()._compute_product_price_extra()
        for product in self:
            if product.ke_list_price:
                product.price_extra = product.ke_list_price - product.list_price

    def _set_product_lst_price(self):
        # Prezzo scritto su una variante di famiglia: resta sulla variante,
        # non cambia il prezzo della famiglia (comportamento standard di Odoo).
        family = self.filtered(lambda p: p.product_tmpl_id.product_variant_count > 1)
        for product in family:
            value = product.lst_price
            if self.env.context.get('uom'):
                value = self.env['uom.uom'].browse(self.env.context['uom'])._compute_price(value, product.uom_id)
            product.ke_list_price = value
        return super(ProductProduct, self - family)._set_product_lst_price()
