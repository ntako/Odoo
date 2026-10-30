import hashlib
import secrets

from odoo import api, fields, models
from odoo.exceptions import UserError


class AmazonPrintStation(models.Model):
    _name = 'ke.amazon.print.station'
    _description = "Postazione agente di stampa ZPL (PC Windows magazzino)"

    name = fields.Char(required=True, help='Es. "Magazzino PC1 - Zebra ZD621".')
    active = fields.Boolean(default=True)
    # Solo l'hash viene conservato (stesso principio di res.users.apikeys,
    # mai la password/chiave in chiaro) — il valore vero si vede una volta
    # sola al momento della generazione, via action_generate_api_key.
    # SHA-256 semplice (non bcrypt/scrypt): la chiave è generata con 256 bit
    # di entropia casuale (secrets.token_hex), non è una password a bassa
    # entropia da proteggere da dizionario — un hash deterministico permette
    # anche la ricerca diretta per uguaglianza nel controller, invece di
    # iterare e verificare una per una tutte le postazioni a ogni poll.
    api_key_hash = fields.Char(readonly=True, copy=False, index=True)
    last_seen = fields.Datetime(
        readonly=True,
        help="Ultimo poll ricevuto da questa postazione — usato per capire "
             "se l'agente è online (vedi progetto-odoo-amazon-fba-etichettatura.md §8).",
    )
    note = fields.Text()

    @api.model
    def _hash_key(self, raw_key):
        return hashlib.sha256((raw_key or '').encode()).hexdigest()

    def action_generate_api_key(self):
        """
        Rigenera la chiave API di questa postazione e la mostra UNA sola
        volta (vedi wizard ke.amazon.print.station.show.key) — da quel
        momento solo l'hash resta leggibile da Odoo, esattamente come per
        una res.users.apikeys. Rigenerare invalida la chiave precedente:
        l'agente Windows configurato con quella vecchia smette di
        autenticarsi finché non viene aggiornato con la nuova.
        """
        self.ensure_one()
        raw_key = secrets.token_hex(32)
        self.write({'api_key_hash': self._hash_key(raw_key)})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ke.amazon.print.station.show.key',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_station_id': self.id, 'default_raw_key': raw_key},
        }

    @api.model
    def _authenticate(self, raw_key):
        """Postazione attiva corrispondente a 'raw_key', o record vuoto se
        la chiave è mancante/sbagliata/di una postazione disattivata."""
        if not raw_key:
            return self.browse()
        return self.sudo().search([
            ('api_key_hash', '=', self._hash_key(raw_key)), ('active', '=', True),
        ], limit=1)

    @api.model
    def _get_configured_station(self):
        """
        Postazione da usare per accodare la stampa ZPL invece di generare il
        PDF (vedi amazon_fba_prep_wizard/amazon_fba_standalone_label_wizard),
        secondo l'impostazione "Usa stampa di rete (ZPL)" di Impostazioni →
        Amazon (res_config_settings.py). Un record vuoto significa
        "disattiva, stampa il PDF come sempre" — non un errore, è il
        comportamento di default. Se invece l'opzione è attiva ma non è
        stata scelta una postazione valida, è un errore esplicito: ricadere
        silenziosamente sul PDF nasconderebbe all'utente perché l'opzione
        che ha attivato non ha alcun effetto.
        """
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.zpl_printing_enabled', 'False') != 'True':
            return self.browse()
        station_id = params.get_param('ke_amazon_sync.zpl_default_station_id')
        station = self.sudo().browse(int(station_id)) if station_id else self.browse()
        if not station.exists() or not station.active:
            raise UserError(
                'La stampa di rete (ZPL) è attiva nelle impostazioni Amazon, ma non è '
                'configurata una postazione predefinita valida. Impostala in '
                'Impostazioni → Amazon Seller Central, oppure disattiva la stampa di '
                'rete per tornare alla stampa PDF.'
            )
        return station
