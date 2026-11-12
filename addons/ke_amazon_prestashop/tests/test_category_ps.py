"""
Categoria PrestaShop dalla corrispondenza Amazon (2026-11-10) e categoria
dei prodotti già su PS lasciata com'è (salvo opzione o solo radice).
"""
import base64
import xml.etree.ElementTree as ET
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_prestashop_sync.services.ps_api_client import PsApiClient

_PRODUCT_XML = """<prestashop><product><id>7</id><id_category_default>{default}</id_category_default>
<price>1</price><reference>R</reference><ean13/><upc/><id_manufacturer>0</id_manufacturer><active>1</active>
<name><language id="1">x</language></name><description_short><language id="1"/></description_short>
<associations><categories>{cats}</categories></associations></product></prestashop>"""


@tagged('post_install', '-at_install')
class TestCategoryPs(TransactionCase):

    def setUp(self):
        super().setUp()
        PsCat = self.env['ke.prestashop.category']
        self.ps_reels = PsCat.create({'ps_id': 91001, 'name': 'ZZT Mulinelli da Pesca',
                                      'complete_name': 'ZZT Pesca / ZZT Mulinelli da Pesca'})
        self.ps_surf = PsCat.create({'ps_id': 91002, 'name': 'ZZT Surfcasting Reels',
                                     'complete_name': 'ZZT Pesca / ZZT Mulinelli da Pesca / ZZT Surfcasting Reels'})
        self.reels = self.env['product.category'].create({'name': 'ZZT Mulinelli', 'ps_category_id': self.ps_reels.id})

    def test_amazon_mapping_wins_over_odoo_category(self):
        tmpl = self.env['product.template'].create({
            'name': 'ZZT surf', 'categ_id': self.reels.id, 'x_amazon_category': 'ZZT Mulinelli da Surfcasting'})
        self.assertEqual(tmpl._ke_ps_category(), self.ps_reels, 'senza corrispondenza: categoria Odoo')
        self.env['ke.amazon.category.map'].create({
            'name': 'ZZT Mulinelli da surfcasting', 'categ_id': self.reels.id, 'ps_category_id': self.ps_surf.id})
        self.assertEqual(tmpl._ke_ps_category(), self.ps_surf)
        ps_acc = self.env['ke.prestashop.category'].create({'ps_id': 91003, 'name': 'ZZT Accessori',
                                                            'complete_name': 'ZZT Pesca / ZZT Accessori'})
        tmpl.categ_id = self.env['product.category'].create({'name': 'ZZT Accessori', 'ps_category_id': ps_acc.id})
        self.assertEqual(tmpl._ke_ps_category(), ps_acc, 'spostato a mano: vale la mappatura della nuova categoria')

    def test_import_reads_prestashop_column(self):
        data = ('Origine;Categoria trovata;Prodotti;Categoria Odoo;Categoria PrestaShop\n'
                'Amazon;ZZT Carretes de surf;1;ZZT Pesca / ZZT Mulinelli;ZZT Pesca / ZZT Mulinelli da Pesca / ZZT Surfcasting Reels\n'
                'Odoo produzione;ZZT Reels;1;ZZT Pesca / ZZT Mulinelli;91001\n'
                'Amazon;ZZT Altro;1;ZZT Pesca / ZZT Mulinelli;Categoria che non esiste\n').encode('utf-8-sig')
        wizard = self.env['ke.amazon.category.map.import'].create({
            'file_data': base64.b64encode(data), 'file_name': 'c.csv', 'delete_empty': False})
        wizard.action_import()
        mapping = self.env['ke.amazon.category.map'].search([('key', '=', 'zzt carretes de surf')])
        self.assertEqual(mapping.ps_category_id, self.ps_surf)
        target = self.env['product.category'].search([('complete_name', '=', 'ZZT Pesca / ZZT Mulinelli')])
        self.assertEqual(target.ps_category_id, self.ps_reels)
        self.assertIn('Categorie PrestaShop impostate: 2', wizard.result)
        self.assertIn('Categoria che non esiste', wizard.result)

    def _update(self, default, cats, replace=False, extra=()):
        client = PsApiClient(self.env)
        xml = _PRODUCT_XML.format(default=default, cats=''.join(f'<category><id>{c}</id></category>' for c in cats))
        sent = {}
        with patch.object(PsApiClient, '_get_xml_str', return_value=xml), \
                patch.object(PsApiClient, '_put_xml', side_effect=lambda r, i, b: sent.update(xml=b)):
            client.update_product(7, 'Nome', category_id=55, replace_category=replace, extra_category_ids=extra)
        prod = ET.fromstring(sent['xml']).find('product')
        return prod.findtext('id_category_default'), [c.findtext('id') for c in prod.find('associations/categories')]

    def test_existing_ps_category_kept_unless_forced(self):
        self.assertEqual(self._update(30, [2, 30]), ('30', ['2', '30']), 'scelta del sito non toccata')
        self.assertEqual(self._update(2, [2]), ('55', ['55']), 'solo nella predefinita: si imposta')
        self.assertEqual(self._update(30, [2, 30], replace=True), ('55', ['55']))

    def _update_texts(self, replace):
        client = PsApiClient(self.env)
        xml = _PRODUCT_XML.format(default=30, cats='<category><id>30</id></category>').replace(
            '<description_short>', '<meta_title><language id="1">vecchio</language></meta_title><description_short>')
        sent = {}
        with patch.object(PsApiClient, '_get_xml_str', return_value=xml), \
                patch.object(PsApiClient, '_put_xml', side_effect=lambda r, i, b: sent.update(xml=b)):
            client.update_product(7, 'Nome nuovo', meta_title='Meta nuovo', replace_texts=replace)
        prod = ET.fromstring(sent['xml']).find('product')
        return prod.findtext('name/language'), prod.findtext('meta_title/language')

    def test_texts_only_on_creation_unless_forced(self):
        self.assertEqual(self._update_texts(False), ('x', 'vecchio'), 'prodotto già sul sito: testi non toccati')
        self.assertEqual(self._update_texts(True), ('Nome nuovo', 'Meta nuovo'))

    def test_create_sends_website_texts(self):
        client = PsApiClient(self.env)
        sent = {}
        with patch.object(PsApiClient, '_post_xml', side_effect=lambda r, b: sent.update(xml=b) or 99):
            client.create_product('DAM Quick 1', meta_title='DAM Quick 1 | DAM', meta_description='Mulinello',
                                  description='<p>Testo</p>', link_rewrite='dam-quick-uno')
        prod = ET.fromstring(sent['xml']).find('product')
        self.assertEqual(prod.findtext('link_rewrite/language'), 'dam-quick-uno')
        self.assertEqual(prod.findtext('meta_title/language'), 'DAM Quick 1 | DAM')
        self.assertEqual(prod.findtext('description/language'), '<p>Testo</p>')

    def test_engine_uses_website_texts(self):
        from odoo.addons.ke_prestashop_sync.services.sync_engine import SyncEngine as PsSyncEngine
        tmpl = self.env['product.template'].create({'name': 'ZZT titolo lungo, Amazon', 'ke_web_name': 'ZZT Breve',
                                                    'ke_web_meta_title': 'ZZT Meta'})
        engine = PsSyncEngine.__new__(PsSyncEngine)
        engine.env = self.env
        with patch.object(PsSyncEngine, '_ps_price', return_value=1.0), \
                patch.object(PsSyncEngine, '_ps_manufacturer_id', return_value=0), \
                patch.object(PsSyncEngine, '_ps_lang', return_value='en_US'):
            vals = engine._ps_product_vals(tmpl, [])
        self.assertEqual((vals['name'], vals['meta_title']), ('ZZT Breve', 'ZZT Meta'))

    def test_forced_category_wins_and_source(self):
        """Categoria forzata sul prodotto (2026-11-12): prima di corrispondenza Amazon e categoria Odoo."""
        self.env['ke.amazon.category.map'].create({
            'name': 'ZZT Surf forzato', 'categ_id': self.reels.id, 'ps_category_id': self.ps_surf.id})
        tmpl = self.env['product.template'].create({
            'name': 'ZZT forzato', 'categ_id': self.reels.id, 'x_amazon_category': 'ZZT Surf forzato'})
        self.assertEqual(tmpl.ps_category_source, 'Corrispondenza categoria Amazon')
        offers = self.env['ke.prestashop.category'].create({'ps_id': 91004, 'name': 'ZZT Offerte',
                                                            'complete_name': 'ZZT Offerte'})
        wizard = self.env['ke.ps.category.assign'].with_context(
            active_model='product.template', active_ids=tmpl.ids).create({
                'main_action': 'set', 'category_id': self.ps_reels.id,
                'extra_action': 'add', 'extra_category_ids': [(6, 0, (offers | self.ps_reels).ids)]})
        wizard.action_apply()
        self.assertEqual(tmpl._ke_ps_category(), self.ps_reels)
        self.assertEqual(tmpl.ps_category_source, 'Forzata sul prodotto')
        self.assertEqual(tmpl._ke_ps_extra_category_ids(91001), [91004], 'la principale non si ripete')
        self.env['ke.ps.category.assign'].with_context(
            active_model='product.template', active_ids=tmpl.ids).create({
                'main_action': 'auto', 'extra_action': 'replace'}).action_apply()
        self.assertEqual(tmpl._ke_ps_category(), self.ps_surf)
        self.assertFalse(tmpl.ps_extra_category_ids)

    def test_extra_categories_on_update_and_create(self):
        self.assertEqual(self._update(30, [2, 30], extra=[40]), ('30', ['2', '30', '40']),
                         'aggiuntive aggiunte, scelta del sito non toccata')
        self.assertEqual(self._update(2, [2, 40], extra=[40]), ('55', ['55', '40']),
                         'le aggiuntive non contano come scelta fatta sul sito')
        self.assertEqual(self._update(30, [30, 40], replace=True, extra=[41]), ('55', ['55', '41']))
        client = PsApiClient(self.env)
        sent = {}
        with patch.object(PsApiClient, '_post_xml', side_effect=lambda r, b: sent.update(xml=b) or 99):
            client.create_product('ZZT', category_id=55, extra_category_ids=[40, 55])
        prod = ET.fromstring(sent['xml']).find('product')
        self.assertEqual([c.findtext('id') for c in prod.find('associations/categories')], ['55', '40'])
