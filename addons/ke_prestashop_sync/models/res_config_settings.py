import logging

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
    ps_shop_id = fields.Integer(
        string='ID Shop PS',
        config_parameter='ke_prestashop_sync.shop_id',
        default=1,
        help='ID dello shop PrestaShop usato per lo stock (multistore: 1 di default).',
    )
    ps_category_id = fields.Integer(
        string='Categoria PS di default',
        config_parameter='ke_prestashop_sync.category_id',
        default=2,
        help='ID categoria PrestaShop assegnata ai prodotti creati dall\'export.',
    )
    ps_tax_rules_group_id = fields.Integer(
        string='Gruppo regole IVA PS',
        config_parameter='ke_prestashop_sync.tax_rules_group_id',
        default=1,
        help='ID id_tax_rules_group assegnato ai prodotti creati dall\'export.',
    )

    # --- Magazzino sorgente per la sync stock ---
    ps_warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Magazzino PrestaShop',
        compute='_compute_ps_warehouse',
        inverse='_set_ps_warehouse',
        help='Magazzino Odoo da cui leggere le quantità da inviare a PS e '
             'assegnato agli ordini importati da PrestaShop.',
    )
    ps_filter_by_warehouse = fields.Boolean(
        string='Sincronizza solo i prodotti con giacenza nel magazzino',
        config_parameter='ke_prestashop_sync.filter_by_warehouse',
        help='Se attivo, le sincronizzazioni Odoo → PrestaShop (stock, prezzi, '
             'immagini, export) considerano solo i prodotti con quantità '
             'disponibile maggiore di zero nel magazzino selezionato.',
    )

    # --- Cron: sync stock ---
    ps_cron_sync_stock_active = fields.Boolean(
        string='Abilita sync automatica stock',
        compute='_compute_cron_states',
        inverse='_set_cron_sync_stock_active',
    )
    ps_sync_stock_interval = fields.Integer(
        string='Sync stock ogni (minuti)',
        compute='_compute_cron_states',
        inverse='_set_sync_stock_interval',
    )

    # --- Cron: sync prezzi ---
    ps_cron_sync_prices_active = fields.Boolean(
        string='Abilita sync automatica prezzi',
        compute='_compute_cron_states',
        inverse='_set_cron_sync_prices_active',
    )
    ps_sync_prices_interval = fields.Integer(
        string='Sync prezzi ogni (minuti)',
        compute='_compute_cron_states',
        inverse='_set_sync_prices_interval',
    )

    # --- Cron: import ordini ---
    ps_cron_import_orders_active = fields.Boolean(
        string='Abilita import automatico ordini',
        compute='_compute_cron_states',
        inverse='_set_cron_import_orders_active',
    )
    ps_import_orders_interval = fields.Integer(
        string='Import ordini ogni (minuti)',
        compute='_compute_cron_states',
        inverse='_set_import_orders_interval',
    )
    ps_import_orders_hours_back = fields.Integer(
        string='Importa ordini ultime N ore',
        config_parameter='ke_prestashop_sync.orders_hours_back',
        default=24,
    )
    ps_order_auto_confirm = fields.Boolean(
        string='Conferma automaticamente gli ordini importati',
        config_parameter='ke_prestashop_sync.order_auto_confirm',
        help='Se attivo, gli ordini PrestaShop importati vengono confermati '
             '(action_confirm) invece di restare come preventivi.',
    )

    # --- Cron: sync immagini ---
    ps_cron_sync_images_active = fields.Boolean(
        string='Abilita sync automatica immagini',
        compute='_compute_cron_states',
        inverse='_set_cron_sync_images_active',
    )
    ps_sync_images_interval = fields.Integer(
        string='Sync immagini ogni (minuti)',
        compute='_compute_cron_states',
        inverse='_set_sync_images_interval',
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
        cron_images = self._get_cron('cron_sync_images')
        for rec in self:
            rec.ps_cron_sync_stock_active = bool(cron_stock and cron_stock.active)
            rec.ps_cron_sync_prices_active = bool(cron_prices and cron_prices.active)
            rec.ps_cron_import_orders_active = bool(cron_orders and cron_orders.active)
            rec.ps_cron_sync_images_active = bool(cron_images and cron_images.active)
            rec.ps_sync_stock_interval = cron_stock.interval_number if cron_stock else 30
            rec.ps_sync_prices_interval = cron_prices.interval_number if cron_prices else 60
            rec.ps_import_orders_interval = cron_orders.interval_number if cron_orders else 15
            rec.ps_sync_images_interval = cron_images.interval_number if cron_images else 360

    def _set_cron_active(self, xml_id, active):
        cron = self._get_cron(xml_id)
        if cron:
            cron.sudo().active = bool(active)

    def _set_cron_interval(self, xml_id, minutes):
        cron = self._get_cron(xml_id)
        if cron and minutes and minutes > 0:
            cron.sudo().write({'interval_number': minutes, 'interval_type': 'minutes'})

    def _set_cron_sync_stock_active(self):
        self._set_cron_active('cron_sync_stock', self.ps_cron_sync_stock_active)

    def _set_cron_sync_prices_active(self):
        self._set_cron_active('cron_sync_prices', self.ps_cron_sync_prices_active)

    def _set_cron_import_orders_active(self):
        self._set_cron_active('cron_import_orders', self.ps_cron_import_orders_active)

    def _set_cron_sync_images_active(self):
        self._set_cron_active('cron_sync_images', self.ps_cron_sync_images_active)

    def _set_sync_stock_interval(self):
        self._set_cron_interval('cron_sync_stock', self.ps_sync_stock_interval)

    def _set_sync_prices_interval(self):
        self._set_cron_interval('cron_sync_prices', self.ps_sync_prices_interval)

    def _set_import_orders_interval(self):
        self._set_cron_interval('cron_import_orders', self.ps_import_orders_interval)

    def _set_sync_images_interval(self):
        self._set_cron_interval('cron_sync_images', self.ps_sync_images_interval)

    # ------------------------------------------------------------------
    # Bottoni manuali (esecuzione asincrona in thread dedicato)
    # ------------------------------------------------------------------

    def _run(self, sync_type, engine_method, label=None, **kwargs):
        log_id = self.env['ke.prestashop.sync.log'].enqueue(
            sync_type, engine_method, label=label, validate=True, **kwargs)
        return self._open_log_record(log_id)

    def action_test_connection(self):
        self._validate_credentials()
        raise UserError('Connessione a PrestaShop riuscita!')

    def action_run_sync_stock(self):
        return self._run('sync_stock', 'sync_stock_to_prestashop')

    def action_run_sync_prices(self):
        return self._run('sync_prices', 'sync_prices_to_prestashop')

    def action_run_import_orders(self):
        hours_back = int(self.env['ir.config_parameter'].sudo().get_param(
            'ke_prestashop_sync.orders_hours_back', '24'))
        return self._run('import_orders', 'import_orders_from_ps', hours_back=hours_back)

    def action_run_sync_images(self):
        return self._run('sync_images', 'sync_images_to_prestashop')

    def action_run_sync_images_force(self):
        label = (f'Sync immagini → PS (re-upload) — '
                 f'{fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}')
        return self._run('sync_images', 'sync_images_to_prestashop',
                         label=label, force=True)

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
        except Exception as exc:
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
