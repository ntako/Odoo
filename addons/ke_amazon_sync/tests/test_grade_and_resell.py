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
        self.assertIn('Usato Amazon', log.log_lines, "le unità usate vanno nella giacenza 'FBA – Usato'")


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestResellOffers(TransactionCase):
    """
    Fase 1 dell'usato (richiesta dell'utente il 2026-11-03): offerte usato
    collegate al prodotto originale e giacenza nell'ubicazione "FBA – Usato",
    una per magazzino FBA (pool condiviso, marketplace dedicati la propria).
    """

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.SyncEngine = SyncEngine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')
        self.warehouse = self.env['stock.warehouse'].create({'name': 'ZZT WH Usato', 'code': 'ZZTUS'})
        self.env.company.amazon_fba_warehouse_id = self.warehouse.id
        self.it = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT IT usato', 'code': 'ZZTUI', 'marketplace_id': 'ZZT_US_IT', 'active': True,
        })
        self.fr = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT FR usato', 'code': 'ZZTUF', 'marketplace_id': 'ZZT_US_FR', 'active': True,
        })
        self.tmpl = self.env['product.template'].create({
            'name': 'ZZT kit usato', 'type': 'consu', 'is_storable': True,
            'default_code': REAL_SKU, 'x_amazon_sku': REAL_SKU, 'x_amazon_asin': ASIN,
        })
        self.product = self.tmpl.product_variant_id

    def _log(self, sync_type):
        return self.env['ke.amazon.sync.log'].create({'name': 'ZZT usato', 'sync_type': sync_type})

    def _sync_fba(self, marketplace_code, items):
        client = FakeSpApiClient(marketplace_id=marketplace_code, inventory_items=items)
        log = self._log('sync_fba')
        with no_commit(self.env):
            self.SyncEngine(self.env, client).sync_fba_quantities_page(log, page_token=None, asin_totals={})
        return log

    def _qty(self, location):
        quant = self.env['stock.quant'].search([
            ('product_id', '=', self.product.id), ('location_id', '=', location.id),
        ])
        return sum(quant.mapped('quantity'))

    def test_original_product_from_asin_or_truncated_sku(self):
        eng = self.SyncEngine(self.env, None)
        self.assertEqual(eng._resell_original_variant(GR_SKU, ASIN), self.product)
        # Senza ASIN: SKU originale seguito da una parte casuale con '_' e '-'
        # (forma reale: amzn.gr.C4C-0MAC4PCOBK_-2-z2FzG0vyUks-LN).
        self.assertEqual(
            eng._resell_original_variant(f'amzn.gr.{REAL_SKU}_-2-z2FzG0vyUks-LN', ''), self.product,
        )
        polluted = self.env['product.template'].create({
            'name': 'ZZT residuo', 'type': 'consu', 'default_code': 'amzn.gr.ZZT-ALTRO-xxxxxxxxxxxxxxxx-LN',
            'x_amazon_sku': 'amzn.gr.ZZT-ALTRO-xxxxxxxxxxxxxxxx-LN', 'x_amazon_asin': 'B0ZZALTRO1',
        })
        self.assertFalse(
            eng._resell_original_variant('amzn.gr.ZZT-ALTRO-xxxxxxxxxxxxxxxx-LN', 'B0ZZALTRO1'),
            'mai un prodotto che ha esso stesso uno SKU Grade & Resell',
        )
        self.assertTrue(polluted)

    def test_full_import_records_resell_offer(self):
        client = FakeSpApiClient(marketplace_id='ZZT_US_IT')
        log = self._log('full_import')
        rows = [
            make_report_row(GR_SKU, asin=ASIN, price='7.50', status='Inactive', fulfillment='AMAZON_EU'),
            make_report_row(REAL_SKU, asin=ASIN, fulfillment='AMAZON_EU'),
        ]
        with no_commit(self.env):
            self.SyncEngine(self.env, client).full_import_report_rows(log, rows, 0, 10, True)
        offer = self.env['ke.amazon.resell.offer'].search([('sku', '=', GR_SKU)])
        self.assertEqual(len(offer), 1)
        self.assertEqual(offer.product_id, self.product)
        self.assertEqual(offer.marketplace_id, self.it)
        self.assertEqual(offer.condition, 'used_like_new', 'dal suffisso -LN')
        self.assertEqual(offer.status, 'inactive')
        self.assertEqual(offer.price, 7.5)
        self.assertEqual(self.tmpl.x_amazon_sku, REAL_SKU)
        self.assertIn('registrati come offerte usato', log.log_lines)

    def test_fba_sync_puts_used_units_in_used_location(self):
        log = self._sync_fba('ZZT_US_IT', [
            {'sku': REAL_SKU, 'asin': ASIN, 'qty': 9, 'condition': 'NewItem'},
            {'sku': GR_SKU, 'asin': ASIN, 'qty': 2, 'condition': 'UsedVeryGood'},
        ])
        used = self.warehouse.amazon_fba_used_location_id
        self.assertTrue(used, 'creata da sola e salvata sul magazzino FBA')
        self.assertEqual(used.location_id, self.warehouse.view_location_id, 'accanto allo stock, come il transito')
        self.assertEqual(self._qty(self.warehouse.lot_stock_id), 9, 'il nuovo non include l\'usato')
        self.assertEqual(self._qty(used), 2)
        offer = self.env['ke.amazon.resell.offer'].search([('sku', '=', GR_SKU)])
        self.assertEqual((offer.qty, offer.condition), (2, 'used_very_good'), 'condizione dall\'inventario')
        self.assertIn('Usato Amazon', log.log_lines)

        self.tmpl._compute_x_amazon_stock_by_warehouse()
        self.assertEqual(self.tmpl.x_amazon_stock_fba, 9, "'Giacenza magazzino FBA' esclude l'usato")
        self.assertEqual(self.tmpl.x_amazon_stock_fba_used, 2)

        # Venduta l'ultima unità usata: lo SKU sparisce dall'inventario.
        self._sync_fba('ZZT_US_IT', [{'sku': REAL_SKU, 'asin': ASIN, 'qty': 9, 'condition': 'NewItem'}])
        self.assertEqual(self._qty(used), 0)
        self.assertEqual(offer.qty, 0)

    def test_pooled_marketplaces_do_not_double_count_used_units(self):
        items = [{'sku': GR_SKU, 'asin': ASIN, 'qty': 2, 'condition': 'UsedLikeNew'}]
        self._sync_fba('ZZT_US_IT', items)
        self._sync_fba('ZZT_US_FR', items)
        self._sync_fba('ZZT_US_IT', items)
        self.assertEqual(self._qty(self.warehouse.amazon_fba_used_location_id), 2,
                         'stesse unità viste da IT e FR (pool): contate una volta')

    def test_dedicated_marketplace_has_its_own_used_location(self):
        uk_wh = self.env['stock.warehouse'].create({'name': 'ZZT WH UK', 'code': 'ZZTUK'})
        uk = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT UK usato', 'code': 'ZZTUU', 'marketplace_id': 'ZZT_US_UK',
            'active': True, 'fba_warehouse_id': uk_wh.id,
        })
        self._sync_fba('ZZT_US_IT', [{'sku': GR_SKU, 'asin': ASIN, 'qty': 2, 'condition': 'UsedLikeNew'}])
        self._sync_fba('ZZT_US_UK', [{'sku': GR_SKU + 'UK', 'asin': ASIN, 'qty': 1, 'condition': 'UsedGood'}])
        uk_used = uk_wh.amazon_fba_used_location_id
        self.assertTrue(uk_used, 'il magazzino UK ha la sua')
        self.assertNotEqual(uk_used, self.warehouse.amazon_fba_used_location_id)
        self.assertEqual(uk_used.location_id, uk_wh.view_location_id)
        self.assertEqual(self._qty(uk_used), 1)
        self.assertEqual(self._qty(self.warehouse.amazon_fba_used_location_id), 2)
        self.assertTrue(uk_wh.is_amazon_fba_warehouse)
        self.assertTrue(self.warehouse.is_amazon_fba_warehouse)

    def test_configured_used_location_is_respected(self):
        chosen = self.env['stock.location'].create({
            'name': 'ZZT Usato scelto', 'usage': 'internal', 'location_id': self.warehouse.view_location_id.id,
        })
        self.warehouse.amazon_fba_used_location_id = chosen
        self._sync_fba('ZZT_US_IT', [{'sku': GR_SKU, 'asin': ASIN, 'qty': 3, 'condition': 'UsedGood'}])
        self.assertEqual(self.warehouse.amazon_fba_used_location_id, chosen, 'nessuna ubicazione nuova')
        self.assertEqual(self._qty(chosen), 3)
