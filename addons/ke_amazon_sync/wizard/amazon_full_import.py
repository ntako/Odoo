import logging

from odoo import fields, models
from odoo.exceptions import UserError

_log = logging.getLogger(__name__)


class AmazonFullImport(models.TransientModel):
    _name = 'ke.amazon.full.import'
    _description = 'Import Completo da Amazon'

    limit = fields.Integer(
        'Limite prodotti', default=0,
        help='0 = nessun limite (consigliato). Usa un numero piccolo (es. 20) solo per test.',
    )
    update_existing = fields.Boolean(
        'Aggiorna prodotti esistenti', default=True,
        help='Se attivo, aggiorna i campi Amazon dei prodotti già presenti in Odoo.',
    )

    def action_start(self):
        self.ensure_one()
        from ..services.sp_api_client import SpApiClient

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.import_state') == 'running':
            raise UserError(
                'Un import è già in corso. Controlla il log sincronizzazioni.'
            )

        log_rec = self.env['ke.amazon.sync.log'].create({
            'name': f'Import completo — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'full_import',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })

        params.set_param('ke_amazon_sync.import_state', 'running')
        params.set_param('ke_amazon_sync.import_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.import_page_token', '')
        params.set_param('ke_amazon_sync.import_update_existing', str(self.update_existing))
        params.set_param('ke_amazon_sync.import_max_products', str(self.limit or 0))
        params.set_param('ke_amazon_sync.import_report_id', '')
        params.set_param('ke_amazon_sync.import_report_attach_id', '')
        params.set_param('ke_amazon_sync.import_report_row_offset', '')

        cron = self.env.ref('ke_amazon_sync.cron_full_import_chunk', raise_if_not_found=False)
        if cron:
            cron.sudo().active = True

        return {
            'type': 'ir.actions.act_window',
            'name': 'Log Import Amazon',
            'res_model': 'ke.amazon.sync.log',
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }
