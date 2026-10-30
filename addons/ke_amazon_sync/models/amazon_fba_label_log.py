import base64

from odoo import api, fields, models


class AmazonFbaLabelLog(models.Model):
    _name = 'ke.amazon.fba.label.log'
    _description = 'Storico etichette FNSKU stampate (spedizione FBA)'
    _order = 'create_date desc'

    picking_id = fields.Many2one(
        'stock.picking', string='Spedizione', ondelete='cascade', index=True,
        help='Vuoto per una stampa autonoma (ke.amazon.fba.standalone.label.wizard), '
             'non legata a una spedizione/trasferimento — vedi move_id.',
    )
    move_id = fields.Many2one(
        'stock.move', string='Movimento', ondelete='cascade',
        help='Riga della spedizione (un prodotto) a cui questa etichetta si riferisce, '
             'se stampata dal flusso di etichettatura spedizione. Vuoto per una stampa '
             'autonoma per variante — non conta per la quantità etichettata di nessun '
             'movimento e non richiede giacenza, trasferimento o spedizione aperta.',
    )
    product_id = fields.Many2one(
        'product.product', string='Prodotto', required=True, index=True,
    )
    fnsku = fields.Char(
        'FNSKU', required=True,
        help="Valore FNSKU stampato su questa etichetta, congelato al momento della "
             "stampa (se cambiasse più tardi su un nuovo import, lo storico resta corretto).",
    )
    user_id = fields.Many2one(
        'res.users', string='Operatore', required=True, default=lambda self: self.env.user,
    )
    is_reprint = fields.Boolean(
        'Ristampa', default=False,
        help="True se al momento della stampa la riga aveva già raggiunto la quantità "
             "prevista (unità danneggiata/etichetta da rifare) — non ha incrementato la "
             "quantità fatta sul movimento, serve solo come traccia storica.",
    )
    is_own_brand_no_label = fields.Boolean(
        'Marchio proprio (nessuna etichetta)', default=False,
        help="True se il valore FNSKU qui sopra è in realtà l'ASIN (prefisso 'B0', non "
             "'X0') — il prodotto traccia tramite il codice del produttore già sull'unità, "
             "nessuna etichetta è stata davvero stampata/accodata per questa riga. La riga "
             "esiste solo per contare la quantità come completata (vedi "
             "stock.move._print_fnsku_labels) — non confonderla con una stampa reale "
             "guardando lo storico.",
    )

    @api.depends('product_id', 'fnsku')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f'{rec.product_id.display_name} — {rec.fnsku}'

    @api.model
    def _create_and_dispatch_many(self, specs, move=None, picking=None):
        """
        'specs': lista di (product, fnsku, quantity, is_reprint). Crea TUTTE
        le righe di log di TUTTE le voci in un colpo solo e fa UN SOLO
        dispatch (un documento con una pagina per etichetta in tutto, o
        un'unica coda di lavori) — usato quando si stampano più prodotti
        diversi in una sola azione (es. tutte le righe del wizard "Spedisci
        a FBA" insieme, un click invece di uno per riga — richiesto
        dall'utente il 2026-10-29). 'move'/'picking' valgono per TUTTE le
        voci (stesso trasferimento); non ha senso mischiare trasferimenti
        diversi in una singola chiamata.
        Ritorna un dict {'queued', 'station', 'report_action'}.
        """
        vals_list = [{
            'picking_id': picking.id if picking else False,
            'move_id': move.id if move else False,
            'product_id': product.id, 'fnsku': fnsku, 'is_reprint': is_reprint,
        } for product, fnsku, quantity, is_reprint in specs for _ in range(quantity)]
        logs = self.create(vals_list)

        station = self.env['ke.amazon.print.station']._get_configured_station()
        if station:
            for log in logs:
                self.env['ke.amazon.print.job']._enqueue_from_label_log(station, log)
            return {'queued': True, 'station': station.name, 'report_action': None}

        report_action = self.env.ref('ke_amazon_sync.action_report_amazon_fba_label').report_action(
            logs, config=False,
        )
        return {'queued': False, 'station': None, 'report_action': report_action}

    @api.model
    def _create_and_dispatch(self, product, fnsku, quantity, is_reprint=False, move=None, picking=None):
        """Caso con un solo prodotto — vedi _create_and_dispatch_many."""
        return self._create_and_dispatch_many(
            [(product, fnsku, quantity, is_reprint)], move=move, picking=picking,
        )

    def _get_barcode_data_uri(self):
        """
        Barcode Code128 del FNSKU come data URI PNG, generato qui in Python
        (env['ir.actions.report'].barcode, nessuna chiamata HTTP) invece di
        un <img src="/report/barcode/..."> nel template — quella forma
        richiede che wkhtmltopdf raggiunga se stesso via web.base.url, che su
        questa istanza punta al dominio pubblico e non è raggiungibile dal
        processo di stampa locale (trovato dal vivo: "ProtocolUnknownError",
        barcode assente dal PDF). Generare l'immagine qui elimina la
        richiesta di rete del tutto, corretto a prescindere da come
        web.base.url è configurato in qualunque ambiente.
        """
        self.ensure_one()
        png = self.env['ir.actions.report'].barcode(
            'Code128', self.fnsku, width=600, height=150, humanreadable=0, quiet=1,
        )
        return 'data:image/png;base64,%s' % base64.b64encode(png).decode()
