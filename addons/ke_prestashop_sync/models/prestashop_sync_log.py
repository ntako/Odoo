import json
import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

_log = logging.getLogger('ke.prestashop.sync')

# Un job "In corso" che non aggiorna il log da oltre questo tempo è considerato
# orfano (worker terminato): il cron della coda lo chiude in errore.
_STALE_AFTER = timedelta(hours=1)


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
        ('queued', 'In coda'),
        ('running', 'In corso'),
        ('done', 'Completato'),
        ('error', 'Errore'),
        ('cancelled', 'Interrotto'),
    ], string='Stato', default='queued')

    cancel_requested = fields.Boolean('Interruzione richiesta', default=False, copy=False)
    job_method = fields.Char('Metodo job', copy=False)
    job_kwargs = fields.Text('Parametri job', copy=False)

    products_total = fields.Integer('Totale')
    products_created = fields.Integer('Creati')
    products_updated = fields.Integer('Aggiornati')
    products_skipped = fields.Integer('Saltati')
    products_error = fields.Integer('Errori')

    progress_done = fields.Integer('Elaborati')
    progress_total = fields.Integer('Da elaborare')

    start_date = fields.Datetime('Inizio')
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
        return self.action_refresh()

    def action_refresh(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_cancel(self):
        """Interruzione dolce: se in coda annulla subito, se in corso lo segnala
        al worker che si ferma entro pochi elementi."""
        for rec in self:
            if rec.state == 'queued':
                rec.write({'state': 'cancelled', 'end_date': fields.Datetime.now()})
            elif rec.state == 'running':
                rec.write({'cancel_requested': True})
        self.env.cr.commit()
        return self.action_refresh()

    def action_force_close(self):
        """Chiude a forza un job che sembra bloccato (es. worker terminato)."""
        for rec in self:
            if rec.state in ('queued', 'running'):
                rec.write({
                    'state': 'cancelled',
                    'end_date': fields.Datetime.now(),
                    'error_summary': (rec.error_summary or '')
                    + '\nChiuso manualmente (processo non più attivo).',
                })
        self.env.cr.commit()
        return self.action_refresh()

    # ------------------------------------------------------------------
    # Coda job
    # ------------------------------------------------------------------

    def _new_log(self, sync_type, label=None, state='queued'):
        type_labels = dict(self._fields['sync_type'].selection)
        name = label or (
            f'{type_labels.get(sync_type, sync_type)} — '
            f'{fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}'
        )
        return self.create({
            'name': name,
            'sync_type': sync_type,
            'state': state,
            'start_date': fields.Datetime.now() if state == 'running' else False,
        })

    @api.model
    def _get_client(self, raise_on_error=True):
        from ..services.ps_api_client import PsApiClient
        client = PsApiClient(self.env)
        try:
            client.validate_credentials()
        except Exception as exc:
            if raise_on_error:
                raise UserError(str(exc)) from exc
            _log.warning('Credenziali PrestaShop non valide: %s', exc)
            return None
        return client

    @api.model
    def enqueue(self, sync_type, engine_method, label=None, validate=False,
                skip_if_pending=False, **kwargs):
        """Accoda un job. L'esecuzione avviene nel cron worker (robusto, non
        muore con il worker HTTP). Restituisce l'id del log (0 se saltato)."""
        if validate:
            self._get_client()  # solleva UserError se le credenziali non vanno

        if skip_if_pending and self.search_count([
            ('sync_type', '=', sync_type),
            ('state', 'in', ('queued', 'running')),
        ]):
            _log.info('PS enqueue %s saltato: un job è già in coda o in corso.', sync_type)
            return 0

        log = self._new_log(sync_type, label=label, state='queued')
        log.write({'job_method': engine_method, 'job_kwargs': json.dumps(kwargs)})
        self.env.cr.commit()
        self._wake_queue_runner()
        return log.id

    @api.model
    def _wake_queue_runner(self):
        cron = self.env.ref('ke_prestashop_sync.cron_run_queue', raise_if_not_found=False)
        if not cron:
            return
        try:
            cron.sudo()._trigger()
        except Exception as exc:
            _log.warning('Impossibile innescare il cron della coda PS: %s', exc)

    @api.model
    def _cron_run_queue(self):
        """Esegue un job in coda per volta e bonifica i job orfani."""
        # 1. bonifica job "In corso" senza aggiornamenti da troppo tempo
        stale = self.search([
            ('state', '=', 'running'),
            ('write_date', '<', fields.Datetime.now() - _STALE_AFTER),
        ])
        if stale:
            stale.write({
                'state': 'error',
                'error_summary': 'Interrotto: il processo si è arrestato senza completare.',
                'end_date': fields.Datetime.now(),
            })
            self.env.cr.commit()

        # 2. un job per esecuzione; se ne restano altri, ri-innesca il cron
        job = self.search([('state', '=', 'queued')], order='create_date asc', limit=1)
        if not job:
            return
        job._run_job()
        if self.search_count([('state', '=', 'queued')]):
            self._wake_queue_runner()

    def _run_job(self):
        self.ensure_one()
        from ..services.sync_engine import SyncEngine

        if self.state != 'queued':
            return
        if self.cancel_requested:
            self.write({'state': 'cancelled', 'end_date': fields.Datetime.now()})
            self.env.cr.commit()
            return

        client = self._get_client(raise_on_error=False)
        if client is None:
            self.write({'state': 'error', 'end_date': fields.Datetime.now(),
                        'error_summary': 'Credenziali PrestaShop non configurate.'})
            self.env.cr.commit()
            return

        kwargs = json.loads(self.job_kwargs or '{}')
        self.write({'state': 'running', 'start_date': fields.Datetime.now()})
        self.env.cr.commit()
        try:
            getattr(SyncEngine(self.env, client), self.job_method)(self, **kwargs)
        except Exception as exc:
            _log.error('Job PS %s (log %s) fallito: %s',
                       self.job_method, self.id, exc, exc_info=True)
            self.env.cr.rollback()
            self.write({'state': 'error', 'error_summary': str(exc),
                        'end_date': fields.Datetime.now()})
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # Cron periodici → accodano (l'esecuzione è serializzata dalla coda)
    # ------------------------------------------------------------------

    def _cron_sync_stock(self):
        self.enqueue('sync_stock', 'sync_stock_to_prestashop', skip_if_pending=True)

    def _cron_sync_prices(self):
        self.enqueue('sync_prices', 'sync_prices_to_prestashop', skip_if_pending=True)

    def _cron_import_orders(self):
        hours_back = int(self.env['ir.config_parameter'].sudo().get_param(
            'ke_prestashop_sync.orders_hours_back', '24'))
        self.enqueue('import_orders', 'import_orders_from_ps',
                     skip_if_pending=True, hours_back=hours_back)

    def _cron_sync_images(self):
        self.enqueue('sync_images', 'sync_images_to_prestashop', skip_if_pending=True)
