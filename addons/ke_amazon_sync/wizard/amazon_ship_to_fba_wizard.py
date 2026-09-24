from odoo import fields, models
from odoo.exceptions import UserError


class AmazonShipToFbaWizard(models.TransientModel):
    _name = 'ke.amazon.ship.wizard'
    _description = 'Registra spedizione di rifornimento verso Amazon FBA'

    source_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Magazzino di partenza', required=True,
        default=lambda self: self.env.company.amazon_default_warehouse_id,
    )
    marketplace_id = fields.Many2one(
        'ke.amazon.marketplace', string='Marketplace di destinazione',
        domain="[('active', '=', True)]",
        help="Lascia vuoto per il pool FBA Pan-EU condiviso (IT/DE/ES/FR oggi). "
             "Seleziona un marketplace solo se ha un magazzino FBA dedicato "
             "impostato (inventario non pooled, es. UK/US).",
    )
    line_ids = fields.One2many(
        'ke.amazon.ship.wizard.line', 'wizard_id', string='Prodotti',
    )

    def action_confirm(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError('Aggiungi almeno un prodotto da spedire.')

        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        engine = SyncEngine(self.env, None)
        lines = [(line.product_id, line.quantity) for line in self.line_ids]
        picking = engine.ship_to_fba(self.source_warehouse_id, lines, marketplace=self.marketplace_id)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }


class AmazonShipToFbaWizardLine(models.TransientModel):
    _name = 'ke.amazon.ship.wizard.line'
    _description = 'Riga spedizione verso Amazon FBA'

    wizard_id = fields.Many2one('ke.amazon.ship.wizard', required=True, ondelete='cascade')
    product_id = fields.Many2one(
        'product.product', string='Prodotto', required=True,
        domain="[('product_tmpl_id.x_amazon_fulfillment', '=', 'fba')]",
    )
    quantity = fields.Float('Quantità', required=True, default=1.0)
