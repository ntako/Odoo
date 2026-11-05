"""
Test per il listino per marketplace (2026-11-05): prezzo inviato ad Amazon
calcolato con il listino e l'IVA del marketplace, nella sua valuta; senza
listino resta il calcolo di prima (listino "Amazon" +15%); senza tasso di
cambio il calcolo si blocca invece di convertire 1:1 (caso reale: GBP
disattivata e senza tassi su arundo_prod, marketplace UK in GBP).
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMarketplacePricelist(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.home = self.company.currency_id
        self.foreign = self.env.ref('base.GBP') if self.home != self.env.ref('base.GBP') else self.env.ref('base.EUR')
        self.foreign.active = True
        self.foreign.rate_ids.unlink()
        self.tax = self.env['account.tax'].create({
            'name': 'ZZT IVA 22 listini', 'amount': 22.0, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'price_include_override': 'tax_excluded',
        })
        self.tmpl = self.env['product.template'].create({
            'name': 'ZZT prezzo per marketplace', 'type': 'consu', 'is_storable': True,
            'list_price': 100.0, 'taxes_id': [(6, 0, [self.tax.id])],
        })
        amazon = self.env.ref('ke_amazon_sync.pricelist_amazon')
        amazon.item_ids.unlink()
        self.env['product.pricelist.item'].create({
            'pricelist_id': amazon.id, 'applied_on': '3_global', 'compute_price': 'formula',
            'base': 'list_price', 'price_discount': -15.0,
        })

    def _marketplace(self, code, currency=None, pricelist=None, vat=0.0):
        return self.env['ke.amazon.marketplace'].create({
            'name': f'ZZT {code}', 'code': code, 'marketplace_id': f'ZZ_PL_{code}',
            'currency_id': (currency or self.home).id, 'pricelist_id': pricelist and pricelist.id,
            'vat_rate': vat,
        })

    def _pricelist(self, currency, markup):
        pricelist = self.env['product.pricelist'].create({'name': f'ZZT {currency.name}', 'currency_id': currency.id})
        self.env['product.pricelist.item'].create({
            'pricelist_id': pricelist.id, 'applied_on': '3_global', 'compute_price': 'formula',
            'base': 'list_price', 'price_discount': -markup,
        })
        return pricelist

    def test_without_marketplace_same_as_before(self):
        self.assertAlmostEqual(self.tmpl.get_amazon_sale_price(), 140.30, places=2)  # 100 × 1,15 × 1,22

    def test_country_vat_replaces_product_vat(self):
        es = self._marketplace('ZES', vat=21.0)
        self.assertAlmostEqual(self.tmpl.get_amazon_sale_price(es), 139.15, places=2)  # 100 × 1,15 × 1,21

    def test_marketplace_pricelist_used_instead_of_amazon(self):
        it = self._marketplace('ZIT', pricelist=self._pricelist(self.home, 10.0), vat=22.0)
        self.assertAlmostEqual(self.tmpl.get_amazon_sale_price(it), 134.20, places=2)  # 100 × 1,10 × 1,22

    def test_foreign_currency_without_rate_is_blocked(self):
        uk = self._marketplace('ZUK', currency=self.foreign, vat=20.0)
        with self.assertRaises(UserError):
            self.tmpl.get_amazon_sale_price(uk)
        uk.pricelist_id = self._pricelist(self.foreign, 10.0)
        with self.assertRaises(UserError):
            self.tmpl.get_amazon_sale_price(uk)

    def test_foreign_currency_pricelist_converts_with_rate(self):
        self.env['res.currency.rate'].create({
            'currency_id': self.foreign.id, 'rate': 0.85, 'name': '2026-01-01', 'company_id': self.company.id,
        })
        uk = self._marketplace('ZUK', currency=self.foreign, pricelist=self._pricelist(self.foreign, 10.0), vat=20.0)
        self.assertAlmostEqual(self.tmpl.get_amazon_sale_price(uk), 112.20, places=2)  # 100 × 0,85 × 1,10 × 1,20

    def test_foreign_currency_without_pricelist_converts_amazon_price(self):
        self.env['res.currency.rate'].create({
            'currency_id': self.foreign.id, 'rate': 0.85, 'name': '2026-01-01', 'company_id': self.company.id,
        })
        uk = self._marketplace('ZUK', currency=self.foreign, vat=20.0)
        self.assertAlmostEqual(self.tmpl.get_amazon_sale_price(uk), 117.30, places=2)  # 100 × 1,15 × 0,85 × 1,20

    def test_pricelist_in_wrong_currency_is_blocked(self):
        self.env['res.currency.rate'].create({
            'currency_id': self.foreign.id, 'rate': 0.85, 'name': '2026-01-01', 'company_id': self.company.id,
        })
        uk = self._marketplace('ZUK', currency=self.foreign, pricelist=self._pricelist(self.home, 10.0))
        with self.assertRaises(UserError):
            self.tmpl.get_amazon_sale_price(uk)


@tagged('post_install', '-at_install')
class TestBasePriceFromAmazon(TransactionCase):
    """Prezzo base ricavato all'indietro dal prezzo Amazon alla creazione."""

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.SyncEngine = SyncEngine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')
        self.tax = self.env['account.tax'].create({
            'name': 'ZZT IVA 22 inverso', 'amount': 22.0, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'price_include_override': 'tax_excluded',
        })
        self.env.company.account_sale_tax_id = self.tax.id
        amazon = self.env.ref('ke_amazon_sync.pricelist_amazon')
        amazon.item_ids.unlink()
        self.env['product.pricelist.item'].create({
            'pricelist_id': amazon.id, 'applied_on': '3_global', 'compute_price': 'formula',
            'base': 'list_price', 'price_discount': -15.0,
        })
        self.mp = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT IT inverso', 'code': 'ZZBI', 'marketplace_id': 'ZZ_BASE_IT', 'active': True,
            'currency_id': self.env.company.currency_id.id, 'vat_rate': 22.0,
        })

    def test_base_price_removes_vat_and_markup(self):
        from .common import FakeSpApiClient
        eng = self.SyncEngine(self.env, FakeSpApiClient(marketplace_id='ZZ_BASE_IT'))
        tmpl = self.env['product.template'].create({
            'name': 'ZZT inverso', 'list_price': 1.0, 'taxes_id': [(6, 0, [self.tax.id])],
        })
        self.assertAlmostEqual(eng._base_price_from_amazon(140.30, tmpl), 100.0, places=2)
        tmpl.list_price = 100.0
        self.assertAlmostEqual(tmpl.get_amazon_sale_price(self.mp), 140.30, places=2,
                               msg='andata e ritorno: con la sync il prezzo Amazon non cambia')

    def test_family_variants_keep_their_own_price(self):
        from .common import FakeSpApiClient, make_report_row, no_commit
        a, b = 'B0ZZBASEA1', 'B0ZZBASEB1'
        client = FakeSpApiClient({a: 'B0ZZBASEPA', b: 'B0ZZBASEPA'}, marketplace_id='ZZ_BASE_IT')
        eng = self.SyncEngine(self.env, client)
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT inverso', 'sync_type': 'full_import'})
        rows = [make_report_row('ZZT-BASE-A', asin=a, price='28.06'),
                make_report_row('ZZT-BASE-B', asin=b, price='42.09')]
        with no_commit(self.env):
            eng.full_import_report_rows(log, rows, 0, len(rows), True)
        va = self.env['product.product'].search([('default_code', '=', 'ZZT-BASE-A')])
        vb = self.env['product.product'].search([('default_code', '=', 'ZZT-BASE-B')])
        self.assertEqual(va.product_tmpl_id, vb.product_tmpl_id, 'stessa famiglia')
        self.assertAlmostEqual(va.lst_price, 20.0, places=2)   # 28,06 / 1,22 / 1,15
        self.assertAlmostEqual(vb.lst_price, 30.0, places=2)   # 42,09 / 1,22 / 1,15
        self.assertAlmostEqual(vb.get_amazon_sale_price(self.mp), 42.09, places=2)
