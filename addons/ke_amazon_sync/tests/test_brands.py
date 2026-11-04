"""Marca di catalogo (ke_product_catalog) assegnata dall'import Amazon (2026-11-04)."""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestBrands(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services import sync_engine
        self.se = sync_engine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')
        self.mkt = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT IT marche', 'code': 'ZZBR', 'marketplace_id': 'ZZ_BRAND_IT', 'active': True,
        })
        self.Brand = self.env['ke.product.brand']

    def _client(self, brands):
        class Client(FakeSpApiClient):
            def get_catalog_items_batch(self, asins):
                out = super().get_catalog_items_batch(asins)
                for a in asins:
                    if a in brands:
                        out[a]['summaries'] = [{'brand': brands[a]}]
                return out
        return Client(marketplace_id='ZZ_BRAND_IT')

    def _run(self, brands, rows):
        eng = self.se.SyncEngine(self.env, self._client(brands))
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT marche', 'sync_type': 'full_import'})
        with no_commit(self.env):
            eng.full_import_report_rows(log, rows, 0, len(rows), True)
        return eng

    def _tmpl(self, sku):
        return self.env['product.template'].search([('default_code', '=', sku)])

    def test_import_assigns_one_brand_for_different_spellings(self):
        self._run({'B0ZZBR1': 'ZZT Yamashita', 'B0ZZBR2': 'ZZT YAMASHITA'}, [
            make_report_row('ZZT-BR-1', asin='B0ZZBR1'), make_report_row('ZZT-BR-2', asin='B0ZZBR2'),
        ])
        b1, b2 = self._tmpl('ZZT-BR-1').ke_brand_id, self._tmpl('ZZT-BR-2').ke_brand_id
        self.assertTrue(b1)
        self.assertEqual(b1, b2)
        listing = self._tmpl('ZZT-BR-2').product_variant_id.x_amazon_listing_ids
        self.assertEqual(listing.brand, 'ZZT YAMASHITA', "la marca esatta di Amazon resta sull'annuncio")

    def test_confirmed_brand_not_changed_and_backfill(self):
        mine = self.Brand.create({'name': 'ZZT Marca Mia'})
        eng = self._run({'B0ZZBR3': 'ZZT Altra'}, [make_report_row('ZZT-BR-3', asin='B0ZZBR3')])
        tmpl = self._tmpl('ZZT-BR-3')
        auto = tmpl.ke_brand_id
        self.assertTrue(auto.to_review)
        # Dopo un'unione la marca Amazon "ZZT Altra" diventa alternativa di "ZZT Marca Mia".
        (auto | mine)._merge_into(mine)
        self.assertEqual(tmpl.ke_brand_id, mine)
        tmpl.ke_brand_id = False
        eng.assign_brands_from_amazon()
        self.assertEqual(tmpl.ke_brand_id, mine, 'riconosciuta dal nome alternativo')
        self.assertEqual(mine.amazon_name, 'ZZT Altra', 'nome su Amazon proposto dalla marca Amazon')
        other = self.Brand.create({'name': 'ZZT Scelta a mano'})
        tmpl.ke_brand_id = other
        eng.assign_brands_from_amazon()
        self.assertEqual(tmpl.ke_brand_id, other, 'una marca confermata non si cambia')
