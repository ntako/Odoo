import csv
import io
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.ke_inventory_import.models.enrich import do_enrich

HEADER = ['SAP', 'SAP Description', 'Master Description', 'Category', 'SubCategory', 'Product Type', 'Water Type', 'Color',
          'Gear Ratio', 'Bearing Count', 'Max Drag', 'Reel Handle Position', 'Advertised Diameter',
          'Break Strength', 'Advertised Length', 'EAN/UPC',
          'Each Dimension - Length', 'Each Dimension - Width', 'Each Dimension - Height',
          'Each Dimension - Unit of Dimension', 'Each Dimension - Gross Weight', 'Each Dimension - Unit of Weight',
          'UK\nCopy', 'UKBullets', 'IT\nCopy', 'IT\nBullets', 'DE\nCopy', 'DE\nBullets']


@tagged('post_install', '-at_install')
class TestSupplierCatalog(TransactionCase):
    """Arricchimento dal catalogo Pure Fishing col preset predefinito (2026-11-17)."""

    def setUp(self):
        super().setUp()
        for code in ('it_IT', 'de_DE', 'en_GB'):
            self.env['res.lang']._activate_lang(code)
        patcher = patch.object(self.env.cr, 'commit', lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        Preset = self.env['inventory.mapping.preset']
        Preset.search([('name', '=', 'Pure Fishing EMEA (arricchimento)')]).unlink()
        Preset._ke_create_purefishing_enrich_preset()   # con le lingue attivate qui sopra
        self.preset = Preset.search([('name', '=', 'Pure Fishing EMEA (arricchimento)')])
        self.reel = self.env['product.template'].create({'name': 'ZZF Mulinello', 'default_code': 'PFS-1636736+'})
        self.berkley = self.env['ke.product.brand'].create({'name': 'Berkley'})
        self.line = self.env['product.template'].create({'name': 'ZZF Treccia malata', 'barcode': '036282999998',
                                                        'ke_brand_id': self.berkley.id})
        self.rows = [
            {'SAP': '1636736', 'SAP Description': 'BEAST 200', 'Category': 'Reels',
             'SubCategory': 'Spinning Reels', 'Water Type': 'Freshwater', 'Color': 'Grey/Red',
             'Gear Ratio': '7.3:1', 'Bearing Count': '8', 'Max Drag': '25lb|11.3kg',
             'Reel Handle Position': 'Interchangeable', 'EAN/UPC': '036282038110',
             'Each Dimension - Length': ' 14.3 ', 'Each Dimension - Width': ' 16.4 ',
             'Each Dimension - Height': ' 9.3 ', 'Each Dimension - Unit of Dimension': 'CM',
             'Each Dimension - Gross Weight': ' 0.38 ', 'Each Dimension - Unit of Weight': 'KG',
             'UK\nCopy': 'No compromises.', 'UKBullets': '_8 bearings_Gear ratio 7.3:1',
             'IT\nCopy': 'Nessun compromesso.', 'IT\nBullets': '8 cuscinetti_Rapporto 7,3:1',
             'DE\nCopy': 'Keine Kompromisse.', 'DE\nBullets': '_8 Kugellager'},
            {'SAP': '9999999', 'Master Description': 'Sick Braid', 'Category': 'Line', 'SubCategory': 'Superline', 'Advertised Diameter': '0.16 mm',
             'Break Strength': '40lb|18.1kg', 'Advertised Length': '150m', 'EAN/UPC': '0036282999998'},
            {'SAP': '1111111', 'Category': 'Reels', 'EAN/UPC': '000000000017'},
        ]

    def _run(self, dry_run=False):
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=HEADER, delimiter=';')
        w.writeheader()
        w.writerows(self.rows)
        job = self.env['inventory.import.job'].new({'preset_id': self.preset.id})
        job._onchange_preset_id()
        self.assertTrue(job.enrich_only)
        job = self.env['inventory.import.job'].create({
            'enrich_only': True, 'code_prefix': job.code_prefix, 'match_ean': job.match_ean,
            'delimiter': ';', 'dry_run': dry_run,
            'mapping_line_ids': [(0, 0, {'target': l.target, 'csv_column': l.csv_column, 'sequence': l.sequence,
                                         'spec_key_id': l.spec_key_id.id, 'lang_code': l.lang_code})
                                 for l in job.mapping_line_ids]})
        options = dict(delimiter=';', enrich_only=True, dry_run=dry_run, code_prefix='PFS-', match_ean=True,
                       spec_text_lang='en_GB', weight_unit='kg', volume_unit='cm',
                       enrich_mapping=[{'column': l.csv_column or '', 'target': l.target, 'default': '',
                                        'spec': l.spec_key_id.code or '', 'lang': l.lang_code or ''}
                                       for l in job.mapping_line_ids.sorted('sequence')])
        do_enrich(job, self.env, out.getvalue(), options, self.env.cr)
        return job.error_log

    def _specs(self, tmpl, lang='it_IT'):
        return {s.key_id.code: s.value for s in tmpl.with_context(lang=lang).ke_spec_ids}

    def test_dry_run_writes_nothing(self):
        result = self._run(dry_run=True)
        self.assertIn('abbinate a prodotti Odoo: 2', result)
        self.assertIn('non in Odoo: 1', result)
        self.assertFalse(self.reel.ke_spec_ids)
        self.assertFalse(self.reel.ke_supplier_ref)

    def test_texts_specs_dims_and_matching(self):
        self._run()
        reel = self.reel.with_context(lang='it_IT')
        self.assertEqual(reel.ke_supplier_ref, '1636736')
        self.assertEqual(reel.ke_supplier_category, 'Reels / Spinning Reels')
        self.assertEqual(reel.ke_supplier_copy, 'Nessun compromesso.')
        self.assertEqual(reel.ke_supplier_bullets, '8 cuscinetti\nRapporto 7,3:1')
        self.assertEqual(self.reel.with_context(lang='de_DE').ke_supplier_copy, 'Keine Kompromisse.')
        self.assertEqual(self.reel.with_context(lang='en_US').ke_supplier_copy, 'No compromises.')
        specs = self._specs(self.reel)
        self.assertEqual(specs['gear_ratio'], '7,3:1')
        self.assertEqual(specs['max_drag'], '11,3 kg', 'la parte metrica di "25lb|11.3kg"')
        self.assertEqual(specs['hand'], 'Ambidestro')
        self.assertEqual(specs['technique'], 'Spinning')
        self.assertEqual(specs['water'], 'Acqua dolce')
        self.assertEqual(self._specs(self.reel, 'en_GB')['color'], 'Grey/Red')
        self.assertEqual(self._specs(self.reel, 'de_DE')['gear_ratio'], '7,3:1')
        line = self._specs(self.line)
        self.assertEqual((line['diameter'], line['breaking_strength'], line['length'], line['line_type']),
                         ('0,16 mm', '18,1 kg', '150 m', 'Trecciato'), 'abbinato per EAN (zeri iniziali)')
        if 'product_length' in self.reel._fields:
            self.assertEqual(self.reel.product_length, 14.3)
        self.assertAlmostEqual(self.reel.weight, 0.38)

    def test_priority_manual_over_supplier_over_amazon(self):
        Key = self.env['ke.product.spec.key']
        ratio = Key._ensure('gear_ratio', {'it_IT': 'Rapporto di recupero'})
        drag = Key._ensure('max_drag', {'it_IT': 'Frizione massima'})
        self.reel._ke_set_specs({ratio: '6,2:1', drag: '9 kg'}, 'amazon', 'it_IT')
        self.env['ke.product.spec'].search([('product_tmpl_id', '=', self.reel.id), ('key_id', '=', drag.id)]) \
            .with_context(lang='it_IT').write({'value': '12 kg', 'source': 'manual'})
        self.reel.weight = 1.5
        self._run()
        specs = self._specs(self.reel)
        self.assertEqual(specs['gear_ratio'], '7,3:1', 'il fornitore prevale su Amazon')
        self.assertEqual(specs['max_drag'], '12 kg', 'il valore manuale resta')
        self.reel._ke_set_specs({ratio: '6,2:1'}, 'amazon', 'it_IT')
        self.assertEqual(self._specs(self.reel)['gear_ratio'], '7,3:1', 'Amazon non sovrascrive il fornitore')
        self.assertEqual(self.reel.weight, 1.5, 'peso già presente: non cambia')

    def test_website_uses_supplier_text_of_the_language(self):
        self.reel.with_context(lang='it_IT').description_sale = '<p>Testo Amazon</p>'
        self._run()
        texts = self.reel.with_context(lang='it_IT')._ke_web_proposal()
        self.assertIn('Nessun compromesso.', texts['ke_web_description'])
        self.assertIn('<li>8 cuscinetti</li>', texts['ke_web_description'])
        self.assertNotIn('Amazon', texts['ke_web_description'])
        de = self.reel._ke_web_texts_lang('de_DE', 'it_IT')
        self.assertIn('Keine Kompromisse.', de.get('description', ''))

    def test_site_name_from_supplier_name(self):
        """Nome sul sito: marca + nome commerciale + tipo nella lingua (formato B, 2026-11-17)."""
        self._run()
        self.assertEqual(self.line.ke_supplier_name, 'Sick Braid')
        self.assertEqual(self.line.with_context(lang='it_IT')._ke_web_texts()['name'],
                         'Berkley Sick Braid – Filo trecciato')
        self.assertEqual(self.line._ke_web_texts_lang('de_DE', 'it_IT').get('name'),
                         'Berkley Sick Braid – Geflochtene Schnur')
        self.line.with_context(lang='it_IT').ke_web_name = 'Nome scelto a mano'
        self.assertEqual(self.line.with_context(lang='it_IT')._ke_web_texts()['name'], 'Nome scelto a mano')

    def test_family_common_name(self):
        from odoo.addons.ke_inventory_import.models.enrich import _common_name
        self.assertEqual(_common_name(['Flex Pike Deadbait', 'Flex Zander', 'Flex Zander']), 'Flex')
        self.assertEqual(_common_name(['Sick Braid', 'Sick Braid']), 'Sick Braid')
        self.assertEqual(_common_name(['Gulp! Crabby', 'Pulse Goby', 'Pulse Goby']), 'Pulse Goby')
