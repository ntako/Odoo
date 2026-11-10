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

    def _update(self, default, cats, replace=False):
        client = PsApiClient(self.env)
        xml = _PRODUCT_XML.format(default=default, cats=''.join(f'<category><id>{c}</id></category>' for c in cats))
        sent = {}
        with patch.object(PsApiClient, '_get_xml_str', return_value=xml), \
                patch.object(PsApiClient, '_put_xml', side_effect=lambda r, i, b: sent.update(xml=b)):
            client.update_product(7, 'Nome', category_id=55, replace_category=replace)
        prod = ET.fromstring(sent['xml']).find('product')
        return prod.findtext('id_category_default'), [c.findtext('id') for c in prod.find('associations/categories')]

    def test_existing_ps_category_kept_unless_forced(self):
        self.assertEqual(self._update(30, [2, 30]), ('30', ['2', '30']), 'scelta del sito non toccata')
        self.assertEqual(self._update(2, [2]), ('55', ['55']), 'solo nella predefinita: si imposta')
        self.assertEqual(self._update(30, [2, 30], replace=True), ('55', ['55']))
