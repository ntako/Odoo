from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestArchivePolicy(TransactionCase):
    """Riattivazione dei prodotti con inserzioni e filtro "Candidati all'archiviazione" (2026-11-19)."""

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.engine = SyncEngine(self.env, None)
        self.mp = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT ES arch', 'code': 'ZZAR', 'marketplace_id': 'ZZ_ARCH_ES', 'active': True})

    def _product(self, name, status='inactive', barcode=False):
        tmpl = self.env['product.template'].create({'name': name, 'barcode': barcode, 'is_storable': True})
        self.env['ke.amazon.listing'].create({
            'product_id': tmpl.product_variant_id.id, 'marketplace_id': self.mp.id,
            'sku': f'ZZT-{tmpl.id}', 'asin': f'B0ZZ{tmpl.id:06d}', 'status': status})
        return tmpl

    def test_reactivates_archived_products_with_inactive_listings(self):
        tmpl = self._product('ZZT esaurito')
        variant = tmpl.product_variant_id
        tmpl.action_archive()
        self.assertFalse(variant.active)
        n_tmpl, n_var, skipped, lines = self.engine.reactivate_listed_products()
        self.assertTrue(tmpl.active)
        self.assertTrue(variant.active)
        self.assertGreaterEqual(n_tmpl, 1)
        self.assertTrue(any('ZZT esaurito' in l and l.startswith('RIATTIVATO') for l in lines))

    def test_skips_probable_duplicate_by_ean(self):
        old = self._product('ZZT doppione vecchio', barcode='9990000000017')
        old.action_archive()
        twin = self.env['product.template'].create({'name': 'ZZT doppione attivo'})
        # Stesso EAN su un prodotto attivo (scritto in SQL: non dipende dal controllo di unicità di Odoo).
        self.env.cr.execute('UPDATE product_product SET barcode = %s WHERE id = %s',
                            ('9990000000017', twin.product_variant_id.id))
        self.env.invalidate_all()
        _n_tmpl, _n_var, skipped, lines = self.engine.reactivate_listed_products()
        self.assertFalse(old.active)
        self.assertGreaterEqual(skipped, 1)
        self.assertTrue(any('9990000000017' in l and l.startswith('SALTATO') for l in lines))

    def test_archive_candidate_filter(self):
        idle = self._product('ZZT fermo')
        selling = self._product('ZZT attivo su Amazon', status='active')
        stocked = self._product('ZZT con giacenza')
        stock = self.env.ref('stock.stock_location_stock')
        self.env['stock.quant']._update_available_quantity(stocked.product_variant_id, stock, 3)
        found = self.env['product.template'].search([('ke_archive_candidate', '=', True)])
        self.assertIn(idle, found)
        self.assertNotIn(selling, found)
        self.assertNotIn(stocked, found)
        self.assertTrue(idle.ke_archive_candidate)
        self.assertFalse(stocked.ke_archive_candidate)
