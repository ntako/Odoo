from odoo import api, fields, models


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    # Sul magazzino, non sull'azienda né sul marketplace: l'usato segue il
    # magazzino FBA — chi lo condivide (il pool IT/ES/FR) condivide anche
    # l'ubicazione dell'usato, chi ne ha uno proprio (UK) ha la propria.
    # Osservazione dell'utente il 2026-11-03.
    amazon_fba_used_location_id = fields.Many2one(
        'stock.location', string='Ubicazione FBA – Usato',
        domain="[('usage', '=', 'internal')]",
        help="Solo per un magazzino FBA Amazon: dove stanno le unità usate rivendute da "
             "Amazon (Grade & Resell), separate dallo stock del nuovo. Va messa allo stesso "
             "livello dello stock (padre: il magazzino), non dentro lo stock — altrimenti "
             "le consegne potrebbero prelevare unità usate. Se vuota, la sync giacenze FBA "
             "la crea da sola ('Usato').",
    )
    is_amazon_fba_warehouse = fields.Boolean(
        compute='_compute_is_amazon_fba_warehouse',
        help="Magazzino FBA dell'azienda o di un marketplace Amazon — mostra il campo sopra solo qui.",
    )

    @api.depends()
    def _compute_is_amazon_fba_warehouse(self):
        fba_ids = set(self.env['ke.amazon.marketplace'].sudo().with_context(active_test=False).search([
            ('fba_warehouse_id', '!=', False),
        ]).fba_warehouse_id.ids)
        if self.env.company.amazon_fba_warehouse_id:
            fba_ids.add(self.env.company.amazon_fba_warehouse_id.id)
        for warehouse in self:
            warehouse.is_amazon_fba_warehouse = warehouse.id in fba_ids
