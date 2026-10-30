from odoo import api, fields, models
from odoo.exceptions import UserError

from ..services.sp_api_client import SpApiClient
from ..services.fba_inbound_service import FbaInboundService


class AmazonInboundPlan(models.Model):
    _name = 'ke.amazon.inbound.plan'
    _description = 'Piano di spedizione inbound FBA (Fulfillment Inbound API v2024-03-20)'
    _order = 'id desc'

    name = fields.Char(required=True, copy=False, default='Nuovo')
    picking_id = fields.Many2one(
        'stock.picking', string='Trasferimento FBA', required=True, ondelete='restrict',
        # Solo 'state' nel domain: x_amazon_is_fba_shipment è un campo
        # calcolato non memorizzato (compute, niente store/search) — usarlo
        # in un domain fa fallire la validazione della vista al caricamento
        # del modulo ("Campo non ricercabile"), trovato dal vivo il
        # 2026-10-29 con un -u reale. Il controllo vero e proprio è nel
        # vincolo _check_picking_is_fba_shipment sotto, non solo nel domain
        # (che resta solo un aiuto per restringere la tendina).
        domain="[('state', '=', 'done')]",
        help="Trasferimento 'In transito verso Amazon' già validato e già etichettato "
             "FNSKU (wizard di preparazione FBA) — il piano riusa i suoi SKU/quantità, "
             "non ne definisce di nuovi.",
    )
    marketplace_id = fields.Many2one(
        'ke.amazon.marketplace', string='Marketplace', required=True,
        help='Marketplace di destinazione. Per un pool EU (es. IT/DE/ES/FR condiviso), '
             'un qualunque marketplace pooled attivo va bene.',
    )

    # Indirizzo di origine — snapshot editabile (Amazon richiede un oggetto
    # indirizzo fisso per piano, non un riferimento vivo a un partner che
    # potrebbe cambiare dopo). Default dal magazzino sorgente del picking.
    source_name = fields.Char('Nome (indirizzo origine)')
    source_address_line1 = fields.Char('Indirizzo (indirizzo origine)')
    source_city = fields.Char('Città (indirizzo origine)')
    source_state_code = fields.Char('Provincia/stato (indirizzo origine)')
    source_country_code = fields.Char('Paese (indirizzo origine, ISO2)', default='IT')
    source_postal_code = fields.Char('CAP (indirizzo origine)')
    source_email = fields.Char('Email (indirizzo origine)')
    source_phone = fields.Char('Telefono (indirizzo origine)')

    amazon_inbound_plan_id = fields.Char('Inbound Plan ID (Amazon)', readonly=True, copy=False)
    packing_option_id = fields.Char(readonly=True, copy=False)
    packing_group_id = fields.Char(
        readonly=True, copy=False,
        help='Primo packingGroupId offerto dalla opzione di imballaggio confermata — '
             "semplificazione v1: tutti i colli del piano vengono assegnati allo stesso "
             'gruppo, nessuno split su più gruppi.',
    )
    placement_option_id = fields.Char(readonly=True, copy=False)
    last_options_raw = fields.Text(
        readonly=True, copy=False,
        help='Payload grezzo delle ultime opzioni elencate da Amazon (packing/placement/'
             'trasporto/finestra di consegna) — per controllo visivo prima di confermare, '
             "sovrascritto a ogni step 'genera/elenca'.",
    )

    # Ordine della sequenza verificato sulla guida ufficiale "Create an
    # inbound shipment" (vedi RICERCA_fulfillment_inbound_api_sandbox_
    # 2026-10-29.md §2, passi 10-12): confirmPlacementOption, POI
    # confirmDeliveryWindowOptions, POI confirmTransportationOptions — non
    # l'ordine inverso. Nota onesta: la documentazione della libreria segnala
    # che la finestra di consegna è un prerequisito STRETTO solo per le
    # opzioni di trasporto con program='CONFIRMED_DELIVERY_WINDOW', non per
    # tutte — questa sequenza fissa è la più sicura per default, da
    # verificare contro una risposta reale se mai risultasse troppo rigida.
    state = fields.Selection([
        ('draft', 'Bozza'),
        ('plan_created', 'Piano creato'),
        ('packing_confirmed', 'Imballaggio confermato'),
        ('placement_options_ready', 'Opzioni di collocazione pronte'),
        ('placement_confirmed', 'Collocazione confermata (irreversibile)'),
        ('delivery_window_options_ready', 'Opzioni finestra di consegna pronte'),
        ('delivery_window_confirmed', 'Finestra di consegna confermata'),
        ('transport_options_ready', 'Opzioni di trasporto pronte'),
        ('transport_confirmed', 'Trasporto confermato'),
        ('labels_ready', 'Etichette pronte'),
        ('shipped', 'Spedito'),
        ('done', 'Completato'),
        ('error', 'Errore'),
        ('cancelled', 'Annullato'),
    ], default='draft', required=True, index=True, copy=False)

    error_message = fields.Text(readonly=True, copy=False)
    confirmed_by = fields.Many2one(
        'res.users', readonly=True, copy=False,
        help='Chi ha premuto il bottone di conferma collocazione — audit del momento '
             'irreversibile (confirmPlacementOption).',
    )
    confirmed_date = fields.Datetime(readonly=True, copy=False)

    box_ids = fields.One2many('ke.amazon.inbound.plan.box', 'plan_id', string='Colli')
    shipment_ids = fields.One2many('ke.amazon.inbound.plan.shipment', 'plan_id', string='Spedizioni')

    @api.constrains('picking_id')
    def _check_picking_is_fba_shipment(self):
        for plan in self:
            if not plan.picking_id.x_amazon_is_fba_shipment:
                raise UserError(
                    f'{plan.picking_id.name} non è una spedizione FBA '
                    f"(destinazione 'In transito verso Amazon')."
                )

    @api.constrains('picking_id', 'state')
    def _check_picking_unique(self):
        for plan in self:
            other = self.search([
                ('picking_id', '=', plan.picking_id.id),
                ('id', '!=', plan.id),
                ('state', 'not in', ('error', 'cancelled')),
            ], limit=1)
            if other:
                raise UserError(
                    f'{plan.picking_id.name} è già collegato al piano inbound "{other.name}".'
                )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        picking_id = self.env.context.get('default_picking_id') or self.env.context.get('active_id')
        if picking_id and 'picking_id' in fields_list:
            picking = self.env['stock.picking'].browse(picking_id)
            res['picking_id'] = picking.id
            warehouse = picking.picking_type_id.warehouse_id
            partner = warehouse.partner_id or self.env.company.partner_id
            if partner and 'source_name' in fields_list:
                res.update({
                    'source_name': partner.name or warehouse.name,
                    'source_address_line1': partner.street or '',
                    'source_city': partner.city or '',
                    'source_state_code': partner.state_id.code or '',
                    'source_country_code': partner.country_id.code or 'IT',
                    'source_postal_code': partner.zip or '',
                    'source_email': partner.email or '',
                    'source_phone': partner.phone or partner.mobile or '',
                })
        return res

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'Nuovo') == 'Nuovo' and vals.get('picking_id'):
                # Nessun ir.sequence dedicato: il nome deriva dal trasferimento
                # collegato, già univoco (1 piano attivo per picking — vedi
                # _check_picking_unique), niente da inventare in più.
                picking = self.env['stock.picking'].browse(vals['picking_id'])
                vals['name'] = f'INB/{picking.name}'
        return super().create(vals_list)

    def _build_client(self):
        self.ensure_one()
        client = SpApiClient(self.env, marketplace_id=self.marketplace_id.marketplace_id)
        client.validate_credentials()
        return client

    def _service(self):
        return FbaInboundService(self.env, self._build_client())

    def _require_state(self, expected):
        self.ensure_one()
        if self.state != expected:
            raise UserError(
                f'Azione non disponibile nello stato attuale ("{self.state}") — '
                f'richiede "{expected}".'
            )

    def _handle_step_error(self, exc):
        """
        Il commit esplicito qui non è opzionale: senza, lo stato 'error'
        scritto sotto resterebbe dentro la stessa transazione dell'eccezione
        rilanciata subito dopo — se qualcosa più a monte (dispatch web,
        chiamante) fa un rollback sull'errore, lo stato 'error' sparirebbe
        con esso, lasciando il piano bloccato nel suo stato precedente senza
        traccia del fallimento. Stesso pattern già usato ovunque nel modulo
        per i log di sync in thread (vedi amazon_sync_log.py). Nei test,
        questo commit è reso no-op da no_commit (vedi tests/common.py) — per
        verificare che lo stato sopravviva comunque a un rollback ambientale
        va usato un try/except esplicito, non self.assertRaises (che avvolge
        il blocco in un suo proprio savepoint, annullato apposta quando
        l'eccezione attesa si verifica — vedi test_inbound_plan.py).
        """
        self.ensure_one()
        self.write({'state': 'error', 'error_message': str(exc)})
        self.env.cr.commit()
        raise UserError(f'{exc}')

    # ------------------------------------------------------------------
    # Step del workflow — ciascuno valida lo stato di partenza, chiama il
    # service (vera logica/chiamate SP-API) e si limita ad aggiornare lo
    # stato qui. Nessuna chiamata API diretta in questo modello — vedi
    # services/fba_inbound_service.py.
    # ------------------------------------------------------------------

    def action_create_plan(self):
        self.ensure_one()
        self._require_state('draft')
        if not self.box_ids:
            # Gli articoli/quantità del piano vengono dal picking, non dai
            # colli (quelli si scansionano dopo) — ma non ha senso creare un
            # piano per un trasferimento senza righe.
            if not self.picking_id.move_ids:
                raise UserError('Il trasferimento selezionato non ha righe.')
        try:
            self._service().create_plan(self)
        except Exception as exc:
            self._handle_step_error(exc)

    def action_generate_packing_options(self):
        self.ensure_one()
        self._require_state('plan_created')
        try:
            self._service().generate_and_list_packing_options(self)
        except Exception as exc:
            self._handle_step_error(exc)

    def action_confirm_packing_option(self):
        self.ensure_one()
        self._require_state('plan_created')
        if not self.packing_option_id:
            raise UserError('Genera prima le opzioni di imballaggio.')
        try:
            self._service().confirm_packing_option(self)
        except Exception as exc:
            self._handle_step_error(exc)

    def action_set_packing_information(self):
        self.ensure_one()
        self._require_state('packing_confirmed')
        open_boxes = self.box_ids.filtered(lambda b: b.state == 'open')
        if open_boxes:
            raise UserError(
                f'Chiudi tutti i colli prima di procedere (ancora aperti: '
                f'{", ".join(open_boxes.mapped("name"))}).'
            )
        if not self.box_ids:
            raise UserError('Nessun collo definito.')
        all_lines = self.box_ids.line_ids
        for move in self.picking_id.move_ids:
            expected = move.product_uom_qty
            packed = sum(all_lines.filtered(lambda l: l.product_id == move.product_id).mapped('quantity'))
            if packed != expected:
                raise UserError(
                    f'{move.product_id.display_name}: imballate {packed:g} unità su {expected:g} '
                    f'previste — completa i colli prima di procedere.'
                )
        try:
            self._service().set_packing_information(self)
        except Exception as exc:
            self._handle_step_error(exc)

    def action_generate_placement_options(self):
        self.ensure_one()
        self._require_state('packing_confirmed')
        try:
            self._service().generate_and_list_placement_options(self)
        except Exception as exc:
            self._handle_step_error(exc)

    def action_confirm_placement_option(self):
        """
        ATTENZIONE: unico metodo che chiama confirmPlacementOption — punto di
        non ritorno dichiarato dalla documentazione ufficiale Amazon ("This
        option cannot be reversed after it is selected."). Il bottone nella
        vista usa l'attributo nativo confirm="..." di Odoo con testo
        esplicito; questo metodo verifica comunque lo stato di partenza per
        bloccare un doppio click/rilancio accidentale.
        """
        self.ensure_one()
        self._require_state('placement_options_ready')
        if not self.placement_option_id:
            raise UserError('Nessuna opzione di collocazione selezionata.')
        try:
            self._service().confirm_placement_option(self)
        except Exception as exc:
            self._handle_step_error(exc)

    def action_generate_delivery_window_options(self):
        self.ensure_one()
        self._require_state('placement_confirmed')
        try:
            self._service().generate_and_list_delivery_window_options(self)
        except Exception as exc:
            self._handle_step_error(exc)

    def action_confirm_delivery_window_options(self):
        self.ensure_one()
        self._require_state('delivery_window_options_ready')
        if any(not s.delivery_window_option_id for s in self.shipment_ids):
            raise UserError("Ogni spedizione deve avere una finestra di consegna selezionata.")
        try:
            self._service().confirm_delivery_window_options(self)
        except Exception as exc:
            self._handle_step_error(exc)

    def action_generate_transportation_options(self):
        self.ensure_one()
        self._require_state('delivery_window_confirmed')
        try:
            self._service().generate_and_list_transportation_options(self)
        except Exception as exc:
            self._handle_step_error(exc)

    def action_confirm_transportation_options(self):
        self.ensure_one()
        self._require_state('transport_options_ready')
        if any(not s.transportation_option_id for s in self.shipment_ids):
            raise UserError('Ogni spedizione deve avere un\'opzione di trasporto selezionata.')
        try:
            self._service().confirm_transportation_options(self)
        except Exception as exc:
            self._handle_step_error(exc)

    def action_get_labels(self):
        self.ensure_one()
        self._require_state('transport_confirmed')
        try:
            self._service().get_labels(self)
            self.write({'state': 'labels_ready'})
        except Exception as exc:
            self._handle_step_error(exc)

    def action_mark_shipped(self):
        self.ensure_one()
        self._require_state('labels_ready')
        self.write({'state': 'shipped'})

    def action_cancel_plan(self):
        self.ensure_one()
        if self.state in ('transport_confirmed', 'labels_ready', 'shipped', 'done'):
            raise UserError(
                'Non annullabile da qui: il trasporto è già confermato. Vedi '
                'RICERCA_fulfillment_inbound_api_sandbox_2026-10-29.md — da questo punto '
                "l'annullamento ha una finestra a costo zero limitata (24h/1h), gestirlo "
                'solo consapevolmente.'
            )
        if self.state == 'draft':
            self.write({'state': 'cancelled'})
            return
        try:
            self._service().cancel_plan(self)
        except Exception as exc:
            self._handle_step_error(exc)


