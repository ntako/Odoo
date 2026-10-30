"""
Test dell'FNSKU per variante (process_fnsku_report_rows,
product.product._get_amazon_fnsku).

Prima del 2026-10-22 l'Import FNSKU saltava ogni variante di una famiglia
con più varianti (x_amazon_fnsku solo sul template, condiviso): su un giro
reale circa due terzi delle righe di ogni marketplace. Ora l'FNSKU di una
variante di famiglia va su product.product.x_amazon_fnsku.

SyncEngine(env, None): nessun client, quindi nessun marketplace corrente —
stesso ramo "pooled" di IT/ES/FR, nessuna chiamata Amazon possibile.
"""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_dedicated_marketplace, no_commit


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestFnskuVariant(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.engine = SyncEngine(self.env, None)
        self.log = self.env['ke.amazon.sync.log'].create({
            'name': 'ZZT import FNSKU', 'sync_type': 'import_fnsku',
        })

    def _make_family(self):
        attr = self.env['product.attribute'].create({
            'name': 'ZZT Colore',
            'value_ids': [(0, 0, {'name': 'Rosso'}), (0, 0, {'name': 'Blu'})],
        })
        tmpl = self.env['product.template'].create({
            'name': 'ZZT famiglia FNSKU', 'type': 'consu', 'is_storable': True,
            'x_amazon_fnsku': 'X00TEMPLATE',
            'attribute_line_ids': [(0, 0, {
                'attribute_id': attr.id, 'value_ids': [(6, 0, attr.value_ids.ids)],
            })],
        })
        red, blue = tmpl.product_variant_ids
        red.write({'x_amazon_sku': 'ZZT-FNSKU-RED'})
        blue.write({'x_amazon_sku': 'ZZT-FNSKU-BLUE'})
        return tmpl, red, blue

    def _process(self, rows):
        with no_commit(self.env):
            return self.engine.process_fnsku_report_rows(self.log, rows)

    def test_family_variants_get_their_own_fnsku(self):
        tmpl, red, blue = self._make_family()
        matched, not_found, errors = self._process([
            {'sku': 'ZZT-FNSKU-RED', 'fnsku': 'X00RED'},
            {'sku': 'ZZT-FNSKU-BLUE', 'fnsku': 'X00BLUE'},
        ])
        self.assertEqual((matched, not_found, errors), (2, 0, 0))
        self.assertEqual(red.x_amazon_fnsku, 'X00RED')
        self.assertEqual(blue.x_amazon_fnsku, 'X00BLUE')
        self.assertEqual(tmpl.x_amazon_fnsku, 'X00TEMPLATE',
                         "l'FNSKU del template non deve essere toccato per una famiglia")
        self.assertEqual(red._get_amazon_fnsku(), 'X00RED')

    def test_simple_product_still_uses_template_field(self):
        tmpl = self.env['product.template'].create({
            'name': 'ZZT semplice FNSKU', 'type': 'consu', 'is_storable': True,
            'x_amazon_sku': 'ZZT-FNSKU-SIMPLE',
        })
        self._process([{'sku': 'ZZT-FNSKU-SIMPLE', 'fnsku': 'X00SIMPLE'}])
        self.assertEqual(tmpl.x_amazon_fnsku, 'X00SIMPLE')
        self.assertFalse(tmpl.product_variant_id.x_amazon_fnsku)
        self.assertEqual(tmpl.product_variant_id._get_amazon_fnsku(), 'X00SIMPLE')

    def test_family_variant_never_falls_back_to_template_fnsku(self):
        """Etichetta: su una famiglia l'FNSKU del template non identifica
        nessuna variante — meglio vuoto (il wizard blocca) che sbagliato."""
        tmpl, red, blue = self._make_family()
        self.assertEqual(red._get_amazon_fnsku(), '')

    def test_family_variant_dedicated_marketplace_gets_own_listing_fnsku(self):
        """
        Refactor ke.amazon.listing → variante (2026-10-23): su un marketplace
        DEDICATO (non pooled, es. UK) una famiglia non viene più saltata in
        blocco — ogni variante riceve l'FNSKU sulla propria riga
        ke.amazon.listing, senza toccare x_amazon_fnsku (quello resta per i
        marketplace pooled).
        """
        tmpl, red, blue = self._make_family()
        marketplace = make_dedicated_marketplace(self.env)
        Listing = self.env['ke.amazon.listing']
        listing_red = Listing.create({
            'product_id': red.id, 'marketplace_id': marketplace.id, 'sku': 'ZZT-FNSKU-RED',
        })
        listing_blue = Listing.create({
            'product_id': blue.id, 'marketplace_id': marketplace.id, 'sku': 'ZZT-FNSKU-BLUE',
        })
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        client = FakeSpApiClient(marketplace_id=marketplace.marketplace_id)
        engine = SyncEngine(self.env, client)
        with no_commit(self.env):
            matched, not_found, errors = engine.process_fnsku_report_rows(self.log, [
                {'sku': 'ZZT-FNSKU-RED', 'fnsku': 'UK00RED'},
                {'sku': 'ZZT-FNSKU-BLUE', 'fnsku': 'UK00BLUE'},
            ])
        self.assertEqual((matched, not_found, errors), (2, 0, 0))
        self.assertEqual(listing_red.fnsku, 'UK00RED')
        self.assertEqual(listing_blue.fnsku, 'UK00BLUE')
        self.assertFalse(red.x_amazon_fnsku,
                          "il marketplace dedicato non deve toccare il campo pooled")
        self.assertFalse(blue.x_amazon_fnsku)
        self.assertEqual(tmpl.x_amazon_fnsku, 'X00TEMPLATE')

    def test_family_variant_dedicated_marketplace_without_listing_is_skipped(self):
        """Senza una riga ke.amazon.listing già esistente per quella
        variante+marketplace (nessun full import/riconciliazione ancora
        passato di lì), si salta esplicitamente invece di indovinare."""
        tmpl, red, blue = self._make_family()
        marketplace = make_dedicated_marketplace(self.env)
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        client = FakeSpApiClient(marketplace_id=marketplace.marketplace_id)
        engine = SyncEngine(self.env, client)
        with no_commit(self.env):
            matched, not_found, errors = engine.process_fnsku_report_rows(self.log, [
                {'sku': 'ZZT-FNSKU-RED', 'fnsku': 'UK00RED'},
            ])
        self.assertEqual((matched, not_found, errors), (0, 1, 0))
        self.assertFalse(red.x_amazon_fnsku)
