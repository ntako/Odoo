"""Scheda "Sito web" (2026-11-11): proposta dei testi dai dati del prodotto, solo nei campi vuoti."""
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_product_catalog.models.product_website import short_name, slugify


@tagged('post_install', '-at_install')
class TestProductWebsite(TransactionCase):

    def _product(self, **vals):
        brand = self.env['ke.product.brand'].create({'name': 'ZZT DAM'})
        return self.env['product.template'].create(dict({
            'name': 'ZZT DAM Quick 1, Mulinello da pesca, Mulinelli da spinning, il mulinello definitivo',
            'ke_brand_id': brand.id,
            'description_sale': 'Mulinello leggero con corpo in grafite e frizione anteriore precisa.'
                                '<ul><li>Bobina in alluminio</li><li>5 cuscinetti</li><li>Rapporto 5.2:1</li>'
                                '<li>Peso 250 g</li></ul>',
        }, **vals))

    def test_helpers(self):
        self.assertEqual(short_name('DAM Quick 1, Mulinello da pesca, Mulinelli da spinning'), 'DAM Quick 1')
        self.assertEqual(short_name('Savage Gear Micro Jig – esca da mare 3 cm'), 'Savage Gear Micro Jig')
        self.assertEqual(short_name('DAM - Mulinello'), 'DAM - Mulinello', 'taglio troppo corto: titolo intero')
        self.assertEqual(slugify('Mulinello Più Leggero 3000 FD!'), 'mulinello-piu-leggero-3000-fd')

    def test_propose_fills_empty_fields(self):
        tmpl = self._product()
        tmpl.action_ke_web_propose()
        self.assertEqual(tmpl.ke_web_name, 'ZZT DAM Quick 1')
        self.assertEqual(tmpl.ke_web_slug, 'zzt-dam-quick-1')
        self.assertEqual(tmpl.ke_web_meta_title, 'ZZT DAM Quick 1', 'marca già nel nome')
        self.assertTrue(tmpl.ke_web_meta_description.startswith('Mulinello leggero'))
        self.assertLessEqual(tmpl.ke_web_meta_description_len, 160)
        self.assertIn('<li>Bobina in alluminio</li>', str(tmpl.ke_web_short_description))
        self.assertNotIn('Peso 250 g', str(tmpl.ke_web_short_description), 'solo i primi 3 punti')
        self.assertIn('Peso 250 g', str(tmpl.ke_web_description))

    def test_propose_keeps_manual_texts_and_adds_brand(self):
        tmpl = self._product(name='ZZT Quick 1, Mulinello', ke_web_meta_description='Scritta a mano')
        tmpl.action_ke_web_propose()
        self.assertEqual(tmpl.ke_web_meta_title, 'ZZT Quick 1 | ZZT DAM')
        self.assertEqual(tmpl.ke_web_meta_description, 'Scritta a mano')
        tmpl.ke_web_name = 'Nome mio'
        tmpl.action_ke_web_propose()
        self.assertEqual(tmpl.ke_web_name, 'Nome mio')

    def test_bullets_joined_with_dots_and_export_texts(self):
        tmpl = self._product(description_sale='Treccia liscia.<ul><li>• Sottile • Alta resistenza al nodo • 8 filamenti</li></ul>')
        texts = tmpl._ke_web_texts()
        self.assertIn('<li>Sottile</li><li>Alta resistenza al nodo</li><li>8 filamenti</li>', texts['description'])
        self.assertEqual(texts['name'], tmpl.name, 'senza "Nome sul sito" il nome resta quello del prodotto')
        self.assertEqual(texts['slug'], 'zzt-dam-quick-1')
        self.assertFalse(tmpl.ke_web_description, 'la proposta per l\'export non viene salvata')

