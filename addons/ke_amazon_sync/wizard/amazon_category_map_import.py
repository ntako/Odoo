import base64
import csv
import io

from odoo import fields, models
from odoo.exceptions import UserError

from ..services.category_map import CategoryMapper


class AmazonCategoryMapImport(models.TransientModel):
    """
    "Importa corrispondenze categorie" (2026-11-10): file con le colonne
    "Categoria trovata" e "Categoria Odoo" (percorso con " / "), facoltativa
    "Origine" ("Amazon" / "Odoo produzione"). Crea l'albero e le
    corrispondenze, sposta i prodotti delle vecchie categorie Odoo, poi
    assegna le categorie. Logica in services/category_map.py.
    """
    _name = 'ke.amazon.category.map.import'
    _description = 'Importa corrispondenze categorie'

    file_data = fields.Binary('File (xlsx o csv)', required=True)
    file_name = fields.Char('Nome file')
    default_path = fields.Char(
        'Categoria per i prodotti non mappati', default='Altro / Da classificare',
        help='Percorso della categoria predefinita (creata se manca), salvata nelle Impostazioni.')
    delete_empty = fields.Boolean(
        'Elimina le vecchie categorie rimaste vuote', default=True,
        help='Solo le vecchie categorie del file e quelle create dal vecchio Full Import, se non hanno '
             'più prodotti né sottocategorie. Mai le categorie di destinazione.')
    result = fields.Text('Risultato', readonly=True)

    def _rows(self):
        raw = base64.b64decode(self.file_data)
        if (self.file_name or '').lower().endswith('.csv'):
            text = raw.decode('utf-8-sig')
            dialect = csv.Sniffer().sniff(text[:2000], delimiters=',;\t')
            return list(csv.reader(io.StringIO(text), dialect))
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        return [[c if c is not None else '' for c in row] for row in wb.worksheets[0].iter_rows(values_only=True)]

    def action_import(self):
        self.ensure_one()
        rows = self._rows()
        if len(rows) < 2:
            raise UserError('Il file è vuoto.')
        mapper = CategoryMapper(self.env)
        stats = mapper.import_rows(rows, self.default_path)
        assigned = mapper.assign()
        deleted = mapper.delete_unused(stats['old_categories']) if self.delete_empty else 0
        self.result = (
            f'Categorie Odoo create: {stats["categories_created"]}. '
            f'Corrispondenze Amazon: {stats["maps"]} ({stats["unmapped"]} senza categoria, vanno nella predefinita). '
            f'Prodotti spostati dalle vecchie categorie Odoo: {stats["moved"]}. '
            f'Prodotti assegnati dalle corrispondenze: {assigned}. '
            f'Vecchie categorie eliminate: {deleted}.')
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
            'view_mode': 'form', 'target': 'new',
        }
