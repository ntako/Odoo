from odoo import api, fields, models
from odoo.exceptions import UserError


class AmazonFbaPrepWizard(models.TransientModel):
    _name = 'ke.amazon.fba.prep.wizard'
    _description = 'Scansione ed etichettatura FNSKU spedizione FBA'

    picking_id = fields.Many2one('stock.picking', string='Spedizione', required=True)
    ean_scan = fields.Char('Scansiona EAN')
    scan_quantity = fields.Integer(
        'Quantità', default=1,
        help="Unità identiche già in mano per lo stesso SKU: scansioni una volta sola e "
             "vengono stampate/accodate tutte le copie in un unico documento (una pagina "
             "per etichetta), invece di scansionare un'unità alla volta. Non può superare "
             "la quantità ancora da completare per quella riga, a meno di 'Forza ristampa'.",
    )
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
        Quantità già effettivamente etichettata per 'move' — delega al campo
        calcolato stock.move.x_amazon_labeled_qty (stesso conteggio reale
        dallo storico ke.amazon.fba.label.log, non move.quantity: su questa
        versione di Odoo action_assign() precompila move.quantity con
        l'intera quantità prevista non appena la riga è riservata, con
        picked=False, prima di qualunque etichettatura reale). Il campo è
        stato estratto qui per poterlo mostrare anche nella vista (colonna
        "Etichettata") — prima la vista leggeva 'quantity' direttamente,
        mostrando una spedizione come già completamente etichettata ancora
        prima di scansionare la prima unità. Segnalato dal vivo il 2026-10-29.
        """
        return move.x_amazon_labeled_qty

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
        quantity = self.scan_quantity or 1
        # Logica di completamento/registrazione condivisa con il bottone di
        # stampa diretta sulla riga — vedi stock.move._print_fnsku_labels.
        result = move._print_fnsku_labels(quantity=quantity, force_reprint=self.force_reprint)
        self.scan_quantity = 1
        self.force_reprint = False
        status = "RISTAMPA" if result['is_reprint'] else "OK"
        qty_note = f' ({quantity} etichette)' if quantity > 1 else ''
        self.last_scan_message = (
            f'{status} {result["product"]}: {result["labeled"]:g}/{move.product_uom_qty:g}{qty_note}'
        )
        if result['skipped_print']:
            self.last_scan_message += ' — marchio proprio, nessuna etichetta necessaria'
            return True
        if result['queued']:
            self.last_scan_message += f' — in coda di stampa su "{result["station"]}"'
            return True
        return result['report_action']

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
