"""
Test per la conservazione/invalidazione dell'FNSKU — bug trovato in
revisione il 2026-10-27 (vedi TASK.md "FNSKU", REVIEW_AMAZON_2026-10-27.md
rilievo P1/P2 #7): due problemi distinti.

1. _upsert_listing scriveva sempre 'fnsku': data.get('fnsku') — ma NESSUN
   chiamante (Full Import, riconciliazione) passa mai quella chiave (viene
   dal report bulk listing, _parse_listing_report non la espone mai), quindi
   ogni singola chiamata azzerava l'FNSKU già confermato da un Import FNSKU
   precedente, anche senza alcun cambio di SKU.
2. Quando lo SKU di un prodotto/variante cambia (es. rifornimento con '+'),
   _update_product e il ramo di aggiornamento variante di
   _process_variant_listing non invalidavano mai il vecchio FNSKU (legato
   alla coppia SKU/venditore) — _do_reconcile lo faceva già per lo stesso
   caso, qui mancava.
"""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestFnskuInvalidation(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.SyncEngine = SyncEngine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')

    def _make_log(self, name):
        return self.env['ke.amazon.sync.log'].create({'name': name, 'sync_type': 'full_import'})

    def _run(self, eng, log, rows):
        with no_commit(self.env):
            return eng.full_import_report_rows(log, rows, 0, 10, True)

    # ------------------------------------------------------------------
    # 1. _upsert_listing non deve azzerare un FNSKU già scritto quando i
    #    dati in arrivo non ne contengono uno (caso normale: ogni riga del
    #    report bulk listing di Full Import/riconciliazione).
    # ------------------------------------------------------------------

    def test_upsert_listing_without_fnsku_key_preserves_existing(self):
        marketplace = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT MKT FNSKU', 'code': 'ZZTF', 'marketplace_id': 'ZZT_FNSKU_MKT', 'active': True,
        })
        product = self.env['product.product'].create({
            'name': 'ZZT prodotto fnsku', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-FNSKU-UP', 'x_amazon_sku': 'ZZT-FNSKU-UP', 'x_amazon_asin': 'B0ZZFNSKUUP',
        })
        listing = self.env['ke.amazon.listing'].create({
            'product_id': product.id, 'marketplace_id': marketplace.id,
            'sku': 'ZZT-FNSKU-UP', 'asin': 'B0ZZFNSKUUP', 'fnsku': 'X00ALREADYSET',
        })
        eng = self.SyncEngine(self.env, FakeSpApiClient(marketplace_id='ZZT_FNSKU_MKT'))
        # Dati come quelli di una riga del report bulk: nessuna chiave 'fnsku'.
        data = {'sku': 'ZZT-FNSKU-UP', 'asin': 'B0ZZFNSKUUP', 'status': 'BUYABLE',
                'fulfillment': 'fba', 'price': 9.99, 'qty_available': 3}
        eng._upsert_listing(product, data)
        listing.invalidate_recordset()
        self.assertEqual(listing.fnsku, 'X00ALREADYSET', 'un dato assente non deve azzerare un FNSKU già confermato')

    def test_upsert_listing_invalidate_fnsku_true_clears_it(self):
        marketplace = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT MKT FNSKU2', 'code': 'ZZTF2', 'marketplace_id': 'ZZT_FNSKU_MKT2', 'active': True,
        })
        product = self.env['product.product'].create({
            'name': 'ZZT prodotto fnsku2', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-FNSKU-INV', 'x_amazon_sku': 'ZZT-FNSKU-INV', 'x_amazon_asin': 'B0ZZFNSKUINV',
        })
        listing = self.env['ke.amazon.listing'].create({
            'product_id': product.id, 'marketplace_id': marketplace.id,
            'sku': 'ZZT-FNSKU-INV', 'asin': 'B0ZZFNSKUINV', 'fnsku': 'X00OLDCODE',
        })
        eng = self.SyncEngine(self.env, FakeSpApiClient(marketplace_id='ZZT_FNSKU_MKT2'))
        data = {'sku': 'ZZT-FNSKU-INV+', 'asin': 'B0ZZFNSKUINV', 'status': 'BUYABLE',
                'fulfillment': 'fba', 'price': 9.99, 'qty_available': 3}
        eng._upsert_listing(product, data, invalidate_fnsku=True)
        listing.invalidate_recordset()
        self.assertFalse(listing.fnsku, "invalidate_fnsku=True deve azzerare l'FNSKU")

    # ------------------------------------------------------------------
    # 2a. Full Import reale (prodotto semplice): stesso SKU → FNSKU
    #     conservato; SKU cambiato ('+' ) → FNSKU invalidato.
    # ------------------------------------------------------------------

    def test_full_import_same_sku_preserves_fnsku(self):
        sku = 'ZZT-FNSKU-SIMPLE-SAME'
        asin = 'B0ZZFNSKUSAME'
        tmpl = self.env['product.template'].create({
            'name': 'ZZT semplice stesso sku', 'type': 'consu', 'is_storable': True,
            'default_code': sku, 'x_amazon_sku': sku, 'x_amazon_asin': asin,
            'x_amazon_fnsku': 'X00SIMPLESAME',
        })
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self._make_log('test fnsku stesso sku')
        self._run(eng, log, [make_report_row(sku, asin=asin, price='12.00')])

        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.x_amazon_fnsku, 'X00SIMPLESAME',
                          "un Full Import con lo stesso SKU non deve mai azzerare l'FNSKU")

    def test_full_import_sku_change_invalidates_fnsku(self):
        sku = 'ZZT-FNSKU-SIMPLE-CHG'
        asin = 'B0ZZFNSKUCHG'
        tmpl = self.env['product.template'].create({
            'name': 'ZZT semplice sku cambiato', 'type': 'consu', 'is_storable': True,
            'default_code': sku, 'x_amazon_sku': sku, 'x_amazon_asin': asin,
            'x_amazon_fnsku': 'X00SIMPLEOLD',
        })
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self._make_log('test fnsku sku cambiato')
        self._run(eng, log, [make_report_row(sku + '+', asin=asin)])

        tmpl.invalidate_recordset()
        self.assertFalse(tmpl.x_amazon_fnsku,
                          "un cambio SKU deve invalidare l'FNSKU finché non arriva un nuovo Import FNSKU")

    # ------------------------------------------------------------------
    # 2b. Stesso scenario per una variante di famiglia (_process_variant_listing).
    # ------------------------------------------------------------------

    def _make_family_variant(self, sku, asin, parent):
        tmpl = self.env['product.template'].create({
            'name': 'ZZT famiglia fnsku', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': parent,
        })
        variant = tmpl.product_variant_id
        variant.write({
            'x_amazon_sku': sku, 'x_amazon_asin': asin, 'default_code': sku,
            'x_amazon_fnsku': 'X00VARIANTOLD',
        })
        return tmpl, variant

    def test_variant_update_same_sku_preserves_fnsku(self):
        sku = 'ZZT-FNSKU-VAR-SAME'
        asin = 'B0ZZFNSKUVARSAME'
        parent = 'B0ZZFNSKUVARSAMEPARENT'
        tmpl, variant = self._make_family_variant(sku, asin, parent)
        client = FakeSpApiClient({asin: parent})
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test variante fnsku stesso sku')
        self._run(eng, log, [make_report_row(sku, asin=asin, price='15.00')])

        variant.invalidate_recordset()
        self.assertEqual(variant.x_amazon_fnsku, 'X00VARIANTOLD',
                          "un aggiornamento con lo stesso SKU non deve mai azzerare l'FNSKU della variante")

    def test_variant_update_sku_change_invalidates_fnsku(self):
        sku = 'ZZT-FNSKU-VAR-CHG'
        asin = 'B0ZZFNSKUVARCHG'
        parent = 'B0ZZFNSKUVARCHGPARENT'
        tmpl, variant = self._make_family_variant(sku, asin, parent)
        client = FakeSpApiClient({asin: parent})
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test variante fnsku sku cambiato')
        self._run(eng, log, [make_report_row(sku + '+', asin=asin)])

        variant.invalidate_recordset()
        self.assertFalse(variant.x_amazon_fnsku,
                          "un cambio SKU sulla variante deve invalidare il suo FNSKU")


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestFnskuInvalidationSecondaryMarketplace(TransactionCase):
    """
    Bug trovato in revisione (2026-10-27): _update_product/
    _process_variant_listing confrontavano lo SKU in arrivo con quello
    LEGACY (primario, es. product.x_amazon_sku) per decidere se invalidare
    l'FNSKU della LISTING del marketplace corrente — ma su un marketplace
    secondario quel campo riflette l'ultimo marketplace primario, non lo
    SKU precedente di QUESTA listing, che può differire in entrambe le
    direzioni (conserva un FNSKU ormai sbagliato, o ne invalida uno ancora
    valido).
    """

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.SyncEngine = SyncEngine
        self.primary = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT Primario', 'code': 'ZZTPR', 'marketplace_id': 'ZZT_PRIMARY_MKT', 'active': True,
        })
        self.secondary = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT Secondario', 'code': 'ZZTSEC', 'marketplace_id': 'ZZT_SECONDARY_MKT', 'active': True,
        })
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', 'ZZT_PRIMARY_MKT')

    def _make_log(self, name):
        return self.env['ke.amazon.sync.log'].create({'name': name, 'sync_type': 'full_import'})

    def _run_secondary(self, rows):
        client = FakeSpApiClient(marketplace_id='ZZT_SECONDARY_MKT')
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test fnsku marketplace secondario')
        with no_commit(self.env):
            eng.full_import_report_rows(log, rows, 0, 10, True)

    def test_secondary_marketplace_unchanged_own_sku_preserves_fnsku_despite_different_primary_sku(self):
        """Il campo legacy (primario) ha uno SKU diverso da quello di
        QUESTA listing — ma per il marketplace secondario non è cambiato
        nulla: l'FNSKU deve restare."""
        tmpl = self.env['product.template'].create({
            'name': 'ZZT prodotto multi-mkt', 'type': 'consu', 'is_storable': True,
            'default_code': 'SECONDARY-OLD-SKU', 'x_amazon_sku': 'PRIMARY-SKU', 'x_amazon_asin': 'B0ZZMULTIMKT',
        })
        self.env['ke.amazon.listing'].create({
            'product_id': tmpl.product_variant_id.id, 'marketplace_id': self.secondary.id,
            'sku': 'SECONDARY-OLD-SKU', 'asin': 'B0ZZMULTIMKT', 'fnsku': 'X00SECONDARYVALID',
        })
        self._run_secondary([make_report_row('SECONDARY-OLD-SKU', asin='B0ZZMULTIMKT', price='12.00')])

        listing = self.env['ke.amazon.listing'].search([
            ('product_id', '=', tmpl.product_variant_id.id), ('marketplace_id', '=', self.secondary.id),
        ])
        self.assertEqual(listing.fnsku, 'X00SECONDARYVALID',
                          "confrontare con lo SKU primario invece che con quello della listing "
                          "invalidava un FNSKU ancora valido sul marketplace secondario")

    def test_secondary_marketplace_real_sku_change_invalidates_fnsku_despite_matching_primary_sku(self):
        """Il nuovo SKU coincide per caso con quello legacy (primario) — ma
        per QUESTA listing è comunque un cambio reale: l'FNSKU va invalidato."""
        tmpl = self.env['product.template'].create({
            'name': 'ZZT prodotto multi-mkt 2', 'type': 'consu', 'is_storable': True,
            'default_code': 'PRIMARY-SKU', 'x_amazon_sku': 'PRIMARY-SKU', 'x_amazon_asin': 'B0ZZMULTIMKT2',
        })
        self.env['ke.amazon.listing'].create({
            'product_id': tmpl.product_variant_id.id, 'marketplace_id': self.secondary.id,
            'sku': 'SECONDARY-OLD-SKU-2', 'asin': 'B0ZZMULTIMKT2', 'fnsku': 'X00SECONDARYSTALE',
        })
        # La riga del marketplace secondario porta il NUOVO sku, che
        # coincide col primario solo per coincidenza testuale.
        self._run_secondary([make_report_row('PRIMARY-SKU', asin='B0ZZMULTIMKT2', price='12.00')])

        listing = self.env['ke.amazon.listing'].search([
            ('product_id', '=', tmpl.product_variant_id.id), ('marketplace_id', '=', self.secondary.id),
        ])
        self.assertFalse(listing.fnsku,
                          "confrontare con lo SKU primario (che per coincidenza combaciava) "
                          "conservava un FNSKU ormai sbagliato sul marketplace secondario")


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestFnskuSingleVariantFamilyTemplateFallback(TransactionCase):
    """
    Bug trovato in revisione (2026-10-27): su una famiglia con UNA sola
    variante, invalidare solo x_amazon_fnsku della variante non basta —
    product.product._get_amazon_fnsku() ricade sul campo del TEMPLATE
    quando quello della variante è vuoto, quindi un FNSKU stantio lì
    restava comunque recuperabile e stampabile.
    """

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.SyncEngine = SyncEngine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')

    def _make_log(self, name):
        return self.env['ke.amazon.sync.log'].create({'name': name, 'sync_type': 'full_import'})

    def test_sku_change_on_single_variant_family_clears_template_fallback_too(self):
        sku = 'ZZT-FNSKU-1VAR'
        asin = 'B0ZZFNSKU1VAR'
        parent = 'B0ZZFNSKU1VARPARENT'
        tmpl = self.env['product.template'].create({
            'name': 'ZZT famiglia a variante unica', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': parent,
            # Stantio: scritto in passato da process_fnsku_report_rows sul
            # ramo pooled-famiglia-a-variante-unica (scrive sul TEMPLATE in
            # quel caso, non sulla variante — vedi process_fnsku_report_rows).
            'x_amazon_fnsku': 'X00TEMPLATESTALE',
        })
        variant = tmpl.product_variant_id
        variant.write({'x_amazon_sku': sku, 'x_amazon_asin': asin, 'default_code': sku})
        self.assertEqual(variant._get_amazon_fnsku(), 'X00TEMPLATESTALE',
                          'baseline: il fallback sul template funziona prima del cambio SKU')

        client = FakeSpApiClient({asin: parent})
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test fallback template famiglia singola')
        with no_commit(self.env):
            eng.full_import_report_rows(log, [make_report_row(sku + '+', asin=asin)], 0, 10, True)

        variant.invalidate_recordset()
        tmpl.invalidate_recordset()
        self.assertFalse(variant.x_amazon_fnsku)
        self.assertFalse(tmpl.x_amazon_fnsku, 'il fallback sul template deve essere azzerato anche lui')
        self.assertEqual(variant._get_amazon_fnsku(), '',
                          "senza azzerare anche il template, l'FNSKU stantio resterebbe stampabile")
