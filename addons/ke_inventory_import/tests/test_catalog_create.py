"""Import del catalogo fornitore integrato: crea, riattiva, arricchisce (2026-11-20)."""
import csv
import io
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.ke_inventory_import.models.csv_file import file_text
from odoo.addons.ke_inventory_import.models.enrich import do_enrich


@tagged('post_install', '-at_install')
class TestCatalogCreate(TransactionCase):

    def setUp(self):
        super().setUp()
        patcher = patch.object(self.env.cr, 'commit', lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.Tmpl = self.env['product.template']
        self.Variant = self.env['product.product'].with_context(active_test=False)

    def _run(self, rows, mapping, dry_run=False, **opts):
        header = list(dict.fromkeys(k for r in rows for k in r))
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=header, delimiter=';')
        writer.writeheader()
        writer.writerows(rows)
        job = self.env['inventory.import.job'].create({'enrich_only': True, 'delimiter': ';', 'dry_run': dry_run})
        options = dict(delimiter=';', enrich_only=True, dry_run=dry_run, match_ean=True, spec_text_lang='en_GB',
                       weight_unit='kg', volume_unit='cm', create_missing=True, **opts,
                       enrich_mapping=[{'column': c, 'target': t, 'default': '', 'spec': '', 'lang': ''}
                                       for c, t in mapping])
        options.setdefault('supplier_tag', options.get('code_prefix'))
        do_enrich(job, self.env, out.getvalue(), options, self.env.cr)
        return job

    def _by_code(self, code):
        return self.Variant.search([('default_code', '=', code)])

    def test_purefishing_like_create_add_and_revive(self):
        existing = self.Tmpl.create({'name': 'ZZK Whiplash 0,10', 'default_code': 'PFS-9000001'})
        existing.product_variant_id.write({'ke_supplier_family_ref': 'ZZKF', 'ke_supplier_family_name': 'Whiplash'})
        archived = self.Tmpl.create({'name': 'ZZK vecchio di marzo', 'default_code': '9000004',
                                     'barcode': '8000000000045'})
        archived.action_archive()
        mapping = [('SAP', 'default_code'), ('EAN', 'barcode'), ('Master code', '_supplier_family_ref'),
                   ('Master Description', '_supplier_family_name'), ('Desc', 'name'), ('Price', 'standard_price'),
                   ('Brand', '_brand')]
        rows = [
            {'SAP': '9000001', 'EAN': '', 'Master code': 'ZZKF', 'Master Description': 'Whiplash', 'Desc': 'WHIP 0.10',
             'Price': '10,50', 'Brand': 'Berkley'},
            {'SAP': '9000002', 'EAN': '8000000000021', 'Master code': 'ZZKF', 'Master Description': 'Whiplash',
             'Desc': 'WHIP 0.12', 'Price': '11.00', 'Brand': 'Berkley'},
            {'SAP': '9000003', 'EAN': '8000000000038', 'Master code': 'ZZKG', 'Master Description': 'Beast Reel',
             'Desc': 'BEAST 200', 'Price': '99', 'Brand': 'Berkley'},
            {'SAP': '9000004', 'EAN': '8000000000045', 'Master code': 'ZZKH', 'Master Description': 'Toby',
             'Desc': 'TOBY 7G', 'Price': '3', 'Brand': 'Berkley'},
        ]
        check = self._run(rows, mapping, dry_run=True, code_prefix='PFS-')
        self.assertIn('1 prodotti archiviati da riattivare; 2 articoli da creare in 2 famiglie', check.error_log)
        self.assertFalse(self._by_code('PFS-9000002'), 'solo verifica')

        job = self._run(rows, mapping, code_prefix='PFS-')
        new = self._by_code('PFS-9000002')
        self.assertEqual(new.product_tmpl_id, existing, 'aggiunta alla famiglia che c\'era già')
        self.assertEqual(len(existing.product_variant_ids), 2)
        self.assertEqual((new.barcode, new.ke_supplier_code, new.ke_supplier_tag), ('8000000000021', '9000002', 'PFS-'))
        beast = self._by_code('PFS-9000003')
        self.assertEqual(len(beast.product_tmpl_id.product_variant_ids), 1, 'famiglia di una riga: prodotto semplice')
        self.assertEqual(beast.product_tmpl_id.ke_brand_id.name, 'Berkley')
        self.assertTrue(archived.active, 'archiviato riattivato, non duplicato')
        self.assertEqual(archived.default_code, 'PFS-9000004')
        self.assertEqual(self.Variant.search_count([('barcode', '=', '8000000000045')]), 1)
        self.assertEqual(existing.product_variant_ids.filtered(lambda v: v.default_code == 'PFS-9000001').standard_price,
                         10.5, 'costo = prezzo di listino')
        self.assertIn('CREAZIONE: 1 prodotti archiviati riattivati; 2 articoli creati', job.error_log)
        # Di nuovo: niente da creare, abbinamento dal codice fornitore.
        again = self._run(rows, mapping, code_prefix='PFS-')
        self.assertIn('0 articoli creati', again.error_log)

    def test_new_item_joins_existing_product_without_family_code(self):
        # Prodotto da Amazon: nessun codice famiglia del fornitore finché l'arricchimento non lo scrive.
        existing = self.Tmpl.create({'name': 'ZZK Sick Braid 0,06', 'default_code': 'PFS-9100001'})
        mapping = [('SAP', 'default_code'), ('Master code', '_supplier_family_ref'),
                   ('Master Description', '_supplier_family_name'), ('Desc', 'name')]
        rows = [
            {'SAP': '9100001', 'Master code': 'ZZKS', 'Master Description': 'Sick Braid', 'Desc': 'SICK 0.06'},
            {'SAP': '9100002', 'Master code': 'ZZKS', 'Master Description': 'Sick Braid', 'Desc': 'SICK 0.08'},
        ]
        job = self._run(rows, mapping, code_prefix='PFS-')
        new = self._by_code('PFS-9100002')
        self.assertEqual(new.product_tmpl_id, existing, 'nella famiglia del prodotto già abbinato, non in una nuova')
        self.assertIn('1 aggiunte a famiglie esistenti', job.error_log)

    def test_tubertini_like_suffix_and_family_from_name(self):
        old = self.Tmpl.create({'name': 'ZZK Marginal 1,50', 'default_code': 'TUB-01007'})
        mapping = [('Codice', 'default_code'), ('Descrizione', 'name'), ('Barcode EAN13', 'barcode'),
                   ('Prezzo Listino', 'standard_price')]
        rows = [
            {'Codice': '01007XX', 'Descrizione': 'ZZK Marginal Carp X 1,50 Mt.', 'Barcode EAN13': '', 'Prezzo Listino': '12.1'},
            {'Codice': '01008XX', 'Descrizione': 'ZZK Marginal Carp X 1,80 Mt.', 'Barcode EAN13': '8000000000052',
             'Prezzo Listino': '14.21'},
            {'Codice': '01009XX', 'Descrizione': 'ZZK Marginal Carp X 2,10 Mt.', 'Barcode EAN13': '8000000000069',
             'Prezzo Listino': '15.98'},
        ]
        self._run(rows, mapping, code_prefix='TUB-', code_suffix='XX', brand_name='Tubertini', family_from_name=True)
        v8, v9 = self._by_code('TUB-01008'), self._by_code('TUB-01009')
        self.assertEqual(old.product_variant_id.standard_price, 12.1, '"01007XX" = TUB-01007')
        self.assertEqual(v8.product_tmpl_id, v9.product_tmpl_id, 'stessa famiglia dal nome')
        self.assertEqual(v8.product_tmpl_id.ke_supplier_name, 'ZZK Marginal Carp X')
        self.assertEqual(sorted(v.product_template_attribute_value_ids.name for v in (v8 | v9)), ['1,80 Mt.', '2,10 Mt.'])
        self.assertEqual(v8.ke_supplier_code, '01008XX')

    def test_c4_like_header_rows_and_brand_first_word(self):
        self.env['ke.product.brand'].create({'name': 'ZZKC4'})
        mapping = [('CODICE', 'default_code'), ('DESCRIZIONE', 'name'), ('MODELLO', '_supplier_family_name'),
                   ('LINEA', '_brand')]
        rows = [
            {'LINEA': 'ZZKC4 FREEDIVING', 'MODELLO': 'MONOPINNA PRO-TECH', 'CODICE': '', 'DESCRIZIONE': ''},
            {'LINEA': 'ZZKC4 FREEDIVING', 'MODELLO': '', 'CODICE': 'ZZK0B136', 'DESCRIZIONE': 'Monopinna PRO TECH - 36/37'},
            {'LINEA': 'ZZKC4 FREEDIVING', 'MODELLO': '', 'CODICE': 'ZZK0B138', 'DESCRIZIONE': 'Monopinna PRO TECH - 38/39'},
            {'LINEA': 'ZZKC4 FREEDIVING', 'MODELLO': 'MUTA CAMO', 'CODICE': '', 'DESCRIZIONE': ''},
            {'LINEA': 'ZZKC4 FREEDIVING', 'MODELLO': '', 'CODICE': 'ZZKMC2', 'DESCRIZIONE': 'Muta camo taglia 2'},
        ]
        self._run(rows, mapping, code_prefix='C4C-')
        a, b, muta = self._by_code('C4C-ZZK0B136'), self._by_code('C4C-ZZK0B138'), self._by_code('C4C-ZZKMC2')
        self.assertEqual(a.product_tmpl_id, b.product_tmpl_id)
        self.assertNotEqual(a.product_tmpl_id, muta.product_tmpl_id, 'nuova intestazione, nuova famiglia')
        self.assertEqual(a.product_tmpl_id.ke_brand_id.name, 'ZZKC4', 'marca dalla prima parola della linea')
        self.assertEqual(sorted(v.product_template_attribute_value_ids.name for v in (a | b)), ['36/37', '38/39'])

    def test_excel_file_is_converted(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['', 'https://catalogo', '', 'User'])
        ws.append(['Codice', 'Descrizione', 'Barcode EAN13', 'Prezzo'])
        ws.append(['01007XX', 'Marginal 1,50', 8053831480369.0, 12.1])
        buf = io.BytesIO()
        wb.save(buf)
        rows = list(csv.reader(io.StringIO(file_text(buf.getvalue(), ';')), delimiter=';'))
        self.assertEqual(rows[0], ['Codice', 'Descrizione', 'Barcode EAN13', 'Prezzo'])
        self.assertEqual(rows[1], ['01007XX', 'Marginal 1,50', '8053831480369', '12.1'])

    def test_excel_numbered_row_above_header(self):
        """File Pure Fishing EMEA: riga di numeri di colonna e riga di indirizzi sopra l'intestazione."""
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([1, 2, 3, 4, 5])
        ws.append([None, None, 5800, 'https://media.purefishing.com/s/', 'Bait Attributes'])
        ws.append(['SAP', 'SAP Description', 'Brand', 'Master code', 'Master Description'])
        ws.append([1636736, 'BST200LP ABU BEAST 200 LP', 'Abu Garcia', 1641516, 'Beast 200'])
        buf = io.BytesIO()
        wb.save(buf)
        rows = list(csv.reader(io.StringIO(file_text(buf.getvalue(), ';')), delimiter=';'))
        self.assertEqual(rows[0][:3], ['SAP', 'SAP Description', 'Brand'])
        self.assertEqual(rows[1][0], '1636736')
