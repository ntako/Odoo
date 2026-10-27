"""
Test per la coda di stampa ZPL (ke.amazon.print.station / ke.amazon.print.job)
e l'API HTTP per l'agente Windows (controllers/print_agent_controller.py) —
vedi progetto-odoo-amazon-fba-etichettatura.md §6/§8 e TASK.md.
"""
import json
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestPrintStationAndJob(TransactionCase):

    def setUp(self):
        super().setUp()
        self.station = self.env['ke.amazon.print.station'].create({'name': 'ZZT Stazione test'})
        self.product = self.env['product.product'].create({
            'name': 'ZZT Prodotto etichetta', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-PRINT-1', 'x_amazon_fnsku': 'B0ZZPRINT1',
        })
        self.label_log = self.env['ke.amazon.fba.label.log'].create({
            'product_id': self.product.id, 'fnsku': 'B0ZZPRINT1',
        })

    def test_generate_api_key_only_hash_is_stored(self):
        action = self.station.action_generate_api_key()
        raw_key = action['context']['default_raw_key']
        self.assertTrue(raw_key)
        self.station.invalidate_recordset()
        self.assertTrue(self.station.api_key_hash)
        self.assertNotEqual(self.station.api_key_hash, raw_key, 'la chiave in chiaro non va mai salvata')

        found = self.env['ke.amazon.print.station']._authenticate(raw_key)
        self.assertEqual(found.id, self.station.id)

    def test_authenticate_wrong_or_empty_key_fails(self):
        self.station.action_generate_api_key()
        self.assertFalse(self.env['ke.amazon.print.station']._authenticate('chiave-sbagliata'))
        self.assertFalse(self.env['ke.amazon.print.station']._authenticate(''))
        self.assertFalse(self.env['ke.amazon.print.station']._authenticate(False))

    def test_authenticate_ignores_inactive_station(self):
        action = self.station.action_generate_api_key()
        raw_key = action['context']['default_raw_key']
        self.station.active = False
        self.assertFalse(self.env['ke.amazon.print.station']._authenticate(raw_key))

    def test_enqueue_builds_zpl_with_fnsku(self):
        job = self.env['ke.amazon.print.job']._enqueue_from_label_log(self.station, self.label_log)
        self.assertEqual(job.station_id, self.station)
        self.assertEqual(job.state, 'queued')
        self.assertIn('B0ZZPRINT1', job.zpl)
        self.assertIn('ZZT-PRINT-1', job.zpl)

    def test_poll_marks_queued_as_sent_and_does_not_resend_immediately(self):
        job = self.env['ke.amazon.print.job']._enqueue_from_label_log(self.station, self.label_log)

        first_poll = self.env['ke.amazon.print.job']._poll_for_station(self.station)
        self.assertEqual(first_poll.ids, [job.id])
        job.invalidate_recordset()
        self.assertEqual(job.state, 'sent')
        self.assertTrue(job.sent_date)

        second_poll = self.env['ke.amazon.print.job']._poll_for_station(self.station)
        self.assertFalse(second_poll, 'un lavoro appena inviato non va riproposto subito')

    def test_poll_resends_pending_job_after_timeout(self):
        job = self.env['ke.amazon.print.job']._enqueue_from_label_log(self.station, self.label_log)
        job.write({
            'state': 'sent',
            'sent_date': fields.Datetime.now() - timedelta(minutes=10),
        })
        jobs = self.env['ke.amazon.print.job']._poll_for_station(self.station)
        self.assertEqual(jobs.ids, [job.id], 'un lavoro mai confermato va riproposto dopo il timeout')

    def test_poll_refreshes_sent_date_and_does_not_resend_immediately_again(self):
        """Trovato in revisione (TASK.md 'Agente di stampa'): senza
        aggiornare sent_date al reinvio, lo stesso lavoro scaduto
        ricompariva a OGNI poll successivo, non solo dopo un altro timeout
        intero — rischio di ristampe ripetute prima del primo ack."""
        job = self.env['ke.amazon.print.job']._enqueue_from_label_log(self.station, self.label_log)
        job.write({
            'state': 'sent',
            'sent_date': fields.Datetime.now() - timedelta(minutes=10),
        })
        first_poll = self.env['ke.amazon.print.job']._poll_for_station(self.station)
        self.assertEqual(first_poll.ids, [job.id])
        job.invalidate_recordset()
        self.assertGreater(job.sent_date, fields.Datetime.now() - timedelta(minutes=1),
                            'sent_date deve essere aggiornata a ora, non lasciata al primo invio')

        second_poll = self.env['ke.amazon.print.job']._poll_for_station(self.station)
        self.assertFalse(second_poll, 'appena reinviato, non deve ricomparire al poll immediatamente successivo')

    def test_poll_does_not_return_other_station_jobs(self):
        other_station = self.env['ke.amazon.print.station'].create({'name': 'ZZT Altra stazione'})
        self.env['ke.amazon.print.job']._enqueue_from_label_log(other_station, self.label_log)
        jobs = self.env['ke.amazon.print.job']._poll_for_station(self.station)
        self.assertFalse(jobs)

    def test_get_configured_station_disabled_by_default(self):
        self.station.action_generate_api_key()
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.zpl_printing_enabled', 'False')
        self.assertFalse(self.env['ke.amazon.print.station']._get_configured_station())

    def test_get_configured_station_enabled_without_station_raises(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_amazon_sync.zpl_printing_enabled', 'True')
        params.set_param('ke_amazon_sync.zpl_default_station_id', '')
        with self.assertRaises(UserError):
            self.env['ke.amazon.print.station']._get_configured_station()

    def test_get_configured_station_enabled_with_station_returns_it(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_amazon_sync.zpl_printing_enabled', 'True')
        params.set_param('ke_amazon_sync.zpl_default_station_id', str(self.station.id))
        found = self.env['ke.amazon.print.station']._get_configured_station()
        self.assertEqual(found.id, self.station.id)

    def test_get_configured_station_ignores_inactive_station(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_amazon_sync.zpl_printing_enabled', 'True')
        params.set_param('ke_amazon_sync.zpl_default_station_id', str(self.station.id))
        self.station.active = False
        with self.assertRaises(UserError):
            self.env['ke.amazon.print.station']._get_configured_station()

    def test_mark_done_and_mark_error(self):
        job = self.env['ke.amazon.print.job']._enqueue_from_label_log(self.station, self.label_log)
        job._mark_done()
        self.assertEqual(job.state, 'done')
        self.assertTrue(job.done_date)

        job2 = self.env['ke.amazon.print.job']._enqueue_from_label_log(self.station, self.label_log)
        job2._mark_error('stampante offline')
        self.assertEqual(job2.state, 'error')
        self.assertEqual(job2.error_message, 'stampante offline')


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestPrintAgentController(HttpCase):

    def setUp(self):
        super().setUp()
        self.station = self.env['ke.amazon.print.station'].create({'name': 'ZZT Stazione HTTP'})
        action = self.station.action_generate_api_key()
        self.raw_key = action['context']['default_raw_key']
        self.product = self.env['product.product'].create({
            'name': 'ZZT Prodotto HTTP', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-PRINT-HTTP-1', 'x_amazon_fnsku': 'B0ZZPRINTHTTP1',
        })
        self.label_log = self.env['ke.amazon.fba.label.log'].create({
            'product_id': self.product.id, 'fnsku': 'B0ZZPRINTHTTP1',
        })

    def _post_json(self, url, payload):
        response = self.url_open(url, data=json.dumps(payload), headers={'Content-Type': 'application/json'})
        return response

    def test_poll_with_wrong_api_key_returns_401(self):
        response = self._post_json('/ke_amazon_sync/print_agent/poll', {'api_key': 'sbagliata'})
        self.assertEqual(response.status_code, 401)

    def test_poll_and_ack_full_cycle(self):
        job = self.env['ke.amazon.print.job']._enqueue_from_label_log(self.station, self.label_log)

        poll_response = self._post_json('/ke_amazon_sync/print_agent/poll', {'api_key': self.raw_key})
        self.assertEqual(poll_response.status_code, 200)
        poll_data = poll_response.json()
        self.assertEqual(len(poll_data['jobs']), 1)
        self.assertEqual(poll_data['jobs'][0]['id'], job.id)
        self.assertIn('B0ZZPRINTHTTP1', poll_data['jobs'][0]['zpl'])

        job.invalidate_recordset()
        self.assertEqual(job.state, 'sent')

        ack_response = self._post_json('/ke_amazon_sync/print_agent/ack', {
            'api_key': self.raw_key, 'job_id': job.id, 'result': 'done',
        })
        self.assertEqual(ack_response.status_code, 200)
        self.assertTrue(ack_response.json()['ok'])

        job.invalidate_recordset()
        self.assertEqual(job.state, 'done')

        # Un secondo poll non deve più restituire il lavoro già confermato.
        second_poll = self._post_json('/ke_amazon_sync/print_agent/poll', {'api_key': self.raw_key})
        self.assertEqual(second_poll.json()['jobs'], [])

    def test_ack_with_error_result(self):
        job = self.env['ke.amazon.print.job']._enqueue_from_label_log(self.station, self.label_log)

        ack_response = self._post_json('/ke_amazon_sync/print_agent/ack', {
            'api_key': self.raw_key, 'job_id': job.id, 'result': 'error', 'error': 'carta esaurita',
        })
        self.assertEqual(ack_response.status_code, 200)
        job.invalidate_recordset()
        self.assertEqual(job.state, 'error')
        self.assertEqual(job.error_message, 'carta esaurita')


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestLabelPrintingToggle(TransactionCase):
    """
    'Usa stampa di rete (ZPL)' (Impostazioni → Amazon) deve cambiare il
    comportamento dei due wizard di stampa SOLO quando attiva e configurata
    — disattiva di default, deve restare identico a prima (PDF).
    """

    def setUp(self):
        super().setUp()
        self.station = self.env['ke.amazon.print.station'].create({'name': 'ZZT Stazione toggle'})
        self.product = self.env['product.product'].create({
            'name': 'ZZT Prodotto toggle', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-TOGGLE-1', 'x_amazon_fnsku': 'B0ZZTOGGLE1',
            'barcode': '0000000ZZTOGL1',
        })

    def _enable_zpl(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_amazon_sync.zpl_printing_enabled', 'True')
        params.set_param('ke_amazon_sync.zpl_default_station_id', str(self.station.id))

    def test_standalone_wizard_prints_pdf_by_default(self):
        wiz = self.env['ke.amazon.fba.standalone.label.wizard'].create({
            'product_id': self.product.id, 'quantity': 2,
        })
        action = wiz.action_print()
        self.assertEqual(action.get('report_name'), 'ke_amazon_sync.report_amazon_fba_label_document')
        self.assertFalse(self.env['ke.amazon.print.job'].search([('station_id', '=', self.station.id)]))

    def test_standalone_wizard_enqueues_when_zpl_enabled(self):
        self._enable_zpl()
        wiz = self.env['ke.amazon.fba.standalone.label.wizard'].create({
            'product_id': self.product.id, 'quantity': 2,
        })
        result = wiz.action_print()
        self.assertTrue(result, 'quando la stampa di rete è attiva non deve tornare il report PDF')
        jobs = self.env['ke.amazon.print.job'].search([('station_id', '=', self.station.id)])
        self.assertEqual(len(jobs), 2, 'una copia richiesta = un lavoro in coda')
        self.assertIn('B0ZZTOGGLE1', jobs[0].zpl)
        self.assertIn('in coda di stampa', wiz.last_scan_message)

    def _make_picking_with_move(self, qty=2):
        picking_type = self.env.ref('stock.picking_type_internal', raise_if_not_found=False) \
            or self.env['stock.picking.type'].search([], limit=1)
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': picking_type.default_location_dest_id.id,
        })
        self.env['stock.move'].create({
            'picking_id': picking.id,
            'product_id': self.product.id,
            'product_uom_qty': qty,
            'product_uom': self.product.uom_id.id,
            'location_id': picking.location_id.id,
            'location_dest_id': picking.location_dest_id.id,
        })
        return picking

    def test_prep_wizard_prints_pdf_by_default(self):
        picking = self._make_picking_with_move()
        wiz = self.env['ke.amazon.fba.prep.wizard'].create({
            'picking_id': picking.id, 'ean_scan': self.product.barcode,
        })
        action = wiz.action_scan()
        self.assertEqual(action.get('report_name'), 'ke_amazon_sync.report_amazon_fba_label_document')

    def test_prep_wizard_enqueues_when_zpl_enabled(self):
        self._enable_zpl()
        picking = self._make_picking_with_move()
        wiz = self.env['ke.amazon.fba.prep.wizard'].create({
            'picking_id': picking.id, 'ean_scan': self.product.barcode,
        })
        wiz.action_scan()
        jobs = self.env['ke.amazon.print.job'].search([('station_id', '=', self.station.id)])
        self.assertEqual(len(jobs), 1)
        self.assertIn('in coda di stampa', wiz.last_scan_message)
        # La riga di spedizione resta tracciata come etichettata (1/2) a
        # prescindere dal canale di stampa scelto.
        move = picking.move_ids
        self.assertEqual(move.quantity, 1)
