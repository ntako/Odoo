"""
Test per le correzioni al Full Import del 2026-11-03, emerse dall'import
da capo sul Docker (caduto per un deadlock, e con ~2.700 righe ES saltate):
- una riga di un marketplace secondario la cui variante esiste già in Odoo
  ma che Amazon, su quel marketplace, colloca in una famiglia diversa:
  l'annuncio del marketplace va registrato sulla variante esistente invece
  di saltare la riga;
- ogni riga in un proprio savepoint: un errore non blocca le successive;
- deadlock/serializzazione: la riga viene ripetuta;
- niente messaggi automatici "creato" nello storico dei prodotti importati.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit


class _PgError(Exception):
    """Errore finto con il codice SQLSTATE come quelli di psycopg2."""

    def __init__(self, pgcode):
        super().__init__(f'errore simulato {pgcode}')
        self.pgcode = pgcode


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestFullImportRobustness(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.SyncEngine = SyncEngine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')
        self.it = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT IT rob', 'code': 'ZZRI', 'marketplace_id': 'ZZ_ROB_IT', 'active': True,
        })
        self.es = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT ES rob', 'code': 'ZZRE', 'marketplace_id': 'ZZ_ROB_ES', 'active': True,
        })

    def _run(self, client, rows, log_name='ZZT robustezza'):
        log = self.env['ke.amazon.sync.log'].create({'name': log_name, 'sync_type': 'full_import'})
        eng = self.SyncEngine(self.env, client)
        with no_commit(self.env):
            eng.full_import_report_rows(log, rows, 0, len(rows), True)
        return log, eng

    def _listing(self, variant, marketplace):
        return self.env['ke.amazon.listing'].search([
            ('product_id', '=', variant.id), ('marketplace_id', '=', marketplace.id),
        ])

    def test_secondary_marketplace_with_different_family_attaches_listing(self):
        # IT crea la famiglia A con il figlio; ES dice che lo stesso figlio
        # sta nella famiglia B (caso reale Savage Gear del 2026-11-03).
        child, sibling = 'B0ZZROBCH1', 'B0ZZROBCH2'
        it_client = FakeSpApiClient({child: 'B0ZZROBPARA', sibling: 'B0ZZROBPARA'}, marketplace_id='ZZ_ROB_IT')
        self._run(it_client, [
            make_report_row('ZZT-ROB-1', asin=child), make_report_row('ZZT-ROB-2', asin=sibling),
        ])
        variant = self.env['product.product'].search([('x_amazon_asin', '=', child)])
        self.assertEqual(len(variant), 1)
        family_before = variant.product_tmpl_id

        es_client = FakeSpApiClient({child: 'B0ZZROBPARB'}, marketplace_id='ZZ_ROB_ES')
        log, _eng = self._run(es_client, [make_report_row('ZZT-ROB-1', asin=child, name='Nombre ES')])

        listing = self._listing(variant, self.es)
        self.assertEqual(len(listing), 1, "l'annuncio ES è registrato sulla variante esistente")
        self.assertEqual(listing.sku, 'ZZT-ROB-1')
        self.assertEqual(variant.product_tmpl_id, family_before, 'struttura della famiglia non modificata')
        self.assertFalse(
            self.env['product.template'].search([('x_amazon_parent_asin', '=', 'B0ZZROBPARB')]),
            'nessuna seconda famiglia creata',
        )
        self.assertIn('COLLEGATO (famiglia diversa su ZZRE)', log.log_lines)
        self.assertEqual(log.products_updated, 1)

    def test_different_family_with_different_sku_creates_no_empty_family(self):
        # Import del 2026-11-04: su ES la prima riga di una famiglia "diversa"
        # aveva uno SKU diverso da IT → nessun conflitto di SKU → la famiglia
        # ES veniva creata e restava vuota (325 famiglie fantasma).
        child, sibling = 'B0ZZROBSK1', 'B0ZZROBSK2'
        it_client = FakeSpApiClient({child: 'B0ZZROBSKPA', sibling: 'B0ZZROBSKPA'}, marketplace_id='ZZ_ROB_IT')
        self._run(it_client, [
            make_report_row('ZZT-ROBSK-1', asin=child), make_report_row('ZZT-ROBSK-2', asin=sibling),
        ])
        variant = self.env['product.product'].search([('x_amazon_asin', '=', child)])
        templates_before = self.env['product.template'].search_count([])

        es_client = FakeSpApiClient({child: 'B0ZZROBSKPB'}, marketplace_id='ZZ_ROB_ES')
        log, _eng = self._run(es_client, [make_report_row('ZZT-ROBSK-1-ES', asin=child)])

        self.assertFalse(
            self.env['product.template'].search([('x_amazon_parent_asin', '=', 'B0ZZROBSKPB')]),
            'nessuna famiglia vuota creata',
        )
        self.assertEqual(self.env['product.template'].search_count([]), templates_before)
        self.assertEqual(self._listing(variant, self.es).sku, 'ZZT-ROBSK-1-ES')
        self.assertIn('COLLEGATO (famiglia diversa su ZZRE)', log.log_lines)

    def test_row_without_asin_attaches_listing_to_variant_by_sku(self):
        # Report FR del 2026-11-04: ASIN vuoto per 4.051 righe su 8.254,
        # quasi tutte varianti di famiglia → prima la riga era solo saltata.
        child, sibling = 'B0ZZROBNA1', 'B0ZZROBNA2'
        it_client = FakeSpApiClient({child: 'B0ZZROBNAPA', sibling: 'B0ZZROBNAPA'}, marketplace_id='ZZ_ROB_IT')
        self._run(it_client, [
            make_report_row('ZZT-ROBNA-1', asin=child), make_report_row('ZZT-ROBNA-2', asin=sibling),
        ])
        variant = self.env['product.product'].search([('x_amazon_asin', '=', child)])
        family = variant.product_tmpl_id
        variant_count = len(family.product_variant_ids)

        es_client = FakeSpApiClient(marketplace_id='ZZ_ROB_ES')
        log, _eng = self._run(es_client, [make_report_row('ZZT-ROBNA-1', asin='', price='12.50')])

        listing = self._listing(variant, self.es)
        self.assertEqual((listing.sku, listing.asin, listing.price), ('ZZT-ROBNA-1', child, 12.5))
        self.assertEqual(len(family.product_variant_ids), variant_count, 'famiglia non modificata')
        self.assertEqual(log.products_skipped, 0)
        # Dal 2026-11-06 la riga prende l'ASIN dello stesso SKU (altro
        # marketplace) e si collega per ASIN, prima per SKU: stesso esito.
        self.assertIn('COLLEGATO', log.log_lines)
        self.assertIn('ZZT-ROB', log.log_lines)

    def test_row_without_asin_fills_listing_asin_from_same_sku(self):
        # arundo_prod 2026-11-05: 1.577 annunci FR senza ASIN su prodotti
        # trovati per SKU, mentre lo stesso SKU su ES/UK aveva l'ASIN.
        it_client = FakeSpApiClient(marketplace_id='ZZ_ROB_IT')
        self._run(it_client, [make_report_row('ZZT-ROBFA-1', asin='B0ZZROBFA1')])
        product = self.env['ke.amazon.listing'].search([('sku', '=', 'ZZT-ROBFA-1')]).product_id
        self.assertTrue(product)
        self.env['ke.amazon.listing'].create({
            'product_id': product.id, 'marketplace_id': self.es.id, 'sku': 'ZZT-ROBFA-1',
        })
        self._run(FakeSpApiClient(marketplace_id='ZZ_ROB_ES'), [make_report_row('ZZT-ROBFA-1', asin='')])
        self.assertEqual(self._listing(product, self.es).asin, 'B0ZZROBFA1')

    def test_row_without_asin_matches_variant_by_internal_code(self):
        # arundo_prod 2026-11-04: due SKU sullo stesso ASIN ("TUB-20104" e
        # "TUB-20104-in comune"), sulla variante resta l'ultimo; la riga FR
        # senza ASIN con il primo SKU creava un prodotto doppio.
        child, sibling = 'B0ZZROBDC1', 'B0ZZROBDC2'
        it_client = FakeSpApiClient({child: 'B0ZZROBDCPA', sibling: 'B0ZZROBDCPA'}, marketplace_id='ZZ_ROB_IT')
        self._run(it_client, [
            make_report_row('ZZT-ROBDC-1', asin=child), make_report_row('ZZT-ROBDC-2', asin=sibling),
        ])
        variant = self.env['product.product'].search([('x_amazon_asin', '=', child)])
        variant.x_amazon_sku = 'ZZT-ROBDC-1-in comune'
        self.assertEqual(variant.default_code, 'ZZT-ROBDC-1')
        templates_before = self.env['product.template'].search_count([])

        es_client = FakeSpApiClient(marketplace_id='ZZ_ROB_ES')
        log, _eng = self._run(es_client, [make_report_row('ZZT-ROBDC-1', asin='')])

        self.assertEqual(self.env['product.template'].search_count([]), templates_before, 'nessun prodotto doppio')
        self.assertEqual(self._listing(variant, self.es).sku, 'ZZT-ROBDC-1')
        # Dal 2026-11-06 la riga prende l'ASIN dello stesso SKU (altro
        # marketplace) e si collega per ASIN, prima per SKU: stesso esito.
        self.assertIn('COLLEGATO', log.log_lines)
        self.assertIn('ZZT-ROB', log.log_lines)

    def test_second_sku_on_same_asin_is_a_second_listing(self):
        child, sibling = 'B0ZZROBAC1', 'B0ZZROBAC2'
        it_client = FakeSpApiClient({child: 'B0ZZROBACPA', sibling: 'B0ZZROBACPA'}, marketplace_id='ZZ_ROB_IT')
        self._run(it_client, [
            make_report_row('ZZT-ROBAC-1', asin=child), make_report_row('ZZT-ROBAC-2', asin=sibling),
        ])
        variant = self.env['product.product'].search([('x_amazon_asin', '=', child)])
        es_client = FakeSpApiClient({child: 'B0ZZROBACPA', sibling: 'B0ZZROBACPA'}, marketplace_id='ZZ_ROB_ES')
        self._run(es_client, [
            make_report_row('ZZT-ROBAC-1', asin=child),
            make_report_row('ZZT-ROBAC-1-in comune', asin=child, status='Inactive'),
        ])
        # Due annunci sulla stessa variante; la variante tiene il suo SKU e
        # l'annuncio principale è quello attivo.
        listings = self._listing(variant, self.es)
        self.assertEqual(
            sorted(listings.mapped(lambda l: (l.sku, l.status))),
            [('ZZT-ROBAC-1', 'active'), ('ZZT-ROBAC-1-in comune', 'inactive')],
        )
        self.assertEqual(variant.x_amazon_sku, 'ZZT-ROBAC-1')
        self.assertEqual(self.env['ke.amazon.listing']._main_for(variant, self.es).sku, 'ZZT-ROBAC-1')

    def test_plus_sku_on_primary_marketplace_fixes_variant_created_elsewhere(self):
        # arundo_prod 2026-11-05: famiglia creata da UK con il vecchio SKU
        # ("TUB-4011012"); su IT (principale) vende "TUB-4011012+" in una
        # famiglia diversa → l'annuncio veniva collegato ma la variante
        # restava con lo SKU vecchio.
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', 'ZZ_ROB_IT')
        child, sibling = 'B0ZZROBUP1', 'B0ZZROBUP2'
        es_client = FakeSpApiClient({child: 'B0ZZROBUPPA', sibling: 'B0ZZROBUPPA'}, marketplace_id='ZZ_ROB_ES')
        self._run(es_client, [
            make_report_row('ZZT-ROBUP-1', asin=child), make_report_row('ZZT-ROBUP-2', asin=sibling),
        ])
        variant = self.env['product.product'].search([('x_amazon_asin', '=', child)])
        self.assertEqual(variant.x_amazon_sku, 'ZZT-ROBUP-1')

        it_client = FakeSpApiClient({child: 'B0ZZROBUPPB'}, marketplace_id='ZZ_ROB_IT')
        log, _eng = self._run(it_client, [make_report_row('ZZT-ROBUP-1+', asin=child)])
        self.assertIn('COLLEGATO (famiglia diversa su ZZRI)', log.log_lines)
        self.assertEqual((variant.x_amazon_sku, variant.default_code), ('ZZT-ROBUP-1+', 'ZZT-ROBUP-1+'))
        self.assertEqual(self._listing(variant, self.es).sku, 'ZZT-ROBUP-1', "l'annuncio ES resta col suo SKU")

        # Su un marketplace secondario lo SKU principale non cambia.
        es2 = FakeSpApiClient({child: 'B0ZZROBUPPC'}, marketplace_id='ZZ_ROB_ES')
        self._run(es2, [make_report_row('ZZT-ROBUP-1-ALT', asin=child)])
        self.assertEqual(variant.x_amazon_sku, 'ZZT-ROBUP-1+')

    def test_unknown_sku_without_asin_gets_asin_from_amazon(self):
        # arundo_prod 2026-11-05: righe FR senza ASIN con SKU mai visti
        # ("PFS-73032}") diventavano prodotti nuovi. Ora l'ASIN si chiede ad
        # Amazon per quello SKU; gli SKU già noti non vengono chiesti.
        child, sibling = 'B0ZZROBLA1', 'B0ZZROBLA2'
        it_client = FakeSpApiClient({child: 'B0ZZROBLAPA', sibling: 'B0ZZROBLAPA'}, marketplace_id='ZZ_ROB_IT')
        self._run(it_client, [
            make_report_row('ZZT-ROBLA-1', asin=child), make_report_row('ZZT-ROBLA-2', asin=sibling),
        ])
        variant = self.env['product.product'].search([('x_amazon_asin', '=', child)])
        templates_before = self.env['product.template'].search_count([])

        es_client = FakeSpApiClient({child: 'B0ZZROBLAPA', sibling: 'B0ZZROBLAPA'}, marketplace_id='ZZ_ROB_ES',
                                    asin_by_sku={'ZZT-ROBLA-1}': child})
        log, _eng = self._run(es_client, [
            make_report_row('ZZT-ROBLA-1}', asin=''),
            make_report_row('ZZT-ROBLA-2', asin=''),
        ])
        self.assertEqual(es_client.asin_requests, ['ZZT-ROBLA-1}'], 'solo lo SKU sconosciuto viene chiesto')
        self.assertEqual(self.env['product.template'].search_count([]), templates_before, 'nessun prodotto nuovo')
        listings = self._listing(variant, self.es)
        self.assertIn('ZZT-ROBLA-1}', listings.mapped('sku'))
        self.assertEqual(listings.filtered(lambda l: l.sku == 'ZZT-ROBLA-1}').asin, child)
        self.assertIn('ASIN chiesto ad Amazon per 1 SKU', log.log_lines)

    def test_main_sku_chosen_by_status_then_open_date(self):
        # Stesso ASIN, due SKU nello stesso report del marketplace principale.
        it = FakeSpApiClient(marketplace_id='ZZ_ROB_IT')
        self._run(it, [make_report_row('ZZT-ROBOD-A', asin='B0ZZROBOD1', open_date='01/03/2026 10:00:00 MET')])
        tmpl = self.env['product.template'].search([('x_amazon_asin', '=', 'B0ZZROBOD1')])
        self.assertEqual(tmpl.x_amazon_sku, 'ZZT-ROBOD-A')
        # Entrambi attivi: vince quello aperto più di recente.
        self._run(it, [
            make_report_row('ZZT-ROBOD-A', asin='B0ZZROBOD1', open_date='01/03/2026 10:00:00 MET'),
            make_report_row('ZZT-ROBOD-B', asin='B0ZZROBOD1', open_date='05/08/2026 10:00:00 MEST'),
        ])
        self.assertEqual(tmpl.x_amazon_sku, 'ZZT-ROBOD-B')
        # Il più recente inattivo, l'altro attivo: vince l'attivo.
        self._run(it, [
            make_report_row('ZZT-ROBOD-A', asin='B0ZZROBOD1', open_date='01/03/2026 10:00:00 MET'),
            make_report_row('ZZT-ROBOD-B', asin='B0ZZROBOD1', open_date='05/08/2026 10:00:00 MEST',
                            status='Inactive'),
        ])
        self.assertEqual(tmpl.x_amazon_sku, 'ZZT-ROBOD-A')
        listing = self._listing(tmpl.product_variant_id, self.it).filtered(lambda l: l.sku == 'ZZT-ROBOD-B')
        self.assertEqual(str(listing.open_date), '2026-08-05 10:00:00')

    def test_listing_moved_by_hand_stays_put(self):
        it = FakeSpApiClient(marketplace_id='ZZ_ROB_IT')
        self._run(it, [make_report_row('ZZT-ROBMH-1', asin='B0ZZROBMH1'),
                       make_report_row('ZZT-ROBMH-2', asin='B0ZZROBMH2')])
        p1 = self.env['product.template'].search([('x_amazon_asin', '=', 'B0ZZROBMH1')]).product_variant_id
        p2 = self.env['product.template'].search([('x_amazon_asin', '=', 'B0ZZROBMH2')]).product_variant_id
        listing = self._listing(p2, self.it)
        listing.product_id = p1
        self.assertTrue(listing.manual_product)
        self._run(it, [make_report_row('ZZT-ROBMH-2', asin='B0ZZROBMH2', price='7.00')])
        self.assertEqual((listing.product_id, listing.price), (p1, 7.0), "l'import non riporta indietro l'annuncio")

    def test_similar_skus_on_different_products_are_reported(self):
        it = FakeSpApiClient(marketplace_id='ZZ_ROB_IT')
        self._run(it, [make_report_row('ZZT-ROBSIM-1', asin='B0ZZROBSIM1'),
                       make_report_row('ZZT-ROBSIM-1+', asin='B0ZZROBSIM2')])
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT simili', 'sync_type': 'full_import'})
        n = self.SyncEngine(self.env, None).report_similar_skus_on_different_products(log)
        self.assertGreaterEqual(n, 1)
        self.assertIn('"ZZT-ROBSIM-1"', log.log_lines)

    def test_old_sku_on_secondary_marketplace_registers_listing(self):
        # UK del 2026-11-04: il report aveva ancora il vecchio SKU (senza '+')
        # di prodotti già passati al '+' su IT → annuncio UK mai registrato.
        # ES qui è secondario: lo SKU principale lo decide IT.
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', 'ZZ_ROB_IT')
        it_client = FakeSpApiClient({'B0ZZROBPL2': 'B0ZZROBPLPA', 'B0ZZROBPL3': 'B0ZZROBPLPA'},
                                    marketplace_id='ZZ_ROB_IT')
        self._run(it_client, [
            make_report_row('ZZT-ROBPL-1+', asin='B0ZZROBPL1'),
            make_report_row('ZZT-ROBPL-2+', asin='B0ZZROBPL2'),
            make_report_row('ZZT-ROBPL-3', asin='B0ZZROBPL3'),
        ])
        simple = self.env['product.template'].search([('default_code', '=', 'ZZT-ROBPL-1+')]).product_variant_id
        variant = self.env['product.product'].search([('x_amazon_asin', '=', 'B0ZZROBPL2')])

        es_client = FakeSpApiClient({'B0ZZROBPL2': 'B0ZZROBPLPA', 'B0ZZROBPL3': 'B0ZZROBPLPA'},
                                    marketplace_id='ZZ_ROB_ES')
        log, _eng = self._run(es_client, [
            make_report_row('ZZT-ROBPL-1', asin='B0ZZROBPL1', price='11.00'),
            make_report_row('ZZT-ROBPL-1', asin='', price='11.00'),
            make_report_row('ZZT-ROBPL-2', asin='B0ZZROBPL2'),
        ])
        self.assertEqual(self._listing(simple, self.es).sku, 'ZZT-ROBPL-1')
        self.assertEqual(self._listing(simple, self.es).asin, 'B0ZZROBPL1')
        self.assertEqual(self._listing(variant, self.es).sku, 'ZZT-ROBPL-2')
        self.assertEqual(simple.x_amazon_sku or simple.product_tmpl_id.x_amazon_sku, 'ZZT-ROBPL-1+',
                         "lo SKU '+' del prodotto non cambia")
        self.assertEqual(variant.x_amazon_sku, 'ZZT-ROBPL-2+')

    def test_old_sku_is_a_second_listing_when_marketplace_has_plus(self):
        # Dal 2026-11-05: il vecchio SKU è un annuncio a sé della stessa
        # variante (prima veniva saltato se c'era già l'annuncio '+').
        it_client = FakeSpApiClient(marketplace_id='ZZ_ROB_IT')
        self._run(it_client, [make_report_row('ZZT-ROBPK-1+', asin='B0ZZROBPK1')])
        simple = self.env['product.template'].search([('default_code', '=', 'ZZT-ROBPK-1+')]).product_variant_id
        es_client = FakeSpApiClient(marketplace_id='ZZ_ROB_ES')
        self._run(es_client, [
            make_report_row('ZZT-ROBPK-1+', asin='B0ZZROBPK1', open_date='05/08/2026 10:00:00 MEST'),
            make_report_row('ZZT-ROBPK-1', asin='B0ZZROBPK1', open_date='01/03/2026 10:00:00 MET'),
        ])
        self.assertEqual(sorted(self._listing(simple, self.es).mapped('sku')), ['ZZT-ROBPK-1', 'ZZT-ROBPK-1+'])
        self.assertEqual(simple.product_tmpl_id.x_amazon_sku, 'ZZT-ROBPK-1+')

    def test_row_not_recognized_as_variant_attaches_listing(self):
        child = 'B0ZZROBNV1'
        it_client = FakeSpApiClient({child: 'B0ZZROBPARN', 'B0ZZROBNV2': 'B0ZZROBPARN'}, marketplace_id='ZZ_ROB_IT')
        self._run(it_client, [
            make_report_row('ZZT-ROBNV-1', asin=child), make_report_row('ZZT-ROBNV-2', asin='B0ZZROBNV2'),
        ])
        variant = self.env['product.product'].search([('x_amazon_asin', '=', child)])
        # ES: il catalogo non rivela la relazione di variante (riga "semplice").
        es_client = FakeSpApiClient({}, marketplace_id='ZZ_ROB_ES')
        log, _eng = self._run(es_client, [make_report_row('ZZT-ROBNV-1-ES', asin=child)])
        listing = self._listing(variant, self.es)
        self.assertEqual(listing.sku, 'ZZT-ROBNV-1-ES')
        self.assertIn('riga non riconosciuta come variante', log.log_lines)
        self.assertIn('COLLEGATO', log.log_lines)

    def test_failing_row_does_not_abort_the_rest_of_the_chunk(self):
        client = FakeSpApiClient(marketplace_id='ZZ_ROB_IT')
        eng_cls = self.SyncEngine
        original = eng_cls._create_product

        def flaky(engine, data, product_type):
            if data['sku'] == 'ZZT-ROB-BAD':
                engine.env['product.template'].create({'name': 'ZZT scrittura annullata', 'type': 'consu'})
                raise ValueError('errore simulato')
            return original(engine, data, product_type)

        with patch.object(eng_cls, '_create_product', flaky):
            log, _eng = self._run(client, [
                make_report_row('ZZT-ROB-BAD', asin='B0ZZROBBAD'),
                make_report_row('ZZT-ROB-OK', asin='B0ZZROBOK1'),
            ])
        self.assertEqual(log.products_error, 1)
        self.assertTrue(self.env['product.template'].search([('default_code', '=', 'ZZT-ROB-OK')]),
                        'la riga successiva viene comunque importata')
        self.assertFalse(self.env['product.template'].search([('name', '=', 'ZZT scrittura annullata')]),
                         'il savepoint annulla solo le scritture della riga fallita')

    def test_deadlock_on_a_row_is_retried(self):
        client = FakeSpApiClient(marketplace_id='ZZ_ROB_IT')
        eng_cls = self.SyncEngine
        original = eng_cls._create_product
        calls = {'n': 0}

        def deadlock_once(engine, data, product_type):
            calls['n'] += 1
            if calls['n'] == 1:
                raise _PgError('40P01')
            return original(engine, data, product_type)

        with patch.object(eng_cls, '_create_product', deadlock_once), \
                patch('odoo.addons.ke_amazon_sync.services.sync_engine.time.sleep', lambda s: None):
            log, _eng = self._run(client, [make_report_row('ZZT-ROB-DL', asin='B0ZZROBDL1')])
        self.assertEqual(calls['n'], 2, 'riga ripetuta dopo il deadlock')
        self.assertEqual(log.products_error, 0)
        self.assertTrue(self.env['product.template'].search([('default_code', '=', 'ZZT-ROB-DL')]))

    def test_imported_products_have_no_automatic_chatter(self):
        client = FakeSpApiClient(marketplace_id='ZZ_ROB_IT')
        self._run(client, [make_report_row('ZZT-ROB-MAIL', asin='B0ZZROBML1')])
        tmpl = self.env['product.template'].search([('default_code', '=', 'ZZT-ROB-MAIL')])
        self.assertTrue(tmpl)
        self.assertFalse(
            self.env['mail.message'].search([('model', '=', 'product.template'), ('res_id', '=', tmpl.id)]),
            'nessun messaggio automatico "creato" né tracciamento',
        )