class AmazonInboundPlanBox(models.Model):
    _name = 'ke.amazon.inbound.plan.box'
    _description = 'Collo di un piano di spedizione inbound FBA'
    _order = 'sequence, id'

    plan_id = fields.Many2one('ke.amazon.inbound.plan', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, default='Collo')
    state = fields.Selection([
        ('open', 'Aperto'),
        ('closed', 'Chiuso'),
    ], default='open', required=True)
    length_cm = fields.Float('Lunghezza (cm)')
    width_cm = fields.Float('Larghezza (cm)')
    height_cm = fields.Float('Altezza (cm)')
    weight_kg = fields.Float('Peso (kg)')
    line_ids = fields.One2many('ke.amazon.inbound.plan.box.line', 'box_id', string='Articoli')

    def action_scan_items(self):
        self.ensure_one()
        if self.state != 'open':
            raise UserError(f'{self.name} è già chiuso: riaprilo prima di scansionare altri articoli.')
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ke.amazon.inbound.box.scan.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_box_id': self.id},
        }

    def action_close_box(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(f'{self.name}: aggiungi almeno un articolo prima di chiudere il collo.')
        if not (self.length_cm and self.width_cm and self.height_cm and self.weight_kg):
            raise UserError(f'{self.name}: indica dimensioni e peso prima di chiudere il collo.')
        self.write({'state': 'closed'})

    def action_reopen_box(self):
        self.ensure_one()
        self._require_plan_not_packed()
        self.write({'state': 'open'})

    def _require_plan_not_packed(self):
        self.ensure_one()
        if self.plan_id.state not in ('draft', 'plan_created'):
            raise UserError(
                'Il piano ha già superato lo step di imballaggio: non modificare più i colli da qui.'
            )


class AmazonInboundPlanBoxLine(models.Model):
    _name = 'ke.amazon.inbound.plan.box.line'
    _description = 'Riga articolo di un collo (piano inbound FBA)'

    box_id = fields.Many2one('ke.amazon.inbound.plan.box', required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', required=True)
    quantity = fields.Integer(required=True, default=1)

    @api.constrains('quantity', 'product_id')
    def _check_quantity_within_picking(self):
        for line in self:
            box = line.box_id
            if not box or not box.plan_id:
                continue
            picking = box.plan_id.picking_id
            move = picking.move_ids.filtered(lambda m: m.product_id == line.product_id)
            expected = sum(move.mapped('product_uom_qty'))
            packed = sum(
                box.plan_id.box_ids.line_ids.filtered(
                    lambda l: l.product_id == line.product_id
                ).mapped('quantity')
            )
            if packed > expected:
                raise UserError(
                    f'{line.product_id.display_name}: quantità nei colli ({packed}) superiore '
                    f'a quella del trasferimento ({expected:g}).'
                )


class AmazonInboundPlanShipment(models.Model):
    _name = 'ke.amazon.inbound.plan.shipment'
    _description = 'Spedizione (shipmentId Amazon) generata da un piano inbound FBA'
    _order = 'id'

    plan_id = fields.Many2one('ke.amazon.inbound.plan', required=True, ondelete='cascade', index=True)
    amazon_shipment_id = fields.Char('Shipment ID (Amazon)', required=True, copy=False)
    shipment_confirmation_id = fields.Char('Shipment Confirmation ID', readonly=True, copy=False)
    destination_fc_name = fields.Char('Centro di distribuzione', readonly=True, copy=False)
    transportation_option_id = fields.Char(
        readonly=True, copy=False,
        help='Prima opzione di trasporto offerta da Amazon, selezionata automaticamente '
             '(semplificazione v1, stessa convenzione già usata negli script di esplorazione: '
             "sempre la prima opzione) — consulta 'last_options_raw' sul piano per le altre.",
    )
    delivery_window_option_id = fields.Char(readonly=True, copy=False)
    tracking_number = fields.Char(
        help='Non usato nel percorso solo-corrieri-partner (v1) — il campo resta pronto '
             'per quando verrà aggiunto il percorso self-ship (corriere proprio).',
    )
    labels_raw_response = fields.Text(
        readonly=True, copy=False,
        help="Payload grezzo di getLabels — shape non verificata in questa sessione per "
             "la v2024-03-20 (vedi SpApiClient.get_labels). Conservato così com'è finché "
             "non si verifica il formato reale (download URL o contenuto) contro una "
             "risposta vera, prima di collegarlo a stampa/download automatico.",
    )
