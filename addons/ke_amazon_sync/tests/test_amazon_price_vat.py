"""
Test per il calcolo IVA del prezzo seedato alla creazione (_net_price,
_correct_seeded_list_price) — bug trovato in revisione (TASK.md "Prezzi"):
_net_price usava sempre l'unica aliquota di default configurata, mai la
tassa di vendita reale del prodotto, a differenza del calcolo inverso
(compute_amazon_sale_price, product_template.py) che la legge già
correttamente. Per una categoria con IVA ridotta (4%/10%) il list_price
netto calcolato alla creazione risultava sbagliato.
"""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestAmazonPriceVat(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.SyncEngine = SyncEngine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.default_vat_rate', '22')

    def _make_log(self, name):
        return self.env['ke.amazon.sync.log'].create({'name': name, 'sync_type': 'full_import'})

    def _make_tax(self, rate, price_include=False):
        # 'price_include' è un campo calcolato in Odoo 19 (da
        # price_include_override + default azienda) — non scrivibile
        # direttamente, va impostato tramite l'override esplicito.
        return self.env['account.tax'].create({
            'name': f'ZZT IVA {rate}%', 'amount': rate, 'amount_type': 'percent',
            'type_tax_use': 'sale',
            'price_include_override': 'tax_included' if price_include else 'tax_excluded',
        })

    # ------------------------------------------------------------------
    # _net_price
    # ------------------------------------------------------------------

    def test_net_price_without_product_uses_default_rate(self):
        eng = self.SyncEngine(self.env, None)
        self.assertAlmostEqual(eng._net_price(122.0), 100.0, places=2)

    def test_net_price_with_product_uses_real_rate_not_default(self):
        tax10 = self._make_tax(10.0, price_include=False)
        tmpl = self.env['product.template'].create({
            'name': 'ZZT prodotto iva reale', 'type': 'consu', 'is_storable': True,
            'taxes_id': [(6, 0, [tax10.id])],
        })
        eng = self.SyncEngine(self.env, None)
        # Netto corretto con IVA 10% reale, non con il 22% di default.
        self.assertAlmostEqual(eng._net_price(110.0, product=tmpl), 100.0, places=2)

    def test_net_price_with_product_price_include_returns_gross_unchanged(self):
        """price_include=True: list_price per questo prodotto è già nella
        convenzione lorda (stessa lettura di compute_amazon_sale_price) —
        niente da scorporare, va scritto il lordo così com'è."""
        tax22_incl = self._make_tax(22.0, price_include=True)
        tmpl = self.env['product.template'].create({
            'name': 'ZZT prodotto iva inclusa', 'type': 'consu', 'is_storable': True,
            'taxes_id': [(6, 0, [tax22_incl.id])],
        })
        eng = self.SyncEngine(self.env, None)
        self.assertEqual(eng._net_price(122.0, product=tmpl), 122.0)

    # ------------------------------------------------------------------
    # _correct_seeded_list_price
    # ------------------------------------------------------------------

    def test_correct_seeded_list_price_fixes_reduced_rate(self):
        tax10 = self._make_tax(10.0, price_include=False)
        tmpl = self.env['product.template'].create({
            'name': 'ZZT da correggere', 'type': 'consu', 'is_storable': True,
            'taxes_id': [(6, 0, [tax10.id])],
            # Simula il valore seedato alla creazione con l'aliquota di
            # default (22%) usata da _net_price senza 'product'.
            'list_price': 90.16,
        })
        eng = self.SyncEngine(self.env, None)
        eng._correct_seeded_list_price(tmpl, 110.0)
        self.assertAlmostEqual(tmpl.list_price, 100.0, places=2)

    def test_correct_seeded_list_price_no_write_when_same_rate(self):
        tax22 = self._make_tax(22.0, price_include=False)
        tmpl = self.env['product.template'].create({
            'name': 'ZZT stessa aliquota', 'type': 'consu', 'is_storable': True,
            'taxes_id': [(6, 0, [tax22.id])],
            'list_price': 100.0,
        })
        eng = self.SyncEngine(self.env, None)
        eng._correct_seeded_list_price(tmpl, 122.0)
        self.assertAlmostEqual(tmpl.list_price, 100.0, places=2)

    def test_correct_seeded_list_price_no_tax_leaves_default_approximation(self):
        tmpl = self.env['product.template'].create({
            'name': 'ZZT senza tassa', 'type': 'consu', 'is_storable': True,
            'taxes_id': [(6, 0, [])],
            'list_price': 90.16,
        })
        eng = self.SyncEngine(self.env, None)
        eng._correct_seeded_list_price(tmpl, 110.0)
        self.assertAlmostEqual(tmpl.list_price, 90.16, places=2,
                                msg="senza una tassa reale assegnata, l'approssimazione di default resta")

    # ------------------------------------------------------------------
    # Integrazione: Full Import reale, prodotto creato con una tassa di
    # vendita aziendale diversa dall'aliquota di default configurata.
    # ------------------------------------------------------------------

    def test_full_import_creates_product_with_corrected_list_price(self):
        tax10 = self._make_tax(10.0, price_include=False)
        self.env.company.account_sale_tax_id = tax10.id

        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self._make_log('test prezzo iva reale full import')
        self._run(eng, log, [make_report_row('ZZT-PREZZO-IVA10', asin='B0ZZPREZZOIVA10', price='110.00')])

        tmpl = self.env['product.template'].search([('default_code', '=', 'ZZT-PREZZO-IVA10')])
        self.assertTrue(tmpl, 'il prodotto deve essere stato creato')
        self.assertIn(10.0, tmpl.taxes_id.mapped('amount'), 'deve aver ricevuto la tassa aziendale reale')
        self.assertAlmostEqual(
            tmpl.list_price, 100.0, places=2,
            msg='con IVA reale al 10% (non il 22% di default) il netto di 110 lordi deve essere 100',
        )

    def _run(self, eng, log, rows):
        with no_commit(self.env):
            return eng.full_import_report_rows(log, rows, 0, 10, True)
