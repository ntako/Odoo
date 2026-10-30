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
import base64
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

    def _owners_for(self, product):
        """
        labelOwner/prepOwner per uno SKU. Prima erano sempre 'SELLER', e
        Amazon rifiutava il piano (createInboundPlan, 2026-10-30): "does not
        require labelOwner but SELLER was assigned. Accepted values: [NONE]".
        - labelOwner: SELLER solo se l'FNSKU va davvero stampato (prefisso
          'X0'); marchio proprio ('B0', in realtà l'ASIN) → NONE, stessa
          regola dell'etichettatura (product._amazon_fnsku_needs_label).
        - prepOwner: NONE — nessun articolo di questo venditore ha richiesto
          preparazione finora (Amazon ha accettato solo NONE su entrambi gli
          SKU del primo piano reale). Se un giorno uno SKU la richiedesse,
          Amazon lo dice nello stesso modo ("requires prepOwner") e andrà
          gestito per prodotto.
        """
        return {
            'labelOwner': 'SELLER' if product._amazon_fnsku_needs_label() else 'NONE',
            'prepOwner': 'NONE',
        }

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
            **self._owners_for(product),
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
                **self._owners_for(line.product_id),
            } for line in box.line_ids],
        }

    def _contact_information(self, plan):
        if not (plan.source_name and plan.source_phone):
            raise UserError('Nome e telefono dell\'indirizzo di origine sono richiesti da Amazon come referente.')
        return {'name': plan.source_name, 'phoneNumber': plan.source_phone}

    def _dump(self, payload):
        return json.dumps(payload, indent=2, ensure_ascii=False, default=str)

    def _all_pages(self, fetch, key):
        """Unisce le pagine di una list* v2024 (pagination.nextToken)."""
        items, token, raw = [], None, []
        while True:
            page = fetch(token)
            raw.append(page)
            items.extend(page.get(key) or [])
            token = (page.get('pagination') or {}).get('nextToken')
            if not token:
                return items, raw

    @staticmethod
    def _amount(currency):
        return float((currency or {}).get('amount') or 0.0)

    # ------------------------------------------------------------------
    # Step del workflow
    # ------------------------------------------------------------------

    def latest_source_address(self, exclude_plan_id=None):
        """
        Indirizzo di origine dell'ultimo piano inbound già presente
        sull'account (anche creato da Seller Central con "Send to Amazon"),
        come valori dei campi source_* del piano Odoo — solo lettura, nessun
        impegno. Ritorna {} se nessun piano ha un indirizzo. 'exclude_plan_id'
        salta il piano stesso, se fosse già stato creato.
        """
        payload = self.client.list_inbound_plans()
        for summary in payload.get('inboundPlans') or []:
            if exclude_plan_id and summary.get('inboundPlanId') == exclude_plan_id:
                continue
            address = summary.get('sourceAddress') or {}
            if not address.get('addressLine1'):
                continue
            line1 = ', '.join(filter(None, [address.get('addressLine1'), address.get('addressLine2')]))
            return {
                'source_name': address.get('name') or address.get('companyName') or '',
                'source_address_line1': line1,
                'source_city': address.get('city') or '',
                'source_state_code': address.get('stateOrProvinceCode') or '',
                'source_country_code': address.get('countryCode') or '',
                'source_postal_code': address.get('postalCode') or '',
                'source_email': address.get('email') or '',
                'source_phone': address.get('phoneNumber') or '',
            }
        return {}

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
        plan.write({'state': 'packing_info_sent'})
        self.env.cr.commit()

    def generate_and_list_placement_options(self, plan):
        """
        Genera le opzioni di collocazione e le salva come righe selezionabili
        (ke.amazon.inbound.plan.option, kind='placement'): prima veniva
        presa d'ufficio la prima e confermata così, proprio sul passo
        irreversibile (2026-10-30). Preselezionata la prima, nell'ordine
        di Amazon; le spedizioni si creano solo alla conferma, da quella
        effettivamente scelta.
        """
        resp = self.client.generate_placement_options(plan.amazon_inbound_plan_id)
        self.client.poll_inbound_operation(resp['operationId'])
        options, raw = self._all_pages(
            lambda token: self.client.list_placement_options(plan.amazon_inbound_plan_id, pagination_token=token),
            'placementOptions',
        )
        options = [o for o in options if o.get('status') != 'EXPIRED']
        if not options:
            raise UserError('Amazon non ha restituito nessuna opzione di collocazione.')
        plan.option_ids.filtered(lambda o: o.kind == 'placement').unlink()
        Option = self.env['ke.amazon.inbound.plan.option']
        for index, option in enumerate(options):
            fees = sum(self._amount(f.get('value')) for f in option.get('fees') or [])
            discounts = sum(self._amount(d.get('value')) for d in option.get('discounts') or [])
            currency = next(
                ((i.get('value') or {}).get('code') for i in (option.get('fees') or []) + (option.get('discounts') or [])),
                '',
            )
            shipment_ids = option.get('shipmentIds') or []
            details = '; '.join(
                f"{i.get('description') or i.get('target')}: {self._amount(i.get('value')):g}"
                for i in (option.get('fees') or []) + (option.get('discounts') or [])
                if self._amount(i.get('value'))
            )
            Option.create({
                'plan_id': plan.id, 'kind': 'placement',
                'amazon_option_id': option['placementOptionId'],
                'amazon_shipment_ids': ','.join(shipment_ids),
                'description': f"{len(shipment_ids)} spedizion{'e' if len(shipment_ids) == 1 else 'i'}"
                               + (f' — {details}' if details else ''),
                'cost': fees - discounts, 'currency_code': currency or '',
                'expiration': option.get('expiration') or '',
                'is_selected': index == 0,
            })
        plan.write({
            'last_options_raw': self._dump(raw),
            'placement_option_id': options[0]['placementOptionId'],
            'state': 'placement_options_ready',
        })
        self.env.cr.commit()

    def confirm_placement_option(self, plan):
        chosen = plan.option_ids.filtered(
            lambda o: o.kind == 'placement' and o.amazon_option_id == plan.placement_option_id
        )
        if not chosen:
            raise UserError('Nessuna opzione di collocazione selezionata.')
        resp = self.client.confirm_placement_option(plan.amazon_inbound_plan_id, plan.placement_option_id)
        self.client.poll_inbound_operation(resp['operationId'])
        # Le spedizioni sono quelle dell'opzione CONFERMATA, non di un'altra
        # solo elencata.
        wanted = [sid for sid in (chosen.amazon_shipment_ids or '').split(',') if sid]
        plan.shipment_ids.filtered(lambda s: s.amazon_shipment_id not in wanted).unlink()
        existing = set(plan.shipment_ids.mapped('amazon_shipment_id'))
        for shipment_id in wanted:
            if shipment_id not in existing:
                self.env['ke.amazon.inbound.plan.shipment'].create({
                    'plan_id': plan.id, 'amazon_shipment_id': shipment_id,
                })
        plan.write({
            'state': 'placement_confirmed',
            'confirmed_by': self.env.uid,
            'confirmed_date': datetime.now(),
        })
        # Commit subito dopo la conferma irreversibile: i dettagli sotto
        # (destinazione, ID FBA) sono letture, se fallissero lo stato
        # confermato non deve andare perso.
        self.env.cr.commit()
        self.refresh_shipment_details(plan)

    def refresh_shipment_details(self, plan):
        for shipment in plan.shipment_ids:
            detail = self.client.get_shipment(plan.amazon_inbound_plan_id, shipment.amazon_shipment_id)
            destination = detail.get('destination') or {}
            shipment.write({
                'shipment_confirmation_id': detail.get('shipmentConfirmationId') or '',
                'destination_fc_name': destination.get('warehouseId') or detail.get('name') or '',
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
        """
        Preventivi di trasporto salvati come righe selezionabili per
        spedizione (kind='transport'): solo corrieri partner Amazon (scelta
        della v1 — il percorso con corriere proprio richiede tracking e
        appuntamenti, non gestiti). Preselezionato il più economico per
        ogni spedizione; prima veniva presa d'ufficio la prima elencata.
        """
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
        options, raw = self._all_pages(
            lambda token: self.client.list_transportation_options(
                plan.amazon_inbound_plan_id, plan.placement_option_id, pagination_token=token,
            ),
            'transportationOptions',
        )
        plan.option_ids.filtered(lambda o: o.kind == 'transport').unlink()
        Option = self.env['ke.amazon.inbound.plan.option']
        for shipment in plan.shipment_ids:
            partnered = [
                o for o in options
                if o.get('shipmentId') == shipment.amazon_shipment_id
                and o.get('shippingSolution') == 'AMAZON_PARTNERED_CARRIER'
            ]
            if not partnered:
                raise UserError(
                    f'Nessuna opzione con corriere partner Amazon per la spedizione '
                    f'{shipment.amazon_shipment_id}: questa versione gestisce solo quelle.'
                )
            partnered.sort(key=lambda o: self._amount((o.get('quote') or {}).get('cost')))
            for index, option in enumerate(partnered):
                quote = option.get('quote') or {}
                carrier = option.get('carrier') or {}
                Option.create({
                    'plan_id': plan.id, 'kind': 'transport', 'shipment_id': shipment.id,
                    'amazon_option_id': option['transportationOptionId'],
                    'description': f"{carrier.get('name') or carrier.get('alphaCode') or 'Corriere'}"
                                   f" — {option.get('shippingMode') or ''}",
                    'cost': self._amount(quote.get('cost')),
                    'currency_code': (quote.get('cost') or {}).get('code') or '',
                    'expiration': quote.get('expiration') or '',
                    'is_selected': index == 0,
                })
            shipment.write({'transportation_option_id': partnered[0]['transportationOptionId']})
        plan.write({'last_options_raw': self._dump(raw), 'state': 'transport_options_ready'})
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
        """
        Per ogni spedizione: colli assegnati da Amazon (listShipmentBoxes,
        con il contenuto, per sapere quale collo fisico va in quale
        spedizione) e PDF delle etichette (getLabels v0, vedi
        SpApiClient.get_labels), salvato come allegato della spedizione.
        """
        Attachment = self.env['ir.attachment']
        for shipment in plan.shipment_ids:
            if not shipment.shipment_confirmation_id:
                self.refresh_shipment_details(plan)
            boxes, _raw = self._all_pages(
                lambda token, sid=shipment.amazon_shipment_id: self.client.list_shipment_boxes(
                    plan.amazon_inbound_plan_id, sid, pagination_token=token,
                ),
                'boxes',
            )
            box_ids = [b['boxId'] for b in boxes if b.get('boxId')]
            if not box_ids:
                raise UserError(f'Amazon non ha restituito i colli della spedizione {shipment.amazon_shipment_id}.')
            pdf = self.client.get_labels(shipment.shipment_confirmation_id, plan.label_page_type, box_ids)
            contents = '\n'.join(
                f"{b.get('boxId')}: " + ', '.join(f"{i.get('msku')} x{i.get('quantity')}" for i in b.get('items') or [])
                for b in boxes
            )
            old = shipment.labels_attachment_id
            attachment = Attachment.create({
                'name': f'Etichette colli {shipment.shipment_confirmation_id or shipment.amazon_shipment_id}.pdf',
                'datas': base64.b64encode(pdf),
                'mimetype': 'application/pdf',
                'res_model': 'ke.amazon.inbound.plan.shipment',
                'res_id': shipment.id,
            })
            shipment.write({
                'labels_attachment_id': attachment.id,
                'box_count': len(box_ids),
                'box_contents': contents,
            })
            old.unlink()
            self.env.cr.commit()

    def cancel_plan(self, plan):
        self.client.cancel_inbound_plan(plan.amazon_inbound_plan_id)
        plan.write({'state': 'cancelled'})
        self.env.cr.commit()
