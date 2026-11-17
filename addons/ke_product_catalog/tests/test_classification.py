import base64
import csv
import io

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProductClassification(TransactionCase):
    """Classificazione portabile (2026-11-17): categoria per codice/EAN e percorso."""

    def setUp(self):
        super().setUp()
        Categ = self.env['product.category']
        self.pesca = Categ.create({'name': 'ZZC Pesca'})
        self.mulinelli = Categ.create({'name': 'Mulinelli', 'parent_id': self.pesca.id})
        self.p1 = self.env['product.template'].create({
            'name': 'ZZC Mulinello', 'default_code': 'ZZC-1', 'categ_id': self.mulinelli.id})
        self.p2 = self.env['product.template'].create({
            'name': 'ZZC Esca', 'barcode': '8000000000017', 'categ_id': self.pesca.id})

    def _import(self, rows, dry_run=False):
        out = io.StringIO()
        w = csv.writer(out, delimiter=';')
        w.writerow(['Codice', 'EAN', 'Prodotto', 'Categoria'])
        w.writerows(rows)
        wiz = self.env['ke.product.classification.import'].create({
            'file_data': base64.b64encode(out.getvalue().encode('utf-8-sig')),
            'file_name': 't.csv', 'dry_run': dry_run})
        wiz.action_import()
        return wiz.result

    def test_export_rows(self):
        wiz = self.env['ke.product.classification.export'].with_context(
            active_model='product.template', active_ids=(self.p1 | self.p2).ids).create({})
        self.assertEqual(wiz.scope, 'selected')
        wiz.action_export()
        rows = list(csv.reader(io.StringIO(base64.b64decode(wiz.file_data).decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(rows[0], ['Codice', 'EAN', 'Prodotto', 'Categoria'])
        self.assertIn(['ZZC-1', '', 'ZZC Mulinello', 'ZZC Pesca / Mulinelli'], rows)
        self.assertIn(['', '8000000000017', 'ZZC Esca', 'ZZC Pesca'], rows)
        self.assertEqual(wiz.exported_count, 2)

    def test_dry_run_writes_nothing(self):
        result = self._import([['ZZC-1', '', '', 'ZZC Pesca / Mulinelli / Spinning']], dry_run=True)
        self.assertEqual(self.p1.categ_id, self.mulinelli)
        self.assertFalse(self.env['product.category'].search([('name', '=', 'Spinning'), ('parent_id', '=', self.mulinelli.id)]))
        self.assertIn('Prodotti da spostare: 1', result)
        self.assertIn('Categorie da creare: 1', result)

    def test_import_by_code_with_plus_and_by_ean_creates_categories(self):
        result = self._import([
            ['zzc-1+', '', '', 'ZZC Pesca / Mulinelli / Spinning'],
            ['', '8000000000017', '', 'ZZC Pesca/Esche artificiali'],
            ['NON-ESISTE', '', '', 'ZZC Pesca'],
        ])
        self.assertEqual(self.p1.categ_id.complete_name, 'ZZC Pesca / Mulinelli / Spinning')
        self.assertEqual(self.p1.categ_id.parent_id, self.mulinelli, 'sotto la categoria esistente, non un doppione')
        self.assertEqual(self.p2.categ_id.complete_name, 'ZZC Pesca / Esche artificiali')
        self.assertIn('Codici non trovati: 1', result)
        self.assertIn('NON-ESISTE', result)

    def test_same_category_and_ambiguous_code(self):
        other = self.env['product.template'].create({'name': 'ZZC doppio', 'default_code': 'ZZC-1+'})
        result = self._import([['ZZC-1', '', '', 'ZZC Pesca / Mulinelli']])
        self.assertIn('Già nella categoria giusta: 1', result, 'a parità di stato vince il codice identico')
        result = self._import([['ZZC-9', '', '', 'ZZC Pesca']])
        self.assertIn('Codici non trovati: 1', result)
        other.default_code = 'ZZC-1'
        result = self._import([['ZZC-1', '', '', 'ZZC Pesca / Mulinelli']])
        self.assertIn('Codici ambigui (più prodotti): 1', result)
        other.active = False
        self._import([['ZZC-1', '', '', 'ZZC Pesca']])
        self.assertEqual(self.p1.categ_id, self.pesca, 'vince il prodotto attivo, non l\'archiviato')
        self.p2.default_code = 'ZZC-2'
        result = self._import([['ZZC-2', '', '', ' zzc pesca ']])
        self.assertIn('Già nella categoria giusta: 1', result)
