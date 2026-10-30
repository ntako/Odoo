import logging
import threading

from odoo import api, fields, models
from odoo.exceptions import UserError

_log = logging.getLogger(__name__)


class AmazonMoveToGeneralWizard(models.TransientModel):
    _name = 'ke.amazon.move.to.general.wizard'
    _description = 'Sposta in blocco la giacenza esistente verso il Magazzino Generale'

    source_location_id = fields.Many2one(
        'stock.location', string='Ubicazione di partenza', required=True,
        domain="[('usage', '=', 'internal')]",
        help='La location dove si trova oggi la giacenza da correggere (es. quella dove è '
             'finita una rettifica di inventario iniziale senza magazzino specificato). '
             'Tutti i prodotti con giacenza positiva lì dentro verranno spostati.',
    )

    def action_confirm(self):
        self.ensure_one()
        if not self.env.company.amazon_default_warehouse_id:
            raise UserError('Magazzino Generale non configurato (Impostazioni → Amazon → Magazzini).')

        log_rec = self.env['ke.amazon.sync.log'].create({
            'name': f'Spostamento giacenza verso Magazzino Generale — '
                    f'{fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'move_to_general',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        self.env.cr.commit()

        db = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        location_id = self.source_location_id.id
        threading.Thread(
            target=self._run_move, args=(db, uid, ctx, location_id, log_rec.id), daemon=True,
        ).start()

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ke.amazon.sync.log',
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.model
    def _run_move(self, db, uid, ctx, location_id, log_id):
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from ..services.sync_engine import SyncEngine

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            log_rec = env['ke.amazon.sync.log'].browse(log_id)
            try:
                location = env['stock.location'].browse(location_id)
                engine = SyncEngine(env, None)
                picking, count = engine.move_all_stock_to_general(location)
                if picking:
                    msg = (f'Spostati {count} prodotti verso il Magazzino Generale '
                           f'(trasferimento {picking.name}).')
                else:
                    msg = 'Nessuna giacenza trovata in quella ubicazione, nulla da spostare.'
                log_rec.write({
                    'state': 'done',
                    'products_total': count,
                    'products_updated': count,
                    'log_lines': msg,
                    'end_date': fields.Datetime.now(),
                })
                _log.info(msg)
            except Exception as exc:
                _log.error('Thread move_to_general fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc),
                               'end_date': fields.Datetime.now()})
            cr.commit()
