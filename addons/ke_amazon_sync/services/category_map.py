"""
Categorie Odoo dei prodotti Amazon (2026-11-10, deciso con l'utente): dalla
categoria Amazon del prodotto (x_amazon_category, in qualunque lingua) alla
categoria Odoo tramite ke.amazon.category.map, invece di creare una categoria
Odoo per ogni nome Amazon come faceva il Full Import (440 categorie su
odoo_2, la stessa in italiano, spagnolo e inglese).

- category_for(nome): categoria di un prodotto nuovo (Full Import); un nome
  mai visto crea solo la riga di corrispondenza, vuota.
- assign(): sposta i prodotti nella categoria della loro corrispondenza; di
  default solo quelli ancora nella categoria predefinita, in "Tutti" o in una
  categoria creata dal vecchio import (stesso nome di una categoria Amazon),
  mai da una categoria scelta come destinazione (scelta a mano).
- import_rows(): file "Categoria trovata" → "Categoria Odoo" (percorso con
  " / "): crea l'albero, le corrispondenze, e per le righe con origine
  "Odoo produzione" sposta i prodotti delle vecchie categorie Odoo.
"""
from collections import defaultdict

from odoo.addons.ke_amazon_sync.models.amazon_category_map import category_key

DEFAULT_PARAM = 'ke_amazon_sync.default_categ_id'


