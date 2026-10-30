from odoo import fields, models
from odoo.exceptions import UserError


class AmazonAllocateFbmWizard(models.TransientModel):
    _name = 'ke.amazon.allocate.fbm.wizard'
    _description = 'Alloca giacenza dal magazzino generale ad Amazon FBM'

    source_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Magazzino generale (sorgente)', required=True,
        default=lambda self: self.env.company.amazon_default_warehouse_id,
    )
    line_ids = fields.One2many(
        'ke.amazon.allocate.fbm.wizard.line', 'wizard_id', string='Prodotti',
    )

    def action_confirm(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError('Aggiungi almeno un prodotto da allocare.')

        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        engine = SyncEngine(self.env, None)
        lines = [(line.product_id, line.quantity) for line in self.line_ids]
        picking = engine.allocate_fbm_stock(self.source_warehouse_id, lines)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }


class AmazonAllocateFbmWizardLine(models.TransientModel):
    _name = 'ke.amazon.allocate.fbm.wizard.line'
    _description = 'Riga allocazione giacenza Amazon FBM'

    wizard_id = fields.Many2one('ke.amazon.allocate.fbm.wizard', required=True, ondelete='cascade')
    product_id = fields.Many2one(
        'product.product', string='Prodotto', required=True,
        domain="[('product_tmpl_id.x_amazon_fulfillment', '=', 'fbm')]",
    )
    quantity = fields.Float('Quantità', required=True, default=1.0)
