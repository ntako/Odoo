from datetime import datetime

from odoo import fields, models


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
        label_suffix = f' (limite {self.limit})' if self.limit else ''
        label = (f'Import prodotti da PS{label_suffix} — '
                 f'{datetime.now().strftime("%d/%m/%Y %H:%M")}')
        log_id = self.env['ke.prestashop.sync.log'].enqueue(
            'import_products', 'import_products_from_ps', label=label, validate=True,
            limit=self.limit, update_existing=self.update_existing,
        )
        return {
            'type': 'ir.actions.act_window',
            'name': 'Log Import PrestaShop',
            'res_model': 'ke.prestashop.sync.log',
            'res_id': log_id,
            'view_mode': 'form',
            'target': 'current',
        }
