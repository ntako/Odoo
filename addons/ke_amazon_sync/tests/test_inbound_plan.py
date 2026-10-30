"""
Test per ke.amazon.inbound.plan / services/fba_inbound_service.py — piano di
spedizione inbound FBA (Fulfillment Inbound API v2024-03-20). Nessuna
chiamata reale: FakeInboundClient sostituisce l'intero client (mai
mock.patch su SpApiClient, vedi memoria persistente del progetto), iniettato
sostituendo AmazonInboundPlan._build_client — un metodo di wiring interno,
non codice che chiama Amazon, quindi fuori da quella regola.
"""
from contextlib import contextmanager
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_amazon_sync.services.fba_inbound_service import FbaInboundService
from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine

from .common import no_commit


class FakeInboundClient:
    """Finto client Fulfillment Inbound — risposte canned, nessuna rete."""

    def __init__(self, fail_at=None):
        # Nome del metodo su cui simulare un fallimento (RuntimeError, come
        # poll_inbound_operation farebbe per operationStatus=FAILED).
        self.fail_at = fail_at
        self.calls = {}

    def _maybe_fail(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.fail_at == name:
            raise RuntimeError(f'Operazione fallita lato Amazon (simulata): {name}')

    def poll_inbound_operation(self, operation_id, poll_interval_s=10, poll_max_minutes=2):
        return {'operationStatus': 'SUCCESS'}

    def list_inbound_plans(self, page_size=10):
        self._maybe_fail('list_inbound_plans')
        return {'inboundPlans': getattr(self, 'existing_plans', [])}

    def create_inbound_plan(self, name, source_address, destination_marketplace_ids, items):
        self._maybe_fail('create_inbound_plan')
        self.last_items = items
        self.last_source_address = source_address
        return {'inboundPlanId': 'PLAN000000000000000000000000000001', 'operationId': 'OP1'}

    def generate_packing_options(self, inbound_plan_id):
        self._maybe_fail('generate_packing_options')
        return {'operationId': 'OP2'}

    def list_packing_options(self, inbound_plan_id):
        return {'packingOptions': [{'packingOptionId': 'PACK1', 'packingGroups': ['GROUP1']}]}

    def confirm_packing_option(self, inbound_plan_id, packing_option_id):
        self._maybe_fail('confirm_packing_option')
        return {'operationId': 'OP3'}

    def set_packing_information(self, inbound_plan_id, package_groupings):
        self._maybe_fail('set_packing_information')
        self.last_package_groupings = package_groupings
        return {'operationId': 'OP4'}

    def generate_placement_options(self, inbound_plan_id):
        self._maybe_fail('generate_placement_options')
        return {'operationId': 'OP5'}

    def list_placement_options(self, inbound_plan_id):
        return {'placementOptions': [{'placementOptionId': 'PLACE1', 'shipmentIds': ['SHIP1', 'SHIP2']}]}

    def confirm_placement_option(self, inbound_plan_id, placement_option_id):
        self._maybe_fail('confirm_placement_option')
        return {'operationId': 'OP6'}

    def get_shipment(self, inbound_plan_id, shipment_id):
        return {
            'shipmentConfirmationId': f'CONF-{shipment_id}',
            'destination': {'warehouseId': f'FC-{shipment_id}'},
        }

    def generate_delivery_window_options(self, inbound_plan_id, shipment_id):
        self._maybe_fail('generate_delivery_window_options')
        return {'operationId': 'OP7'}

    def list_delivery_window_options(self, inbound_plan_id, shipment_id):
        return {'deliveryWindowOptions': [{'deliveryWindowOptionId': f'DW-{shipment_id}'}]}

    def confirm_delivery_window_options(self, inbound_plan_id, shipment_id, delivery_window_option_id):
        self._maybe_fail('confirm_delivery_window_options')
        return {'operationId': 'OP8'}

    def generate_transportation_options(self, inbound_plan_id, placement_option_id,
                                         shipment_transportation_configurations):
        self._maybe_fail('generate_transportation_options')
        self.last_transport_configs = shipment_transportation_configurations
        return {'operationId': 'OP9'}

    def list_transportation_options(self, inbound_plan_id, placement_option_id):
        return {'transportationOptions': [
            {'shipmentId': 'SHIP1', 'transportationOptionId': 'TOPT-SHIP1'},
            {'shipmentId': 'SHIP2', 'transportationOptionId': 'TOPT-SHIP2'},
        ]}

    def confirm_transportation_options(self, inbound_plan_id, transportation_selections):
        self._maybe_fail('confirm_transportation_options')
        self.last_transport_selections = transportation_selections
        return {'operationId': 'OP10'}

    def get_labels(self, shipment_id, **kwargs):
        return {'DownloadURL': f'https://example.invalid/{shipment_id}'}

    def cancel_inbound_plan(self, inbound_plan_id):
        self._maybe_fail('cancel_inbound_plan')
        return {'operationId': 'OP11'}


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestInboundPlan(TransactionCase):

    def setUp(self):
        super().setUp()
        self.source_wh = self.env['stock.warehouse'].create({'name': 'ZZT Sorgente', 'code': 'ZZTSRC'})
        self.fba_wh = self.env['stock.warehouse'].create({'name': 'ZZT FBA', 'code': 'ZZTFBA'})
        self.env.company.amazon_fba_warehouse_id = self.fba_wh.id
        self.env.company.partner_id.write({
            'street': 'Via Test 1', 'city': 'Roma', 'zip': '00100',
            'country_id': self.env.ref('base.it').id, 'phone': '0612345678',
        })
        self.marketplace = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT IT', 'code': 'ZZTIT', 'marketplace_id': 'ZZT_MKT_IT',
            'active': True, 'fba_warehouse_id': self.fba_wh.id,
        })
        self.product = self.env['product.product'].create({
            'name': 'ZZT Prodotto inbound', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-INB-1', 'x_amazon_sku': 'ZZT-INB-1', 'barcode': 'ZZTINB0001',
        })
        picking = SyncEngine(self.env, None).ship_to_fba(self.source_wh, [(self.product, 3)])
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking.with_context(skip_immediate=True, skip_backorder=True)._action_done()
        self.picking = picking
        self.assertEqual(self.picking.state, 'done')
        self.assertTrue(self.picking.x_amazon_is_fba_shipment)

        self.plan = self.env['ke.amazon.inbound.plan'].create({
            'picking_id': self.picking.id,
            'marketplace_id': self.marketplace.id,
            'source_name': 'ZZT Magazzino',
            'source_address_line1': 'Via Test 1',
            'source_city': 'Roma',
            'source_postal_code': '00100',
            'source_country_code': 'IT',
            'source_phone': '0612345678',
        })

    @contextmanager
    def _with_client(self, client):
        # I metodi action_* chiamano env.cr.commit() ad ogni step confermato
        # (stesso principio di sync_engine.py) — no_commit (vedi common.py)
        # lo rende un no-op solo per la durata del test, altrimenti il
        # framework di test di Odoo lo blocca esplicitamente.
        with no_commit(self.env), patch.object(type(self.plan), '_build_client', lambda self: client):
            yield

    def test_full_happy_path_state_transitions(self):
        client = FakeInboundClient()
        with self._with_client(client):
            self.plan.action_create_plan()
            self.assertEqual(self.plan.state, 'plan_created')
            self.assertEqual(self.plan.amazon_inbound_plan_id, 'PLAN000000000000000000000000000001')
            self.assertEqual(client.last_items, [{
                'msku': 'ZZT-INB-1', 'quantity': 3, 'labelOwner': 'NONE', 'prepOwner': 'NONE',  # nessun FNSKU X0: vedi _owners_for
            }])

            self.plan.action_generate_packing_options()
            self.assertEqual(self.plan.packing_option_id, 'PACK1')
            self.assertEqual(self.plan.packing_group_id, 'GROUP1')

            self.plan.action_confirm_packing_option()
            self.assertEqual(self.plan.state, 'packing_confirmed')

            box = self.env['ke.amazon.inbound.plan.box'].create({
                'plan_id': self.plan.id, 'name': 'Collo 1',
                'length_cm': 30, 'width_cm': 20, 'height_cm': 10, 'weight_kg': 2,
            })
            self.env['ke.amazon.inbound.plan.box.line'].create({
                'box_id': box.id, 'product_id': self.product.id, 'quantity': 3,
            })
            box.action_close_box()
            self.assertEqual(box.state, 'closed')

            self.plan.action_set_packing_information()
            self.assertEqual(client.last_package_groupings[0]['packingGroupId'], 'GROUP1')

            self.plan.action_generate_placement_options()
            self.assertEqual(self.plan.state, 'placement_options_ready')
            self.assertEqual(self.plan.placement_option_id, 'PLACE1')
            self.assertEqual(set(self.plan.shipment_ids.mapped('amazon_shipment_id')), {'SHIP1', 'SHIP2'})

            self.plan.action_confirm_placement_option()
            self.assertEqual(self.plan.state, 'placement_confirmed')
            self.assertTrue(self.plan.confirmed_by)
            self.assertTrue(self.plan.confirmed_date)
            ship1 = self.plan.shipment_ids.filtered(lambda s: s.amazon_shipment_id == 'SHIP1')
            self.assertEqual(ship1.shipment_confirmation_id, 'CONF-SHIP1')
            self.assertEqual(ship1.destination_fc_name, 'FC-SHIP1')

            self.plan.action_generate_delivery_window_options()
            self.assertEqual(self.plan.state, 'delivery_window_options_ready')
            self.assertEqual(ship1.delivery_window_option_id, 'DW-SHIP1')

            self.plan.action_confirm_delivery_window_options()
            self.assertEqual(self.plan.state, 'delivery_window_confirmed')

            self.plan.action_generate_transportation_options()
            self.assertEqual(self.plan.state, 'transport_options_ready')
            self.assertEqual(ship1.transportation_option_id, 'TOPT-SHIP1')

            self.plan.action_confirm_transportation_options()
            self.assertEqual(self.plan.state, 'transport_confirmed')

            self.plan.action_get_labels()
            self.assertEqual(self.plan.state, 'labels_ready')
            self.assertIn('DownloadURL', ship1.labels_raw_response)

            self.plan.action_mark_shipped()
            self.assertEqual(self.plan.state, 'shipped')

    def test_actions_blocked_out_of_sequence(self):
        with self.assertRaises(UserError):
            self.plan.action_confirm_placement_option()
        with self.assertRaises(UserError):
            self.plan.action_generate_packing_options()

    def test_confirm_placement_option_cannot_rerun(self):
        client = FakeInboundClient()
        with self._with_client(client):
            self.plan.action_create_plan()
            self.plan.action_generate_packing_options()
            self.plan.action_confirm_packing_option()
            box = self.env['ke.amazon.inbound.plan.box'].create({
                'plan_id': self.plan.id, 'name': 'Collo 1',
                'length_cm': 30, 'width_cm': 20, 'height_cm': 10, 'weight_kg': 2,
            })
            self.env['ke.amazon.inbound.plan.box.line'].create({
                'box_id': box.id, 'product_id': self.product.id, 'quantity': 3,
            })
            box.action_close_box()
            self.plan.action_set_packing_information()
            self.plan.action_generate_placement_options()
            self.plan.action_confirm_placement_option()
            with self.assertRaises(UserError):
                self.plan.action_confirm_placement_option()

    def test_box_line_quantity_cannot_exceed_picking_quantity(self):
        box = self.env['ke.amazon.inbound.plan.box'].create({'plan_id': self.plan.id, 'name': 'Collo 1'})
        with self.assertRaises(UserError):
            self.env['ke.amazon.inbound.plan.box.line'].create({
                'box_id': box.id, 'product_id': self.product.id, 'quantity': 999,
            })

    def test_close_box_requires_lines_and_dimensions(self):
        box = self.env['ke.amazon.inbound.plan.box'].create({'plan_id': self.plan.id, 'name': 'Collo 1'})
        with self.assertRaises(UserError):
            box.action_close_box()
        self.env['ke.amazon.inbound.plan.box.line'].create({
            'box_id': box.id, 'product_id': self.product.id, 'quantity': 1,
        })
        with self.assertRaises(UserError):
            box.action_close_box()

    def test_error_path_preserves_earlier_confirmed_data(self):
        client = FakeInboundClient(fail_at='confirm_packing_option')
        with self._with_client(client):
            self.plan.action_create_plan()
            self.plan.action_generate_packing_options()
            created_plan_id = self.plan.amazon_inbound_plan_id
            # try/except, non self.assertRaises: l'assertRaises di Odoo avvolge
            # il blocco in un savepoint che viene annullato quando l'eccezione
            # attesa si verifica (stesso meccanismo con cui simula un rollback
            # di richiesta reale) — qui testiamo invece che lo stato 'error'
            # scritto da _handle_step_error (via un vero env.cr.commit(),
            # no-op solo perché siamo dentro no_commit) sopravviva a un
            # eventuale rollback successivo, esattamente il motivo per cui
            # quel metodo fa un commit esplicito prima di rilanciare in
            # produzione. Con assertRaises quel commit verrebbe annullato dal
            # savepoint del framework di test, nascondendo il comportamento
            # reale invece di verificarlo. Trovato dal vivo il 2026-10-29
            # (il test falliva con 'plan_created' != 'error' solo dentro il
            # vero test runner Odoo, non riproducibile isolando la stessa
            # logica a mano via odoo shell).
            try:
                self.plan.action_confirm_packing_option()
                self.fail('UserError attesa')
            except UserError:
                pass
            self.assertEqual(self.plan.state, 'error')
            self.assertTrue(self.plan.error_message)
            # I dati dello step precedente, già confermato con successo, non
            # vengono persi dal fallimento dello step successivo.
            self.assertEqual(self.plan.amazon_inbound_plan_id, created_plan_id)
            self.assertEqual(self.plan.packing_option_id, 'PACK1')

    def test_cancel_blocked_after_transport_confirmed(self):
        client = FakeInboundClient()
        with self._with_client(client):
            self.plan.action_create_plan()
            self.plan.action_generate_packing_options()
            self.plan.action_confirm_packing_option()
            box = self.env['ke.amazon.inbound.plan.box'].create({
                'plan_id': self.plan.id, 'name': 'Collo 1',
                'length_cm': 30, 'width_cm': 20, 'height_cm': 10, 'weight_kg': 2,
            })
            self.env['ke.amazon.inbound.plan.box.line'].create({
                'box_id': box.id, 'product_id': self.product.id, 'quantity': 3,
            })
            box.action_close_box()
            self.plan.action_set_packing_information()
            self.plan.action_generate_placement_options()
            self.plan.action_confirm_placement_option()
            self.plan.action_generate_delivery_window_options()
            self.plan.action_confirm_delivery_window_options()
            self.plan.action_generate_transportation_options()
            self.plan.action_confirm_transportation_options()
            with self.assertRaises(UserError):
                self.plan.action_cancel_plan()

    def test_picking_cannot_be_linked_to_two_active_plans(self):
        with self.assertRaises(Exception):
            self.env['ke.amazon.inbound.plan'].create({
                'picking_id': self.picking.id, 'marketplace_id': self.marketplace.id,
            })

    def test_default_marketplace_is_italy(self):
        # Richiesto dall'utente il 2026-10-30: con il Pan-EU il marketplace
        # del piano decide il paese di arrivo della merce, default Amazon.it.
        italy = self.env.ref('ke_amazon_sync.amazon_marketplace_it')
        italy.active = True
        self.assertEqual(self.env['ke.amazon.inbound.plan']._default_marketplace(), italy)
        italy.active = False
        self.assertFalse(
            self.env['ke.amazon.inbound.plan']._default_marketplace(),
            'marketplace archiviato: nessun default, meglio sceglierlo a mano',
        )

    def test_source_address_filled_without_phone(self):
        # 'mobile' non esiste più su res.partner in Odoo 19: con 'phone'
        # vuoto default_get andava in AttributeError (trovato il 2026-10-30).
        partner = self.picking.picking_type_id.warehouse_id.partner_id or self.env.company.partner_id
        partner.write({'phone': False, 'city': 'ZZT Città'})
        vals = self.env['ke.amazon.inbound.plan'].with_context(
            default_picking_id=self.picking.id,
        ).default_get(['picking_id', 'source_name', 'source_city', 'source_phone'])
        self.assertEqual(vals['source_city'], 'ZZT Città')
        self.assertEqual(vals['source_phone'], '')

    def test_source_address_filled_when_picking_chosen_manually(self):
        # Piano creato dal menu, trasferimento scelto dopo: l'indirizzo va
        # compilato anche qui, non solo dal bottone sul trasferimento.
        partner = self.picking.picking_type_id.warehouse_id.partner_id or self.env.company.partner_id
        partner.write({'street': 'ZZT Via 1', 'city': 'ZZT Città', 'zip': '00000'})
        plan = self.env['ke.amazon.inbound.plan'].new({'picking_id': self.picking.id})
        plan._onchange_picking_id_source_address()
        self.assertEqual(plan.source_address_line1, 'ZZT Via 1')
        self.assertEqual(plan.source_city, 'ZZT Città')
        self.assertEqual(plan.source_postal_code, '00000')

    def test_fill_remaining_single_box_takes_whole_picking(self):
        # Richiesto dall'utente il 2026-10-30: un collo solo, tutto il
        # trasferimento in un click senza riscansionare.
        box = self.env['ke.amazon.inbound.plan.box'].create({'plan_id': self.plan.id, 'name': 'Collo 1'})
        box.action_fill_remaining()
        self.assertEqual(len(box.line_ids), 1)
        self.assertEqual(box.line_ids.product_id, self.product)
        self.assertEqual(box.line_ids.quantity, 3)
        with self.assertRaises(UserError):
            box.action_fill_remaining()  # niente più da aggiungere

    def test_fill_remaining_second_box_takes_only_the_rest(self):
        first = self.env['ke.amazon.inbound.plan.box'].create({'plan_id': self.plan.id, 'name': 'Collo 1'})
        self.env['ke.amazon.inbound.plan.box.line'].create({
            'box_id': first.id, 'product_id': self.product.id, 'quantity': 1,
        })
        second = self.env['ke.amazon.inbound.plan.box'].create({'plan_id': self.plan.id, 'name': 'Collo 2'})
        second.action_fill_remaining()
        self.assertEqual(second.line_ids.quantity, 2, '3 previste - 1 già nel primo collo')
        self.assertEqual(first.line_ids.quantity, 1, 'il primo collo non viene toccato')

    def test_fill_remaining_sums_on_existing_line(self):
        box = self.env['ke.amazon.inbound.plan.box'].create({'plan_id': self.plan.id, 'name': 'Collo 1'})
        self.env['ke.amazon.inbound.plan.box.line'].create({
            'box_id': box.id, 'product_id': self.product.id, 'quantity': 1,
        })
        box.action_fill_remaining()
        self.assertEqual(len(box.line_ids), 1, 'nessuna riga doppia per lo stesso prodotto')
        self.assertEqual(box.line_ids.quantity, 3)

    def test_label_and_prep_owner_follow_fnsku(self):
        # Amazon rifiutava il piano con labelOwner/prepOwner sempre SELLER
        # ("Accepted values: [NONE]", primo piano reale del 2026-10-30).
        client = FakeInboundClient()
        self.product.x_amazon_fnsku = 'X0ZZINB0001'
        with self._with_client(client):
            self.plan.action_create_plan()
        item = client.last_items[0]
        self.assertEqual(item['labelOwner'], 'SELLER', 'FNSKU X0: etichetta da applicare')
        self.assertEqual(item['prepOwner'], 'NONE')

        service = FbaInboundService(self.env, client)
        self.product.x_amazon_fnsku = 'B0ZZINB0001'
        self.assertEqual(
            service._owners_for(self.product), {'labelOwner': 'NONE', 'prepOwner': 'NONE'},
            "marchio proprio (B0 = ASIN): nessuna etichetta, Amazon accetta solo NONE",
        )

    def test_reset_to_draft_only_if_never_created_on_amazon(self):
        client = FakeInboundClient(fail_at='create_inbound_plan')
        with self._with_client(client):
            try:
                self.plan.action_create_plan()
            except UserError:
                pass
        self.assertEqual(self.plan.state, 'error')
        self.plan.action_reset_to_draft()
        self.assertEqual(self.plan.state, 'draft')
        self.assertFalse(self.plan.error_message)

        self.plan.write({'state': 'error', 'amazon_inbound_plan_id': 'PLAN_ESISTENTE'})
        with self.assertRaises(UserError):
            self.plan.action_reset_to_draft()

    def test_copy_source_address_from_latest_amazon_plan(self):
        # Richiesto dall'utente il 2026-10-30: telefono/email del mittente
        # da un piano già fatto su Seller Central (l'unica fonte Amazon).
        client = FakeInboundClient()
        client.existing_plans = [
            {'inboundPlanId': 'PLAN_SENZA_INDIRIZZO', 'sourceAddress': {}},
            {'inboundPlanId': 'PLAN_SC', 'sourceAddress': {
                'name': 'Arundo Magazzino', 'addressLine1': 'Via Amazon 1', 'addressLine2': 'Int. 2',
                'city': 'Cavallino', 'stateOrProvinceCode': 'LE', 'postalCode': '73020',
                'countryCode': 'IT', 'phoneNumber': '+39 000 0000', 'email': '',
            }},
        ]
        self.plan.source_email = 'gia@compilata.it'
        with self._with_client(client):
            self.plan.action_copy_source_address_from_amazon()
        self.assertEqual(self.plan.source_address_line1, 'Via Amazon 1, Int. 2')
        self.assertEqual(self.plan.source_state_code, 'LE')
        self.assertEqual(self.plan.source_phone, '+39 000 0000')
        self.assertEqual(self.plan.source_email, 'gia@compilata.it', 'un campo vuoto su Amazon non cancella quello in Odoo')
        self.assertNotIn('create_inbound_plan', client.calls, 'solo lettura')

    def test_copy_source_address_without_amazon_plans(self):
        client = FakeInboundClient()
        with self._with_client(client):
            with self.assertRaises(UserError):
                self.plan.action_copy_source_address_from_amazon()
        self.assertEqual(self.plan.state, 'draft', 'una lettura fallita non porta il piano in errore')
