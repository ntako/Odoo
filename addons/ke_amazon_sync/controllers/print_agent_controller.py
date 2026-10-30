import json
import logging

from odoo import fields, http
from odoo.http import request

_log = logging.getLogger(__name__)


class PrintAgentController(http.Controller):
    """
    API per l'agente di stampa Windows (vedi
    progetto-odoo-amazon-fba-etichettatura.md §6/§8): polling dei lavori in
    coda e conferma esito. 'auth=public' perché l'agente non è un utente
    Odoo (nessuna sessione/cookie) — l'autenticazione è la chiave API della
    postazione (ke.amazon.print.station), verificata a mano qui dentro.
    Mai usare type='json' (richiede l'involucro JSON-RPC, pensato per
    chiamate stesso-origine da un client Odoo, non per un client HTTP
    generico come l'agente) — 'type=http' con corpo JSON semplice.
    """

    def _get_json_body(self):
        """request.get_json_data() fa json.loads() senza rete di sicurezza —
        un corpo vuoto o malformato da un client esterno (l'agente, non un
        client Odoo) non deve risultare in un 500 non gestito."""
        try:
            return request.get_json_data() or {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def _authenticate(self, data):
        api_key = (data or {}).get('api_key')
        station = request.env['ke.amazon.print.station']._authenticate(api_key)
        if not station:
            _log.warning('print_agent: chiave API non valida o postazione disattivata')
        return station

    @http.route('/ke_amazon_sync/print_agent/poll', type='http', auth='public',
                methods=['POST'], csrf=False)
    def poll(self, **kwargs):
        data = self._get_json_body()
        station = self._authenticate(data)
        if not station:
            return request.make_json_response({'error': 'invalid_api_key'}, status=401)

        station.sudo().write({'last_seen': fields.Datetime.now()})
        jobs = request.env['ke.amazon.print.job'].sudo()._poll_for_station(station)
        _log.info('print_agent: poll da "%s" (#%d) — %d lavori restituiti',
                   station.name, station.id, len(jobs))
        return request.make_json_response({
            'jobs': [{'id': j.id, 'zpl': j.zpl} for j in jobs],
        })

    @http.route('/ke_amazon_sync/print_agent/ack', type='http', auth='public',
                methods=['POST'], csrf=False)
    def ack(self, **kwargs):
        data = self._get_json_body()
        station = self._authenticate(data)
        if not station:
            return request.make_json_response({'error': 'invalid_api_key'}, status=401)

        job_id = (data or {}).get('job_id')
        result = (data or {}).get('result')
        error = (data or {}).get('error')
        job = request.env['ke.amazon.print.job'].sudo().search([
            ('id', '=', job_id), ('station_id', '=', station.id),
        ], limit=1)
        if not job:
            _log.warning('print_agent: ack per lavoro %r non trovato su postazione "%s"',
                         job_id, station.name)
            return request.make_json_response({'error': 'job_not_found'}, status=404)

        if result == 'done':
            job._mark_done()
            _log.info('print_agent: lavoro #%d confermato stampato da "%s"', job.id, station.name)
        else:
            job._mark_error(error)
            _log.warning('print_agent: lavoro #%d segnalato in errore da "%s": %s',
                         job.id, station.name, error)
        return request.make_json_response({'ok': True})
