"""
Chi ha scritto il costo della variante (2026-11-24).

Il costo del magazzino (file "Giacenze e costi", o scritto a mano) è il costo reale, spesso più basso del
listino (es. C4 9,23 € contro 12,92 €): i listini dei fornitori lo scrivono solo dove il costo manca o era
già del listino. Ogni scrittura del costo fuori dall'import dei listini (a mano, magazzino, altro) vale
"Manuale"; l'import dei listini scrive con il contesto ke_cost_source='supplier'.
"""
from odoo import fields, models

COST_SOURCES = [('supplier', 'Listino fornitore'), ('manual', 'Manuale / magazzino')]


class ProductProduct(models.Model):
    _inherit = 'product.product'

    ke_cost_source = fields.Selection(
        COST_SOURCES, string='Fonte del costo', copy=False,
        help='Listino fornitore: aggiornato a ogni import del listino. Manuale / magazzino: il listino non lo '
             'cambia più (scritto a mano o dal file del magazzino).')

    def write(self, vals):
        if 'standard_price' in vals and 'ke_cost_source' not in vals:
            vals = dict(vals, ke_cost_source=self.env.context.get('ke_cost_source') or 'manual')
        return super().write(vals)

    def ke_supplier_may_write_cost(self):
        """
        Il listino può scrivere il costo: manca, o è già del listino. Un costo di fonte sconosciuta (scritto
        prima del 2026-11-24) resta: non si sa se era reale.
        """
        self.ensure_one()
        return not self.standard_price or self.ke_cost_source == 'supplier'
