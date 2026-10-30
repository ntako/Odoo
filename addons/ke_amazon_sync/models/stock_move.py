from odoo import api, fields, models
from odoo.exceptions import UserError


class StockMove(models.Model):
    _inherit = 'stock.move'

    # Relazione inversa esplicita verso ke.amazon.fba.label.log (che ha già
    # move_id) — SENZA questa, @api.depends sotto non avrebbe nessun modo di
    # sapere che il compute va invalidato quando si crea una nuova etichetta:
    # Odoo traccia le dipendenze solo lungo relazioni dichiarate, mai tra
    # modelli collegati solo da una ricerca manuale. Trovato dal vivo il
    # 2026-10-29: senza questo campo (prima versione, solo un
    # search_count() nel compute), il valore restava in cache al primo
    # valore letto (0) e non si aggiornava mai dopo la prima etichetta
    # stampata, anche con righe reali nello storico.
    x_amazon_label_log_ids = fields.One2many('ke.amazon.fba.label.log', 'move_id')
    x_amazon_labeled_qty = fields.Float(
        'Quantità etichettata FNSKU', compute='_compute_x_amazon_labeled_qty',
        help="Conteggio REALE dallo storico ke.amazon.fba.label.log, non move.quantity: "
             "su questa versione di Odoo action_assign() precompila move.quantity con "
             "l'intera quantità prevista non appena la riga è riservata (picked=False), "
             "prima di qualunque etichettatura reale — mostrarla come 'Etichettata' nel "
             "wizard di preparazione FBA fa sembrare una spedizione già completa quando "
             "non è stata ancora scansionata nemmeno un'unità. Segnalato dal vivo il "
             "2026-10-29. Stessa fonte già usata dalla logica di blocco (vedi "
             "ke.amazon.fba.prep.wizard._labeled_count, che ora delega qui).",
    )

    @api.depends('x_amazon_label_log_ids', 'x_amazon_label_log_ids.is_reprint')
    def _compute_x_amazon_labeled_qty(self):
        for move in self:
            move.x_amazon_labeled_qty = len(
                move.x_amazon_label_log_ids.filtered(lambda log: not log.is_reprint)
            )

    def _print_fnsku_labels(self, quantity=1, force_reprint=False):
        """
        Logica condivisa di stampa/accodamento di N etichette FNSKU per
        'self' (una riga del trasferimento, tutte identiche — stesso SKU) —
        unica fonte usata sia dalla scansione EAN (ke.amazon.fba.prep.wizard.
        action_scan, dopo aver risolto la riga dal codice a barre) sia dal
        bottone di stampa diretta sulla riga (action_print_fnsku_label
        sotto), per non dover mantenere la stessa logica di completamento/
        registrazione in due posti che potrebbero disallinearsi.
        'quantity' > 1 serve per le unità identiche già in mano (stesso SKU,
        più pezzi): un'unica scansione copre tutte, un solo documento con
        una pagina per etichetta — niente scansioni ripetute per ogni pezzo
        dello stesso prodotto. Richiesto dall'utente il 2026-10-29.
        force_reprint=True tratta l'INTERA quantità come ristampa (non
        aumenta la quantità etichettata/non segna le unità come prelevate) —
        niente quantità "miste" reali+ristampa in una sola chiamata, per
        restare inequivocabile.
        Se il prodotto è marchio proprio (FNSKU con prefisso 'B0', in realtà
        l'ASIN — vedi product.product._amazon_fnsku_needs_label) non stampa
        né accoda nulla: la riga viene comunque segnata come completata (lo
        storico esiste solo per il conteggio, 'skipped_print' lo segnala al
        chiamante) perché il codice del produttore già sull'unità è
        sufficiente, non serve nessuna etichetta aggiuntiva. Richiesto
        dall'utente il 2026-10-29.
        Ritorna un dict {'queued', 'station', 'report_action', 'product',
        'labeled', 'is_reprint', 'skipped_print'} — il chiamante costruisce
        il proprio messaggio/azione da questi dati (il wizard ha un
        messaggio testuale proprio, il bottone sulla riga no).
        """
        self.ensure_one()
        if quantity <= 0:
            raise UserError('Indica una quantità maggiore di zero.')
        product = self.product_id
        fnsku = product._get_amazon_fnsku()
        if not fnsku:
            raise UserError(f'{product.display_name}: FNSKU non ancora importato. Esegui prima "Import FNSKU".')

        labeled = self.x_amazon_labeled_qty
        remaining = self.product_uom_qty - labeled
        if not force_reprint and quantity > remaining:
            if remaining <= 0:
                raise UserError(
                    f'{product.display_name}: quantità già completata '
                    f'({labeled:g}/{self.product_uom_qty:g}). '
                    f'Spunta "Forza ristampa" per stampare comunque altre copie.'
                )
            raise UserError(
                f'{product.display_name}: richieste {quantity} etichette ma ne restano {remaining:g} '
                f'da completare ({labeled:g}/{self.product_uom_qty:g}) — riduci la quantità o spunta '
                f'"Forza ristampa".'
            )

        is_reprint = force_reprint
        if not is_reprint:
            labeled += quantity
            # Allinea move.quantity al conteggio REALE etichettato finora
            # (non al valore precompilato da action_assign, vedi
            # x_amazon_labeled_qty) — è quello che _action_done() userà per
            # chiudere il trasferimento.
            self.quantity = labeled
            self.picked = True

        if product._amazon_fnsku_needs_label():
            result = self.env['ke.amazon.fba.label.log']._create_and_dispatch(
                product, fnsku, quantity, is_reprint=is_reprint, move=self, picking=self.picking_id,
            )
            skipped_print = False
        else:
            self.env['ke.amazon.fba.label.log'].create([{
                'picking_id': self.picking_id.id, 'move_id': self.id,
                'product_id': product.id, 'fnsku': fnsku, 'is_reprint': is_reprint,
                'is_own_brand_no_label': True,
            } for _ in range(quantity)])
            result = {'queued': False, 'station': None, 'report_action': None}
            skipped_print = True

        return {**result, 'product': product.display_name, 'labeled': labeled,
                'is_reprint': is_reprint, 'skipped_print': skipped_print}

    def action_print_fnsku_label(self):
        """
        Stampa diretta sulla riga, dal bottone nella lista del wizard di
        preparazione FBA — alternativa alla scansione EAN per chi non ha
        subito a portata lo scanner. Il bottone in vista ha una conferma
        esplicita che mostra il prodotto, per mantenere comunque un
        controllo visivo cosciente prima di stampare (la scansione resta
        la via raccomandata: lega la stampa al codice a barre fisico
        dell'unità in mano, non solo al click su una riga — vedi
        progetto-odoo-amazon-fba-etichettatura.md §3).
        Stampa in un colpo solo TUTTA la quantità ancora da completare per
        questa riga (Prevista - Etichettata), un unico documento con una
        pagina per etichetta — non una copia alla volta. Richiesto
        dall'utente il 2026-10-29. Nessuna ristampa da qui: per quello resta
        la scansione, che ha il campo dedicato.
        """
        self.ensure_one()
        remaining = self.product_uom_qty - self.x_amazon_labeled_qty
        if remaining <= 0:
            raise UserError(
                f'{self.product_id.display_name}: quantità già completata '
                f'({self.x_amazon_labeled_qty:g}/{self.product_uom_qty:g}).'
            )
        result = self._print_fnsku_labels(quantity=int(round(remaining)), force_reprint=False)
        if result['skipped_print'] or result['queued']:
            return True
        return result['report_action']
