import logging
from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError

_log = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # --- Connessione ---
    ps_url = fields.Char(
        string='URL PrestaShop',
        config_parameter='ke_prestashop_sync.url',
        help='URL base del negozio (es: https://myshop.com). Senza slash finale.',
    )
    ps_api_key = fields.Char(
        string='API Key WebService',
        config_parameter='ke_prestashop_sync.api_key',
        help='Chiave generata in PrestaShop → Parametri Avanzati → WebService.',
    )
    ps_default_lang_id = fields.Integer(
        string='ID Lingua PS',
        config_parameter='ke_prestashop_sync.lang_id',
        default=1,
        help='ID della lingua di default in PrestaShop (normalmente 1).',
    )

    # --- Magazzino sorgente per la sync stock ---
    ps_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Magazzino per sync stock',
        compute='_compute_ps_warehouse',
        inverse='_set_ps_warehouse',
        help='Magazzino Odoo da cui leggere le quantità da inviare a PS.',
    )

    # --- Cron: sync stock ---
    ps_cron_sync_stock_active = fields.Boolean(
        string='Abilita sync automatica stock',
        compute='_compute_cron_states',
        inverse='_set_cron_sync_stock_active',
    )
    ps_sync_stock_interval = fields.Integer(
        string='Ogni (minuti)',
        config_parameter='ke_prestashop_sync.stock_interval_minutes',
        default=30,
    )

    # --- Cron: sync prezzi ---
    ps_cron_sync_prices_active = fields.Boolean(
        string='Abilita sync automatica prezzi',
        compute='_compute_cron_states',
        inverse='_set_cron_sync_prices_active',
    )
    ps_sync_prices_interval = fields.Integer(
        string='Ogni (minuti)',
        config_parameter='ke_prestashop_sync.prices_interval_minutes',
        default=60,
    )

    # --- Cron: import ordini ---
    ps_cron_import_orders_active = fields.Boolean(
        string='Abilita import automatico ordini',
        compute='_compute_cron_states',
        inverse='_set_cron_import_orders_active',
    )
    ps_import_orders_interval = fields.Integer(
        string='Ogni (minuti)',
        config_parameter='ke_prestashop_sync.orders_interval_minutes',
        default=15,
    )
    ps_import_orders_hours_back = fields.Integer(
        string='Importa ordini ultime N ore',
        config_parameter='ke_prestashop_sync.orders_hours_back',
        default=24,
    )

    # --- Opzioni import prodotti ---
    ps_import_update_existing = fields.Boolean(
        string='Aggiorna prodotti esistenti durante import',
        config_parameter='ke_prestashop_sync.import_update_existing',
        default=True,
    )
    ps_import_product_type = fields.Selection(
        string='Tipo prodotto Odoo',
        selection=[('consu', 'Beni'), ('service', 'Servizio')],
        config_parameter='ke_prestashop_sync.odoo_product_type',
        default='consu',
    )

    # ------------------------------------------------------------------
    # Magazzino: compute/inverse
    # ------------------------------------------------------------------

    def _compute_ps_warehouse(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'ke_prestashop_sync.warehouse_id', '')
        warehouse = self.env['stock.warehouse'].browse(int(param)) if param else \
            self.env['stock.warehouse'].search([], limit=1)
        for rec in self:
            rec.ps_warehouse_id = warehouse

    def _set_ps_warehouse(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ke_prestashop_sync.warehouse_id',
            str(self.ps_warehouse_id.id) if self.ps_warehouse_id else '',
        )

    # ------------------------------------------------------------------
    # Cron: compute/inverse
    # ------------------------------------------------------------------

    def _get_cron(self, xml_id):
        return self.env.ref(f'ke_prestashop_sync.{xml_id}', raise_if_not_found=False)

    @api.depends()
    def _compute_cron_states(self):
        cron_stock = self._get_cron('cron_sync_stock')
        cron_prices = self._get_cron('cron_sync_prices')
        cron_orders = self._get_cron('cron_import_orders')
        for rec in self:
            rec.ps_cron_sync_stock_active = cron_stock.active if cron_stock else False
            rec.ps_cron_sync_prices_active = cron_prices.active if cron_prices else False
            rec.ps_cron_import_orders_active = cron_orders.active if cron_orders else False

    def _set_cron_sync_stock_active(self):
        cron = self._get_cron('cron_sync_stock')
        if cron:
            cron.sudo().active = self.ps_cron_sync_stock_active

    def _set_cron_sync_prices_active(self):
        cron = self._get_cron('cron_sync_prices')
        if cron:
            cron.sudo().active = self.ps_cron_sync_prices_active

    def _set_cron_import_orders_active(self):
        cron = self._get_cron('cron_import_orders')
        if cron:
            cron.sudo().active = self.ps_cron_import_orders_active

    # ------------------------------------------------------------------
    # Bottoni manuali (esecuzione sincrona — nessun thread)
    # ------------------------------------------------------------------

    def action_test_connection(self):
        self._validate_credentials()
        raise UserError('Connessione a PrestaShop riuscita!')

    def action_run_sync_stock(self):
        from ..services.ps_api_client import PsApiClient
        from ..services.sync_engine import SyncEngine
        self._validate_credentials()
        log_rec = self.env['ke.prestashop.sync.log'].create({
            'name': f'Sync quantità → PS (manuale) — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'sync_stock',
            'state': 'running',
            'start_date': datetime.now(),
        })
        try:
            SyncEngine(self.env, PsApiClient(self.env)).sync_stock_to_prestashop(log_rec)
        except Exception as exc:
            _log.error('sync_stock fallito: %s', exc, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(exc),
                           'end_date': datetime.now()})
        return self._open_log_record(log_rec.id)

    def action_run_sync_prices(self):
        from ..services.ps_api_client import PsApiClient
        from ..services.sync_engine import SyncEngine
        self._validate_credentials()
        log_rec = self.env['ke.prestashop.sync.log'].create({
            'name': f'Sync prezzi → PS (manuale) — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'sync_prices',
            'state': 'running',
            'start_date': datetime.now(),
        })
        try:
            SyncEngine(self.env, PsApiClient(self.env)).sync_prices_to_prestashop(log_rec)
        except Exception as exc:
            _log.error('sync_prices fallito: %s', exc, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(exc),
                           'end_date': datetime.now()})
        return self._open_log_record(log_rec.id)

    def action_run_import_orders(self):
        from ..services.ps_api_client import PsApiClient
        from ..services.sync_engine import SyncEngine
        self._validate_credentials()
        hours_back = int(self.env['ir.config_parameter'].sudo().get_param(
            'ke_prestashop_sync.orders_hours_back', '24'))
        log_rec = self.env['ke.prestashop.sync.log'].create({
            'name': f'Import ordini PS (manuale) — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'import_orders',
            'state': 'running',
            'start_date': datetime.now(),
        })
        try:
            SyncEngine(self.env, PsApiClient(self.env)).import_orders_from_ps(
                log_rec, hours_back=hours_back)
        except Exception as exc:
            _log.error('import_orders fallito: %s', exc, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(exc),
                           'end_date': datetime.now()})
        return self._open_log_record(log_rec.id)

    def action_run_sync_images(self):
        from ..services.ps_api_client import PsApiClient
        from ..services.sync_engine import SyncEngine
        self._validate_credentials()
        log_rec = self.env['ke.prestashop.sync.log'].create({
            'name': f'Sync immagini → PS (manuale) — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'sync_images',
            'state': 'running',
            'start_date': datetime.now(),
        })
        try:
            SyncEngine(self.env, PsApiClient(self.env)).sync_images_to_prestashop(log_rec)
        except Exception as exc:
            _log.error('sync_images fallito: %s', exc, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(exc),
                           'end_date': datetime.now()})
        return self._open_log_record(log_rec.id)

    def action_run_sync_images_force(self):
        from ..services.ps_api_client import PsApiClient
        from ..services.sync_engine import SyncEngine
        self._validate_credentials()
        log_rec = self.env['ke.prestashop.sync.log'].create({
            'name': f'Sync immagini → PS (forza) — {datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'sync_images',
            'state': 'running',
            'start_date': datetime.now(),
        })
        try:
            SyncEngine(self.env, PsApiClient(self.env)).sync_images_to_prestashop(
                log_rec, force=True)
        except Exception as exc:
            _log.error('sync_images_force fallito: %s', exc, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(exc),
                           'end_date': datetime.now()})
        return self._open_log_record(log_rec.id)

    def action_diagnose_ps_links(self):
        self.env.cr.execute("""
            SELECT ps_product_id, COUNT(*) AS n, array_agg(id ORDER BY id) AS odoo_ids
            FROM product_template
            WHERE ps_product_id > 0
            GROUP BY ps_product_id
            HAVING COUNT(*) > 1
            ORDER BY n DESC, ps_product_id
        """)
        rows = self.env.cr.fetchall()
        total_with_id = self.env['product.template'].search_count([('ps_product_id', '>', 0)])
        total_without_id = self.env['product.template'].search_count([('ps_product_id', '=', 0)])
        if not rows:
            msg = (
                f'Nessun ps_product_id duplicato trovato.\n'
                f'Prodotti con ps_product_id > 0: {total_with_id}\n'
                f'Prodotti con ps_product_id = 0 (da esportare): {total_without_id}'
            )
        else:
            dup_count = sum(r[1] - 1 for r in rows)
            lines = [
                f'ps_id={r[0]}  ({r[1]} prodotti Odoo → IDs: {r[2]})'
                for r in rows[:30]
            ]
            msg = (
                f'{len(rows)} ps_product_id condivisi, {dup_count} prodotti Odoo "extra".\n'
                f'Prodotti con ps_product_id > 0: {total_with_id}\n'
                f'Prodotti con ps_product_id = 0: {total_without_id}\n\n'
                + '\n'.join(lines)
                + ('\n...' if len(rows) > 30 else '')
            )
        raise UserError(msg)

    def action_reset_orphaned_ps_links(self):
        from ..services.ps_api_client import PsApiClient
        from ..services.sync_engine import SyncEngine
        self._validate_credentials()
        count = SyncEngine(self.env, PsApiClient(self.env)).reset_orphaned_ps_links()
        raise UserError(
            f'{count} prodott{"o" if count == 1 else "i"} resettati: '
            f'ps_product_id azzerato perché il prodotto non esiste più su PS. '
            f'Riesegui l\'export per ricrearli.'
        ) if count else UserError('Nessun link orfano trovato: tutti i ps_product_id sono validi.')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_credentials(self):
        from ..services.ps_api_client import PsApiClient
        try:
            PsApiClient(self.env).validate_credentials()
        except (ValueError, Exception) as exc:
            raise UserError(str(exc)) from exc

    def _open_log_record(self, log_id):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Log sincronizzazione PrestaShop',
            'res_model': 'ke.prestashop.sync.log',
            'res_id': log_id,
            'view_mode': 'form',
            'target': 'current',
        }
