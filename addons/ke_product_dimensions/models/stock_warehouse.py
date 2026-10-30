from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    # Non memorizzato: calcolato al volo sulle ubicazioni interne del
    # magazzino, così resta sempre coerente con la giacenza reale senza
    # bisogno di ricalcoli innescati da ogni movimento di magazzino.
    total_volume = fields.Float(
        string='Volume totale (m³)',
        compute='_compute_total_volume',
        help='Somma di quantità × volume prodotto per tutte le ubicazioni interne del magazzino.',
    )

    def _compute_total_volume(self):
        for warehouse in self:
            quants = self.env['stock.quant'].search([
                ('location_id', 'child_of', warehouse.view_location_id.id),
                ('location_id.usage', '=', 'internal'),
            ])
            warehouse.total_volume = sum(
                quant.quantity * quant.product_id.volume for quant in quants
            )
