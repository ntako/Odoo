from odoo import fields, models


class AmazonReportRequestWizard(models.TransientModel):
    _name = 'ke.amazon.report.request.wizard'
    _description = 'Richiedi nuovo report Amazon'

    marketplace_id = fields.Many2one(
        'ke.amazon.marketplace', string='Marketplace',
        domain=[('active', '=', True)],
        help='Lascia vuoto per richiedere un report per tutti i marketplace attivi.',
    )

    def action_request(self):
        self.ensure_one()
        marketplace_ids = [self.marketplace_id.id] if self.marketplace_id else None
        records = self.env['ke.amazon.report'].action_request_new(marketplace_ids)
        return {
            'type': 'ir.actions.act_window',
            'name': 'Report Amazon',
            'res_model': 'ke.amazon.report',
            'view_mode': 'list,form',
            'domain': [('id', 'in', records.ids)],
            'target': 'current',
        }
