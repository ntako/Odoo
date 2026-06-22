from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    ps_product_id = fields.Integer(
        string='ID Prodotto PrestaShop',
        index=True,
        copy=False,
        help='ID del prodotto nel catalogo PrestaShop. 0 = non ancora sincronizzato.',
    )
    ps_sync_enabled = fields.Boolean(
        string='Sync PS attiva',
        default=True,
        help='Include questo prodotto nella sincronizzazione con PrestaShop.',
    )
    ps_last_sync_date = fields.Datetime(
        string='Ultima sync PS',
        readonly=True,
        copy=False,
    )
    ps_image_synced = fields.Boolean(
        string='Immagine caricata su PS',
        default=False,
        copy=False,
        help='True se l\'immagine principale è stata caricata su PrestaShop.',
    )
