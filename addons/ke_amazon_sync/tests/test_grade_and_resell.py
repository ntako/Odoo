"""
Test per gli SKU Grade & Resell (`amzn.gr.…`): Amazon li crea per i resi
rivenduti come usati, sullo stesso ASIN dello SKU del venditore. Prima
venivano abbinati allo stesso prodotto e, letti per primi, ne prendevano il
posto (SKU legacy e listing del marketplace), mentre lo SKU vero finiva su
un prodotto doppione senza ASIN. Caso reale verificato su Seller Central il
2026-11-03: MGP-KIT42015 / amzn.gr.MGP-KIT42015-lZ9ONo9Bvr79U2ay-LN, ASIN
B0DGDL3BLM. Decisione con l'utente: ignorarli in tutti i flussi di import.
"""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit

REAL_SKU = 'ZZT-GR-KIT'
GR_SKU = 'amzn.gr.ZZT-GR-KIT-lZ9ONo9Bvr79U2ay-LN'
ASIN = 'B0ZZGRKIT1'


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestGradeAndResell(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services import sync_engine
        self.sync_engine = sync_engine
        self.SyncEngine = sync_engine.SyncEngine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')

    def _log(self, sync_type='full_import'):
        return self.env['ke.amazon.sync.log'].create({'name': 'ZZT G&R', 'sync_type': sync_type})

    def test_recognises_grade_and_resell_sku(self):
        is_gr = self.sync_engine._is_grade_and_resell_sku
        self.assertTrue(is_gr(GR_SKU))
        self.assertTrue(is_gr('  AMZN.GR.X-1  '))
        self.assertFalse(is_gr(REAL_SKU))
        self.assertFalse(is_gr('amzn-gr-falso'))
        self.assertFalse(is_gr(None))

    def test_full_import_keeps_real_sku_when_resell_row_comes_first(self):
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self._log()
        rows = [
            make_report_row(GR_SKU, asin=ASIN, status='Inactive', fulfillment='AMAZON_EU'),
            make_report_row(REAL_SKU, asin=ASIN, fulfillment='AMAZON_EU'),
        ]
        with no_commit(self.env):
            eng.full_import_report_rows(log, rows, 0, 10, True)

        tmpl = self.env['product.template'].search([('x_amazon_asin', '=', ASIN)])
        self.assertEqual(len(tmpl), 1)
        self.assertEqual(tmpl.x_amazon_sku, REAL_SKU, 'lo SKU usato non prende il posto di quello vero')
        self.assertFalse(
            self.env['product.product'].search([
                '|', ('default_code', 'ilike', 'amzn.gr.'), ('x_amazon_sku', 'ilike', 'amzn.gr.'),
            ]).filtered(lambda p: 'ZZT-GR-KIT' in (p.default_code or p.x_amazon_sku or '')),
            'nessun prodotto creato per lo SKU Grade & Resell',
        )
        self.assertIn('Grade & Resell', log.log_lines)

    def test_existing_resell_listing_does_not_drive_matching(self):
        # Un import precedente ha lasciato una listing amzn.gr su un altro
        # prodotto con lo stesso ASIN: non deve attirare lo SKU vero.
        stale = self.env['product.product'].create({'name': 'ZZT residuo G&R', 'type': 'consu'})
        marketplace = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT GR residuo', 'code': 'ZZTGRR', 'marketplace_id': 'ZZT_GRR_MKT', 'active': True,
        })
        self.env['ke.amazon.listing'].create({
            'product_id': stale.id, 'marketplace_id': marketplace.id, 'sku': GR_SKU, 'asin': ASIN,
        })
        maps = self.sync_engine._MatchMaps(self.env, {})
        maps.add_listing_fallback(self.env['ke.amazon.listing'].search([('sku', '=', GR_SKU)]))
        self.assertNotIn(GR_SKU, maps.by_sku)
        self.assertNotIn(ASIN, maps.by_asin)

    def test_reconcile_ignores_resell_sku(self):
        self.env['product.template'].create({
            'name': 'ZZT riconcilia', 'type': 'consu', 'is_storable': True,
            'default_code': REAL_SKU, 'x_amazon_sku': REAL_SKU, 'x_amazon_asin': ASIN,
        })
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self._log('reconcile')
        rows = [make_report_row(GR_SKU, asin=ASIN), make_report_row(REAL_SKU, asin=ASIN)]
        with no_commit(self.env):
            matched, only_odoo, only_amazon, errors = eng.reconcile_from_report_rows(log, rows)
        self.assertEqual(only_amazon, 0, 'lo SKU usato non è un "prodotto solo su Amazon"')
        self.assertIn('Grade & Resell', log.log_lines)

    def test_fnsku_import_ignores_resell_sku(self):
        tmpl = self.env['product.template'].create({
            'name': 'ZZT fnsku', 'type': 'consu', 'is_storable': True,
            'default_code': REAL_SKU, 'x_amazon_sku': REAL_SKU, 'x_amazon_asin': ASIN,
        })
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self._log('import_fnsku')
        rows = [
            {'sku': REAL_SKU, 'fnsku': 'X0ZZREALFN'},
            {'sku': GR_SKU, 'fnsku': 'X0ZZGRFNSK'},
        ]
        with no_commit(self.env):
            eng.process_fnsku_report_rows(log, rows)
        self.assertEqual(tmpl.x_amazon_fnsku, 'X0ZZREALFN', "l'FNSKU dell'usato non sovrascrive quello vero")
        self.assertIn('Grade & Resell', log.log_lines)

    def test_fba_sync_does_not_count_used_units(self):
        warehouse = self.env['stock.warehouse'].create({'name': 'ZZT WH GR', 'code': 'ZZTGR'})
        self.env.company.amazon_fba_warehouse_id = warehouse.id
        self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT GR', 'code': 'ZZTGR', 'marketplace_id': 'ZZT_GR_MKT', 'active': True,
        })
        tmpl = self.env['product.template'].create({
            'name': 'ZZT giacenza', 'type': 'consu', 'is_storable': True,
            'default_code': REAL_SKU, 'x_amazon_sku': REAL_SKU, 'x_amazon_asin': ASIN,
        })
        client = FakeSpApiClient(marketplace_id='ZZT_GR_MKT', inventory_items=[
            {'sku': REAL_SKU, 'asin': ASIN, 'qty': 9},
            {'sku': GR_SKU, 'asin': ASIN, 'qty': 2},
        ])
        eng = self.SyncEngine(self.env, client)
        log = self._log('sync_fba')
        with no_commit(self.env):
            eng.sync_fba_quantities_page(log, page_token=None, asin_totals={})
        self.assertEqual(tmpl.x_amazon_qty, 9, 'le 2 unità usate non si sommano alla giacenza del nuovo')
        self.assertIn('Grade & Resell', log.log_lines)
