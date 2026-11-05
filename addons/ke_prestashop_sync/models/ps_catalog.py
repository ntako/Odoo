from odoo import api, fields, models
from odoo.exceptions import UserError


class PrestashopCategory(models.Model):
    """
    Copia in Odoo dell'albero delle categorie PrestaShop (sola lettura,
    aggiornata da "Aggiorna categorie da PrestaShop"), per scegliere su ogni
    categoria Odoo la categoria PS dei suoi prodotti. Deciso con l'utente il
    2026-11-05: si mappa sull'albero PS esistente, non lo si ricrea da Odoo.
    """
    _name = 'ke.prestashop.category'
    _description = 'Categoria PrestaShop'
    _order = 'complete_name'
    _rec_name = 'complete_name'

    ps_id = fields.Integer('ID PrestaShop', required=True, index=True, readonly=True)
    name = fields.Char('Nome', required=True, readonly=True)
    parent_ps_id = fields.Integer('ID padre', readonly=True)
    level = fields.Integer('Livello', readonly=True)
    complete_name = fields.Char('Percorso', readonly=True, index=True)
    active = fields.Boolean('Attiva su PS', default=True, readonly=True)
    odoo_category_ids = fields.One2many('product.category', 'ps_category_id', string='Categorie Odoo')

    _ps_id_uniq = models.Constraint('unique(ps_id)', 'Categoria PrestaShop già presente.')

    @api.model
    def _sync_from_prestashop(self, client):
        """Crea/aggiorna le categorie da PS; quelle sparite da PS restano ma disattivate."""
        rows = client.get_categories()
        by_id = {r['id']: r for r in rows}

        def path(ps_id):
            names, seen = [], set()
            while ps_id in by_id and ps_id not in seen:
                seen.add(ps_id)
                row = by_id[ps_id]
                if row['level'] <= 1:  # Root e Home non fanno parte del percorso
                    break
                names.append(row['name'])
                ps_id = row['parent']
            return ' / '.join(reversed(names)) or by_id.get(ps_id, {}).get('name', '')

        existing = {c.ps_id: c for c in self.with_context(active_test=False).search([])}
        created = updated = 0
        for row in rows:
            vals = {
                'name': row['name'] or str(row['id']), 'parent_ps_id': row['parent'],
                'level': row['level'], 'active': row['active'], 'complete_name': path(row['id']),
            }
            rec = existing.pop(row['id'], None)
            if rec:
                rec.write(vals)
                updated += 1
            else:
                self.create(dict(vals, ps_id=row['id']))
                created += 1
        for rec in existing.values():
            rec.active = False
        return created, updated, len(existing)


class ProductCategory(models.Model):
    _inherit = 'product.category'

    ps_category_id = fields.Many2one(
        'ke.prestashop.category', string='Categoria PrestaShop', ondelete='set null',
        help='Categoria PS dei prodotti di questa categoria. Vuota: vale quella della categoria '
             'padre, altrimenti la categoria PS predefinita (Impostazioni → PrestaShop).',
    )


class ProductBrand(models.Model):
    _inherit = 'ke.product.brand'

    ps_manufacturer_id = fields.Integer(
        'ID marca PrestaShop', copy=False,
        help='Marca (produttore) PrestaShop associata. 0: la associa l\'export per nome, anche '
             'con i nomi alternativi, e la crea su PS se manca.',
    )


class ResConfigSettingsPsCatalog(models.TransientModel):
    _inherit = 'res.config.settings'

    def action_refresh_ps_categories(self):
        from ..services.ps_api_client import PsApiClient
        try:
            created, updated, gone = self.env['ke.prestashop.category'].sudo()._sync_from_prestashop(
                PsApiClient(self.env))
        except Exception as exc:
            raise UserError(f'Lettura categorie da PrestaShop fallita: {exc}') from exc
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': 'Categorie PrestaShop',
                'message': f'{created} nuove, {updated} aggiornate, {gone} non più su PrestaShop.',
                'type': 'success', 'sticky': False,
            },
        }
