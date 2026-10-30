"""
Test per i prodotti marchio proprio idonei all'esenzione FNSKU (Amazon
Brand Registry): tracciano tramite il codice del produttore già sull'unità,
nessuna etichetta FNSKU va stampata. Distinzione verificata sui dati reali
di questo venditore il 2026-10-29 (non dedotta): il valore FNSKU già
importato ha prefisso 'X0' per un vero FNSKU generato da Amazon, 'B0' per un
prodotto che traccia tramite il codice del produttore (il valore è in
realtà l'ASIN) — nessun campo nuovo da mantenere a mano, la distinzione
viene dal valore stesso già importato da "Import FNSKU".
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestAmazonFnskuNeedsLabel(TransactionCase):

    def setUp(self):
        super().setUp()
        self.real_fnsku_product = self.env['product.product'].create({
            'name': 'ZZT Terzi', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-OB-1', 'x_amazon_fnsku': 'X0ZZREAL1',
        })
        self.own_brand_product = self.env['product.product'].create({
            'name': 'ZZT Marchio Proprio', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-OB-2', 'x_amazon_fnsku': 'B0ZZOWNBR1',
        })
        self.not_imported_product = self.env['product.product'].create({
            'name': 'ZZT Non Importato', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-OB-3',
        })

    def test_needs_label_true_for_x0_prefix(self):
        self.assertTrue(self.real_fnsku_product._amazon_fnsku_needs_label())

    def test_needs_label_false_for_b0_prefix(self):
        self.assertFalse(self.own_brand_product._amazon_fnsku_needs_label())

    def test_needs_label_false_when_empty(self):
        # Caso diverso (non ancora importato) — il chiamante deve bloccare
        # PRIMA con un errore esplicito, non trattarlo come "non serve".
        self.assertFalse(self.not_imported_product._amazon_fnsku_needs_label())

    def test_prefix_check_is_case_insensitive(self):
        product = self.env['product.product'].create({
            'name': 'ZZT minuscolo', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-OB-4', 'x_amazon_fnsku': 'b0lowercase',
        })
        self.assertFalse(product._amazon_fnsku_needs_label())


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestOwnBrandInPrepWizard(TransactionCase):

    def setUp(self):
        super().setUp()
        self.source_wh = self.env['stock.warehouse'].create({'name': 'ZZT Src OB', 'code': 'ZOB1'})
        self.fba_wh = self.env['stock.warehouse'].create({'name': 'ZZT FBA OB', 'code': 'ZOB2'})
        self.env.company.amazon_fba_warehouse_id = self.fba_wh.id
        self.product = self.env['product.product'].create({
            'name': 'ZZT Marchio Proprio Spedizione', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-OBW-1', 'x_amazon_fnsku': 'B0ZZOBWIZ1', 'barcode': 'ZZTOBWIZ0001',
        })
        self.env['stock.quant']._update_available_quantity(self.product, self.source_wh.lot_stock_id, 3)
        self.picking = SyncEngine(self.env, None).ship_to_fba(self.source_wh, [(self.product, 3)])
        self.move = self.picking.move_ids
        self.wizard = self.env['ke.amazon.fba.prep.wizard'].create({'picking_id': self.picking.id})

    def test_scan_own_brand_completes_line_without_printing(self):
        self.wizard.ean_scan = 'ZZTOBWIZ0001'
        action = self.wizard.action_scan()
        self.assertTrue(action, "nessun report da aprire, solo un refresh")
        self.assertNotIsInstance(action, dict, "non deve restituire un'azione report")
        self.assertIn('marchio proprio', self.wizard.last_scan_message)
        self.assertEqual(self.move.x_amazon_labeled_qty, 1, "la riga conta comunque come completata")
        self.assertTrue(self.move.picked)
        log = self.env['ke.amazon.fba.label.log'].search([('move_id', '=', self.move.id)])
        self.assertEqual(len(log), 1)
        self.assertTrue(log.is_own_brand_no_label)

    def test_row_button_own_brand_completes_without_error(self):
        result = self.move.action_print_fnsku_label()
        self.assertTrue(result)
        self.assertEqual(self.move.x_amazon_labeled_qty, 3, "completa tutta la riga in un colpo solo")

    def test_picking_can_be_confirmed_without_any_real_print(self):
        # "Conferma spedizione" non deve restare bloccata in attesa di
        # un'etichetta che non deve mai esistere per questo prodotto.
        self.move.action_print_fnsku_label()
        self.wizard.action_validate_picking()
        self.assertEqual(self.picking.state, 'done')


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestOwnBrandInStandaloneWizard(TransactionCase):

    def test_print_own_brand_shows_message_no_log_created(self):
        product = self.env['product.product'].create({
            'name': 'ZZT Marchio Proprio Standalone', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-OBS-1', 'x_amazon_fnsku': 'B0ZZOBSTAND1',
        })
        wizard = self.env['ke.amazon.fba.standalone.label.wizard'].create({
            'product_id': product.id, 'quantity': 2,
        })
        result = wizard.action_print()
        self.assertTrue(result)
        self.assertIn('marchio proprio', wizard.last_scan_message)
        self.assertFalse(self.env['ke.amazon.fba.label.log'].search([('product_id', '=', product.id)]))

    def test_scan_message_flags_own_brand(self):
        product = self.env['product.product'].create({
            'name': 'ZZT Marchio Proprio Scan', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-OBS-2', 'x_amazon_fnsku': 'B0ZZOBSCAN1', 'barcode': 'ZZTOBSCAN0001',
        })
        wizard = self.env['ke.amazon.fba.standalone.label.wizard'].create({'ean_scan': 'ZZTOBSCAN0001'})
        wizard.action_scan()
        self.assertIn('marchio proprio', wizard.last_scan_message)


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestOwnBrandInShipWizard(TransactionCase):

    def setUp(self):
        super().setUp()
        self.real_fnsku_product = self.env['product.product'].create({
            'name': 'ZZT Spedizione Terzi', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-OBSH-1', 'x_amazon_fnsku': 'X0ZZSHIP1', 'x_amazon_fulfillment': 'fba',
        })
        self.own_brand_product = self.env['product.product'].create({
            'name': 'ZZT Spedizione Proprio', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-OBSH-2', 'x_amazon_fnsku': 'B0ZZSHIP2', 'x_amazon_fulfillment': 'fba',
        })
        source_wh = self.env['stock.warehouse'].create({'name': 'ZZT Src ShipOB', 'code': 'ZOB3'})
        self.wizard = self.env['ke.amazon.ship.wizard'].create({
            'source_warehouse_id': source_wh.id,
            'line_ids': [
                (0, 0, {'product_id': self.real_fnsku_product.id, 'quantity': 2}),
                (0, 0, {'product_id': self.own_brand_product.id, 'quantity': 3}),
            ],
        })

    def test_mixed_lines_print_only_the_ones_that_need_a_label(self):
        action = self.wizard.action_print_all_fnsku_labels()
        self.assertEqual(len(action['context']['active_ids']), 2, "solo le 2 del prodotto terzi, non le 3 proprio")
        logs = self.env['ke.amazon.fba.label.log'].search([('product_id', '=', self.own_brand_product.id)])
        self.assertFalse(logs, "nessun log creato per il marchio proprio in questo flusso")

    def test_all_own_brand_lines_raise_nothing_to_print(self):
        self.wizard.line_ids.filtered(
            lambda l: l.product_id == self.real_fnsku_product
        ).unlink()
        with self.assertRaises(UserError):
            self.wizard.action_print_all_fnsku_labels()
