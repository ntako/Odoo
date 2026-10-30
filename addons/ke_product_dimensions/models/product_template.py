from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Nomi scelti per coincidere con quelli che Odoo core usa dalla v17 per lo
    # stesso scopo (product_length/product_width/product_height): se una
    # futura versione di Odoo li introduce nativamente, il conflitto sarà
    # visibile e risolvibile in un unico punto (questo modulo), non sparso
    # dentro le integrazioni che consumano questi dati (es. ke_amazon_sync).
    product_length = fields.Float(
        'Lunghezza (cm)', copy=False,
        help='Lunghezza fisica del prodotto in cm.',
    )
    product_width = fields.Float(
        'Larghezza (cm)', copy=False,
        help='Larghezza fisica del prodotto in cm.',
    )
    product_height = fields.Float(
        'Altezza (cm)', copy=False,
        help='Altezza fisica del prodotto in cm.',
    )

    # readonly=False: resta modificabile a mano per i prodotti senza
    # lunghezza/larghezza/altezza — il valore manuale resta finché nessuna
    # delle tre dimensioni viene aggiornata (il compute si rilancia solo sui
    # suoi @api.depends, non ad ogni salvataggio).
    volume = fields.Float(compute='_compute_dimensions_volume', store=True, readonly=False)

    @api.depends('product_length', 'product_width', 'product_height')
    def _compute_dimensions_volume(self):
        for product in self:
            if product.product_length and product.product_width and product.product_height:
                product.volume = (
                    product.product_length * product.product_width * product.product_height
                ) / 1_000_000  # cm³ -> m³
            else:
                product.volume = 0.0

    # Non memorizzato: va sempre calcolato al volo per restare coerente con
    # la giacenza reale. Scorre tutti i magazzini esistenti, quindi resta
    # valido a prescindere da quanti/quali magazzini ci sono in una data
    # istanza. Interroga direttamente stock.quant (stesso pattern usato per
    # 'total_volume' su stock.warehouse) invece di affidarsi al contesto
    # 'warehouse' su qty_available: su product.template quel contesto non
    # scorpora correttamente per magazzino e ritorna sempre il totale non
    # filtrato per ognuno (verificato: un prodotto con giacenza reale solo
    # in un magazzino mostrava la stessa quantità ripetuta su tutti e tre).
    warehouse_qty_summary = fields.Char(
        string='Giacenza per magazzino',
        compute='_compute_warehouse_qty_summary',
        help='Quantità disponibile per ciascun magazzino esistente.',
    )

    def _compute_warehouse_qty_summary(self):
        warehouses = self.env['stock.warehouse'].search([])
        Quant = self.env['stock.quant']
        for product in self:
            variant_ids = product.product_variant_ids.ids
            parts = []
            for wh in warehouses:
                qty = 0.0
                if variant_ids:
                    quants = Quant.search([
                        ('product_id', 'in', variant_ids),
                        ('location_id', 'child_of', wh.view_location_id.id),
                        ('location_id.usage', '=', 'internal'),
                    ])
                    qty = sum(quants.mapped('quantity'))
                parts.append(f'{wh.name}: {qty:g}')
            product.warehouse_qty_summary = ' · '.join(parts)
