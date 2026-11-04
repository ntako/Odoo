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
