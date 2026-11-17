from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProductSpec(TransactionCase):
    """Specifiche tecniche (2026-11-17): valori per prodotto/variante, tradotti, con fonte."""

    def setUp(self):
        super().setUp()
        for code in ('it_IT', 'es_ES'):
            self.env['res.lang']._activate_lang(code)
        Key = self.env['ke.product.spec.key']
        self.ratio = Key._ensure('zz_ratio', {'it_IT': 'Rapporto ZZ', 'es_ES': 'Relación ZZ'}, 5)
        self.color = Key._ensure('zz_color', {'it_IT': 'Colore ZZ', 'es_ES': 'Color ZZ'}, 6)
        self.tmpl = self.env['product.template'].create({'name': 'ZZS Mulinello'})

    def _values(self, lang='it_IT'):
        return {s.key_id.code: s.value for s in self.tmpl.with_context(lang=lang).ke_spec_ids}

    def test_key_ensure_translated_and_reused(self):
        self.assertEqual(self.ratio.with_context(lang='es_ES').name, 'Relación ZZ')
        self.assertEqual(self.env['ke.product.spec.key']._ensure('zz_ratio', {'it_IT': 'Altro'}), self.ratio)

    def test_set_update_translate_and_remove_own(self):
        self.assertEqual(self.tmpl._ke_set_specs({self.ratio: '5,2:1', self.color: 'Nero'}, 'amazon', 'it_IT'), 2)
        self.tmpl._ke_set_specs({self.color: 'Negro'}, 'amazon', 'es_ES')
        self.assertEqual(self._values('it_IT'), {'zz_ratio': '5,2:1', 'zz_color': 'Nero'})
        self.assertEqual(self._values('es_ES')['zz_color'], 'Negro')
        self.assertEqual(self.tmpl._ke_set_specs({self.ratio: '5,2:1'}, 'amazon', 'it_IT'), 0, 'niente da cambiare')
        self.tmpl._ke_set_specs({self.ratio: ''}, 'amazon', 'it_IT')
        self.assertNotIn('zz_ratio', self._values())

    def test_manual_value_is_kept(self):
        self.env['ke.product.spec'].create({'product_tmpl_id': self.tmpl.id, 'key_id': self.color.id,
                                           'value': 'Blu notte', 'source': 'manual'})
        self.tmpl._ke_set_specs({self.color: 'Nero'}, 'amazon', 'it_IT')
        self.tmpl._ke_set_specs({self.color: ''}, 'amazon', 'it_IT')
        self.assertEqual(self._values()['zz_color'], 'Blu notte')

    def test_variant_values_are_separate(self):
        attr = self.env['product.attribute'].create({
            'name': 'ZZS Misura', 'value_ids': [(0, 0, {'name': '1000'}), (0, 0, {'name': '2000'})]})
        fam = self.env['product.template'].create({
            'name': 'ZZS Famiglia',
            'attribute_line_ids': [(0, 0, {'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)]})]})
        small, big = fam.product_variant_ids
        fam._ke_set_specs({self.ratio: '5,2:1'}, 'amazon', 'it_IT', variant=small)
        fam._ke_set_specs({self.ratio: '6,2:1'}, 'amazon', 'it_IT', variant=big)
        by_variant = {s.product_id: s.value for s in fam.ke_spec_ids}
        self.assertEqual(by_variant, {small: '5,2:1', big: '6,2:1'})

    def test_each_language_stored_even_if_equal_to_fallback(self):
        """Ogni lingua ha il suo valore, anche se coincide con quello mostrato per ripiego (2026-11-17)."""
        self.env['res.lang']._activate_lang('de_DE')
        self.tmpl._ke_set_specs({self.ratio: '7,3:1'}, 'supplier', 'de_DE')
        self.tmpl._ke_set_specs({self.ratio: '7,3:1'}, 'supplier', 'it_IT')
        self.tmpl._ke_set_specs({self.ratio: '7.3:1'}, 'supplier', 'en_US')
        self.assertEqual(self._values('it_IT')['zz_ratio'], '7,3:1')
        self.assertEqual(self._values('en_US')['zz_ratio'], '7.3:1')
