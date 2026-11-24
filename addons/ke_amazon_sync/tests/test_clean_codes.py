"""Codici prodotto senza "+" finale: gli SKU Amazon restano sulle inserzioni (2026-11-20)."""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit


@tagged('post_install', '-at_install')
class TestCleanCodes(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.SyncEngine = SyncEngine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')
        self.Tmpl = self.env['product.template']

    def _import(self, rows):
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZC', 'sync_type': 'full_import'})
        with no_commit(self.env):
            eng.full_import_report_rows(log, rows, 0, 50, True)
        return log

    def test_cleanup_keeps_skus_and_reports_doubles(self):
        a = self.Tmpl.create({'name': 'ZZC A', 'default_code': 'ZZC-1+', 'x_amazon_sku': 'ZZC-1+'})
        b = self.Tmpl.create({'name': 'ZZC B', 'default_code': 'ZZC-2.'})
        d1 = self.Tmpl.create({'name': 'ZZC D1', 'default_code': 'ZZC-3'})
        d2 = self.Tmpl.create({'name': 'ZZC D2', 'default_code': 'ZZC-3+'})
        n, lines = self.SyncEngine(self.env, None).clean_product_codes(dry_run=True)
        self.assertEqual(a.default_code, 'ZZC-1+', 'solo verifica: niente cambiato')
        self.assertTrue(any('ZZC-3' in l and 'DOPPIONE' in l for l in lines))
        self.SyncEngine(self.env, None).clean_product_codes(dry_run=False)
        self.env.invalidate_all()
        self.assertEqual((a.default_code, a.x_amazon_sku), ('ZZC-1', 'ZZC-1+'), 'lo SKU Amazon resta')
        self.assertEqual(b.default_code, 'ZZC-2.', 'il "." resta: è un altro articolo')
        self.assertEqual((d1.default_code, d2.default_code), ('ZZC-3', 'ZZC-3+'), 'doppione: non toccato')

    def test_full_import_writes_clean_code_and_matches_it(self):
        self._import([make_report_row('ZZC-NEW+', asin='B0ZZCNEW01')])
        tmpl = self.Tmpl.search([('x_amazon_sku', '=', 'ZZC-NEW+')])
        self.assertEqual(len(tmpl), 1)
        self.assertEqual(tmpl.default_code, 'ZZC-NEW', 'codice senza "+"')
        # Lo stesso articolo con lo SKU vecchio (senza "+") e di nuovo quello col "+": nessun doppione,
        # il codice non cambia.
        before = self.Tmpl.search_count([])
        log = self._import([make_report_row('ZZC-NEW+', asin='B0ZZCNEW01'), make_report_row('ZZC-NEW')])
        self.assertEqual(self.Tmpl.search_count([]), before, log.log_lines)
        self.assertEqual(tmpl.default_code, 'ZZC-NEW')

    def test_manual_product_with_clean_code_is_matched(self):
        manual = self.Tmpl.create({'name': 'ZZC manuale', 'default_code': 'ZZC-MAN'})
        before = self.Tmpl.search_count([])
        self._import([make_report_row('ZZC-MAN+', asin='B0ZZCMAN01')])
        self.assertEqual(self.Tmpl.search_count([]), before, 'trovato per codice senza "+"')
        self.assertEqual((manual.x_amazon_sku, manual.default_code), ('ZZC-MAN+', 'ZZC-MAN'))
