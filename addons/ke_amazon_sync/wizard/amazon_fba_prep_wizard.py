from odoo import api, fields, models
from odoo.exceptions import UserError


class AmazonFbaPrepWizard(models.TransientModel):
    _name = 'ke.amazon.fba.prep.wizard'
    _description = 'Scansione ed etichettatura FNSKU spedizione FBA'

    picking_id = fields.Many2one('stock.picking', string='Spedizione', required=True)
    ean_scan = fields.Char('Scansiona EAN')
    force_reprint = fields.Boolean(
        'Forza ristampa',
        help="Spunta per stampare di nuovo l'etichetta di una riga già completata "
             "(es. unità danneggiata) senza aumentare la quantità prevista spedita.",
    )
    last_scan_message = fields.Char('Ultima scansione', readonly=True)
    move_ids = fields.One2many(
        related='picking_id.move_ids', string='Righe da etichettare', readonly=True,
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        picking_id = self.env.context.get('default_picking_id') or self.env.context.get('active_id')
        if picking_id and 'picking_id' in fields_list:
            res['picking_id'] = picking_id
        return res

    def _labeled_count(self, move):
        """
        Quantità già effettivamente etichettata per 'move', contata dallo
        storico (ke.amazon.fba.label.log), NON da move.quantity: su questa
        versione di Odoo, action_assign() precompila move.quantity con
        l'intera quantità prevista non appena la riga è riservata (anche per
        prodotti non storable), con picked=False — quel valore rappresenta
        "quanto SAREBBE da fare", non "quanto è stato fisicamente etichettato
        finora". Verificato con un test reale (non solo dedotto): dopo
        action_assign() su un prodotto non storable, quantity era già = alla
        quantità richiesta con picked ancora False.
        """
        return self.env['ke.amazon.fba.label.log'].search_count([
            ('move_id', '=', move.id), ('is_reprint', '=', False),
        ])

    def _find_move(self, barcode):
        """
        Cerca il prodotto per barcode SOLO tra le righe di questa spedizione
        (non tutto il catalogo): un match altrove nel catalogo va segnalato
        come "non incluso in questa spedizione", non trattato come trovato.
        Ritorna la riga (stock.move) più adatta a ricevere questa scansione:
        preferisce una riga non ancora completa, altrimenti l'unica/prima
        (per il caso di ristampa esplicita).
        """
        picking = self.picking_id
        candidate_moves = picking.move_ids.filtered(lambda m: m.product_id.barcode == barcode)
        if not candidate_moves:
            any_product = self.env['product.product'].search([('barcode', '=', barcode)], limit=1)
            if not any_product:
                raise UserError(f'EAN sconosciuto: nessun prodotto con il codice a barre "{barcode}".')
            raise UserError(
                f'{any_product.display_name} non fa parte di questa spedizione ({picking.name}).'
            )
        open_moves = candidate_moves.filtered(lambda m: self._labeled_count(m) < m.product_uom_qty)
        return open_moves[:1] if open_moves else candidate_moves[:1]

    def action_scan(self):
        self.ensure_one()
        barcode = (self.ean_scan or '').strip()
        self.ean_scan = False
        if not barcode:
            raise UserError('Scansiona o digita un codice EAN.')

        move = self._find_move(barcode)
        product = move.product_id

        # FNSKU della variante per una famiglia, del template per un prodotto
        # semplice — mai quello del template su una famiglia (vedi
        # product.product._get_amazon_fnsku).
        fnsku = product._get_amazon_fnsku()
        if not fnsku:
            raise UserError(f'{product.display_name}: FNSKU mancante. Esegui prima "Import FNSKU".')

        labeled = self._labeled_count(move)
        is_complete = labeled >= move.product_uom_qty
        if is_complete and not self.force_reprint:
            raise UserError(
                f'{product.display_name}: quantità già completata '
                f'({labeled:g}/{move.product_uom_qty:g}). '
                f'Spunta "Forza ristampa" per stampare comunque una copia.'
            )

        is_reprint = is_complete
        if not is_reprint:
            labeled += 1
            # Allinea move.quantity al conteggio REALE etichettato finora (non
            # al valore precompilato da action_assign, vedi _labeled_count) —
            # è quello che _action_done() userà per chiudere il trasferimento.
            move.quantity = labeled
            move.picked = True

        log = self.env['ke.amazon.fba.label.log'].create({
            'picking_id': self.picking_id.id,
            'move_id': move.id,
            'product_id': product.id,
            'fnsku': fnsku,
            'is_reprint': is_reprint,
        })
        self.force_reprint = False
        self.last_scan_message = (
            f'{"RISTAMPA" if is_reprint else "OK"} {product.display_name}: '
            f'{labeled:g}/{move.product_uom_qty:g}'
        )

        # config=False: salta il prompt "Configura il layout documento" che
        # Odoo mostra al primo report stampato su un'azienda mai configurata
        # — irrilevante per un'etichetta di magazzino senza branding, trovato
        # con un test reale (senza, il primo scan apriva quel wizard invece
        # di stampare).
        return self.env.ref('ke_amazon_sync.action_report_amazon_fba_label').report_action(
            log, config=False,
        )

    def action_validate_picking(self):
        """Chiude la spedizione (_action_done) una volta etichettate tutte le
        righe — vedi ship_to_fba: il picking resta apposta 'assigned' finché
        non si arriva qui, non viene più validato subito in un solo passaggio."""
        self.ensure_one()
        picking = self.picking_id
        incomplete = picking.move_ids.filtered(
            lambda m: self._labeled_count(m) < m.product_uom_qty
        )
        if incomplete:
            names = ', '.join(incomplete.mapped('product_id.display_name'))
            raise UserError(f'Etichettatura incompleta per: {names}')
        picking.with_context(skip_immediate=True, skip_backorder=True)._action_done()
        return {'type': 'ir.actions.act_window_close'}
