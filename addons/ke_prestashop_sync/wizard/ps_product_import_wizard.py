import logging
import threading
from datetime import datetime

from odoo import api, models, fields
from odoo import registry as Registry
from odoo.exceptions import UserError

_log = logging.getLogger(__name__)


class PsProductImportWizard(models.TransientModel):
    _name = 'ke.ps.product.import'
    _description = 'Wizard Import Prodotti da PrestaShop'

    limit = fields.Integer(
        string='Limite prodotti',
        default=0,
        help='0 = importa tutto il catalogo PS.',
    )
    update_existing = fields.Boolean(
        string='Aggiorna prodotti esistenti',
        default=True,
        help='Se attivo, aggiorna i campi dei prodotti già presenti in Odoo.',
    )

    def action_run_import(self):
        from ..services.ps_api_client import PsApiClient
        from ..services.sync_engine import SyncEngine

        client = PsApiClient(self.env)
        try:
            client.validate_credentials()
        except (ValueError, Exception) as exc:
            raise UserError(str(exc)) from exc

        label = f' (limite {self.limit})' if self.limit else ''
        log_rec = self.env['ke.prestashop.sync.log'].create({
            'name': f'Import prodotti da PS{label} — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'import_products',
            'state': 'running',
            'start_date': datetime.now(),
        })
        self.env.cr.commit()

        db_name = self._cr.dbname
        uid = self.env.uid
        context = dict(self.env.context)
        log_id = log_rec.id
        limit = self.limit
        update_existing = self.update_existing

        def _run():
            with Registry(db_name).cursor() as cr:
                env = api.Environment(cr, uid, context)
                try:
                    SyncEngine(env, PsApiClient(env)).import_products_from_ps(
                        env['ke.prestashop.sync.log'].browse(log_id),
                        limit=limit,
                        update_existing=update_existing,
                    )
                    cr.commit()
                except Exception as exc:
                    _log.error('import_products fallito: %s', exc, exc_info=True)
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
            'name': 'Log Import PrestaShop',
            'res_model': 'ke.prestashop.sync.log',
            'res_id': log_id,
            'view_mode': 'form',
            'target': 'current',
        }
