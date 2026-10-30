from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    ps_order_id = fields.Integer(
        string='ID Ordine PrestaShop',
        index=True,
        copy=False,
        readonly=True,
        help='ID dell\'ordine nel back-office PrestaShop.',
    )
    ps_order_state = fields.Char(
        string='Stato PS',
        readonly=True,
        copy=False,
        help='Stato dell\'ordine su PrestaShop al momento dell\'importazione.',
    )
    ps_order_reference = fields.Char(
        string='Riferimento PS',
        readonly=True,
        copy=False,
        help='Numero di riferimento ordine PrestaShop (es. XKBKNABJK).',
    )
