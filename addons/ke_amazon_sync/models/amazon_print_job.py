from odoo import api, fields, models


class AmazonPrintJob(models.Model):
    _name = 'ke.amazon.print.job'
    _description = 'Lavoro di stampa ZPL in coda per una postazione agente'
    _order = 'id desc'

    # Lavori "in attesa" (non ancora confermati dall'agente) più vecchi di
    # questa soglia vengono rimandati al prossimo poll come se fossero
    # ancora in coda — recupero automatico dopo un riavvio/crash
    # dell'agente (vedi progetto-odoo-amazon-fba-etichettatura.md §8).
    # Compromesso esplicito e non eliminabile del tutto: se l'agente aveva
    # davvero stampato ma non ha ancora confermato (crash subito dopo la
    # stampa fisica, prima dell'ack), questa ripetizione produce una
    # doppia stampa — più sicuro ridare il lavoro che perderlo, ma va
    # saputo, non è "a esattamente una copia" garantito.
    _PENDING_TIMEOUT_MINUTES = 2

    station_id = fields.Many2one(
        'ke.amazon.print.station', required=True, index=True, ondelete='cascade',
    )
    label_log_id = fields.Many2one(
        'ke.amazon.fba.label.log', ondelete='set null',
        help='Riga storica (SKU/FNSKU/operatore) da cui questo lavoro è stato generato — '
             'solo per tracciabilità, il contenuto da stampare è congelato in \'zpl\'.',
    )
    zpl = fields.Text(
        required=True,
        help='Comando ZPL completo da inviare alla stampante, generato una volta alla '
             'creazione del lavoro (services/zpl_builder.py) — non ricalcolato più tardi, '
             'così un cambio di FNSKU sul prodotto dopo la stampa non altera la storia.',
    )
    state = fields.Selection([
        ('queued', 'In coda'),
        ('sent', 'Inviato (in attesa di conferma)'),
        ('done', 'Stampato'),
        ('error', 'Errore'),
    ], default='queued', required=True, index=True)
    sent_date = fields.Datetime(readonly=True)
    done_date = fields.Datetime(readonly=True)
    error_message = fields.Text(readonly=True)

    @api.model
    def _enqueue_from_label_log(self, station, label_log):
        """
        Accoda un lavoro di stampa ZPL per 'label_log' (ke.amazon.fba.label.log,
        già creato dal wizard di stampa — vedi amazon_fba_standalone_label_wizard/
        amazon_fba_prep_wizard) sulla postazione 'station'. Il contenuto ZPL è
        generato qui una volta sola dal suo fnsku/sku/nome prodotto.
        """
        from odoo.addons.ke_amazon_sync.services.zpl_builder import build_fnsku_zpl

        product = label_log.product_id
        zpl = build_fnsku_zpl(label_log.fnsku, product.default_code, product.name)
        return self.create({
            'station_id': station.id,
            'label_log_id': label_log.id,
            'zpl': zpl,
        })

    @api.model
    def _poll_for_station(self, station, limit=50):
        """
        Lavori da restituire a questa postazione: quelli non ancora inviati,
        più quelli inviati da più di _PENDING_TIMEOUT_MINUTES senza una
        conferma (vedi commento sulla costante). 'sent_date' viene
        aggiornata a ORA per OGNI lavoro restituito qui, non solo per quelli
        appena passati da 'queued' — una prima versione la lasciava invariata
        per i lavori già in ripetizione, così un lavoro scaduto UNA volta
        veniva ripescato a OGNI poll successivo (anche a pochi secondi di
        distanza, prima ancora che l'agente avesse finito di stamparlo),
        rischiando ristampe ripetute dello stesso lavoro prima del suo primo
        ack. Aggiornare 'sent_date' qui fa ripartire la finestra di grazia
        da questo invio, non dal primo: ricompare solo se rimane senza ack
        per un altro timeout intero. Trovato in revisione, vedi TASK.md
        "Agente di stampa".
        """
        from datetime import timedelta
        cutoff = fields.Datetime.now() - timedelta(minutes=self._PENDING_TIMEOUT_MINUTES)
        jobs = self.search([
            ('station_id', '=', station.id),
            '|',
                ('state', '=', 'queued'),
                '&', ('state', '=', 'sent'), ('sent_date', '<', cutoff),
        ], order='id asc', limit=limit)
        if jobs:
            jobs.write({'state': 'sent', 'sent_date': fields.Datetime.now()})
        return jobs

    def _mark_done(self):
        self.write({'state': 'done', 'done_date': fields.Datetime.now(), 'error_message': False})

    def _mark_error(self, message):
        self.write({'state': 'error', 'error_message': message or 'Errore agente non specificato'})
