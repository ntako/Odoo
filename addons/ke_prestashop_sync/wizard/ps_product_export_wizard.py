from datetime import datetime

from odoo import api, fields, models


class PsProductExportWizard(models.TransientModel):
    _name = 'ke.ps.product.export'
    _description = 'Wizard Export Prodotti verso PrestaShop'

    export_mode = fields.Selection([
        ('new_only', 'Solo prodotti non ancora su PS (+ configurabili)'),
        ('full_resync', 'Ri-sincronizza tutto il catalogo'),
        ('selected', 'Prodotti selezionati'),
    ], string='Modalità', default='new_only', required=True)

    product_ids = fields.Many2many(
        'product.template',
        string='Prodotti',
        help='Seleziona i prodotti da esportare su PrestaShop.',
    )

    only_in_stock = fields.Boolean(
        string='Solo prodotti con disponibilità libera > 0',
        default=True,
        help='Esclude dall\'export i prodotti senza disponibilità libera '
             '(giacenza meno quantità riservata) nel magazzino indicato.',
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Magazzino di riferimento',
        default=lambda self: self._default_warehouse_id(),
        help='Le giacenze considerate per l\'opzione qui sopra sono quelle di '
             'questo magazzino.',
    )

    preview_count = fields.Integer(
        string='Prodotti da esportare', compute='_compute_preview')
    preview_info = fields.Char(compute='_compute_preview')

    @api.model
    def _default_warehouse_id(self):
        from ..services.sync_engine import SyncEngine
        return SyncEngine(self.env, None)._warehouse()

    @api.depends('export_mode', 'product_ids', 'only_in_stock', 'warehouse_id')
    def _compute_preview(self):
        from ..services.sync_engine import SyncEngine
        engine = SyncEngine(self.env, None)
        for wiz in self:
            try:
                pids = wiz.product_ids.ids if wiz.export_mode == 'selected' else None
                count = len(engine.export_candidates(
                    product_ids=pids,
                    full_resync=wiz.export_mode == 'full_resync',
                    only_in_stock=wiz.only_in_stock,
                    warehouse_id=wiz.warehouse_id.id or None,
                ))
            except Exception:  # noqa: BLE001 - l'anteprima non deve bloccare il wizard
                wiz.preview_count = -1
                wiz.preview_info = 'Impossibile calcolare l\'anteprima (vedi log).'
                continue
            wiz.preview_count = count
            scope = (
                f'disponibilità libera > 0 in "{wiz.warehouse_id.display_name}"'
                if wiz.only_in_stock and wiz.warehouse_id
                else 'tutti i magazzini'
            )
            wiz.preview_info = f'{count} prodotti verranno esportati   ·   {scope}'

    def action_run_export(self):
        product_ids = self.product_ids.ids if self.export_mode == 'selected' else None
        full_resync = self.export_mode == 'full_resync'

        label = f'Export prodotti → PS — {datetime.now().strftime("%d/%m/%Y %H:%M")}'
        log_id = self.env['ke.prestashop.sync.log'].enqueue(
            'export_products', 'export_products_to_ps', label=label, validate=True,
            product_ids=product_ids, full_resync=full_resync,
            only_in_stock=self.only_in_stock,
            warehouse_id=self.warehouse_id.id or None,
        )
        return {
            'type': 'ir.actions.act_window',
            'name': 'Log Export PrestaShop',
            'res_model': 'ke.prestashop.sync.log',
            'res_id': log_id,
            'view_mode': 'form',
            'target': 'current',
        }
