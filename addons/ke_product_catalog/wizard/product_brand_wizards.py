import base64
import csv
import io

from odoo import fields, models
from odoo.exceptions import UserError

from ..models.product_brand import brand_key


class ProductBrandMerge(models.TransientModel):
    _name = 'ke.product.brand.merge'
    _description = 'Unisci marche'

    brand_ids = fields.Many2many('ke.product.brand', string='Marche da unire')
    target_id = fields.Many2one('ke.product.brand', string='Unisci in', required=True)

    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        ids = self.env.context.get('active_ids') or []
        if self.env.context.get('active_model') == 'ke.product.brand' and ids:
            vals['brand_ids'] = [(6, 0, ids)]
            brands = self.env['ke.product.brand'].browse(ids)
            confirmed = brands.filtered(lambda b: not b.to_review)
            vals['target_id'] = (confirmed or brands)[:1].id
        return vals

    def action_merge(self):
        (self.brand_ids | self.target_id)._merge_into(self.target_id)
        return {
            'type': 'ir.actions.act_window', 'res_model': 'ke.product.brand',
            'res_id': self.target_id.id, 'view_mode': 'form',
        }


class ProductBrandAliasImport(models.TransientModel):
    """
    Importa la tabella delle corrispondenze "nome trovato → marca" (es. foglio
    "Marche" di fornitori_e_marche.xlsx): prima colonna il nome com'è (Amazon,
    fornitore…), colonna "Marca corretta" (o la seconda colonna di testo) la
    marca. Marca vuota = "non è una marca". Crea le marche mancanti
    (confermate), i nomi alternativi, e unisce le marche "Da verificare"
    create in precedenza con quei nomi.
    """
    _name = 'ke.product.brand.alias.import'
    _description = 'Importa corrispondenze marche'

    file_data = fields.Binary('File (xlsx o csv)', required=True)
    file_name = fields.Char('Nome file')
    sheet_name = fields.Char('Foglio (xlsx)', default='Marche')
    result = fields.Text('Risultato', readonly=True)

    def _rows(self):
        raw = base64.b64decode(self.file_data)
        if (self.file_name or '').lower().endswith('.csv'):
            text = raw.decode('utf-8-sig')
            dialect = csv.Sniffer().sniff(text[:2000], delimiters=',;\t')
            return list(csv.reader(io.StringIO(text), dialect))
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb[self.sheet_name] if self.sheet_name and self.sheet_name in wb.sheetnames else wb.worksheets[0]
        return [[c if c is not None else '' for c in row] for row in ws.iter_rows(values_only=True)]

    def action_import(self):
        rows = self._rows()
        if not rows:
            raise UserError('Il file è vuoto.')
        header = [str(h or '').strip().lower() for h in rows[0]]
        target_col = next((i for i, h in enumerate(header) if 'corrett' in h or h == 'marca'), None)
        if target_col is None:
            target_col = 1
        Brand = self.env['ke.product.brand'].with_context(active_test=False)
        Alias = self.env['ke.product.brand.alias']
        created = aliases = merged = ignored = 0
        for row in rows[1:]:
            found = str(row[0] or '').strip() if row else ''
            if not found or found.lower() in ('totale', '(vuota)'):
                continue
            wanted = str(row[target_col] or '').strip() if len(row) > target_col else ''
            if not wanted:
                alias = Alias.search([('key', '=', brand_key(found))], limit=1)
                if not alias:
                    Alias.create({'name': found, 'brand_id': False})
                    ignored += 1
                elif alias.brand_id:
                    # Reimport con la scelta cambiata: ora "non è una marca".
                    Brand._reassign_products_for_name(found, alias.brand_id, Brand.browse())
                    alias.brand_id = False
                    ignored += 1
                stray = Brand.search([('key', '=', brand_key(found)), ('to_review', '=', True)])
                stray.filtered(lambda b: not b.product_count).unlink()
                continue
            target, _ignored = Brand._match(wanted)
            if not target:
                target = Brand.create({'name': wanted})
                created += 1
            elif target.to_review and brand_key(target.name) == brand_key(wanted):
                target.write({'to_review': False, 'name': wanted})
            if brand_key(found) == target.key:
                continue
            stray = Brand.search([('key', '=', brand_key(found)), ('id', '!=', target.id)])
            alias = Alias.search([('key', '=', brand_key(found))], limit=1)
            if stray:
                (stray | target)._merge_into(target)
                merged += len(stray)
            elif not alias:
                Alias.create({'name': found, 'brand_id': target.id})
                aliases += 1
            elif alias.brand_id != target:
                # Reimport con la scelta cambiata: il nome passa alla nuova marca,
                # e con lui i prodotti che avevano quella marca per quel nome.
                if alias.brand_id:
                    Brand._reassign_products_for_name(found, alias.brand_id, target)
                alias.brand_id = target
                aliases += 1
        self.result = (f'Marche create: {created}. Nomi alternativi aggiunti: {aliases}. '
                       f'Marche unite: {merged}. Nomi segnati "non è una marca": {ignored}.')
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
            'view_mode': 'form', 'target': 'new',
        }
