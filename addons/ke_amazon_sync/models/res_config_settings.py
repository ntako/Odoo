import threading
import logging
from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError

_log = logging.getLogger(__name__)

_DEF_REFRESH_TOKEN = (
    '***REMOVED***'
    '***REMOVED***'
    '***REMOVED***'
    '***REMOVED***'
)
_DEF_LWA_APP_ID = '***REMOVED***'
_DEF_LWA_SECRET = '***REMOVED***'
_DEF_SELLER_ID = '***REMOVED***'
_DEF_MARKETPLACE_ID = 'APJ6JRA9NG5V4'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # --- Credenziali SP-API ---
    amazon_refresh_token = fields.Char(
        string='Refresh Token',
        config_parameter='ke_amazon_sync.refresh_token',
        default=_DEF_REFRESH_TOKEN,
    )
    amazon_lwa_app_id = fields.Char(
        string='LWA App ID',
        config_parameter='ke_amazon_sync.lwa_app_id',
        default=_DEF_LWA_APP_ID,
    )
    amazon_lwa_client_secret = fields.Char(
        string='LWA Client Secret',
        config_parameter='ke_amazon_sync.lwa_client_secret',
        password=True,
        default=_DEF_LWA_SECRET,
    )
    amazon_seller_id = fields.Char(
        string='Seller ID (Merchant Token)',
        config_parameter='ke_amazon_sync.seller_id',
        default=_DEF_SELLER_ID,
    )
    amazon_marketplace_id = fields.Char(
        string='Marketplace ID',
        config_parameter='ke_amazon_sync.marketplace_id',
        default=_DEF_MARKETPLACE_ID,
        help='IT=APJ6JRA9NG5V4, DE=A1PA6795UKMFR9, ES=A1RKKUPIHCS9HS, '
             'FR=A13V1IB3VIYZZH, UK=A1F83G8C2ARO7P, US=ATVPDKIKX0DER',
    )
    amazon_locale = fields.Char(
        string='Lingua catalogo',
        config_parameter='ke_amazon_sync.locale',
        default='it_IT',
        help='Codice lingua per descrizioni/bullet points: it_IT, en_GB, de_DE, fr_FR, es_ES',
    )
    amazon_product_type = fields.Selection(
        string='Tipo prodotto Odoo',
        selection=[('consu', 'Beni'), ('service', 'Servizio'), ('combo', 'Combo')],
        config_parameter='ke_amazon_sync.odoo_product_type',
        default='consu',
        help='Tipo da assegnare ai prodotti creati tramite import Amazon',
    )

    # --- Limiti import ---
    amazon_max_products = fields.Integer(
        string='Limite prodotti per import',
        config_parameter='ke_amazon_sync.max_products',
        default=0,
        help='Numero massimo di prodotti da importare durante un import completo (per test). '
             '0 = nessun limite.',
    )
    amazon_import_update_existing = fields.Boolean(
        string='Aggiorna prodotti esistenti durante import',
        config_parameter='ke_amazon_sync.import_update_existing_default',
        default=True,
        help='Se attivo, durante l\'import completo aggiorna anche i prodotti già presenti in Odoo.',
    )

    # --- Controllo nuovi prodotti: cron + intervallo ---
    amazon_cron_check_active = fields.Boolean(
        string='Abilita controllo automatico',
        compute='_compute_cron_states',
        inverse='_set_cron_check_active',
        help='Se attivo, controlla ogni N ore se i nuovi prodotti Odoo sono presenti su Amazon.',
    )
    amazon_sync_check_interval = fields.Integer(
        string='Ogni (ore)',
        config_parameter='ke_amazon_sync.check_interval_hours',
        default=24,
    )

    # --- Sync prezzi/quantità: cron + intervallo ---
    amazon_cron_price_qty_active = fields.Boolean(
        string='Abilita sync automatica',
        compute='_compute_cron_states',
        inverse='_set_cron_price_qty_active',
        help='Se attivo, aggiorna prezzi e quantità su Amazon ogni N minuti.',
    )
    amazon_sync_price_qty_interval = fields.Integer(
        string='Ogni (minuti)',
        config_parameter='ke_amazon_sync.price_qty_interval_minutes',
        default=60,
    )

    # --- Riconciliazione store: cron + intervallo ---
    amazon_cron_reconcile_active = fields.Boolean(
        string='Abilita riconciliazione automatica',
        compute='_compute_cron_states',
        inverse='_set_cron_reconcile_active',
        help='Confronta periodicamente i prodotti Odoo con i listing attivi sul tuo store Amazon.',
    )
    amazon_sync_reconcile_interval = fields.Integer(
        string='Ogni (ore)',
        config_parameter='ke_amazon_sync.reconcile_interval_hours',
        default=24,
    )

    # ------------------------------------------------------------------
    # Computed: legge/scrive active dei cron direttamente
    # ------------------------------------------------------------------

    def _get_cron(self, xml_id):
        return self.env.ref(f'ke_amazon_sync.{xml_id}', raise_if_not_found=False)

    @api.depends()
    def _compute_cron_states(self):
        cron_check = self._get_cron('cron_check_new_products')
        cron_pq = self._get_cron('cron_sync_prices_quantities')
        cron_rec = self._get_cron('cron_reconcile_store')
        for rec in self:
            rec.amazon_cron_check_active = cron_check.active if cron_check else False
            rec.amazon_cron_price_qty_active = cron_pq.active if cron_pq else False
            rec.amazon_cron_reconcile_active = cron_rec.active if cron_rec else False

    def _set_cron_check_active(self):
        cron = self._get_cron('cron_check_new_products')
        if cron:
            cron.sudo().active = self.amazon_cron_check_active

    def _set_cron_price_qty_active(self):
        cron = self._get_cron('cron_sync_prices_quantities')
        if cron:
            cron.sudo().active = self.amazon_cron_price_qty_active

    def _set_cron_reconcile_active(self):
        cron = self._get_cron('cron_reconcile_store')
        if cron:
            cron.sudo().active = self.amazon_cron_reconcile_active

    # ------------------------------------------------------------------
    # Trigger manuali
    # ------------------------------------------------------------------

    def action_run_check_new_products(self):
        """Avvia manualmente la funzione 2 in background e apre il log."""
        self._validate_credentials()
        log_rec = self.env['ke.amazon.sync.log'].create({
            'name': f'Controllo nuovi prodotti (manuale) — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'check_new',
            'state': 'running',
            'start_date': datetime.now(),
        })
        self.env.cr.commit()
        self._start_thread('_thread_check_new', log_rec.id)
        return self._open_log_action()

    def action_run_sync_prices_quantities(self):
        """Avvia manualmente la funzione 3 in background e apre il log."""
        self._validate_credentials()
        log_rec = self.env['ke.amazon.sync.log'].create({
            'name': f'Sync prezzi/quantità (manuale) — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'price_qty',
            'state': 'running',
            'start_date': datetime.now(),
        })
        self.env.cr.commit()
        self._start_thread('_thread_sync_price_qty', log_rec.id)
        return self._open_log_action()

    def action_run_reconcile_store(self):
        """Avvia manualmente la riconciliazione store in background e apre il log."""
        self._validate_credentials()
        log_rec = self.env['ke.amazon.sync.log'].create({
            'name': f'Riconciliazione store (manuale) — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'reconcile',
            'state': 'running',
            'start_date': datetime.now(),
        })
        self.env.cr.commit()
        self._start_thread('_thread_reconcile_store', log_rec.id)
        return self._open_log_action()

    def action_run_download_images(self):
        """Scarica le immagini mancanti dai prodotti Amazon in background."""
        self._validate_credentials()
        log_rec = self.env['ke.amazon.sync.log'].create({
            'name': f'Download immagini mancanti — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'download_images',
            'state': 'running',
            'start_date': datetime.now(),
        })
        self.env.cr.commit()
        self._start_thread('_thread_download_images', log_rec.id)
        return self._open_log_action()

    # ------------------------------------------------------------------
    # Thread workers
    # ------------------------------------------------------------------

    def _start_thread(self, method_name, log_id):
        db = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        t = threading.Thread(
            target=getattr(self, method_name),
            args=(db, uid, ctx, log_id),
            daemon=True,
        )
        t.start()

    @api.model
    def _thread_reconcile_store(self, db, uid, ctx, log_id):
        try:
            from odoo import api as odoo_api
            from odoo.modules.registry import Registry as odoo_registry
            from ..services.sp_api_client import SpApiClient
            from ..services.sync_engine import SyncEngine

            with odoo_registry(db).cursor() as cr:
                env = odoo_api.Environment(cr, uid, ctx)
                log_rec = env['ke.amazon.sync.log'].browse(log_id)
                try:
                    engine = SyncEngine(env, SpApiClient(env))
                    engine.reconcile_store(log_rec)
                except Exception as exc:
                    _log.error('Thread reconcile_store fallito: %s', exc, exc_info=True)
                    log_rec.write({'state': 'error', 'error_summary': str(exc),
                                   'end_date': datetime.now()})
                    cr.commit()
        except Exception as exc:
            _log.error('Thread reconcile_store: errore critico: %s', exc, exc_info=True)

    @api.model
    def _thread_download_images(self, db, uid, ctx, log_id):
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from ..services.sp_api_client import SpApiClient
        from ..services.sync_engine import SyncEngine

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            log_rec = env['ke.amazon.sync.log'].browse(log_id)
            try:
                engine = SyncEngine(env, SpApiClient(env))
                ok, skipped, errors = engine.download_missing_images(log_rec)
                _log.info('Download immagini: %d ok, %d skip, %d errori', ok, skipped, errors)
            except Exception as exc:
                _log.error('Thread download_images fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc),
                               'end_date': datetime.now()})
                cr.commit()

    @api.model
    def _thread_check_new(self, db, uid, ctx, log_id):
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from ..services.sp_api_client import SpApiClient
        from ..services.sync_engine import SyncEngine

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            log_rec = env['ke.amazon.sync.log'].browse(log_id)
            hours = int(env['ir.config_parameter'].sudo().get_param(
                'ke_amazon_sync.check_interval_hours', '24'))
            try:
                engine = SyncEngine(env, SpApiClient(env))
                engine.check_new_products(log_rec, hours_back=hours)
            except Exception as exc:
                _log.error('Thread check_new fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc),
                               'end_date': datetime.now()})
                cr.commit()

    @api.model
    def _thread_sync_price_qty(self, db, uid, ctx, log_id):
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from ..services.sp_api_client import SpApiClient
        from ..services.sync_engine import SyncEngine

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            log_rec = env['ke.amazon.sync.log'].browse(log_id)
            try:
                engine = SyncEngine(env, SpApiClient(env))
                engine.sync_prices_and_quantities(log_rec)
            except Exception as exc:
                _log.error('Thread sync_price_qty fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc),
                               'end_date': datetime.now()})
                cr.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_credentials(self):
        from ..services.sp_api_client import SpApiClient
        try:
            SpApiClient(self.env).validate_credentials()
        except ValueError as exc:
            raise UserError(str(exc)) from exc

    def _open_log_action(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Log sincronizzazioni Amazon',
            'res_model': 'ke.amazon.sync.log',
            'view_mode': 'list,form',
            'target': 'current',
        }
