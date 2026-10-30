"""
Test per stock.move.x_amazon_labeled_qty — la colonna "Etichettata" del
wizard di preparazione FBA (ke.amazon.fba.prep.wizard) leggeva direttamente
move.quantity, che Odoo precompila con l'intera quantità prevista non appena
la riga è riservata (action_assign(), picked=False) — PRIMA di qualunque
etichettatura reale. Risultato: la vista mostrava una spedizione come già
completamente etichettata ancora prima di scansionare la prima unità.
Segnalato dal vivo il 2026-10-29 con uno screenshot reale. Il campo calcolato
conta invece dallo storico ke.amazon.fba.label.log, stessa fonte già usata
dalla logica di blocco (_labeled_count).
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestFbaPrepWizardLabeledDisplay(TransactionCase):

    def setUp(self):
        super().setUp()
        self.source_wh = self.env['stock.warehouse'].create({'name': 'ZZT Src', 'code': 'ZZTL1'})
        self.fba_wh = self.env['stock.warehouse'].create({'name': 'ZZT FBA', 'code': 'ZZTL2'})
        self.env.company.amazon_fba_warehouse_id = self.fba_wh.id
        self.product = self.env['product.product'].create({
            'name': 'ZZT Prodotto etichetta display', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-LBLDISP-1', 'x_amazon_fnsku': 'X0ZZLBLDISP1', 'barcode': 'ZZTLBLDISP0001',
        })
        # Giacenza reale nel magazzino sorgente: senza, action_assign() non
        # riserva nulla (move.quantity resta 0) e non riproduce il sintomo
        # reale segnalato dall'utente (quantity precompilata = prevista).
        self.env['stock.quant']._update_available_quantity(
            self.product, self.source_wh.lot_stock_id, 3,
        )
        self.picking = SyncEngine(self.env, None).ship_to_fba(self.source_wh, [(self.product, 3)])
        self.move = self.picking.move_ids
        self.wizard = self.env['ke.amazon.fba.prep.wizard'].create({'picking_id': self.picking.id})

    def test_labeled_qty_is_zero_before_any_scan_even_though_quantity_is_precompiled(self):
        # Riproduce esattamente il sintomo segnalato: subito dopo
        # action_assign() (fatto da ship_to_fba), move.quantity è già = alla
        # quantità prevista con picked=False — ma nessuna unità è stata
        # davvero etichettata.
        self.assertEqual(self.move.product_uom_qty, 3)
        self.assertEqual(self.move.quantity, 3, "precompilato da action_assign, non significa etichettato")
        self.assertFalse(self.move.picked)
        self.assertEqual(
            self.move.x_amazon_labeled_qty, 0,
            "nessuna unità scansionata: la colonna mostrata nel wizard deve essere 0, non 3",
        )

    def test_labeled_qty_increments_only_on_real_scan(self):
        self.wizard.ean_scan = 'ZZTLBLDISP0001'
        self.wizard.action_scan()
        self.assertEqual(self.move.x_amazon_labeled_qty, 1)
        self.assertEqual(self.wizard._labeled_count(self.move), 1)
        self.wizard.ean_scan = 'ZZTLBLDISP0001'
        self.wizard.action_scan()
        self.assertEqual(self.move.x_amazon_labeled_qty, 2)

    def test_print_button_on_row_completes_whole_line_in_one_click(self):
        # Bottone di stampa diretta sulla riga (richiesto dall'utente il
        # 2026-10-29): un solo click stampa in un unico documento TUTTA la
        # quantità ancora mancante (Prevista - Etichettata), non una copia
        # alla volta — stessa logica condivisa (stock.move._print_fnsku_labels),
        # nessuna ristampa consentita da qui.
        action = self.move.action_print_fnsku_label()
        self.assertEqual(action.get('report_name'), 'ke_amazon_sync.report_amazon_fba_label_document')
        self.assertEqual(len(action.get('context', {}).get('active_ids', [])), 3, "tutte e 3 le copie previste")
        self.assertEqual(self.move.x_amazon_labeled_qty, 3)
        self.assertTrue(self.move.picked)
        log = self.env['ke.amazon.fba.label.log'].search([('move_id', '=', self.move.id)])
        self.assertEqual(len(log), 3)
        self.assertFalse(any(log.mapped('is_reprint')))

    def test_print_button_blocked_when_already_complete_no_reprint(self):
        self.move.action_print_fnsku_label()  # completa subito le 3 previste
        self.assertEqual(self.move.x_amazon_labeled_qty, 3)
        with self.assertRaises(UserError):
            self.move.action_print_fnsku_label()

    def test_scan_and_button_share_the_same_count(self):
        # Scansione e bottone incrementano lo STESSO conteggio, non due
        # storie separate — garantisce che non si possa superare la
        # quantità prevista mischiando i due percorsi. Il bottone completa
        # sempre il RESIDUO (3 previste - 1 già scansionata = 2), non 1 fisso.
        self.wizard.ean_scan = 'ZZTLBLDISP0001'
        self.wizard.action_scan()
        self.assertEqual(self.move.x_amazon_labeled_qty, 1)
        self.move.action_print_fnsku_label()
        self.assertEqual(self.move.x_amazon_labeled_qty, 3)

    def test_scan_quantity_prints_all_in_one_action(self):
        # Richiesto dall'utente il 2026-10-29: più unità identiche dello
        # stesso SKU, un'unica scansione con quantità > 1 invece di
        # scansionare un'unità alla volta — un solo documento con una
        # pagina per etichetta.
        self.wizard.ean_scan = 'ZZTLBLDISP0001'
        self.wizard.scan_quantity = 3
        action = self.wizard.action_scan()
        self.assertEqual(len(action.get('context', {}).get('active_ids', [])), 3)
        self.assertEqual(self.move.x_amazon_labeled_qty, 3)
        self.assertTrue(self.move.picked)
        logs = self.env['ke.amazon.fba.label.log'].search([('move_id', '=', self.move.id)])
        self.assertEqual(len(logs), 3)
        self.assertFalse(any(logs.mapped('is_reprint')))

    def test_scan_quantity_above_remaining_is_blocked(self):
        self.wizard.ean_scan = 'ZZTLBLDISP0001'
        self.wizard.scan_quantity = 5  # solo 3 previste
        with self.assertRaises(UserError):
            self.wizard.action_scan()
        self.assertEqual(self.move.x_amazon_labeled_qty, 0, "nessuna etichetta scritta se la richiesta è bloccata")

    def test_scan_quantity_with_force_reprint_does_not_touch_completion(self):
        self.wizard.ean_scan = 'ZZTLBLDISP0001'
        self.wizard.scan_quantity = 3
        self.wizard.action_scan()
        self.assertEqual(self.move.x_amazon_labeled_qty, 3)

        self.wizard.ean_scan = 'ZZTLBLDISP0001'
        self.wizard.scan_quantity = 2
        self.wizard.force_reprint = True
        self.wizard.action_scan()
        # La quantità "etichettata" (reale, non ristampe) resta 3: le 2
        # ristampe non contano come nuove unità completate.
        self.assertEqual(self.move.x_amazon_labeled_qty, 3)
        logs = self.env['ke.amazon.fba.label.log'].search([('move_id', '=', self.move.id)])
        self.assertEqual(len(logs), 5)
        self.assertEqual(len(logs.filtered('is_reprint')), 2)
