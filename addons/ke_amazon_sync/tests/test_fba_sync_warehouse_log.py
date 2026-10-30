"""
Test per AmazonSyncLog._describe_fba_sync_warehouses — il messaggio iniziale
della sync giacenze FBA deve descrivere il raggruppamento REALE per
magazzino, non affermare sempre "Pan-EU pooled, stesso magazzino per tutti"
a prescindere dalla configurazione (bug segnalato dall'utente il 2026-10-29:
il vecchio messaggio statico mentiva con UK ancora privo di un magazzino FBA
dedicato).
"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestFbaSyncWarehouseLog(TransactionCase):

    def setUp(self):
        super().setUp()
        self.SyncLog = self.env['ke.amazon.sync.log']
        self.company_wh = self.env['stock.warehouse'].create({'name': 'ZZT Pool EU', 'code': 'ZZTPOOL'})
        self.env.company.amazon_fba_warehouse_id = self.company_wh.id

    def _make_marketplace(self, code, fba_warehouse=None):
        return self.env['ke.amazon.marketplace'].create({
            'name': f'ZZT {code}', 'code': code, 'marketplace_id': f'ZZT_MKT_{code}',
            'active': True, 'fba_warehouse_id': fba_warehouse.id if fba_warehouse else False,
        })

    def test_all_pooled_on_company_default_is_one_group(self):
        it = self._make_marketplace('IT')
        es = self._make_marketplace('ES')
        msg = self.SyncLog._describe_fba_sync_warehouses(it | es)
        self.assertIn(f'IT, ES → {self.company_wh.name}', msg)
        self.assertNotIn('Pan-EU pooled', msg, 'niente più affermazioni statiche non verificate')

    def test_dedicated_warehouse_is_a_separate_group(self):
        it = self._make_marketplace('IT')
        uk_wh = self.env['stock.warehouse'].create({'name': 'ZZT UK Dedicato', 'code': 'ZZTUK'})
        uk = self._make_marketplace('UK', fba_warehouse=uk_wh)
        msg = self.SyncLog._describe_fba_sync_warehouses(it | uk)
        self.assertIn(f'IT → {self.company_wh.name}', msg)
        self.assertIn(f'UK → {uk_wh.name}', msg)

    def test_no_warehouse_configured_at_all_is_flagged(self):
        self.env.company.amazon_fba_warehouse_id = False
        it = self._make_marketplace('IT')
        msg = self.SyncLog._describe_fba_sync_warehouses(it)
        self.assertIn('IT → NESSUNO (non configurato)', msg)
