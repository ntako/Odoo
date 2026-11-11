"""Descrizioni dal catalogo fornitore (2026-11-11): testi per lingua dagli attributi informativi."""
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_product_catalog.models.product_supplier_texts import _split_bullets


@tagged('post_install', '-at_install')
class TestSupplierTexts(TransactionCase):

    def setUp(self):
        super().setUp()
        for code in ('it_IT', 'fr_FR', 'en_GB'):
            self.env['res.lang']._activate_lang(code)

    def _attr(self, name, value):
        attr = self.env['product.attribute'].create({'name': name, 'create_variant': 'no_variant'})
        val = self.env['product.attribute.value'].create({'name': value, 'attribute_id': attr.id})
        return (0, 0, {'attribute_id': attr.id, 'value_ids': [(6, 0, val.ids)]})

    def test_split_bullets(self):
        self.assertEqual(_split_bullets('• Uno • Due'), ['Uno', 'Due'])
        self.assertEqual(_split_bullets('_Great value_Very strong'), ['Great value', 'Very strong'])
        self.assertEqual(_split_bullets('Alta\nForte'), ['Alta', 'Forte'])

    def test_descriptions_per_language(self):
        tmpl = self.env['product.template'].with_context(lang='it_IT').create({
            'name': 'ZZT Dura4',
            'description_sale': '<p>Filo classico a 4 capi.</p><ul><li>• Resistente • Fibre PE</li></ul>',
            'attribute_line_ids': [
                self._attr('ZZT IT\nCopy', 'Filo classico a 4 capi.'),
                self._attr('ZZT IT\nBullets', '• Resistente • Fibre PE'),
                self._attr('ZZT FR\nCopy', 'Tresse classique 4 brins.'),
                self._attr('ZZT FR\nBullets', '• Durable • Fibres PE'),
                self._attr('ZZT UK\nCopy', 'Classic 4 carrier braid.'),
                self._attr('ZZT UKBullets', '_Durable_PE fibres'),
            ]})
        # gli attributi del test hanno un prefisso: si legge la chiave dopo "ZZT"
        texts = {k[-2:] if len(k) > 2 else k: v for k, v in tmpl._ke_supplier_texts().items()}
        self.assertFalse(texts, 'i nomi "ZZT IT Copy" non sono attributi del fornitore')

    def test_real_attribute_names(self):
        tmpl = self.env['product.template'].with_context(lang='it_IT').create({
            'name': 'ZZT Dura4 bis',
            'description_sale': '<p>Filo classico a 4 capi.</p><ul><li>• Resistente • Fibre PE</li></ul>',
            'attribute_line_ids': [
                self._attr('IT\nCopy', 'Filo classico a 4 capi.'),
                self._attr('IT\nBullets', '• Resistente • Fibre PE'),
                self._attr('FR\nCopy', 'Tresse classique 4 brins.'),
                self._attr('FR\nBullets', '• Durable • Fibres PE'),
                self._attr('UKBullets', '_Durable_PE fibres'),
                self._attr('UK Copy', 'Classic 4 carrier braid.'),
            ]})
        tmpl.action_ke_supplier_descriptions()
        it = str(tmpl.with_context(lang='it_IT').description_sale)
        self.assertIn('<li>Resistente</li><li>Fibre PE</li>', it, 'italiano rifatto con i punti elenco veri')
        self.assertIn('<li>Durable</li><li>Fibres PE</li>', str(tmpl.with_context(lang='fr_FR').description_sale))
        self.assertIn('<li>PE fibres</li>', str(tmpl.with_context(lang='en_GB').description_sale))
        # un testo Amazon/manuale in italiano non si tocca
        tmpl.with_context(lang='it_IT').description_sale = 'Testo Amazon'
        tmpl.action_ke_supplier_descriptions()
        self.assertEqual(tmpl.with_context(lang='it_IT').description_sale, 'Testo Amazon')