class CategoryMapper:

    def __init__(self, env):
        self.env = env
        self.Map = env['ke.amazon.category.map'].sudo()
        self.Category = env['product.category'].sudo()
        # mail_notrack: senza, ogni prodotto spostato scrive "Categoria: A → B"
        # nella sua cronologia (25.070 messaggi e ~160 s su odoo_2).
        self.Template = env['product.template'].sudo().with_context(active_test=False, mail_notrack=True)
        self._default = None

    # ------------------------------------------------------------------
    # Categoria predefinita e corrispondenze
    # ------------------------------------------------------------------

    def default_category(self):
        """Categoria dei prodotti senza corrispondenza: quella delle Impostazioni, altrimenti "Tutti"."""
        if self._default is None:
            raw = self.env['ir.config_parameter'].sudo().get_param(DEFAULT_PARAM)
            cat = self.Category.browse(int(raw)).exists() if raw and str(raw).isdigit() else self.Category
            if not cat:
                cat = self.env.ref('product.product_category_all', raise_if_not_found=False) or \
                    self.Category.search([], limit=1, order='id')
            self._default = cat
        return self._default

    def map_for(self, amazon_name, create=True):
        key = category_key(amazon_name)
        if not key:
            return self.Map
        rec = self.Map.search([('key', '=', key)], limit=1)
        if not rec and create:
            rec = self.Map.create({'name': amazon_name.strip()})
        return rec

    def category_for(self, amazon_name):
        """Categoria Odoo per un prodotto nuovo con questa categoria Amazon. Non crea categorie."""
        if not amazon_name:
            return self.default_category()
        return self.map_for(amazon_name).categ_id or self.default_category()

    def category_path(self, path):
        """Categoria dal percorso "Pesca / Esche artificiali / Jig", creando i livelli mancanti."""
        parent = self.Category
        for name in [p.strip() for p in (path or '').split('/') if p.strip()]:
            cat = self.Category.search([('name', '=', name), ('parent_id', '=', parent.id or False)], limit=1)
            if not cat:
                cat = self.Category.create({'name': name, 'parent_id': parent.id or False})
            parent = cat
        return parent

    # ------------------------------------------------------------------
    # Assegnazione
    # ------------------------------------------------------------------

    def assign(self, force=False):
        """Categoria Odoo dalla corrispondenza della categoria Amazon. Ritorna i prodotti spostati."""
        default = self.default_category()
        all_categ = self.env.ref('product.product_category_all', raise_if_not_found=False)
        maps = {m.key: m for m in self.Map.search([])}
        targets = {m.categ_id.id for m in maps.values() if m.categ_id}
        moves = defaultdict(list)
        for tmpl in self.Template.search([('x_amazon_category', '!=', False)]):
            key = category_key(tmpl.x_amazon_category)
            if not key:
                continue
            rec = maps.get(key)
            if not rec:
                rec = maps[key] = self.Map.create({'name': tmpl.x_amazon_category.strip()})
            target = rec.categ_id or default
            current = tmpl.categ_id
            if not target or current == target:
                continue
            if not force and not self._replaceable(current, default, all_categ, targets, maps, target):
                continue
            moves[target.id].append(tmpl.id)
        # Senza categoria Amazon salvata (famiglie del vecchio import: la
        # categoria Odoo era il nome Amazon, x_amazon_category vuoto) vale il
        # nome della vecchia categoria (su odoo_2 ~200 prodotti, 2026-11-10).
        legacy = self.Category.search([('id', 'not in', list(targets | {default.id}))]).filtered(
            lambda c: category_key(c.name) in maps)
        for categ in legacy:
            target = maps[category_key(categ.name)].categ_id or default
            if target and target != categ:
                moves[target.id] += self.Template.search(
                    [('categ_id', '=', categ.id), ('x_amazon_category', 'in', [False, ''])]).ids
        for categ_id, ids in moves.items():
            self.Template.browse(ids).write({'categ_id': categ_id})
        return sum(len(ids) for ids in moves.values())

    @staticmethod
    def _replaceable(current, default, all_categ, targets, maps, target=None):
        if not current or current == default or current == all_categ:
            return True
        # Affinamento (2026-11-11): da una categoria padre alla sottocategoria
        # della corrispondenza, es. "Pesca / Esche artificiali" → "… / Siliconiche"
        # (67 prodotti originali con giacenza su odoo_prod). Mai verso l'alto né
        # verso una categoria di un altro ramo.
        if target and target.parent_path and current.parent_path \
                and target.parent_path.startswith(current.parent_path):
            return True
        return current.id not in targets and category_key(current.name) in maps

    # ------------------------------------------------------------------
    # Import del file di corrispondenze
    # ------------------------------------------------------------------

    @staticmethod
    def _columns(header):
        header = [category_key(str(h or '')) for h in header]
        origin = next((i for i, h in enumerate(header) if 'origine' in h), None)
        target = next((i for i, h in enumerate(header) if 'odoo' in h), None)
        found = next((i for i, h in enumerate(header) if i not in (origin, target)
                      and ('trovat' in h or 'amazon' in h)), None)
        if found is None:
            found = next(i for i in range(len(header)) if i not in (origin, target))
        if target is None:
            target = len(header) - 1
        return origin, found, target

    def import_rows(self, rows, default_path=None):
        """
        Righe di un file con intestazione. Ritorna i conteggi e gli id delle
        vecchie categorie Odoo svuotate (per delete_unused).
        """
        if default_path:
            default = self.category_path(default_path)
            self.env['ir.config_parameter'].sudo().set_param(DEFAULT_PARAM, str(default.id))
            self._default = default
        categories_before = self.Category.search_count([])
        origin_col, found_col, target_col = self._columns(rows[0])
        stats = {'maps': 0, 'unmapped': 0, 'moved': 0, 'old_categories': []}
        targets = set()
        old_rows = []
        for row in rows[1:]:
            cell = lambda i: str(row[i] if i is not None and i < len(row) and row[i] is not None else '').strip()
            found, target_path = cell(found_col), cell(target_col)
            if not found:
                continue
            target = self.category_path(target_path) if target_path else self.Category
            if target:
                targets.add(target.id)
            if 'odoo' in category_key(cell(origin_col)):
                old_rows.append((found, target))
                continue
            rec = self.map_for(found)
            rec.categ_id = target.id or False
            stats['maps'] += 1
            stats['unmapped'] += not target
        for found, target in old_rows:
            if not target:
                continue
            old = self.Category.search([('complete_name', '=', found)])
            for categ in old.filtered(lambda c: c != target and c.id not in targets):
                products = self.Template.search([('categ_id', '=', categ.id)])
                products.write({'categ_id': target.id})
                stats['moved'] += len(products)
                stats['old_categories'].append(categ.id)
        stats['categories_created'] = self.Category.search_count([]) - categories_before
        return stats

    def delete_unused(self, extra_ids=()):
        """
        Elimina le categorie rimaste vuote fra le vecchie categorie Odoo del
        file e quelle create dal vecchio import (stesso nome di una categoria
        Amazon); mai una destinazione, la predefinita o una categoria di Odoo
        stesso (con identificativo esterno).
        """
        maps = self.Map.search([])
        keep = set(maps.categ_id.ids) | {self.default_category().id}
        keep |= set(self.env['ir.model.data'].sudo().search(
            [('model', '=', 'product.category')]).mapped('res_id'))
        keys = set(maps.mapped('key'))
        candidates = self.Category.browse(list(extra_ids)) | self.Category.search([]).filtered(
            lambda c: category_key(c.name) in keys)
        used = {c.id for c, in self.Template._read_group([('categ_id', 'in', candidates.ids)], ['categ_id'])}
        empty = candidates.filtered(lambda c: c.id not in keep and c.id not in used and not c.child_id)
        if not empty:
            return 0
        try:
            with self.env.cr.savepoint():
                count = len(empty)
                empty.unlink()
            return count
        except Exception:
            pass
        # Una di loro non si può eliminare (es. usata altrove): una per volta.
        deleted = 0
        for categ in empty:
            try:
                with self.env.cr.savepoint():
                    categ.unlink()
                deleted += 1
            except Exception:
                continue
        return deleted
