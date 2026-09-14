from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    x_amazon_order_id = fields.Char(
        'Amazon Order ID', index=True, copy=False, readonly=True,
        help='Identificatore univoco dell\'ordine su Amazon (es. 111-1234567-8901234).',
    )
    x_amazon_order_status = fields.Selection([
        ('Pending', 'In attesa'),
        ('Unshipped', 'Da spedire'),
        ('PartiallyShipped', 'Parzialmente spedito'),
        ('Shipped', 'Spedito'),
        ('Canceled', 'Annullato'),
        ('Unfulfillable', 'Non evadibile'),
    ], string='Stato Amazon', copy=False, readonly=True)
    x_amazon_fulfillment_channel = fields.Selection([
        ('AFN', 'FBA (Amazon Logistics)'),
        ('MFN', 'FBM (Merchant)'),
    ], string='Fulfillment Amazon', copy=False, readonly=True)
    x_amazon_marketplace_id = fields.Many2one(
        'ke.amazon.marketplace', string='Marketplace Amazon', copy=False, readonly=True,
        help='Marketplace da cui è stato importato questo ordine.',
    )
    x_amazon_purchase_date = fields.Datetime(
        'Data acquisto Amazon', copy=False, readonly=True,
    )
    x_amazon_sales_channel = fields.Char(
        'Canale vendita Amazon', copy=False, readonly=True,
    )
    x_amazon_has_return = fields.Boolean(
        'Ha resi Amazon', copy=False, readonly=True,
        help='Almeno un reso cliente è stato registrato per questo ordine (vedi note).',
    )


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    x_amazon_sku = fields.Char('Amazon SKU', copy=False, readonly=True)
    x_amazon_asin = fields.Char('Amazon ASIN', copy=False, readonly=True)
    x_amazon_order_item_id = fields.Char('Amazon Item ID', copy=False, readonly=True)
