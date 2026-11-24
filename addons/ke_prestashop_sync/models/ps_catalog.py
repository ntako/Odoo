from odoo import api, fields, models
from odoo.exceptions import UserError


class PrestashopCategory(models.Model):
    """
    Copia in Odoo dell'albero delle categorie PrestaShop (sola lettura,
    aggiornata da "Aggiorna categorie da PrestaShop"), per scegliere su ogni
    categoria Odoo la categoria PS dei suoi prodotti. 2026-11-05: si mappava
    sull'albero PS esistente; dal 2026-11-17 (deciso con l'utente) l'albero
    del sito ricalca quello Odoo: "Sincronizza categorie da Odoo"
    (SyncEngine.sync_category_tree) riusa le categorie PS coerenti e crea le
    altre (odoo_managed).
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
    ps_product_count = fields.Integer(
        'Prodotti sul negozio', readonly=True,
        help='Prodotti della categoria e delle sottocategorie sul negozio (all\'ultima lettura).')
    odoo_managed = fields.Boolean(
        'Creata da Odoo', readonly=True,
        help='Creata dalla sincronizzazione dell\'albero: Odoo la attiva e disattiva secondo i prodotti pubblicati.')
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
        # 'active' qui segue il negozio; il campo Odoo 'active' della copia nasconde anche le sparite.
        created = updated = 0
        for row in rows:
            vals = {
                'name': row['name'] or str(row['id']), 'parent_ps_id': row['parent'],
                'level': row['level'], 'active': row['active'], 'complete_name': path(row['id']),
                'ps_product_count': row.get('products', 0),
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
    ps_in_site = fields.Boolean(
        'Nel sito', compute='_compute_ps_site', search='_search_ps_in_site',
        help='In un ramo con "Pubblica sul sito" (questa categoria o una superiore).')
    ps_published_count = fields.Integer(
        'Prodotti sul sito', compute='_compute_ps_site',
        help='Prodotti già su PrestaShop in questa categoria e nelle sottocategorie.')
    ps_shop_active = fields.Boolean(related='ps_category_id.active', string='Attiva sul negozio')
    ps_publish = fields.Boolean(
        'Pubblica sul sito',
        help='Ramo da riportare sul sito con "Sincronizza categorie da Odoo": questa categoria e tutte '
             'le sue sottocategorie. Da spuntare sulle categorie principali (es. Pesca).',
    )


    def _ps_site_roots(self):
        return self.search([('ps_publish', '=', True)])

    def _compute_ps_site(self):
        roots = self._ps_site_roots()
        Tmpl = self.env['product.template']
        direct = {c.id: n for c, n in Tmpl._read_group(
            [('ps_product_id', '>', 0)], ['categ_id'], ['__count'])}
        all_categs = self.search([('id', 'child_of', self.ids)]) if self.ids else self.browse()
        for categ in self:
            path = categ.parent_path or ''
            categ.ps_in_site = any(path.startswith(r.parent_path) for r in roots if r.parent_path)
            categ.ps_published_count = sum(direct.get(d.id, 0) for d in all_categs
                                           if (d.parent_path or '').startswith(path)) if path else 0

    def _search_ps_in_site(self, operator, value):
        roots = self._ps_site_roots()
        inside = self.search([('id', 'child_of', roots.ids)]).ids if roots else []
        # Odoo 19 normalizza "= True" in "in [True]": gestire entrambe le forme.
        if operator in ('in', 'not in'):
            wanted = any(bool(v) for v in (value if isinstance(value, (list, tuple, set)) else [value]))
            positive = wanted if operator == 'in' else not wanted
        else:
            positive = bool(value) if operator in ('=', '==') else not bool(value)
        return [('id', 'in' if positive else 'not in', inside)]

    def action_ps_sync_categories(self):
        # Niente @api.model: il bottone nell'intestazione della lista passa gli id selezionati
        # (anche nessuno) e con @api.model la chiamata fallisce ("takes 1 positional argument").
        self = self.browse()
        """Albero categorie Odoo → PrestaShop, in coda (dalla lista "Categorie del sito")."""
        log_id = self.env['ke.prestashop.sync.log'].enqueue(
            'sync_categories', 'sync_category_tree', validate=True, skip_if_pending=True)
        if not log_id:
            raise UserError('Una sincronizzazione delle categorie è già in coda o in corso.')
        return {'type': 'ir.actions.act_window', 'res_model': 'ke.prestashop.sync.log', 'res_id': log_id,
                'view_mode': 'form', 'target': 'current'}


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
