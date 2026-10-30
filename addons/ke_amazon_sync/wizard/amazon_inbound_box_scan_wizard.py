from odoo import api, fields, models
from odoo.exceptions import UserError


class AmazonInboundBoxScanWizard(models.TransientModel):
    _name = 'ke.amazon.inbound.box.scan.wizard'
    _description = 'Scansione articoli in un collo del piano inbound FBA'

    box_id = fields.Many2one('ke.amazon.inbound.plan.box', string='Collo', required=True)
    ean_scan = fields.Char('Scansiona EAN')
    last_scan_message = fields.Char('Ultima scansione', readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        box_id = self.env.context.get('default_box_id') or self.env.context.get('active_id')
        if box_id and 'box_id' in fields_list:
            res['box_id'] = box_id
        return res

    def _packed_qty(self, plan, product):
        return sum(
            plan.box_ids.line_ids.filtered(lambda l: l.product_id == product).mapped('quantity')
        )

    def _resolve_product(self, barcode):
        """
        Cerca il prodotto per barcode SOLO tra le righe del trasferimento
        collegato al piano (non tutto il catalogo) — stesso principio di
        amazon_fba_prep_wizard._find_move: un match altrove nel catalogo va
        segnalato come "non incluso in questa spedizione", non trattato come
        trovato.
        """
        box = self.box_id
        picking = box.plan_id.picking_id
        candidates = picking.move_ids.filtered(lambda m: m.product_id.barcode == barcode).mapped('product_id')
        if not candidates:
            any_product = self.env['product.product'].search([('barcode', '=', barcode)], limit=1)
            if not any_product:
                raise UserError(f'EAN sconosciuto: nessun prodotto con il codice a barre "{barcode}".')
            raise UserError(
                f'{any_product.display_name} non fa parte del trasferimento {picking.name}.'
            )
        return candidates[0]

    def action_scan(self):
        self.ensure_one()
        box = self.box_id
        if box.state != 'open':
            raise UserError(f'{box.name} è già chiuso: riaprilo prima di scansionare altri articoli.')
        barcode = (self.ean_scan or '').strip()
        self.ean_scan = False
        if not barcode:
            raise UserError('Scansiona o digita un codice EAN.')

        product = self._resolve_product(barcode)
        plan = box.plan_id
        move = plan.picking_id.move_ids.filtered(lambda m: m.product_id == product)
        expected = sum(move.mapped('product_uom_qty'))
        packed = self._packed_qty(plan, product)
        if packed >= expected:
            raise UserError(
                f'{product.display_name}: quantità già completamente imballata ({packed:g}/{expected:g}).'
            )

        line = box.line_ids.filtered(lambda l: l.product_id == product)
        if line:
            line[0].quantity += 1
        else:
            self.env['ke.amazon.inbound.plan.box.line'].create({
                'box_id': box.id, 'product_id': product.id, 'quantity': 1,
            })
        packed += 1
        self.last_scan_message = f'{product.display_name}: {packed:g}/{expected:g} nei colli'
