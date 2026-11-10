from odoo import models

from odoo.addons.ke_amazon_sync.models.amazon_category_map import category_key


class AmazonCategoryMapImport(models.TransientModel):
    _inherit = 'ke.amazon.category.map.import'

    def _import_extra_columns(self, rows, mapper):
        """
        Colonna "Categoria PrestaShop" (percorso come in PrestaShop → Categorie
        PrestaShop, es. "Pesca / Mulinelli da Pesca", oppure l'ID PS): sulle
        righe Amazon va sulla corrispondenza, sulle righe "Odoo produzione"
        sulla categoria Odoo di destinazione. Le categorie PS vanno lette prima
        ("Aggiorna categorie da PrestaShop").
        """
        text = super()._import_extra_columns(rows, mapper)
        header = [category_key(str(h or '')) for h in rows[0]]
        ps_col = next((i for i, h in enumerate(header) if 'prestashop' in h), None)
        if ps_col is None:
            return text
        origin_col, found_col, target_col = mapper._columns(rows[0])
        PsCategory = self.env['ke.prestashop.category'].sudo().with_context(active_test=False)
        ps_all = PsCategory.search([])
        by_path = {category_key(c.complete_name): c for c in ps_all}
        by_id = {str(c.ps_id): c for c in ps_all}
        done, missing = 0, []
        for row in rows[1:]:
            cell = lambda i: str(row[i] if i is not None and i < len(row) and row[i] is not None else '').strip()
            found, ps_text = cell(found_col), cell(ps_col)
            if not found or not ps_text:
                continue
            ps = by_path.get(category_key(ps_text)) or by_id.get(ps_text.split('.')[0])
            if not ps:
                missing.append(ps_text)
                continue
            if 'odoo' in category_key(cell(origin_col)):
                target = mapper.category_path(cell(target_col)) if cell(target_col) else None
                if target:
                    target.ps_category_id = ps
                    done += 1
            else:
                mapper.map_for(found).ps_category_id = ps
                done += 1
        text += f' Categorie PrestaShop impostate: {done}.'
        if missing:
            unique = sorted(set(missing))
            text += (f' Categorie PrestaShop non trovate ({len(unique)}, aggiornare le categorie da '
                     f'PrestaShop?): ' + ', '.join(unique[:10]) + ('…' if len(unique) > 10 else '') + '.')
        return text.strip()
