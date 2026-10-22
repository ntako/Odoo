"""
Test del log dell'import completo incatenato all'Import FNSKU
(_finish_full_import → action_start_fnsku_import → _close_parent_log).

Richiesta dell'utente del 2026-10-22: il log dell'import completo passava a
'Completato' mentre l'Import FNSKU avviato subito dopo girava ancora in
background. Ora resta 'In corso' finché non termina anche l'FNSKU.

_start_thread è sostituito da un no-op: nessun thread parte, quindi nessuna
chiamata ad Amazon (il thread FNSKU vero creerebbe un SpApiClient reale).
Le fasi del thread sono richiamate a mano (_finish_fnsku_import,
_close_parent_log, action_mark_done).
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from .common import no_commit


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestChainedFnskuLog(TransactionCase):

    def setUp(self):
        super().setUp()
        self.SyncLog = self.env['ke.amazon.sync.log']
        self.params = self.env['ir.config_parameter'].sudo()
        self.params.set_param('ke_amazon_sync.fnsku_import_state', 'idle')
        self.params.set_param('ke_amazon_sync.import_state', 'running')
        self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT Catena', 'code': 'ZZCH', 'marketplace_id': 'ZZCHMKTID', 'active': True,
        })
        self.parent = self.SyncLog.create({
            'name': 'ZZT import completo', 'sync_type': 'full_import', 'state': 'running',
        })

    def _finish_full_import(self):
        with no_commit(self.env), patch.object(type(self.SyncLog), '_start_thread', lambda *a: None):
            self.SyncLog._finish_full_import(self.parent)
        return self.SyncLog.search([('parent_log_id', '=', self.parent.id)])

    def test_full_import_stays_running_until_fnsku_done(self):
        child = self._finish_full_import()
        self.assertEqual(len(child), 1, "l'Import FNSKU deve essere collegato al log principale")
        self.assertEqual(self.parent.state, 'running')
        self.assertEqual(self.params.get_param('ke_amazon_sync.import_state'), 'running')

        with no_commit(self.env):
            self.SyncLog._finish_fnsku_import(child)
        self.assertEqual(self.parent.state, 'done')
        self.assertTrue(self.parent.end_date)
        self.assertEqual(self.params.get_param('ke_amazon_sync.import_state'), 'idle')

    def test_fnsku_error_marks_full_import_error(self):
        child = self._finish_full_import()
        with no_commit(self.env):
            self.SyncLog._close_parent_log(child, error='report non pronto')
        self.assertEqual(self.parent.state, 'error')
        self.assertIn('report non pronto', self.parent.error_summary)
        self.assertEqual(self.params.get_param('ke_amazon_sync.import_state'), 'idle')

    def test_fnsku_mark_done_by_watchdog_closes_full_import(self):
        child = self._finish_full_import()
        with no_commit(self.env):
            child.action_mark_done(state='error', error_summary='nessun aggiornamento da 3 ore')
        self.assertEqual(self.parent.state, 'error')

    def test_fnsku_already_running_closes_full_import_immediately(self):
        # action_start_fnsku_import considera "già in corso" solo uno stato
        # 'running' con un log FNSKU esistente collegato (stesso controllo
        # del pulsante manuale).
        other = self.SyncLog.create({
            'name': 'ZZT altro import FNSKU', 'sync_type': 'import_fnsku', 'state': 'running',
        })
        self.params.set_param('ke_amazon_sync.fnsku_import_state', 'running')
        self.params.set_param('ke_amazon_sync.fnsku_import_log_id', str(other.id))
        child = self._finish_full_import()
        self.assertFalse(child, 'nessun nuovo Import FNSKU con un altro già in corso')
        self.assertEqual(self.parent.state, 'done')
        self.assertEqual(self.params.get_param('ke_amazon_sync.import_state'), 'idle')
