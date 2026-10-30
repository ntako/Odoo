"""
Test per "Stampa tutte le etichette" sul wizard "Spedisci a FBA"
(ke.amazon.ship.wizard.action_print_all_fnsku_labels) — richiesto
dall'utente il 2026-10-29 per etichettare i pezzi mentre si compone
l'elenco, prima ancora che il trasferimento/stock.move esista. Un solo
bottone a livello di wizard, non uno per riga: un bottone dentro una riga
della lista editabile non salvava sempre la riga prima della chiamata
("non funziona se non salvo prima"), un bottone sul form principale salva
sempre l'intero form (righe comprese) prima di eseguire l'azione. Stessa
logica di dispatch condivisa con stock.move._print_fnsku_labels e il
wizard standalone (ke.amazon.fba.label.log._create_and_dispatch_many).
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestShipToFbaWizardPrint(TransactionCase):

    def setUp(self):
        super().setUp()
        self.product_a = self.env['product.product'].create({
            'name': 'ZZT Prodotto A', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-SHIPPRINT-A', 'x_amazon_fnsku': 'X0ZZSHIPPRINTA',
            'x_amazon_fulfillment': 'fba',
        })
        self.product_b = self.env['product.product'].create({
            'name': 'ZZT Prodotto B', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-SHIPPRINT-B', 'x_amazon_fnsku': 'X0ZZSHIPPRINTB',
            'x_amazon_fulfillment': 'fba',
        })
        source_wh = self.env['stock.warehouse'].create({'name': 'ZZT Src Ship', 'code': 'ZZTSP'})
        self.wizard = self.env['ke.amazon.ship.wizard'].create({
            'source_warehouse_id': source_wh.id,
            'line_ids': [
                (0, 0, {'product_id': self.product_a.id, 'quantity': 2}),
                (0, 0, {'product_id': self.product_b.id, 'quantity': 3}),
            ],
        })

    def test_print_all_before_confirming_shipment_creates_no_move(self):
        action = self.wizard.action_print_all_fnsku_labels()
        self.assertEqual(action.get('report_name'), 'ke_amazon_sync.report_amazon_fba_label_document')
        logs_a = self.env['ke.amazon.fba.label.log'].search([('product_id', '=', self.product_a.id)])
        logs_b = self.env['ke.amazon.fba.label.log'].search([('product_id', '=', self.product_b.id)])
        self.assertEqual(len(logs_a), 2)
        self.assertEqual(len(logs_b), 3)
        self.assertFalse((logs_a + logs_b).mapped('move_id'), "nessun movimento esiste ancora a questo punto")
        self.assertFalse((logs_a + logs_b).mapped('picking_id'))
        # Un'unica azione di stampa per tutte le righe insieme (un documento
        # con una pagina per etichetta, 2+3 = 5 in tutto).
        self.assertEqual(len(action['context']['active_ids']), 5)

    def test_print_all_requires_at_least_one_line(self):
        empty_wizard = self.env['ke.amazon.ship.wizard'].create({
            'source_warehouse_id': self.wizard.source_warehouse_id.id,
        })
        with self.assertRaises(UserError):
            empty_wizard.action_print_all_fnsku_labels()

    def test_print_all_requires_positive_quantity_on_every_line(self):
        self.wizard.line_ids[1].quantity = 0
        with self.assertRaises(UserError):
            self.wizard.action_print_all_fnsku_labels()
        # Nessuna etichetta scritta per nessuna riga se una sola è bloccata
        # (tutto o niente, non una stampa parziale silenziosa).
        self.assertFalse(self.env['ke.amazon.fba.label.log'].search([('product_id', '=', self.product_a.id)]))

    def test_print_all_requires_fnsku_on_every_line(self):
        product_no_fnsku = self.env['product.product'].create({
            'name': 'ZZT Senza FNSKU', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-SHIPPRINT-C', 'x_amazon_fulfillment': 'fba',
        })
        self.env['ke.amazon.ship.wizard.line'].create({
            'wizard_id': self.wizard.id, 'product_id': product_no_fnsku.id, 'quantity': 1,
        })
        with self.assertRaises(UserError):
            self.wizard.action_print_all_fnsku_labels()
