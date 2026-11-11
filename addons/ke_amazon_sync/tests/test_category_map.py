"""
Corrispondenze categoria Amazon → categoria Odoo (2026-11-10): il Full Import
non crea più categorie per nome Amazon (una per lingua), le prende dalle
corrispondenze; import del file con albero e vecchie categorie Odoo.
"""
import base64

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_amazon_sync.models.amazon_category_map import category_key
from odoo.addons.ke_amazon_sync.services.category_map import CategoryMapper, DEFAULT_PARAM
from odoo.addons.ke_amazon_sync.services.sync_engine import _get_or_create_category


@tagged('post_install', '-at_install')
class TestCategoryMap(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Category = self.env['product.category']
        self.default = self.Category.create({'name': 'ZZT Da classificare'})
        self.env['ir.config_parameter'].sudo().set_param(DEFAULT_PARAM, str(self.default.id))
        self.squid = self.Category.create({'name': 'ZZT Calamaro'})
        self.Map = self.env['ke.amazon.category.map']
        self.Map.create({'name': 'ZZT Esche da calamaro', 'categ_id': self.squid.id})

    def _product(self, name, amazon_category, categ):
        return self.env['product.template'].create({
            'name': name, 'x_amazon_category': amazon_category, 'categ_id': categ.id})

    def test_key_ignores_case_accents_spaces(self):
        self.assertEqual(category_key(' Cebos  de CALAMÁR '), 'cebos de calamar')

    def test_full_import_uses_map_and_never_creates_categories(self):
        before = self.Category.search_count([])
        self.assertEqual(_get_or_create_category(self.env, 'zzt esche da CALAMARO'), self.squid)
        self.assertEqual(_get_or_create_category(self.env, 'ZZT Cebos nuevos'), self.default)
        self.assertEqual(self.Category.search_count([]), before, 'nessuna categoria creata')
        new_map = self.Map.search([('key', '=', 'zzt cebos nuevos')])
        self.assertTrue(new_map and not new_map.categ_id, 'nome nuovo da mappare')

    def test_assign_moves_only_replaceable_products(self):
        legacy = self.Category.create({'name': 'ZZT Esche da calamaro'})  # creata dal vecchio import
        chosen = self.Category.create({'name': 'ZZT Scelta a mano'})
        self.Map.create({'name': 'ZZT Altra', 'categ_id': chosen.id})
        a = self._product('ZZT A', 'ZZT Esche da calamaro', legacy)
        b = self._product('ZZT B', 'ZZT ESCHE DA CALAMARO', self.default)
        c = self._product('ZZT C', 'ZZT Esche da calamaro', chosen)
        d = self._product('ZZT D senza categoria Amazon', False, legacy)
        CategoryMapper(self.env).assign()
        self.assertEqual(a.categ_id, self.squid)
        self.assertEqual(b.categ_id, self.squid)
        self.assertEqual(c.categ_id, chosen, 'una categoria di destinazione non si tocca')
        self.assertEqual(d.categ_id, self.squid, 'senza categoria Amazon vale il nome della vecchia categoria')
        CategoryMapper(self.env).assign(force=True)
        self.assertEqual(c.categ_id, self.squid)

    def test_import_builds_tree_moves_old_and_deletes_empty(self):
        old = self.Category.create({'name': 'ZZT Bait'})
        legacy = self.Category.create({'name': 'ZZT Cebos de calamar'})
        p_old = self._product('ZZT vecchio', False, old)
        p_es = self._product('ZZT spagnolo', 'ZZT Cebos de calamar', legacy)
        rows = [
            ['Origine', 'Categoria trovata', 'Prodotti', 'Categoria Odoo'],
            ['Odoo produzione', 'ZZT Bait', '1', 'ZZT Pesca / ZZT Esche artificiali'],
            ['Amazon', 'ZZT Cebos de calamar', '1', 'ZZT Pesca / ZZT Esche artificiali / ZZT Calamaro'],
            ['Amazon', 'ZZT Senza casa', '1', ''],
        ]
        mapper = CategoryMapper(self.env)
        stats = mapper.import_rows(rows, 'ZZT Altro / ZZT Da classificare')
        mapper.assign()
        deleted = mapper.delete_unused(stats['old_categories'])
        esche = self.Category.search([('complete_name', '=', 'ZZT Pesca / ZZT Esche artificiali')])
        self.assertEqual(p_old.categ_id, esche)
        self.assertEqual(p_es.categ_id.complete_name, 'ZZT Pesca / ZZT Esche artificiali / ZZT Calamaro')
        self.assertEqual(stats['moved'], 1)
        self.assertFalse(old.exists())
        self.assertFalse(legacy.exists())
        self.assertGreaterEqual(deleted, 2)
        default = self.env['ir.config_parameter'].sudo().get_param(DEFAULT_PARAM)
        self.assertEqual(self.Category.browse(int(default)).complete_name, 'ZZT Altro / ZZT Da classificare')
        self.assertFalse(self.Map.search([('key', '=', 'zzt senza casa')]).categ_id)

    def test_wizard_csv(self):
        legacy = self.Category.create({'name': 'ZZT Squid Lures'})
        product = self._product('ZZT inglese', 'ZZT Squid Lures', legacy)
        data = ('Origine;Categoria trovata;Prodotti;Categoria Odoo\n'
                'Amazon;ZZT Squid Lures;1;ZZT Pesca / ZZT Calamaro\n').encode('utf-8-sig')
        wizard = self.env['ke.amazon.category.map.import'].create({
            'file_data': base64.b64encode(data), 'file_name': 'categorie.csv'})
        wizard.action_import()
        self.assertEqual(product.categ_id.complete_name, 'ZZT Pesca / ZZT Calamaro')
        self.assertIn('Corrispondenze Amazon: 1', wizard.result)
        self.assertFalse(legacy.exists())

    def test_assign_refines_from_parent_category(self):
        parent = self.Category.create({'name': 'ZZT Esche'})
        child = self.Category.create({'name': 'ZZT Siliconiche', 'parent_id': parent.id})
        other = self.Category.create({'name': 'ZZT Minuteria'})
        self.Map.create({'name': 'ZZT Soft bait', 'categ_id': child.id})
        self.Map.create({'name': 'ZZT Altro ramo', 'categ_id': parent.id})
        self.Map.create({'name': 'ZZT Minuteria Amazon', 'categ_id': other.id})
        up = self._product('ZZT padre', 'ZZT Soft bait', parent)
        side = self._product('ZZT altro ramo', 'ZZT Soft bait', other)
        CategoryMapper(self.env).assign()
        self.assertEqual(up.categ_id, child, 'affinato nella sottocategoria')
        self.assertEqual(side.categ_id, other, 'categoria di un altro ramo: resta')

