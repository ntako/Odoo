import threading
import logging
from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError

_log = logging.getLogger(__name__)

_DEF_MARKETPLACE_ID = 'APJ6JRA9NG5V4'  # ID pubblico del marketplace IT, non un segreto


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # --- Credenziali SP-API ---
    # Nessun default hardcoded: i parametri vengono creati vuoti all'installazione
    # (data/default_credentials.xml) e vanno compilati dall'utente in questa
    # schermata. Non inserire mai qui valori reali di refresh token/secret.
    amazon_refresh_token = fields.Char(
        string='Refresh Token',
        config_parameter='ke_amazon_sync.refresh_token',
    )
    amazon_lwa_app_id = fields.Char(
        string='LWA App ID',
        config_parameter='ke_amazon_sync.lwa_app_id',
    )
    amazon_lwa_client_secret = fields.Char(
        string='LWA Client Secret',
        config_parameter='ke_amazon_sync.lwa_client_secret',
    )
    amazon_seller_id = fields.Char(
        string='Seller ID (Merchant Token)',
        config_parameter='ke_amazon_sync.seller_id',
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
        string='Controllo ogni (ore)',
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
        string='Riconciliazione ogni (ore)',
        config_parameter='ke_amazon_sync.reconcile_interval_hours',
        default=24,
    )

    # --- Ordini ---
    amazon_orders_days_back = fields.Integer(
        string='Giorni da importare',
        config_parameter='ke_amazon_sync.orders_days_back',
        default=30,
        help='Numero di giorni nel passato da cui recuperare gli ordini Amazon ad ogni import.',
    )
    amazon_default_vat_rate = fields.Float(
        string='Aliquota IVA predefinita (%)',
        config_parameter='ke_amazon_sync.default_vat_rate',
        default=22.0,
        help='Aliquota IVA usata per calcolare il prezzo netto dai prezzi lordi Amazon '
             '(IT: 22, DE: 19, FR: 20, ES: 21). '
             'Usata solo per il prezzo listino dei prodotti importati: '
             'per le righe ordine viene usata l\'imposta esatta restituita da Amazon.',
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
    # Magazzini
    # ------------------------------------------------------------------

    def action_open_company_amazon(self):
        """Apre il form dell'azienda corrente sul tab Amazon per configurare i magazzini."""
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'res.company',
            'res_id': self.env.company.id,
            'view_mode': 'form',
            'target': 'current',
        }

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
        if not self.env['ke.amazon.marketplace'].search([('active', '=', True)]):
            raise UserError(
                'Nessun marketplace attivo. Usa "Rileva marketplace attivi" '
                '(Impostazioni → Amazon) o attivane uno a mano.'
            )
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
        """
        Avvia manualmente la riconciliazione store via Reports API (nessun limite di
        prodotti) e apre il log. Usa lo stesso meccanismo del cron automatico
        (report completo) invece della Listings API, limitata a ~1000 prodotti.
        """
        self._validate_credentials()
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.reconcile_state') == 'running':
            raise UserError('Una riconciliazione è già in corso. Controlla il log sincronizzazioni.')
        log_rec = self.env['ke.amazon.sync.log'].action_start_reconcile(
            name_prefix='Riconciliazione store (manuale)'
        )
        if not log_rec:
            raise UserError(
                'Impossibile avviare la riconciliazione: verifica le credenziali SP-API oppure '
                'attiva almeno un marketplace (Impostazioni → Amazon → "Rileva marketplace attivi").'
            )
        return {
            'type': 'ir.actions.act_window',
            'name': 'Log Riconciliazione Amazon',
            'res_model': 'ke.amazon.sync.log',
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_run_seed_fbm_stock(self):
        """
        Operazione una tantum: imposta la giacenza dei prodotti FBM nel magazzino
        FBM configurato usando x_amazon_qty (quantità dichiarata su Amazon, già
        salvata a ogni sync). Non richiede credenziali SP-API: è un'operazione
        interna, nessuna chiamata ad Amazon.
        """
        log_rec = self.env['ke.amazon.sync.log'].create({
            'name': f'Recupero Giacenza FBM da Amazon — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'seed_fbm_stock',
            'state': 'running',
            'start_date': datetime.now(),
        })
        self.env.cr.commit()
        self._start_thread('_thread_seed_fbm_stock', log_rec.id)
        return self._open_log_action()

    def action_migrate_to_amazon_listings(self):
        """
        Operazione una tantum (fase 1 multi-marketplace, vedi TASK.md): crea una
        riga ke.amazon.listing per ogni prodotto con x_amazon_sku, sul marketplace
        oggi configurato (Marketplace ID qui sopra). Non tocca né rimuove i campi
        x_amazon_* originali — sync_engine.py e le viste esistenti continuano a
        usarli finché non vengono riscritti in una fase successiva. Operazione
        interna, nessuna chiamata ad Amazon, sicura da rilanciare più volte
        (salta i prodotti già migrati per quel marketplace).
        """
        mp_code = self.env['ir.config_parameter'].sudo().get_param(
            'ke_amazon_sync.marketplace_id') or _DEF_MARKETPLACE_ID
        marketplace = self.env['ke.amazon.marketplace'].search(
            [('marketplace_id', '=', mp_code)], limit=1)
        if not marketplace:
            raise UserError(
                f'Nessun marketplace configurato con Marketplace ID "{mp_code}" '
                f'(Magazzino → Amazon Sync → Marketplace). Aggiungilo prima di migrare.'
            )

        Listing = self.env['ke.amazon.listing']
        products = self.env['product.template'].search([('x_amazon_sku', '!=', False)])
        already_ids = set(Listing.search([
            ('marketplace_id', '=', marketplace.id),
            ('product_id', 'in', products.ids),
        ]).mapped('product_id').ids)

        vals_list = [
            {
                'product_id': product.id,
                'marketplace_id': marketplace.id,
                'sku': product.x_amazon_sku,
                'asin': product.x_amazon_asin,
                'fnsku': product.x_amazon_fnsku,
                'product_type': product.x_amazon_product_type,
                'status': product.x_amazon_status,
                'fulfillment': product.x_amazon_fulfillment,
                'price': product.x_amazon_price,
                'qty': product.x_amazon_qty,
                'category': product.x_amazon_category,
                'browse_node_id': product.x_amazon_browse_node_id,
                'condition': product.x_amazon_condition,
                'publish_state': product.x_amazon_publish_state,
                'publish_error': product.x_amazon_publish_error,
                'found_by_manufacturer': product.x_amazon_found_by_manufacturer,
                'sync_price': product.x_amazon_sync_price,
                'sync_qty': product.x_amazon_sync_qty,
                'last_sync': product.x_amazon_last_sync,
                'sync_error': product.x_amazon_sync_error,
            }
            for product in products if product.id not in already_ids
        ]
        if vals_list:
            Listing.create(vals_list)
        self.env.cr.commit()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Migrazione multi-marketplace completata',
                'message': (
                    f'{len(vals_list)} inserzioni create su "{marketplace.name}", '
                    f'{len(already_ids)} già presenti (saltate).'
                ),
                'sticky': True,
                'type': 'success',
            },
        }

    def action_run_download_images(self):
        """Scarica le immagini mancanti dai prodotti Amazon in background."""
        self._validate_credentials()
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.download_images_state') == 'running':
            raise UserError('Un download immagini è già in corso.')
        log_rec = self.env['ke.amazon.sync.log'].create({
            'name': f'Download immagini mancanti — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'download_images',
            'state': 'running',
            'start_date': datetime.now(),
        })
        params.set_param('ke_amazon_sync.download_images_state', 'running')
        self.env.cr.commit()
        self._start_thread('_thread_download_images', log_rec.id)
        return self._open_log_action()

    def action_run_detect_marketplaces(self):
        """
        Chiede ad Amazon su quali marketplace il venditore è realmente attivo
        (Sellers API) e aggiorna ke.amazon.marketplace di conseguenza — da
        eseguire almeno una volta prima di affidarsi a qualunque sync
        multi-marketplace (import ordini incluso). In background: la
        chiamata può richiedere fino a ~60s (rate limit ufficiale
        dell'endpoint), non deve bloccare il click del pulsante.
        """
        self._validate_credentials()
        log_rec = self.env['ke.amazon.sync.log'].create({
            'name': f'Rileva marketplace attivi — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'detect_marketplaces',
            'state': 'running',
            'start_date': datetime.now(),
        })
        self.env.cr.commit()
        self._start_thread('_thread_detect_marketplaces', log_rec.id)
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
    def _thread_download_images(self, db, uid, ctx, log_id):
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from ..services.sp_api_client import SpApiClient
        from ..services.sync_engine import SyncEngine

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            log_rec = env['ke.amazon.sync.log'].browse(log_id)
            params = env['ir.config_parameter'].sudo()
            try:
                engine = SyncEngine(env, SpApiClient(env))
                ok, skipped, errors = engine.download_missing_images(log_rec)
                _log.info('Download immagini: %d ok, %d skip, %d errori', ok, skipped, errors)
            except Exception as exc:
                _log.error('Thread download_images fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc),
                               'end_date': datetime.now()})
                cr.commit()
            finally:
                params.set_param('ke_amazon_sync.download_images_state', 'idle')
                cr.commit()

    @api.model
    def _thread_detect_marketplaces(self, db, uid, ctx, log_id):
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from ..services.sp_api_client import SpApiClient
        from ..services.sync_engine import SyncEngine

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            log_rec = env['ke.amazon.sync.log'].browse(log_id)
            try:
                engine = SyncEngine(env, SpApiClient(env))
                activated, created, not_confirmed = engine.detect_active_marketplaces(log_rec)
                _log.info(
                    'Rileva marketplace: %d attivati, %d creati, %d non confermati',
                    len(activated), len(created), len(not_confirmed),
                )
            except Exception as exc:
                _log.error('Thread detect_marketplaces fallito: %s', exc, exc_info=True)
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
                # Fase 3 multi-marketplace: un giro per ogni marketplace attivo,
                # ognuno con il proprio client SP-API (sync_prices_and_quantities
                # capisce da solo se è il marketplace primario — vedi
                # _is_primary_marketplace — e sceglie di conseguenza campi legacy
                # o ke.amazon.listing). I contatori si sommano tra i marketplace.
                marketplaces = env['ke.amazon.marketplace'].search([('active', '=', True)])
                total_ok = total_errors = 0
                for marketplace in marketplaces:
                    client = SpApiClient(env, marketplace_id=marketplace.marketplace_id)
                    engine = SyncEngine(env, client)
                    ok, errors = engine.sync_prices_and_quantities(log_rec)
                    total_ok += ok
                    total_errors += errors
                log_rec.write({
                    'state': 'error' if total_errors and not total_ok else 'done',
                    'end_date': datetime.now(),
                })
                cr.commit()
            except Exception as exc:
                _log.error('Thread sync_price_qty fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc),
                               'end_date': datetime.now()})
                cr.commit()

    @api.model
    def _thread_seed_fbm_stock(self, db, uid, ctx, log_id):
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from ..services.sync_engine import SyncEngine

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            log_rec = env['ke.amazon.sync.log'].browse(log_id)
            try:
                engine = SyncEngine(env, None)
                updated, skipped, errors = engine.seed_fbm_stock_from_amazon_qty(log_rec)
                _log.info('Recupero giacenza FBM: %d aggiornati, %d saltati, %d errori',
                          updated, skipped, errors)
            except Exception as exc:
                _log.error('Thread seed_fbm_stock fallito: %s', exc, exc_info=True)
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
