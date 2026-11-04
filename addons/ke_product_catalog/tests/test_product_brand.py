import base64

from odoo.tests.common import TransactionCase, tagged

from ..models.product_brand import brand_key


@tagged('post_install', '-at_install')
class TestProductBrand(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Brand = self.env['ke.product.brand']
        self.Alias = self.env['ke.product.brand.alias']

    def test_key(self):
        self.assertEqual(brand_key('YAMASHITA'), brand_key(' Yamashita '))
        self.assertEqual(brand_key('TRABUCCO INTERNATIONAL SRL'), brand_key('Trabucco'))
        self.assertEqual(brand_key('Tubertini Srl'), 'tubertini')
        self.assertEqual(brand_key('Spider-Wire'), brand_key('SPIDERWIRE'))
        self.assertTrue(brand_key('マリア'))

    def test_find_or_create(self):
        yama = self.Brand.create({'name': 'ZZT Yamashita'})
        self.assertEqual(self.Brand.find_or_create('ZZT YAMASHITA'), yama)
        self.Alias.create({'name': 'ZZT マリア', 'brand_id': yama.id})
        self.assertEqual(self.Brand.find_or_create('ZZT マリア'), yama)
        self.Alias.create({'name': 'ZZT Generico', 'brand_id': False})
        self.assertFalse(self.Brand.find_or_create('zzt generico'), '"non è una marca": nessuna marca')
        new = self.Brand.find_or_create('ZZT Nuova Marca')
        self.assertTrue(new.to_review)
        self.assertEqual(self.Brand.find_or_create('ZZT NUOVA MARCA'), new)

    def test_merge(self):
        target = self.Brand.create({'name': 'ZZT Greys'})
        wrong = self.Brand.create({'name': 'ZZT GRAYS', 'to_review': True})
        tmpl = self.env['product.template'].create({'name': 'ZZT prodotto', 'ke_brand_id': wrong.id})
        wizard = self.env['ke.product.brand.merge'].with_context(
            active_model='ke.product.brand', active_ids=[wrong.id, target.id]).create({})
        self.assertEqual(wizard.target_id, target, 'proposta la marca confermata')
        wizard.action_merge()
        self.assertFalse(wrong.exists())
        self.assertEqual(tmpl.ke_brand_id, target)
        self.assertEqual(self.Brand.find_or_create('ZZT GRAYS'), target, 'il nome unito diventa alternativo')

    def test_alias_import(self):
        stray = self.Brand.create({'name': 'ZZT マリア', 'to_review': True})
        tmpl = self.env['product.template'].create({'name': 'ZZT prodotto 2', 'ke_brand_id': stray.id})
        csv_data = ("Marca su Amazon,Prodotti,Marca corretta\n"
                    "ZZT マリア,3,ZZT Maria\n"
                    "ZZT YAMASHITA,5,ZZT Yamashita\n"
                    "ZZT Unbekannt,1,\n").encode()
        wizard = self.env['ke.product.brand.alias.import'].create({
            'file_data': base64.b64encode(csv_data), 'file_name': 'marche.csv',
        })
        wizard.action_import()
        maria = self.Brand.search([('name', '=', 'ZZT Maria')])
        self.assertTrue(maria and not maria.to_review)
        self.assertEqual(tmpl.ke_brand_id, maria, 'la marca "Da verificare" viene unita')
        self.assertFalse(stray.exists())
        self.assertTrue(self.Brand.search([('name', '=', 'ZZT Yamashita')]))
        self.assertFalse(self.Brand.find_or_create('ZZT Unbekannt'))
        self.assertIn('Marche create: 2', wizard.result)
