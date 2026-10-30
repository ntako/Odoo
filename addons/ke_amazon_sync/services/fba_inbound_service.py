"""
Orchestrazione del piano di spedizione inbound FBA (Fulfillment Inbound API
v2024-03-20) — un metodo per ogni transizione di stato di
ke.amazon.inbound.plan. Dominio distinto da sync_engine.py (import/sync/
reconcile): qui si CREA/CONFERMA qualcosa su Amazon, multi-step, con un
punto di non ritorno esplicito (confirm_placement_option). Nessuna chiamata
SP-API diretta nei modelli — solo qui, tramite SpApiClient.

ATTENZIONE — vedi RICERCA_fulfillment_inbound_api_sandbox_2026-10-29.md:
confirm_placement_option (chiamato da confirm_placement_option() sotto) è
dichiarato irreversibile dalla documentazione ufficiale Amazon. Nessun
metodo qui lo richiama da solo: solo
ke.amazon.inbound.plan.action_confirm_placement_option (dopo conferma
esplicita dell'utente nella UI) chiama questo servizio per farlo.
"""
import json
from datetime import datetime, timedelta, timezone

from odoo.exceptions import UserError


class FbaInboundService:
    def __init__(self, env, client):
        self.env = env
        self.client = client

    # ------------------------------------------------------------------
    # Costruzione payload
    # ------------------------------------------------------------------

    def _sku_for(self, product):
        sku = product.x_amazon_sku or product.default_code
        if not sku:
            raise UserError(f'{product.display_name}: nessun SKU Amazon né codice interno.')
        return sku

    def _build_items(self, plan):
        """Un elemento per prodotto del trasferimento collegato (quantità aggregata se il
        prodotto compare su più righe), non per collo: il manifesto del piano è fisso alla
        creazione, l'imballaggio nei colli è un passo successivo."""
        qty_by_product = {}
        for move in plan.picking_id.move_ids:
            qty_by_product[move.product_id] = qty_by_product.get(move.product_id, 0) + move.product_uom_qty
        if not qty_by_product:
            raise UserError('Il trasferimento collegato non ha righe.')
        return [{
            'msku': self._sku_for(product),
            'quantity': int(qty),
            'labelOwner': 'SELLER',
            'prepOwner': 'SELLER',
        } for product, qty in qty_by_product.items()]

    def _build_source_address(self, plan):
        missing = [label for field, label in [
            ('source_address_line1', 'Indirizzo'), ('source_city', 'Città'),
            ('source_postal_code', 'CAP'), ('source_country_code', 'Paese'),
        ] if not getattr(plan, field)]
        if missing:
            raise UserError(f'Indirizzo di origine incompleto, manca: {", ".join(missing)}.')
        return {
            'name': plan.source_name or plan.picking_id.picking_type_id.warehouse_id.name,
            'addressLine1': plan.source_address_line1,
            'city': plan.source_city,
            'stateOrProvinceCode': plan.source_state_code or '',
            'countryCode': plan.source_country_code,
            'postalCode': plan.source_postal_code,
            'email': plan.source_email or '',
            'phoneNumber': plan.source_phone or '',
        }

    def _build_box_payload(self, box):
        return {
            'contentInformationSource': 'BOX_CONTENT_PROVIDED',
            'dimensions': {
                'length': box.length_cm, 'width': box.width_cm, 'height': box.height_cm,
                'unitOfMeasurement': 'CM',
            },
            'weight': {'value': box.weight_kg, 'unit': 'KG'},
            'quantity': 1,
            'items': [{
                'msku': self._sku_for(line.product_id),
                'quantity': line.quantity,
                'labelOwner': 'SELLER',
                'prepOwner': 'SELLER',
            } for line in box.line_ids],
        }

    def _contact_information(self, plan):
        if not (plan.source_name and plan.source_phone):
            raise UserError('Nome e telefono dell\'indirizzo di origine sono richiesti da Amazon come referente.')
        return {'name': plan.source_name, 'phoneNumber': plan.source_phone}

    def _dump(self, payload):
        return json.dumps(payload, indent=2, ensure_ascii=False, default=str)

    # ------------------------------------------------------------------
    # Step del workflow
    # ------------------------------------------------------------------

    def create_plan(self, plan):
        items = self._build_items(plan)
        source_address = self._build_source_address(plan)
        resp = self.client.create_inbound_plan(
            name=plan.name,
            source_address=source_address,
            destination_marketplace_ids=[plan.marketplace_id.marketplace_id],
            items=items,
        )
        inbound_plan_id = resp['inboundPlanId']
        self.client.poll_inbound_operation(resp['operationId'])
        plan.write({'amazon_inbound_plan_id': inbound_plan_id, 'state': 'plan_created'})
        self.env.cr.commit()

    def generate_and_list_packing_options(self, plan):
        resp = self.client.generate_packing_options(plan.amazon_inbound_plan_id)
        self.client.poll_inbound_operation(resp['operationId'])
        listing = self.client.list_packing_options(plan.amazon_inbound_plan_id)
        options = listing.get('packingOptions') or []
        if not options:
            raise UserError('Amazon non ha restituito nessuna opzione di imballaggio.')
        first = options[0]
        groups = first.get('packingGroups') or []
        plan.write({
            'last_options_raw': self._dump(listing),
            'packing_option_id': first['packingOptionId'],
            'packing_group_id': groups[0] if groups else '',
        })
        self.env.cr.commit()

    def confirm_packing_option(self, plan):
        resp = self.client.confirm_packing_option(plan.amazon_inbound_plan_id, plan.packing_option_id)
        self.client.poll_inbound_operation(resp['operationId'])
        plan.write({'state': 'packing_confirmed'})
        self.env.cr.commit()

    def set_packing_information(self, plan):
        groupings = [{
            'packingGroupId': plan.packing_group_id,
            'boxes': [self._build_box_payload(box) for box in plan.box_ids],
        }]
        resp = self.client.set_packing_information(plan.amazon_inbound_plan_id, groupings)
        self.client.poll_inbound_operation(resp['operationId'])
        self.env.cr.commit()

    def generate_and_list_placement_options(self, plan):
        resp = self.client.generate_placement_options(plan.amazon_inbound_plan_id)
        self.client.poll_inbound_operation(resp['operationId'])
        listing = self.client.list_placement_options(plan.amazon_inbound_plan_id)
        options = listing.get('placementOptions') or []
        if not options:
            raise UserError('Amazon non ha restituito nessuna opzione di collocazione.')
        first = options[0]
        plan.write({
            'last_options_raw': self._dump(listing),
            'placement_option_id': first['placementOptionId'],
            'state': 'placement_options_ready',
        })
        Shipment = self.env['ke.amazon.inbound.plan.shipment']
        existing = set(plan.shipment_ids.mapped('amazon_shipment_id'))
        for shipment_id in first.get('shipmentIds', []):
            if shipment_id not in existing:
                Shipment.create({'plan_id': plan.id, 'amazon_shipment_id': shipment_id})
        self.env.cr.commit()

    def confirm_placement_option(self, plan):
        resp = self.client.confirm_placement_option(plan.amazon_inbound_plan_id, plan.placement_option_id)
        self.client.poll_inbound_operation(resp['operationId'])
        for shipment in plan.shipment_ids:
            detail = self.client.get_shipment(plan.amazon_inbound_plan_id, shipment.amazon_shipment_id)
            destination = detail.get('destination') or {}
            shipment.write({
                'shipment_confirmation_id': detail.get('shipmentConfirmationId') or '',
                'destination_fc_name': destination.get('warehouseId') or detail.get('name') or '',
            })
        plan.write({
            'state': 'placement_confirmed',
            'confirmed_by': self.env.uid,
            'confirmed_date': datetime.now(),
        })
        self.env.cr.commit()

    def generate_and_list_delivery_window_options(self, plan):
        raw_by_shipment = {}
        for shipment in plan.shipment_ids:
            resp = self.client.generate_delivery_window_options(
                plan.amazon_inbound_plan_id, shipment.amazon_shipment_id,
            )
            self.client.poll_inbound_operation(resp['operationId'])
            listing = self.client.list_delivery_window_options(
                plan.amazon_inbound_plan_id, shipment.amazon_shipment_id,
            )
            raw_by_shipment[shipment.amazon_shipment_id] = listing
            options = listing.get('deliveryWindowOptions') or []
            if options:
                shipment.write({'delivery_window_option_id': options[0]['deliveryWindowOptionId']})
        plan.write({
            'last_options_raw': self._dump(raw_by_shipment),
            'state': 'delivery_window_options_ready',
        })
        self.env.cr.commit()

    def confirm_delivery_window_options(self, plan):
        for shipment in plan.shipment_ids:
            resp = self.client.confirm_delivery_window_options(
                plan.amazon_inbound_plan_id, shipment.amazon_shipment_id,
                shipment.delivery_window_option_id,
            )
            self.client.poll_inbound_operation(resp['operationId'])
        plan.write({'state': 'delivery_window_confirmed'})
        self.env.cr.commit()

    def generate_and_list_transportation_options(self, plan):
        # Finestra indicativa "da qui a 2 giorni" per la quotazione, come
        # nello script di esplorazione sicura (AMZ_TESTS/02_scritture_esempio/
        # explore_inbound_safe.py) — non è un impegno, solo un input al preventivo.
        ready_to_ship_start = (datetime.now(timezone.utc) + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        contact = self._contact_information(plan)
        configs = [{
            'shipmentId': shipment.amazon_shipment_id,
            'readyToShipWindow': {'start': ready_to_ship_start},
            'contactInformation': contact,
        } for shipment in plan.shipment_ids]
        resp = self.client.generate_transportation_options(
            plan.amazon_inbound_plan_id, plan.placement_option_id, configs,
        )
        self.client.poll_inbound_operation(resp['operationId'])
        listing = self.client.list_transportation_options(
            plan.amazon_inbound_plan_id, plan.placement_option_id,
        )
        first_by_shipment = {}
        for option in listing.get('transportationOptions') or []:
            sid = option.get('shipmentId')
            if sid and sid not in first_by_shipment:
                first_by_shipment[sid] = option
        for shipment in plan.shipment_ids:
            option = first_by_shipment.get(shipment.amazon_shipment_id)
            if option:
                shipment.write({'transportation_option_id': option['transportationOptionId']})
        plan.write({'last_options_raw': self._dump(listing), 'state': 'transport_options_ready'})
        self.env.cr.commit()

    def confirm_transportation_options(self, plan):
        contact = self._contact_information(plan)
        selections = [{
            'shipmentId': shipment.amazon_shipment_id,
            'transportationOptionId': shipment.transportation_option_id,
            'contactInformation': contact,
        } for shipment in plan.shipment_ids]
        resp = self.client.confirm_transportation_options(plan.amazon_inbound_plan_id, selections)
        self.client.poll_inbound_operation(resp['operationId'])
        plan.write({'state': 'transport_confirmed'})
        self.env.cr.commit()

    def get_labels(self, plan):
        for shipment in plan.shipment_ids:
            payload = self.client.get_labels(shipment.amazon_shipment_id)
            shipment.write({'labels_raw_response': self._dump(payload)})
        self.env.cr.commit()

    def cancel_plan(self, plan):
        self.client.cancel_inbound_plan(plan.amazon_inbound_plan_id)
        plan.write({'state': 'cancelled'})
        self.env.cr.commit()
