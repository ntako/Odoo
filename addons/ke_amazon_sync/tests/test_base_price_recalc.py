"""
"Ricalcola prezzi base da Amazon" (2026-11-09): prezzo base dal prezzo
Amazon attuale (meno IVA del paese e ricarico del listino), di default solo
sui prodotti con prezzo 1,00 o vuoto; famiglie col prezzo più frequente e
prezzo proprio sulle varianti diverse; marketplace senza tasso saltati.
"""
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_amazon_sync.services.base_price import BasePriceRecalc


@tagged('post_install', '-at_install')
class TestBasePriceRecalc(TransactionCase):

    def setUp(self):
        super().setUp()
        self.tax = self.env['account.tax'].create({
            'name': 'ZZT IVA 22 ricalcolo', 'amount': 22.0, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'price_include_override': 'tax_excluded',
        })
        amazon = self.env.ref('ke_amazon_sync.pricelist_amazon')
        amazon.item_ids.unlink()
        self._rule(amazon, 15.0)
        eur = self.env.company.currency_id
        self.it = self._mp('ZRIT', eur, 22.0, sequence=50)
        fr_list = self.env['product.pricelist'].create({'name': 'ZZT FR', 'currency_id': eur.id})
        self._rule(fr_list, 22.0)
        self.fr = self._mp('ZRFR', eur, 20.0, pricelist=fr_list, sequence=1)
        gbp = self.env.ref('base.GBP') if eur != self.env.ref('base.GBP') else self.env.ref('base.USD')
        gbp.active = True
        gbp.rate_ids.unlink()
        self.uk = self._mp('ZRUK', gbp, 20.0, sequence=2)
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', self.it.marketplace_id)
        self.Listing = self.env['ke.amazon.listing']

    def _rule(self, pricelist, markup):
        self.env['product.pricelist.item'].create({
            'pricelist_id': pricelist.id, 'applied_on': '3_global', 'compute_price': 'formula',
            'base': 'list_price', 'price_discount': -markup,
        })

    def _mp(self, code, currency, vat, pricelist=None, sequence=10):
        return self.env['ke.amazon.marketplace'].create({
            'name': f'ZZT {code}', 'code': code, 'marketplace_id': f'ZZ_{code}', 'active': True,
            'currency_id': currency.id, 'vat_rate': vat, 'pricelist_id': pricelist and pricelist.id,
            'sequence': sequence,
        })

    def _product(self, name, price=1.0):
        return self.env['product.template'].create({
            'name': name, 'type': 'consu', 'list_price': price, 'taxes_id': [(6, 0, [self.tax.id])]})

    def _listing(self, variant, mp, price, sku):
        return self.Listing.create({'product_id': variant.id, 'marketplace_id': mp.id, 'sku': sku,
                                    'status': 'active', 'price': price, 'fulfillment': 'fbm'})

    def _family(self):
        attr = self.env['product.attribute'].create({'name': 'ZZT colore ricalcolo', 'create_variant': 'always'})
        values = self.env['product.attribute.value'].create(
            [{'name': n, 'attribute_id': attr.id} for n in ('Rosso', 'Blu', 'Verde')])
        tmpl = self._product('ZZT famiglia ricalcolo')
        tmpl.attribute_line_ids = [(0, 0, {'attribute_id': attr.id, 'value_ids': [(6, 0, values.ids)]})]
        return tmpl

    def test_primary_marketplace_first_and_round_trip(self):
        tmpl = self._product('ZZT ricalcolo IT')
        self._listing(tmpl.product_variant_id, self.fr, 50.0, 'ZZT-RC-FR')
        self._listing(tmpl.product_variant_id, self.it, 28.06, 'ZZT-RC-IT')
        recalc = BasePriceRecalc(self.env)
        plan = recalc.plan()
        self.assertAlmostEqual(plan['templates'][tmpl.id], 20.0, places=2)  # 28,06 / 1,22 / 1,15
        recalc.apply(plan)
        self.assertAlmostEqual(tmpl.list_price, 20.0, places=2)
        self.assertAlmostEqual(tmpl.get_amazon_sale_price(self.it), 28.06, places=2)

    def test_secondary_marketplace_uses_its_vat_and_pricelist(self):
        tmpl = self._product('ZZT ricalcolo FR')
        self._listing(tmpl.product_variant_id, self.fr, 29.28, 'ZZT-RC-FR2')
        plan = BasePriceRecalc(self.env).plan()
        self.assertAlmostEqual(plan['templates'][tmpl.id], 20.0, places=2)  # 29,28 / 1,20 / 1,22

    def test_existing_price_kept_unless_asked(self):
        tmpl = self._product('ZZT ricalcolo già prezzato', price=15.0)
        self._listing(tmpl.product_variant_id, self.it, 28.06, 'ZZT-RC-KEEP')
        recalc = BasePriceRecalc(self.env)
        self.assertNotIn(tmpl.id, recalc.plan()['templates'])
        self.assertAlmostEqual(recalc.plan(include_existing=True)['templates'][tmpl.id], 20.0, places=2)

    def test_marketplace_without_rate_skipped(self):
        tmpl = self._product('ZZT ricalcolo solo UK')
        self._listing(tmpl.product_variant_id, self.uk, 24.0, 'ZZT-RC-UK')
        plan = BasePriceRecalc(self.env).plan()
        self.assertNotIn(tmpl.id, plan['templates'])
        self.assertIn('ZRUK', plan['skipped_marketplaces'])

    def test_family_most_frequent_price_and_variant_prices(self):
        tmpl = self._family()
        red, blue, green = tmpl.product_variant_ids
        self._listing(red, self.it, 28.06, 'ZZT-RC-R')
        self._listing(blue, self.it, 28.06, 'ZZT-RC-B')
        self._listing(green, self.it, 42.09, 'ZZT-RC-G')
        recalc = BasePriceRecalc(self.env)
        recalc.apply(recalc.plan())
        self.assertAlmostEqual(tmpl.list_price, 20.0, places=2)
        self.assertFalse(red.ke_list_price)
        self.assertAlmostEqual(green.lst_price, 30.0, places=2)
        self.assertAlmostEqual(green.get_amazon_sale_price(self.it), 42.09, places=2)

    def test_wizard_preview_and_apply(self):
        tmpl = self._product('ZZT ricalcolo wizard')
        listing = self._listing(tmpl.product_variant_id, self.it, 28.06, 'ZZT-RC-W')
        wizard = self.env['ke.amazon.base.price.wizard'].create({})
        self.assertIn(str(tmpl.id), {str(k) for k in wizard.plan['templates']})
        self.assertIn('ZZT ricalcolo wizard', str(wizard.preview))
        self.assertAlmostEqual(tmpl.list_price, 1.0, msg="l'anteprima non scrive")
        wizard.action_apply()
        self.assertAlmostEqual(tmpl.list_price, 20.0, places=2)
        self.assertAlmostEqual(listing.price_sync, 28.06, places=2)
