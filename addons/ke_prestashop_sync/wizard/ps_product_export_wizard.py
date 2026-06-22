import logging
import threading
from datetime import datetime

from odoo import api, models, fields
from odoo import registry as Registry
from odoo.exceptions import UserError

_log = logging.getLogger(__name__)


class PsProductExportWizard(models.TransientModel):
    _name = 'ke.ps.product.export'
    _description = 'Wizard Export Prodotti verso PrestaShop'

    export_mode = fields.Selection([
        ('new_only', 'Solo prodotti non ancora su PS'),
        ('selected', 'Prodotti selezionati'),
    ], string='Modalità', default='new_only', required=True)

    product_ids = fields.Many2many(
        'product.template',
        string='Prodotti',
        help='Seleziona i prodotti da esportare su PrestaShop.',
    )

    def action_run_export(self):
        from ..services.ps_api_client import PsApiClient
        from ..services.sync_engine import SyncEngine

        client = PsApiClient(self.env)
        try:
            client.validate_credentials()
        except (ValueError, Exception) as exc:
            raise UserError(str(exc)) from exc

        product_ids = self.product_ids.ids if self.export_mode == 'selected' else None

        log_rec = self.env['ke.prestashop.sync.log'].create({
            'name': f'Export prodotti → PS — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'export_products',
            'state': 'running',
            'start_date': datetime.now(),
        })
        self.env.cr.commit()

        db_name = self._cr.dbname
        uid = self.env.uid
        context = dict(self.env.context)
        log_id = log_rec.id

        def _run():
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, uid, context)
                try:
                    SyncEngine(env, PsApiClient(env)).export_products_to_ps(
                        env['ke.prestashop.sync.log'].browse(log_id),
                        product_ids=product_ids,
                    )
                    cr.commit()
                except Exception as exc:
                    _log.error('export_products fallito: %s', exc, exc_info=True)
                    env['ke.prestashop.sync.log'].browse(log_id).write({
                        'state': 'error',
                        'error_summary': str(exc),
                        'end_date': datetime.now(),
                    })
                    cr.commit()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        return {
            'type': 'ir.actions.act_window',
            'name': 'Log Export PrestaShop',
            'res_model': 'ke.prestashop.sync.log',
            'res_id': log_id,
            'view_mode': 'form',
            'target': 'current',
        }
