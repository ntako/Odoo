from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services.base_price import BasePriceRecalc


class AmazonBasePriceWizard(models.TransientModel):
    """
    "Ricalcola prezzi base da Amazon" (2026-11-09): anteprima del prezzo base
    ricavato dai prezzi Amazon attuali, scritto solo con "Applica". Logica in
    services/base_price.py.
    """
    _name = 'ke.amazon.base.price.wizard'
    _description = 'Ricalcolo prezzi base da Amazon'

    include_existing = fields.Boolean(
        'Ricalcola anche i prezzi già impostati',
        help='Di solito no: si toccano solo i prodotti con prezzo di vendita vuoto o 1,00, così '
             'un prezzo base messo a mano non viene sovrascritto.')
    plan = fields.Json('Piano', readonly=True)
    preview = fields.Html('Anteprima', readonly=True, sanitize=False)
    templates_count = fields.Integer('Prodotti da aggiornare', readonly=True)

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        vals.update(self._compute_plan_vals(vals.get('include_existing', False)))
        return vals

    @api.onchange('include_existing')
    def _onchange_include_existing(self):
        self.update(self._compute_plan_vals(self.include_existing))

    def _compute_plan_vals(self, include_existing):
        plan = BasePriceRecalc(self.env).plan(include_existing)
        return {'plan': plan, 'preview': self._render(plan), 'templates_count': len(plan['templates'])}

    @staticmethod
    def _money(value):
        return f'{value:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')

    def _render(self, plan):
        items = [
            f'<b>{len(plan["templates"])}</b> prodotti ({plan["variants_total"]} varianti) con prezzo Amazon; '
            f'<b>{plan["changed"]}</b> varianti cambiano prezzo',
            'Sorgente: ' + (', '.join(f'{k} {v}' for k, v in plan['by_marketplace'].items()) or 'nessuna'),
        ]
        if plan['variants']:
            own = sum(1 for p in plan['variants'].values() if p)
            items.append(f'{own} varianti con prezzo diverso dalla famiglia ("Prezzo di vendita variante")')
        if plan['kept_existing'] or plan['variant_own_kept']:
            items.append(f'Non toccati perché hanno già un prezzo: {plan["kept_existing"]} prodotti, '
                         f'{plan["variant_own_kept"]} varianti con prezzo proprio')
        if plan['without_amazon_price']:
            items.append(f'{plan["without_amazon_price"]} prodotti senza un prezzo Amazon: restano come sono')
        if plan['skipped_marketplaces']:
            items.append('Marketplace saltati, valuta senza tasso di cambio: ' + ', '.join(plan['skipped_marketplaces']))
        html = Markup('<ul>') + Markup('').join(Markup('<li>%s</li>') % Markup(i) for i in items) + Markup('</ul>')
        html += Markup('<p class="text-muted">Calcolo usato: %s</p>') % '; '.join(plan['marketplaces'])
        if plan['rows']:
            html += Markup('<table class="table table-sm"><thead><tr><th>Prodotto</th><th>Da</th>'
                           '<th class="text-end">Prezzo Amazon</th><th class="text-end">Prezzo base attuale</th>'
                           '<th class="text-end">Nuovo prezzo base</th></tr></thead><tbody>')
            for row in plan['rows']:
                html += Markup('<tr><td>%s%s</td><td>%s</td><td class="text-end">%s %s</td>'
                               '<td class="text-end">%s</td><td class="text-end">%s</td></tr>') % (
                    row['name'], Markup(' <i>(variante)</i>') if row['variant_price'] else '',
                    row['marketplace'], self._money(row['amazon']), row['currency'],
                    self._money(row['old']), self._money(row['new']))
            html += Markup('</tbody></table>')
            if plan['changed'] > len(plan['rows']):
                html += Markup('<p class="text-muted">Primi %s di %s.</p>') % (len(plan['rows']), plan['changed'])
        return html

    def action_apply(self):
        self.ensure_one()
        if not self.plan or not self.plan.get('templates'):
            raise UserError('Nessun prezzo da aggiornare.')
        templates, variants = BasePriceRecalc(self.env).apply(self.plan)
        done, errors = self.env['ke.amazon.listing'].sudo()._compute_sync_prices()
        message = (f'{templates} prodotti aggiornati, {variants} varianti con prezzo proprio. '
                   f'Prezzi con sync ricalcolati: {done}' + (f', {errors} non calcolabili' if errors else '') + '.')
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': 'Prezzi base da Amazon', 'message': message, 'type': 'success',
                       'sticky': True, 'next': {'type': 'ir.actions.act_window_close'}},
        }
