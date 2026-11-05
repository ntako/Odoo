"""
Test per il prezzo di vendita della variante (ke_list_price, 2026-11-05):
vale al posto del prezzo della famiglia in lst_price e nei listini, e
scrivere il prezzo su una variante di famiglia non cambia la famiglia.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestVariantPrice(TransactionCase):

    def setUp(self):
        super().setUp()
        attr = self.env['product.attribute'].create({
            'name': 'ZZT Taglia prezzo', 'value_ids': [(0, 0, {'name': 'S'}), (0, 0, {'name': 'L'})],
        })
        self.tmpl = self.env['product.template'].create({
            'name': 'ZZT famiglia prezzi', 'list_price': 10.0,
            'attribute_line_ids': [(0, 0, {'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)]})],
        })
        self.small, self.large = self.tmpl.product_variant_ids.sorted(
            lambda p: p.product_template_attribute_value_ids.name)[::-1]

    def test_variant_price_replaces_family_price(self):
        self.large.ke_list_price = 15.0
        self.assertEqual((self.large.lst_price, self.small.lst_price), (15.0, 10.0))

    def test_pricelist_uses_variant_price(self):
        self.large.ke_list_price = 15.0
        pricelist = self.env['product.pricelist'].create({
            'name': 'ZZT +10', 'currency_id': self.env.company.currency_id.id,
            'item_ids': [(0, 0, {'applied_on': '3_global', 'compute_price': 'formula',
                                 'base': 'list_price', 'price_discount': -10.0})],
        })
        self.assertAlmostEqual(pricelist._get_product_price(self.large, 1.0), 16.5, places=2)
        self.assertAlmostEqual(pricelist._get_product_price(self.small, 1.0), 11.0, places=2)

    def test_writing_variant_sale_price_keeps_family_price(self):
        self.small.lst_price = 12.0
        self.assertEqual((self.small.ke_list_price, self.small.lst_price, self.tmpl.list_price), (12.0, 12.0, 10.0))

    def test_single_product_sale_price_still_on_template(self):
        tmpl = self.env['product.template'].create({'name': 'ZZT semplice prezzo', 'list_price': 5.0})
        tmpl.product_variant_id.lst_price = 7.0
        self.assertEqual((tmpl.list_price, tmpl.product_variant_id.ke_list_price), (7.0, 0.0))
