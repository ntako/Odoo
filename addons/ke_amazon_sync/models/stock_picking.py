from odoo import api, fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    x_amazon_is_fba_shipment = fields.Boolean(
        'Spedizione Amazon FBA', compute='_compute_x_amazon_is_fba_shipment',
        help="True se la destinazione di questo trasferimento è la location "
             "'In transito verso Amazon' (creata da ship_to_fba) — usato solo per "
             "mostrare il pulsante di etichettatura FNSKU sul picking giusto.",
    )

    @api.depends('location_dest_id')
    def _compute_x_amazon_is_fba_shipment(self):
        transit_id = int(
            self.env['ir.config_parameter'].sudo().get_param(
                'ke_amazon_sync.transit_location_id', 0
            ) or 0
        )
        for picking in self:
            picking.x_amazon_is_fba_shipment = bool(transit_id) and picking.location_dest_id.id == transit_id

    def action_open_amazon_fba_prep_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ke.amazon.fba.prep.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_picking_id': self.id},
        }
