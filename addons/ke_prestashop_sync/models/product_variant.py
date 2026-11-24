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
    ps_variant_product_id = fields.Integer(
        string='ID Prodotto PS della variante',
        index=True,
        copy=False,
        default=0,
        help='Modalità "varianti come prodotti separati": il prodotto semplice PrestaShop di questa '
             'variante. 0 = non collegata.',
    )
    ps_options_dirty = fields.Boolean(
        string='Valori PS da aggiornare', copy=False,
        help='Il nome della variante (valori degli attributi) è cambiato in Odoo: il prossimo export aggiorna '
             'i valori della combinazione già creata su PrestaShop.',
    )
    ps_legacy_product_id = fields.Integer(
        string='Prodotto PS sostituito',
        copy=False,
        default=0,
        help='Prodotto PrestaShop separato che corrispondeva a questa variante prima che la famiglia '
             'fosse pubblicata con le combinazioni: disattivato dall\'export con redirect 301 alla famiglia.',
    )
