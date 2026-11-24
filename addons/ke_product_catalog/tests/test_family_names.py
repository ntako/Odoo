"""Nomi di famiglie e varianti dal catalogo del fornitore (2026-11-20)."""
from odoo.tests import TransactionCase, tagged

from odoo.addons.ke_product_catalog.spec_rules import SPEC_KEYS


@tagged('post_install', '-at_install')
class TestFamilyNames(TransactionCase):

    def setUp(self):
        super().setUp()
        for code in ('it_IT', 'es_ES'):
            self.env['res.lang']._activate_lang(code)
        Key = self.env['ke.product.spec.key']
        self.keys = {c: Key._ensure(c, SPEC_KEYS[c][1], SPEC_KEYS[c][0])
                     for c in ('diameter', 'length', 'breaking_strength', 'color')}
        brand = self.env['ke.product.brand'].create({'name': 'Berkley'})
        attr = self.env['product.attribute'].create({
            'name': 'ZZF Colore Amazon', 'value_ids': [(0, 0, {'name': n}) for n in (
                '0.25mm | 11Lb | 4,8Kg | 500m – Verde muschio', '0.30mm | 15Lb | 6,8Kg | 400m – Verde muschio',
                '0.35mm | 19Lb | 8,7Kg | 300m')]})
        self.tmpl = self.env['product.template'].create({
            'name': 'Berkley Flex, Monofilamento Flessibile', 'ke_brand_id': brand.id,
            'ke_supplier_category': 'Line / Monofilament',
            'attribute_line_ids': [(0, 0, {'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)]})]})
        self.variants = self.tmpl.product_variant_ids
        self.variants.write({'ke_supplier_family_ref': '1578613', 'ke_supplier_family_name': 'Flex Carp'})
        for variant, (dia, length, load) in zip(self.variants, (
                ('0,25 mm', '500 m', '4,8 kg'), ('0,30 mm', '400 m', '6,8 kg'), ('0,35 mm', '300 m', '8,7 kg'))):
            for lang, d in (('it_IT', dia), ('es_ES', dia), ('en_US', dia.replace(',', '.'))):   # come l'arricchimento: en_US per ultima
                self.tmpl._ke_set_specs({self.keys['diameter']: d, self.keys['length']: length,
                                         self.keys['breaking_strength']: load}, 'supplier', lang, variant=variant)
        # Colore uguale per tutte (per il prodotto): non distingue, non entra nel nome.
        self.tmpl._ke_set_specs({self.keys['color']: 'Moss Green'}, 'supplier', 'en_US')

    def _labels(self, lang):
        return [v.with_context(lang=lang).product_template_attribute_value_ids.name for v in self.variants]

    def test_dry_run_changes_nothing(self):
        lines = self.tmpl.ke_apply_supplier_names(dry_run=True)
        self.assertEqual(len(lines), 1)
        self.assertIn('0,30 mm', lines[0])
        self.assertEqual(self.tmpl.with_context(lang='it_IT').name, 'Berkley Flex, Monofilamento Flessibile')
        self.assertIn('Verde muschio', self._labels('en_US')[0])

    def test_family_and_variant_names(self):
        self.tmpl.ke_apply_supplier_names()
        self.assertEqual(self.tmpl.ke_supplier_name, 'Flex Carp')
        self.assertEqual(self.tmpl.with_context(lang='it_IT').name, 'Berkley Flex Carp – Filo monofilo')
        self.assertEqual(self.tmpl.with_context(lang='es_ES').name, 'Berkley Flex Carp – Hilo monofilamento')
        self.assertEqual(self.tmpl.product_variant_ids, self.variants, 'stesse varianti')
        self.assertEqual(self._labels('it_IT'), ['0,25 mm', '0,30 mm', '0,35 mm'], 'basta il diametro')
        self.assertEqual(self._labels('en_US'), ['0.25 mm', '0.30 mm', '0.35 mm'])
        line = self.tmpl.attribute_line_ids
        self.assertEqual(len(line), 1, 'la riga dell\'attributo Amazon è archiviata')
        self.assertEqual(line.attribute_id.with_context(lang='it_IT').name, 'Diametro')
        self.tmpl._create_variant_ids()
        self.assertEqual(self.tmpl.with_context(active_test=False).product_variant_ids, self.variants,
                         'nessuna variante nuova o archiviata')
        self.assertFalse(self.tmpl.ke_apply_supplier_names(), 'di nuovo: niente da cambiare')

    def test_identical_variants_get_their_code(self):
        a, b, c = self.variants
        (a | b | c).write({'default_code': False})
        a.default_code, b.default_code = 'ZZF-A', 'ZZF-B'
        specs = self.env['ke.product.spec'].search([('product_id', '=', b.id), ('key_id', '=', self.keys['diameter'].id)])
        specs.with_context(lang='it_IT').value = '0,25 mm'
        specs.with_context(lang='en_US').value = '0.25 mm'
        self.env['ke.product.spec'].search([('product_id', 'in', (a | b).ids),
                                            ('key_id', '!=', self.keys['diameter'].id)]).unlink()
        self.tmpl.ke_apply_supplier_names()
        labels = self._labels('it_IT')
        self.assertEqual(labels[2], '0,35 mm')
        self.assertEqual(sorted(labels[:2]), ['0,25 mm (ZZF-A)', '0,25 mm (ZZF-B)'])

    def test_incomplete_or_amazon_specs_are_not_used(self):
        a, b, c = self.variants
        before = self._labels('en_US')
        # Diametro mancante su una variante, il resto da Amazon: nessuna specifica utile → nomi di prima.
        self.env['ke.product.spec'].search([('product_id', '=', a.id)]).unlink()
        self.env['ke.product.spec'].search([('product_id', 'in', (b | c).ids),
                                            ('key_id', '!=', self.keys['diameter'].id)]).unlink()
        self.env['ke.product.spec'].search([('product_id', 'in', (b | c).ids)]).write({'source': 'amazon'})
        self.tmpl.ke_apply_supplier_names()
        self.assertEqual(self._labels('en_US'), before)

    def test_wrong_translation_is_fixed(self):
        self.tmpl.ke_apply_supplier_names()
        value = self.variants[0].product_template_attribute_value_ids.product_attribute_value_id
        value.with_context(lang='it_IT').name = 'Limone posteriore'
        self.assertTrue(self.tmpl.ke_apply_supplier_names(), 'italiano sbagliato: da rifare')
        self.assertEqual(self._labels('it_IT')[0], '0,25 mm')
