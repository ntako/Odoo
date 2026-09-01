from odoo import fields, models
from odoo.exceptions import UserError


class AmazonReceiveFromFbaWizard(models.TransientModel):
    _name = 'ke.amazon.receive.wizard'
    _description = 'Registra rientro scorte fisico da Amazon FBA'

    dest_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Magazzino di destinazione', required=True,
        default=lambda self: self.env.company.amazon_default_warehouse_id,
    )
    line_ids = fields.One2many(
        'ke.amazon.receive.wizard.line', 'wizard_id', string='Prodotti',
    )

    def action_confirm(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError('Aggiungi almeno un prodotto ricevuto.')

        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        engine = SyncEngine(self.env, None)
        lines = [(line.product_id, line.quantity) for line in self.line_ids]
        picking = engine.receive_from_fba(self.dest_warehouse_id, lines)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }


class AmazonReceiveFromFbaWizardLine(models.TransientModel):
    _name = 'ke.amazon.receive.wizard.line'
    _description = 'Riga rientro scorte da Amazon FBA'

    wizard_id = fields.Many2one('ke.amazon.receive.wizard', required=True, ondelete='cascade')
    product_id = fields.Many2one(
        'product.product', string='Prodotto', required=True,
        domain="[('product_tmpl_id.x_amazon_fulfillment', '=', 'fba')]",
    )
    quantity = fields.Float('Quantità', required=True, default=1.0)
