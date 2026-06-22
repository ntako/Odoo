import logging

from odoo import api, fields, models

_log = logging.getLogger('ke.prestashop.sync.cron')


class PrestashopSyncLog(models.Model):
    _name = 'ke.prestashop.sync.log'
    _description = 'PrestaShop Sync Log'
    _order = 'create_date desc'
    _rec_name = 'name'

    name = fields.Char('Operazione', required=True)
    sync_type = fields.Selection([
        ('import_products', 'Import Prodotti da PS'),
        ('export_products', 'Export Prodotti verso PS'),
        ('sync_stock', 'Sync Quantità → PS'),
        ('sync_prices', 'Sync Prezzi → PS'),
        ('import_orders', 'Import Ordini da PS'),
        ('sync_images', 'Sync Immagini → PS'),
    ], string='Tipo', required=True)
    state = fields.Selection([
        ('running', 'In corso'),
        ('done', 'Completato'),
        ('error', 'Errore'),
    ], string='Stato', default='running')

    products_total = fields.Integer('Totale')
    products_created = fields.Integer('Creati')
    products_updated = fields.Integer('Aggiornati')
    products_skipped = fields.Integer('Saltati')
    products_error = fields.Integer('Errori')

    start_date = fields.Datetime('Inizio', default=fields.Datetime.now)
    end_date = fields.Datetime('Fine')
    duration = fields.Float('Durata (s)', compute='_compute_duration', store=True)

    log_lines = fields.Text('Log dettagliato')
    error_summary = fields.Text('Sommario errori')

    @api.depends('start_date', 'end_date')
    def _compute_duration(self):
        for rec in self:
            if rec.start_date and rec.end_date:
                rec.duration = (rec.end_date - rec.start_date).total_seconds()
            else:
                rec.duration = 0.0

    def action_open(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_refresh(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Metodi chiamati dai cron
    # ------------------------------------------------------------------

    def _cron_sync_stock(self):
        from ..services.ps_api_client import PsApiClient
        from ..services.sync_engine import SyncEngine

        client = PsApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron sync_stock saltato: credenziali PS non configurate.')
            return

        log_rec = self.create({
            'name': f'Sync quantità → PS — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'sync_stock',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        self.env.cr.commit()
        try:
            SyncEngine(self.env, client).sync_stock_to_prestashop(log_rec)
        except Exception as exc:
            _log.error('Cron sync_stock fallito: %s', exc, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(exc),
                           'end_date': fields.Datetime.now()})

    def _cron_sync_prices(self):
        from ..services.ps_api_client import PsApiClient
        from ..services.sync_engine import SyncEngine

        client = PsApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron sync_prices saltato: credenziali PS non configurate.')
            return

        log_rec = self.create({
            'name': f'Sync prezzi → PS — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'sync_prices',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        self.env.cr.commit()
        try:
            SyncEngine(self.env, client).sync_prices_to_prestashop(log_rec)
        except Exception as exc:
            _log.error('Cron sync_prices fallito: %s', exc, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(exc),
                           'end_date': fields.Datetime.now()})

    def _cron_import_orders(self):
        from ..services.ps_api_client import PsApiClient
        from ..services.sync_engine import SyncEngine

        client = PsApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron import_orders saltato: credenziali PS non configurate.')
            return

        hours_back = int(self.env['ir.config_parameter'].sudo().get_param(
            'ke_prestashop_sync.orders_hours_back', '24'))
        log_rec = self.create({
            'name': f'Import ordini PS — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'import_orders',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        self.env.cr.commit()
        try:
            SyncEngine(self.env, client).import_orders_from_ps(log_rec, hours_back=hours_back)
        except Exception as exc:
            _log.error('Cron import_orders fallito: %s', exc, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(exc),
                           'end_date': fields.Datetime.now()})
