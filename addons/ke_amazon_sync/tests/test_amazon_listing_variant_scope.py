"""
Test di regressione per il refactor ke.amazon.listing → variante
(2026-10-23, vedi TASK.md). Prima del refactor `product_id` puntava a
product.template: una famiglia con più varianti aveva UNA SOLA riga
ke.amazon.listing condivisa per marketplace, sovrascritta silenziosamente
dall'ultima variante processata ("clobbering") sia durante il Full Import
che durante la riconciliazione (_do_reconcile).

Ogni chiamata a full_import_report_rows/_do_reconcile è avvolta in
`no_commit(self.env)` (vedi common.py) — quelle funzioni fanno
`env.cr.commit()` internamente, non permesso dentro un test.
"""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestAmazonListingVariantScope(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.SyncEngine = SyncEngine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')
        self.marketplace = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT Listing Scope', 'code': 'ZZTLS', 'marketplace_id': 'ZZT_MKT_LISTING_SCOPE',
            'active': True,
        })

    def _make_log(self, sync_type='full_import'):
        return self.env['ke.amazon.sync.log'].create({'name': f'ZZT {sync_type}', 'sync_type': sync_type})

    # ------------------------------------------------------------------
    # Full Import: _process_variant_listing crea una riga per variante
    # ------------------------------------------------------------------

    def test_full_import_creates_one_listing_row_per_sibling_variant(self):
        parent = 'B0ZZLSPARENT'
        client = FakeSpApiClient(
            {'B0ZZLSCHILDA': parent, 'B0ZZLSCHILDB': parent},
            marketplace_id=self.marketplace.marketplace_id,
        )
        eng = self.SyncEngine(self.env, client)
        log = self._make_log()
        rows = [
            make_report_row('ZZT-LS-A', asin='B0ZZLSCHILDA', price='10.00'),
            make_report_row('ZZT-LS-B', asin='B0ZZLSCHILDB', price='20.00'),
        ]
        with no_commit(self.env):
            eng.full_import_report_rows(log, rows, 0, 10, True)

        tmpl = self.env['product.template'].search([('x_amazon_parent_asin', '=', parent)])
        self.assertEqual(len(tmpl), 1)
        self.assertEqual(len(tmpl.product_variant_ids), 2)

        variant_a = tmpl.product_variant_ids.filtered(lambda v: v.x_amazon_sku == 'ZZT-LS-A')
        variant_b = tmpl.product_variant_ids.filtered(lambda v: v.x_amazon_sku == 'ZZT-LS-B')
        listings = self.env['ke.amazon.listing'].search([
            ('marketplace_id', '=', self.marketplace.id),
            ('product_id', 'in', (variant_a + variant_b).ids),
        ])
        self.assertEqual(len(listings), 2, 'ogni variante deve avere la propria riga listing')
        listing_a = listings.filtered(lambda l: l.product_id == variant_a)
        listing_b = listings.filtered(lambda l: l.product_id == variant_b)
        self.assertEqual(listing_a.sku, 'ZZT-LS-A')
        self.assertEqual(listing_a.price, 10.00)
        self.assertEqual(listing_b.sku, 'ZZT-LS-B')
        self.assertEqual(listing_b.price, 20.00)

    # ------------------------------------------------------------------
    # _do_reconcile: fix diretto del bug di clobbering (ramo varianti)
    # ------------------------------------------------------------------

    def test_do_reconcile_keeps_separate_listing_per_sibling_variant(self):
        attr = self.env['product.attribute'].create({
            'name': 'ZZT LS Colore',
            'value_ids': [(0, 0, {'name': 'Rosso'}), (0, 0, {'name': 'Blu'})],
        })
        tmpl = self.env['product.template'].create({
            'name': 'ZZT LS famiglia', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': 'B0ZZLSRPARENT',
            'attribute_line_ids': [(0, 0, {
                'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)],
            })],
        })
        red, blue = tmpl.product_variant_ids
        red.write({'x_amazon_sku': 'ZZT-LSR-RED', 'default_code': 'ZZT-LSR-RED'})
        blue.write({'x_amazon_sku': 'ZZT-LSR-BLUE', 'default_code': 'ZZT-LSR-BLUE'})

        client = FakeSpApiClient(marketplace_id=self.marketplace.marketplace_id)
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('reconcile')

        amazon_by_sku = {
            'ZZT-LSR-RED': {
                'sku': 'ZZT-LSR-RED', 'asin': 'B0ZZLSRRED', 'status': 'BUYABLE',
                'price': 11.11, 'fulfillment': 'fba', 'product_type': '',
                'category': '', 'browse_node_id': '', 'qty_available': 1,
            },
            'ZZT-LSR-BLUE': {
                'sku': 'ZZT-LSR-BLUE', 'asin': 'B0ZZLSRBLUE', 'status': 'BUYABLE',
                'price': 22.22, 'fulfillment': 'fba', 'product_type': '',
                'category': '', 'browse_node_id': '', 'qty_available': 2,
            },
        }
        with no_commit(self.env):
            eng._do_reconcile(log, amazon_by_sku, {})

        listings = self.env['ke.amazon.listing'].search([
            ('marketplace_id', '=', self.marketplace.id),
            ('product_id', 'in', (red + blue).ids),
        ])
        self.assertEqual(len(listings), 2,
                          'bug di clobbering: una riga condivisa sovrascriverebbe red/blue a vicenda')
        listing_red = listings.filtered(lambda l: l.product_id == red)
        listing_blue = listings.filtered(lambda l: l.product_id == blue)
        self.assertEqual(listing_red.price, 11.11)
        self.assertEqual(listing_blue.price, 22.22)
        self.assertNotEqual(listing_red.id, listing_blue.id)

    # ------------------------------------------------------------------
    # detect_duplicate_skus: query via JOIN product_product/product_template
    # ------------------------------------------------------------------

    def test_detect_duplicate_skus_still_finds_duplicates_via_listing(self):
        eng = self.SyncEngine(self.env, None)
        log = self._make_log('full_import')

        tmpl_a = self.env['product.template'].create({
            'name': 'ZZT LS dup A', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-LS-DUP',
        })
        tmpl_b = self.env['product.template'].create({
            'name': 'ZZT LS dup B', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-LS-DUP',
        })
        self.env['ke.amazon.listing'].create({
            'product_id': tmpl_a.product_variant_id.id,
            'marketplace_id': self.marketplace.id,
            'sku': 'ZZT-LS-DUP',
        })
        self.env['ke.amazon.listing'].create({
            'product_id': tmpl_b.product_variant_id.id,
            'marketplace_id': self.marketplace.id,
            'sku': 'ZZT-LS-DUP',
        })
        self.env.flush_all()

        with no_commit(self.env):
            n_groups = eng.detect_duplicate_skus(log)
        self.assertEqual(n_groups, 1)
        self.assertIn('ZZT-LS-DUP', log.log_lines)
        self.assertIn(f'id={tmpl_a.id}', log.log_lines)
        self.assertIn(f'id={tmpl_b.id}', log.log_lines)

    # ------------------------------------------------------------------
    # _merge_manual_into_variant: listing del manuale eliminato per id
    # variante, non più per id template
    # ------------------------------------------------------------------

    def test_merge_manual_unlinks_its_own_variant_listing(self):
        eng = self.SyncEngine(self.env, FakeSpApiClient())

        manual = self.env['product.template'].create({
            'name': 'ZZT LS manuale', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-LS-MANUAL', 'barcode': '1234567890123',
        })
        manual_listing = self.env['ke.amazon.listing'].create({
            'product_id': manual.product_variant_id.id,
            'marketplace_id': self.marketplace.id,
            'sku': 'ZZT-LS-MANUAL-OLD',
        })
        target_tmpl = self.env['product.template'].create({
            'name': 'ZZT LS target', 'type': 'consu', 'is_storable': True,
            'x_amazon_sku': 'ZZT-LS-TARGET', 'default_code': 'ZZT-LS-TARGET',
        })
        target_variant = target_tmpl.product_variant_id

        from odoo.addons.ke_amazon_sync.services.sync_engine import _MatchMaps
        maps = _MatchMaps(self.env, {})
        eng._merge_manual_into_variant(maps, manual, target_variant, 'EAN')

        self.assertFalse(manual_listing.exists(),
                          'la riga listing del manuale deve sparire, non restare orfana')
        self.assertFalse(manual.active)
