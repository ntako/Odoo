import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine

_log = logging.getLogger('ke.amazon.sync.cron')


class AmazonSyncLog(models.Model):
    _name = 'ke.amazon.sync.log'
    _description = 'Amazon Sync Log'
    _order = 'create_date desc'
    _rec_name = 'name'

    name = fields.Char('Operazione', required=True)
    sync_type = fields.Selection([
        ('full_import', 'Import Completo'),
        ('check_new', 'Controllo Nuovi Prodotti'),
        ('price_qty', 'Sincronizzazione Prezzi/Quantità'),
        ('download_images', 'Download Immagini'),
        ('reconcile', 'Riconciliazione Store'),
        ('apply_cat_mapping', 'Aggiornamento da Catalog API'),
        ('import_orders', 'Import Ordini Amazon'),
        ('sync_fba', 'Sync Giacenze FBA'),
        ('returns', 'Import Resi Amazon'),
        ('seed_fbm_stock', 'Recupero Giacenza FBM da Amazon'),
        ('move_to_general', 'Spostamento Giacenza verso Magazzino Generale'),
        ('import_fnsku', 'Import FNSKU'),
        ('detect_marketplaces', 'Rileva Marketplace Attivi'),
    ], string='Tipo', required=True)
    state = fields.Selection([
        ('running', 'In corso'),
        ('done', 'Completato'),
        ('error', 'Errore'),
    ], string='Stato', default='running')

    products_total = fields.Integer('Prodotti totali')
    products_created = fields.Integer('Creati')
    products_updated = fields.Integer('Aggiornati')
    products_skipped = fields.Integer('Saltati')
    products_error = fields.Integer('Errori')

    start_date = fields.Datetime('Inizio', default=fields.Datetime.now)
    end_date = fields.Datetime('Fine')
    duration = fields.Float('Durata (s)', compute='_compute_duration', store=True)

    log_lines = fields.Text('Log dettagliato')
    error_summary = fields.Text('Sommario errori')

    @api.depends('start_date', 'end_date')
    def _compute_duration(self):
        for rec in self:
            if rec.start_date and rec.end_date:
                rec.duration = (rec.end_date - rec.start_date).total_seconds()
            else:
                rec.duration = 0.0

    def action_open(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_refresh(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_mark_done(self, state='done', error_summary=None):
        """
        Segna il log come terminato e ripulisce lo stato/i parametri del
        processo associato (coda marketplace, cron, ecc.) — stessa pulizia
        sia per una chiusura normale (pulsante "Segna come completato",
        state='done') sia per una rilevata da _cron_watchdog_stale_syncs
        (state='error', log rimasto 'running' troppo a lungo senza
        aggiornamenti: probabile thread interrotto da un riavvio o un cron
        bloccato — vedi quel metodo).
        """
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        if self.sync_type == 'import_orders':
            # Come full_import/reconcile/resi/import_fnsku: thread di
            # background, qui liberiamo solo lo stato per permettere un nuovo
            # avvio, non fermiamo un thread già in esecuzione (vedi
            # _thread_import_orders). Niente più ir.attachment da ripulire:
            # il report bulk articoli-ordine (vedi progetto-sync-ordini-report.md)
            # vive solo in memoria dentro il thread, non viene mai persistito.
            params.set_param('ke_amazon_sync.orders_import_state', 'idle')
            params.set_param('ke_amazon_sync.orders_import_marketplace_queue', '')
            params.set_param('ke_amazon_sync.orders_import_current_marketplace_id', '')
        elif self.sync_type == 'sync_fba':
            # La sync gira in un thread di background (non un cron a pagine):
            # qui possiamo solo liberare lo stato per permettere un nuovo avvio,
            # non fermare un thread già in esecuzione (terminerà da solo).
            params.set_param('ke_amazon_sync.fba_sync_state', 'idle')
            params.set_param('ke_amazon_sync.fba_sync_log_id', '')
        elif self.sync_type == 'returns':
            # Come full_import/reconcile: thread di background, qui liberiamo
            # solo lo stato per permettere un nuovo avvio, non fermiamo un
            # thread già in esecuzione (vedi _thread_returns).
            params.set_param('ke_amazon_sync.returns_state', 'idle')
            params.set_param('ke_amazon_sync.returns_log_id', '')
            params.set_param('ke_amazon_sync.returns_marketplace_queue', '')
            params.set_param('ke_amazon_sync.returns_current_marketplace_id', '')
        elif self.sync_type == 'full_import':
            # Come sync_fba/download_images: thread di background, qui
            # liberiamo solo lo stato per permettere un nuovo avvio, non
            # fermiamo un thread già in esecuzione (vedi action_stop_import).
            params.set_param('ke_amazon_sync.import_state', 'idle')
            params.set_param('ke_amazon_sync.import_marketplace_queue', '')
            params.set_param('ke_amazon_sync.import_current_marketplace_id', '')
        elif self.sync_type == 'download_images':
            # Come sync_fba: thread di background, qui liberiamo solo lo
            # stato per permettere un nuovo avvio, non fermiamo un thread
            # già in esecuzione (terminerà da solo, sovrascrivendo poi
            # questo stesso stato a 'idle' di nuovo alla sua fine).
            params.set_param('ke_amazon_sync.download_images_state', 'idle')
        elif self.sync_type == 'reconcile':
            # Come full_import: thread di background, qui liberiamo solo lo
            # stato per permettere un nuovo avvio, non fermiamo un thread già
            # in esecuzione (vedi _thread_reconcile).
            params.set_param('ke_amazon_sync.reconcile_state', 'idle')
            params.set_param('ke_amazon_sync.reconcile_log_id', '')
            params.set_param('ke_amazon_sync.reconcile_marketplace_queue', '')
            params.set_param('ke_amazon_sync.reconcile_current_marketplace_id', '')
        elif self.sync_type == 'import_fnsku':
            # Come full_import/reconcile/resi: thread di background, qui
            # liberiamo solo lo stato per permettere un nuovo avvio, non
            # fermiamo un thread già in esecuzione (vedi _thread_fnsku_import).
            params.set_param('ke_amazon_sync.fnsku_import_state', 'idle')
            params.set_param('ke_amazon_sync.fnsku_import_log_id', '')
            params.set_param('ke_amazon_sync.fnsku_import_marketplace_queue', '')
            params.set_param('ke_amazon_sync.fnsku_import_current_marketplace_id', '')
        elif self.sync_type == 'apply_cat_mapping':
            # Cron a chunk (non un thread): qui bisogna anche disattivare il
            # cron esplicitamente, non solo liberare lo stato — a differenza
            # dei thread di background sopra, che si fermano da soli, il cron
            # continuerebbe a girare al prossimo tick e a rimettere
            # cat_map_state='running' via _cron_apply_category_mapping se
            # non lo disattiviamo qui. Branch mancante fino ad oggi: un click
            # su "Ferma e segna terminato" su questo tipo di log segnava il
            # record 'done' ma lasciava cat_map_state incastrato su 'running'
            # — stesso sintomo del bug del giro prematuramente 'done' più sopra.
            params.set_param('ke_amazon_sync.cat_map_state', 'idle')
            params.set_param('ke_amazon_sync.cat_map_offset', '')
            params.set_param('ke_amazon_sync.cat_map_max_id', '')
            params.set_param('ke_amazon_sync.cat_map_log_id', '')
            self._disable_cat_map_cron()
        vals = {'state': state, 'end_date': fields.Datetime.now()}
        if error_summary:
            vals['error_summary'] = error_summary
        self.write(vals)

    # Soglia oltre la quale un log 'running' senza nessun aggiornamento è
    # quasi certamente interrotto (thread ucciso da un riavvio, cron rimasto
    # bloccato, ecc.) e non una sync semplicemente lenta — scelta prudente,
    # non un limite stretto: anche il full import più lento visto finora
    # (report-based, più marketplace in sequenza) resta ben sotto le 3 ore.
    _STALE_RUNNING_HOURS = 3

    @api.model
    def _cron_watchdog_stale_syncs(self):
        """
        Cron di igiene (indipendente da tutte le altre sync, non chiama mai
        Amazon): cerca log rimasti 'running' senza nessun aggiornamento da
        più di _STALE_RUNNING_HOURS ore e li chiude in errore, ripulendo
        anche lo stato/i parametri associati (stessa logica di
        action_mark_done, per non lasciare un cron disattivato o una coda
        marketplace a metà che blocca il prossimo avvio).
        Trovato il problema reale in una sessione di sviluppo: un log
        rimasto 'running' per una settimana intera senza che nulla lo
        segnalasse, scoperto solo controllando a mano.
        """
        threshold = fields.Datetime.now() - timedelta(hours=self._STALE_RUNNING_HOURS)
        stale_logs = self.search([
            ('state', '=', 'running'),
            ('write_date', '<', threshold),
        ])
        for log in stale_logs:
            age = fields.Datetime.now() - log.write_date
            msg = (
                f'Nessun aggiornamento da {age.total_seconds() / 3600:.1f} ore — '
                f'probabile interruzione (riavvio server, thread bloccato, o simile). '
                f'Segnato come errore automaticamente dal controllo di igiene.'
            )
            _log.warning('Watchdog: log #%d (%s, %s) rimasto "running" da troppo tempo: %s',
                          log.id, log.name, log.sync_type, msg)
            log.action_mark_done(state='error', error_summary=msg)
            self.env.cr.commit()

    def action_export_csv(self):
        self.ensure_one()
        if self.sync_type == 'full_import':
            return self._export_csv_full_import()
        return self._export_csv_reconcile()

    def _export_csv_reconcile(self):
        import base64
        import csv
        import io
        import re

        self.ensure_one()

        # Pattern delle righe del log di riconciliazione
        pat_ok          = re.compile(r'^\[OK\]\s+(.+?)\s+SKU=(\S+)\s+stato=(\S+)\s+prezzo=([\d.]+)€')
        pat_solo_odoo   = re.compile(r'^\[SOLO ODOO\]\s+(.+?)\s+SKU=(\S+)(?:\s+ASIN=(\S+))?')
        pat_solo_amazon = re.compile(r'^\[SOLO AMAZON\]\s+SKU=(\S+)\s+ASIN=(\S+)\s+"(.*)"$')
        pat_sku_upd_asin = re.compile(r'^\[SKU AGGIORNATO\]\s+(.+?)\s+ASIN=(\S+)\s+(\S+)\s+→\s+(\S+)')
        pat_sku_upd_plus = re.compile(
            r'^\[SKU AGGIORNATO\]\s+(.+?)\s+SKU=(\S+)\s+→\s+(\S+)'
            r'(?:\s+stato=(\S+))?(?:\s+prezzo=([\d.]+)€)?$'
        )
        pat_errore      = re.compile(r'^\[ERRORE\]\s+(?:variante\s+)?SKU=(\S+):\s+(.+)$')

        rows = []
        for line in (self.log_lines or '').splitlines():
            line = line.strip()
            if not line or line.startswith('Store Amazon:') or line.startswith('Download') or line.startswith('Riepilogo'):
                continue

            if line.startswith('[OK]'):
                m = pat_ok.match(line)
                if m:
                    rows.append({
                        'tipo': 'OK',
                        'nome': m.group(1),
                        'sku': m.group(2),
                        'asin': '',
                        'stato_amazon': m.group(3),
                        'prezzo_amazon': m.group(4),
                        'note': '',
                    })
            elif line.startswith('[SOLO ODOO]'):
                m = pat_solo_odoo.match(line)
                if m:
                    rows.append({
                        'tipo': 'SOLO_ODOO',
                        'nome': m.group(1),
                        'sku': m.group(2),
                        'asin': m.group(3) or '',
                        'stato_amazon': '',
                        'prezzo_amazon': '',
                        'note': 'Presente in Odoo ma non trovato su Amazon',
                    })
            elif line.startswith('[SOLO AMAZON]'):
                m = pat_solo_amazon.match(line)
                if m:
                    rows.append({
                        'tipo': 'SOLO_AMAZON',
                        'nome': m.group(3),
                        'sku': m.group(1),
                        'asin': m.group(2),
                        'stato_amazon': '',
                        'prezzo_amazon': '',
                        'note': 'Presente su Amazon ma non importato in Odoo',
                    })
            elif line.startswith('[SKU AGGIORNATO]'):
                if 'ASIN=' in line:
                    m = pat_sku_upd_asin.match(line)
                    if m:
                        rows.append({
                            'tipo': 'SKU_AGGIORNATO',
                            'nome': m.group(1),
                            'sku': m.group(4),
                            'asin': m.group(2),
                            'stato_amazon': '',
                            'prezzo_amazon': '',
                            'note': f'SKU aggiornato da {m.group(3)} (rifornimento)',
                        })
                else:
                    m = pat_sku_upd_plus.match(line)
                    if m:
                        rows.append({
                            'tipo': 'SKU_AGGIORNATO',
                            'nome': m.group(1),
                            'sku': m.group(3),
                            'asin': '',
                            'stato_amazon': m.group(4) or '',
                            'prezzo_amazon': m.group(5) or '',
                            'note': f'SKU aggiornato da {m.group(2)} (rifornimento)',
                        })
            elif line.startswith('[ERRORE]'):
                m = pat_errore.match(line)
                if m:
                    rows.append({
                        'tipo': 'ERRORE',
                        'nome': '',
                        'sku': m.group(1),
                        'asin': '',
                        'stato_amazon': '',
                        'prezzo_amazon': '',
                        'note': m.group(2),
                    })

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=['tipo', 'nome', 'sku', 'asin', 'stato_amazon', 'prezzo_amazon', 'note'],
                                delimiter=';')
        writer.writeheader()
        writer.writerows(rows)

        filename = f'riconciliazione_amazon_{self.id}.csv'
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(('﻿' + buf.getvalue()).encode('utf-8')).decode(),
            'res_model': self._name,
            'res_id': self.id,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}/{filename}?download=true',
            'target': 'self',
        }

    def _export_csv_full_import(self):
        import base64
        import csv
        import io
        import re

        self.ensure_one()

        # Pattern delle righe del log di import (full_import / full_import_chunk / full_import_report_rows)
        pat_creato           = re.compile(r'^CREATO:\s+(\S+)\s+/\s+(\S+)\s+—\s+(.*)$')
        pat_creato_variante  = re.compile(r'^CREATO variante:\s+(\S+)\s+/\s+(\S+)\s+\[(.*?)\]\s+→\s+(.*)$')
        pat_aggiornato       = re.compile(r'^AGGIORNATO:\s+(\S+)\s+/\s+(\S+)\s+—\s+(.*)$')
        pat_aggiornato_var   = re.compile(r'^AGGIORNATO variante:\s+(\S+)\s+/\s+(\S+)\s+→\s+(.*)$')
        pat_saltato_sost     = re.compile(r'^SALTATO \(sostituito da (\S+)\):\s+(\S+)$')
        pat_saltato_esiste   = re.compile(r'^SALTATO \((?:già esiste|variante già esiste)\):\s+(\S+)$')
        pat_saltato_parent   = re.compile(r'^SALTATO \(parent listing\):\s+(\S+)\s+/\s+(\S+)$')
        pat_saltato_dim      = re.compile(r'^SALTATO\s+(\S+)\s+/\s+(\S+):\s+(.*)$')
        pat_errore_var_crea  = re.compile(r'^ERRORE creazione variante:\s+(\S+)\s+/\s+(\S+)$')
        pat_errore_variante  = re.compile(r'^ERRORE variante\s+(\S+):\s+(.*)$')
        pat_errore           = re.compile(r'^ERRORE\s+(\S+):\s+(.*)$')

        rows = []
        unmatched = 0
        for raw_line in (self.log_lines or '').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('Report ') or line.startswith('Store Amazon'):
                continue

            m = pat_creato_variante.match(line)
            if m:
                rows.append({'tipo': 'CREATO_VARIANTE', 'sku': m.group(1), 'asin': m.group(2),
                             'nome': m.group(4), 'note': m.group(3)})
                continue
            m = pat_creato.match(line)
            if m:
                rows.append({'tipo': 'CREATO', 'sku': m.group(1), 'asin': m.group(2),
                             'nome': m.group(3), 'note': ''})
                continue
            m = pat_aggiornato_var.match(line)
            if m:
                rows.append({'tipo': 'AGGIORNATO_VARIANTE', 'sku': m.group(1), 'asin': m.group(2),
                             'nome': m.group(3), 'note': ''})
                continue
            m = pat_aggiornato.match(line)
            if m:
                rows.append({'tipo': 'AGGIORNATO', 'sku': m.group(1), 'asin': m.group(2),
                             'nome': m.group(3), 'note': ''})
                continue
            m = pat_saltato_sost.match(line)
            if m:
                rows.append({'tipo': 'SALTATO', 'sku': m.group(2), 'asin': '', 'nome': '',
                             'note': f'sostituito da {m.group(1)}'})
                continue
            m = pat_saltato_esiste.match(line)
            if m:
                rows.append({'tipo': 'SALTATO', 'sku': m.group(1), 'asin': '', 'nome': '',
                             'note': 'già esistente in Odoo'})
                continue
            m = pat_saltato_parent.match(line)
            if m:
                rows.append({'tipo': 'SALTATO', 'sku': m.group(1), 'asin': m.group(2), 'nome': '',
                             'note': 'parent listing'})
                continue
            m = pat_saltato_dim.match(line)
            if m:
                rows.append({'tipo': 'SALTATO', 'sku': m.group(1), 'asin': m.group(2), 'nome': '',
                             'note': m.group(3)})
                continue
            m = pat_errore_var_crea.match(line)
            if m:
                rows.append({'tipo': 'ERRORE', 'sku': m.group(1), 'asin': m.group(2), 'nome': '',
                             'note': 'errore creazione variante'})
                continue
            m = pat_errore_variante.match(line)
            if m:
                rows.append({'tipo': 'ERRORE', 'sku': m.group(1), 'asin': '', 'nome': '',
                             'note': m.group(2)})
                continue
            m = pat_errore.match(line)
            if m:
                rows.append({'tipo': 'ERRORE', 'sku': m.group(1), 'asin': '', 'nome': '',
                             'note': m.group(2)})
                continue

            # Riga non riconosciuta dal parser: esportata comunque per non perdere informazioni
            unmatched += 1
            rows.append({'tipo': 'ALTRO', 'sku': '', 'asin': '', 'nome': '', 'note': line})

        if unmatched:
            _log.info('Export CSV import log %d: %d righe non riconosciute dal parser (tipo=ALTRO)',
                       self.id, unmatched)

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=['tipo', 'sku', 'asin', 'nome', 'note'], delimiter=';')
        writer.writeheader()
        writer.writerows(rows)

        filename = f'import_amazon_{self.id}.csv'
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(('﻿' + buf.getvalue()).encode('utf-8')).decode(),
            'res_model': self._name,
            'res_id': self.id,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}/{filename}?download=true',
            'target': 'self',
        }

    def action_stop_import(self):
        """
        Come sync_fba/download_images: l'import gira in un thread di
        background, qui possiamo solo liberare lo stato per permettere un
        nuovo avvio, non fermare un thread già in esecuzione — il thread
        controlla import_state ad ogni cambio di marketplace e si ferma da
        solo appena può (vedi _thread_full_import), ma non a metà di un
        report/chunk già in corso.
        """
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_amazon_sync.import_state', 'idle')
        self.write({'state': 'done', 'end_date': fields.Datetime.now()})

    def action_start_full_import(self):
        from odoo.exceptions import UserError
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.import_state') == 'running':
            raise UserError('Un import è già in corso. Controlla il log sincronizzazioni.')

        marketplaces = self.env['ke.amazon.marketplace'].search([('active', '=', True)])
        if not marketplaces:
            raise UserError(
                'Nessun marketplace attivo. Usa "Rileva marketplace attivi" '
                '(Impostazioni → Amazon) per attivarli automaticamente, oppure '
                'attivali a mano da Magazzino → Amazon Sync → Marketplace.'
            )

        max_products = int(params.get_param('ke_amazon_sync.max_products', '0') or 0)
        limit_label = f' (limite {max_products})' if max_products else ''
        log_rec = self.create({
            'name': f'Import completo{limit_label} — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'full_import',
            'state': 'running',
            'start_date': fields.Datetime.now(),
            'log_lines': f'Import su {len(marketplaces)} marketplace: {", ".join(marketplaces.mapped("code"))}.',
        })
        update_existing = params.get_param('ke_amazon_sync.import_update_existing_default', 'True') != 'False'

        params.set_param('ke_amazon_sync.import_state', 'running')
        params.set_param('ke_amazon_sync.import_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.import_update_existing', str(update_existing))
        params.set_param('ke_amazon_sync.import_max_products', str(max_products))
        params.set_param('ke_amazon_sync.import_marketplace_queue',
                          ','.join(str(i) for i in marketplaces.ids))
        params.set_param('ke_amazon_sync.import_current_marketplace_id', '')
        self.env.cr.commit()

        self._start_thread('_thread_full_import', log_rec.id)

        return {
            'type': 'ir.actions.act_window',
            'name': 'Log Import Amazon',
            'res_model': 'ke.amazon.sync.log',
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _start_thread(self, method_name, log_id):
        """
        Avvia 'method_name' (uno dei metodi _thread_* di questo modello) in un
        thread daemon separato, stesso pattern già usato altrove nel modulo
        (vedi res_config_settings.py). NB: un thread daemon viene interrotto
        di colpo se il processo Odoo termina (riavvio/crash) — non esegue il
        proprio blocco finally, quindi il log resta 'running' finché il
        watchdog (_cron_watchdog_stale_syncs) non lo intercetta.
        """
        import threading
        db = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        t = threading.Thread(
            target=getattr(self, method_name),
            args=(db, uid, ctx, log_id),
            daemon=True,
        )
        t.start()

    def _append_log_line(self, log_rec, msg):
        """
        Aggiunge una riga a log_lines e fa commit, ritentando se Postgres
        segnala un conflitto di scrittura concorrente sulla stessa riga —
        successo davvero durante lo sviluppo: il form del log si auto-
        ricarica ogni 10s mentre lo stato è 'running' (vedi
        amazon_sync_log_form.js), e un thread che scrive molto più spesso
        del vecchio tick di cron da 2 minuti allarga la finestra di
        collisione con quella lettura concorrente. Ricalcola log_lines da
        capo ad ogni tentativo, non solo il commit: dopo un rollback il
        valore va rinfrescato dal DB (flush=False: non ritentare a scrivere
        il valore fallito, solo scartare la cache locale).
        """
        import time
        from psycopg2 import errors as pg_errors
        env = log_rec.env
        for attempt in range(3):
            try:
                log_rec.write({'log_lines': (log_rec.log_lines or '') + '\n' + msg})
                env.cr.commit()
                _log.info(msg)
                return
            except pg_errors.SerializationFailure:
                env.cr.rollback()
                log_rec.invalidate_recordset(['log_lines'], flush=False)
                if attempt == 2:
                    raise
                time.sleep(0.3 * (attempt + 1))

    # Tempo tra un poll e l'altro dello stato di un report Amazon durante
    # l'attesa: get_report permette 2 richieste/secondo (burst 15) —
    # verificato prima di scegliere questo numero, non indovinato — quindi
    # molto più stretto dei 2 minuti del vecchio tick di cron, senza alcun
    # rischio di throttling anche con più sync di questo tipo attive insieme.
    _REPORT_POLL_INTERVAL_S = 20
    # Tetto di sicurezza: oltre questa attesa un report non ancora pronto è
    # anomalo — meglio segnalarlo come errore (il watchdog lo intercetterebbe
    # comunque, ma prima è meglio) che restare in poll indefinitamente.
    _REPORT_POLL_MAX_MINUTES = 30
    # Righe elaborate tra un commit e l'altro durante l'elaborazione del
    # report: non serve a rispettare un rate limit (full_import_report_rows
    # non chiama mai Amazon, verificato nel codice), solo a non perdere tutto
    # il lavoro fatto se il thread venisse interrotto a metà (vedi
    # _start_thread). 10x il vecchio _REPORT_CHUNK_SIZE (200, dimensionato
    # per il tick di cron, non per un limite reale) — valore di partenza,
    # non ancora misurato a fondo su un catalogo molto grande.
    _ROW_COMMIT_BATCH = 2000

    @api.model
    def _thread_full_import(self, db, uid, ctx, log_id):
        """
        Sostituisce la vecchia state machine a cron-a-tick (_cron_full_import_chunk,
        rimossa): un unico thread continuo che scorre tutti i marketplace in
        coda, per ciascuno crea il report, aspetta che sia pronto con poll
        stretti (non più il tick da 2 minuti), lo scarica e elabora tutte le
        righe senza aspettare nulla tra un chunk e l'altro — vedi
        progetto-sync-thread-invece-di-cron.md per il ragionamento completo.
        Controlla ad ogni marketplace se il log è stato fermato dall'esterno
        (action_stop_import/action_mark_done impostano import_state≠'running')
        prima di proseguire — non può interrompere un report/chunk già in
        corso, ma può fermarsi tra un marketplace e il successivo.
        """
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        import time

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            SyncLog = env['ke.amazon.sync.log']
            log_rec = SyncLog.browse(log_id)
            params = env['ir.config_parameter'].sudo()
            try:
                update_existing = params.get_param('ke_amazon_sync.import_update_existing', 'True') == 'True'
                max_products = int(params.get_param('ke_amazon_sync.import_max_products', '0') or 0)

                while True:
                    if params.get_param('ke_amazon_sync.import_state') != 'running':
                        _log.info('Thread full_import: fermato dall\'esterno, esco senza completare.')
                        return

                    marketplace = SyncLog._full_import_pop_marketplace(params)
                    if not marketplace:
                        break
                    env.cr.commit()

                    client = SpApiClient(env, marketplace_id=marketplace.marketplace_id)
                    engine = SyncEngine(env, client)

                    if max_products and max_products <= 1000:
                        # Fase 0: catalogo piccolo (solo per test), paginazione
                        # classica — nessuna attesa artificiale tra una pagina
                        # e l'altra, a differenza del vecchio tick di cron.
                        page_token = None
                        while not (max_products and log_rec.products_total >= max_products):
                            next_token = engine.full_import_chunk(
                                log_rec, page_token=page_token, update_existing=update_existing,
                            )
                            env.cr.commit()
                            if not next_token:
                                break
                            page_token = next_token
                    else:
                        # Fasi 1-3: import completo via Reports API (nessun limite).
                        report_id = client.request_listings_report()
                        msg = f'[{marketplace.code}] Report Amazon richiesto (ID: {report_id}). In attesa...'
                        SyncLog._append_log_line(log_rec, msg)

                        document_id = None
                        deadline = time.monotonic() + SyncLog._REPORT_POLL_MAX_MINUTES * 60
                        while time.monotonic() < deadline:
                            time.sleep(SyncLog._REPORT_POLL_INTERVAL_S)
                            document_id = client.check_report_ready(report_id)
                            if document_id:
                                break
                        if not document_id:
                            raise RuntimeError(
                                f'Report {report_id} ({marketplace.code}) non pronto dopo '
                                f'{SyncLog._REPORT_POLL_MAX_MINUTES} minuti di attesa.'
                            )

                        rows = client.download_report_rows(document_id)
                        msg = f'[{marketplace.code}] Report scaricato: {len(rows)} prodotti totali. Elaborazione...'
                        SyncLog._append_log_line(log_rec, msg)

                        row_offset = 0
                        while row_offset is not None:
                            row_offset = engine.full_import_report_rows(
                                log_rec, rows, row_offset, SyncLog._ROW_COMMIT_BATCH, update_existing,
                            )
                            env.cr.commit()

                    msg = f'Import completato per {marketplace.name} ({marketplace.code}).'
                    SyncLog._append_log_line(log_rec, msg)
                    params.set_param('ke_amazon_sync.import_current_marketplace_id', '')
                    env.cr.commit()

                SyncLog._finish_full_import(log_rec)
            except Exception as exc:
                _log.error('Thread full_import fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.import_state', 'idle')
                env.cr.commit()

    def _full_import_pop_marketplace(self, params):
        """
        Estrae il prossimo marketplace dalla coda (import_marketplace_queue)
        e lo imposta come corrente. Ritorna il record ke.amazon.marketplace,
        o un recordset vuoto se la coda è esaurita (import completo su tutti
        i marketplace attivi).
        """
        queue_s = params.get_param('ke_amazon_sync.import_marketplace_queue', '')
        queue_ids = [int(i) for i in queue_s.split(',') if i]
        if not queue_ids:
            return self.env['ke.amazon.marketplace']
        next_id, rest = queue_ids[0], queue_ids[1:]
        params.set_param('ke_amazon_sync.import_marketplace_queue',
                          ','.join(str(i) for i in rest))
        params.set_param('ke_amazon_sync.import_current_marketplace_id', str(next_id))
        return self.env['ke.amazon.marketplace'].browse(next_id)

    def _finish_full_import(self, log_rec):
        """
        Chiude l'import completo e avvia automaticamente l'Import FNSKU subito
        dopo. I due restano report Amazon separati (non si possono unire in
        un'unica chiamata, vedi discussione precedente), ma ha senso
        incatenarli: i prodotti appena creati/aggiornati hanno SKU freschi,
        l'FNSKU va ri-abbinato di conseguenza — niente da ricordarsi di
        lanciare a parte. Se l'avvio dell'import FNSKU fallisce (es. un altro
        già in corso), non fa fallire il full import: resta comunque 'done',
        l'FNSKU si può sempre lanciare a mano.
        """
        log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_amazon_sync.import_state', 'idle')
        self.env.cr.commit()

        try:
            self.action_start_fnsku_import(name_prefix='Import FNSKU (dopo import completo)')
        except Exception as exc:
            _log.warning('Import FNSKU automatico non avviato dopo il full import: %s', exc)

    def _cron_check_new_products(self):
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron check_new_products saltato: credenziali SP-API non configurate.')
            return
        hours = int(self.env['ir.config_parameter'].sudo().get_param(
            'ke_amazon_sync.check_interval_hours', '24'))
        log_rec = self.create({
            'name': f'Controllo nuovi prodotti — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'check_new',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        try:
            client = SpApiClient(self.env)
            engine = SyncEngine(self.env, client)
            found, not_found, errors = engine.check_new_products(log_rec, hours_back=hours)
            _log.info('Controllo nuovi prodotti: %d trovati, %d non trovati, %d errori',
                      found, not_found, errors)
        except Exception as e:
            _log.error('Cron check_new_products fallito: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})

    def action_start_reconcile(self, name_prefix='Riconciliazione store'):
        """
        Avvia una nuova riconciliazione via Reports API (nessun limite di prodotti),
        una per ogni marketplace attivo — stesso pattern di action_start_full_import
        (coda marketplace nei parametri, un thread di background continuo invece
        del vecchio cron a tick, vedi _thread_reconcile).
        Usata sia dal cron automatico (avvio periodico) sia dal trigger manuale
        (impostazioni). Ritorna il log creato, o None se non è stato possibile avviarla.
        """
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Avvio riconciliazione saltato: credenziali SP-API non configurate.')
            return None
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.reconcile_state') == 'running':
            _log.info('Riconciliazione già in corso, salto avvio.')
            return None

        marketplaces = self.env['ke.amazon.marketplace'].search([('active', '=', True)])
        if not marketplaces:
            _log.warning(
                'Avvio riconciliazione saltato: nessun marketplace attivo. '
                'Usa "Rileva marketplace attivi" (Impostazioni → Amazon) o attivane uno a mano.'
            )
            return None

        log_rec = self.create({
            'name': f'{name_prefix} — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'reconcile',
            'state': 'running',
            'start_date': fields.Datetime.now(),
            'log_lines': f'Riconciliazione su {len(marketplaces)} marketplace: '
                         f'{", ".join(marketplaces.mapped("code"))}.',
        })
        params.set_param('ke_amazon_sync.reconcile_state', 'running')
        params.set_param('ke_amazon_sync.reconcile_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.reconcile_marketplace_queue',
                          ','.join(str(i) for i in marketplaces.ids))
        params.set_param('ke_amazon_sync.reconcile_current_marketplace_id', '')
        self.env.cr.commit()

        self._start_thread('_thread_reconcile', log_rec.id)
        return log_rec

    @api.model
    def _thread_reconcile(self, db, uid, ctx, log_id):
        """
        Sostituisce il vecchio cron a tick (_cron_reconcile_chunk, rimosso):
        un unico thread continuo che scorre tutti i marketplace in coda,
        stesso schema di _thread_full_import (crea report → poll stretto →
        scarica → elabora, senza attendere nulla tra un marketplace e
        l'altro). A differenza del vecchio schema, le righe scaricate restano
        in memoria tra la fase di download e quella di riconciliazione
        (nello stesso thread, senza attese) — non serve più salvarle in un
        allegato temporaneo per sopravvivere a un tick di cron successivo.
        """
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        import time

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            SyncLog = env['ke.amazon.sync.log']
            log_rec = SyncLog.browse(log_id)
            params = env['ir.config_parameter'].sudo()
            try:
                while True:
                    if params.get_param('ke_amazon_sync.reconcile_state') != 'running':
                        _log.info('Thread reconcile: fermato dall\'esterno, esco senza completare.')
                        return

                    marketplace = SyncLog._reconcile_pop_marketplace(params)
                    if not marketplace:
                        break
                    env.cr.commit()

                    client = SpApiClient(env, marketplace_id=marketplace.marketplace_id)
                    engine = SyncEngine(env, client)

                    report_id = client.request_listings_report()
                    msg = f'[{marketplace.code}] Report Amazon richiesto (ID: {report_id}). In attesa...'
                    SyncLog._append_log_line(log_rec, msg)

                    document_id = None
                    deadline = time.monotonic() + SyncLog._REPORT_POLL_MAX_MINUTES * 60
                    while time.monotonic() < deadline:
                        time.sleep(SyncLog._REPORT_POLL_INTERVAL_S)
                        document_id = client.check_report_ready(report_id)
                        if document_id:
                            break
                    if not document_id:
                        raise RuntimeError(
                            f'Report {report_id} ({marketplace.code}) non pronto dopo '
                            f'{SyncLog._REPORT_POLL_MAX_MINUTES} minuti di attesa.'
                        )

                    rows = client.download_report_rows(document_id)
                    msg = f'[{marketplace.code}] Report scaricato: {len(rows)} listing totali. Riconciliazione...'
                    SyncLog._append_log_line(log_rec, msg)

                    matched, only_odoo, only_amazon, errors = engine.reconcile_from_report_rows(log_rec, rows)
                    _log.info('[%s] Riconciliazione: %d trovati, %d solo Odoo, %d solo Amazon, %d errori',
                              marketplace.code, matched, only_odoo, only_amazon, errors)

                    msg = f'Riconciliazione completata per {marketplace.name} ({marketplace.code}).'
                    SyncLog._append_log_line(log_rec, msg)
                    params.set_param('ke_amazon_sync.reconcile_current_marketplace_id', '')
                    env.cr.commit()

                SyncLog._finish_reconcile(log_rec)
            except Exception as exc:
                _log.error('Thread reconcile fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.reconcile_state', 'idle')
                env.cr.commit()

    def _reconcile_pop_marketplace(self, params):
        """
        Estrae il prossimo marketplace dalla coda (reconcile_marketplace_queue)
        e lo imposta come corrente. Ritorna il record ke.amazon.marketplace,
        o un recordset vuoto se la coda è esaurita (riconciliazione completa
        su tutti i marketplace attivi).
        """
        queue_s = params.get_param('ke_amazon_sync.reconcile_marketplace_queue', '')
        queue_ids = [int(i) for i in queue_s.split(',') if i]
        if not queue_ids:
            return self.env['ke.amazon.marketplace']
        next_id, rest = queue_ids[0], queue_ids[1:]
        params.set_param('ke_amazon_sync.reconcile_marketplace_queue',
                          ','.join(str(i) for i in rest))
        params.set_param('ke_amazon_sync.reconcile_current_marketplace_id', str(next_id))
        return self.env['ke.amazon.marketplace'].browse(next_id)

    def _finish_reconcile(self, log_rec):
        """Chiude la riconciliazione: coda marketplace esaurita."""
        log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_amazon_sync.reconcile_state', 'idle')
        params.set_param('ke_amazon_sync.reconcile_log_id', '')
        self.env.cr.commit()

    # Con apply_category_mapping che ora chiama searchCatalogItems a gruppi
    # da 20 ASIN (SyncEngine._CAT_MAP_BATCH_SIZE) invece di getCatalogItem
    # uno alla volta, 50 prodotti/tick avrebbero richiesto solo 3 chiamate
    # (~1.8s) su un tick da 2 minuti — il vero limite non era mai stato il
    # rate limit di Amazon ma questo chunk fisso. Alzato a 500: 25 chiamate
    # batch (~15s) per tick, ancora ampio margine sotto i 2 minuti anche
    # contando retry su throttling e l'overhead di scrittura Odoo (write()
    # per prodotto, un solo commit a fine chunk). Se in produzione emergono
    # timeout del cron (--limit-time-real-cron) o lock su tabelle collegate,
    # va abbassato di nuovo — non è un valore derivato da un test reale sul
    # carico di scrittura Odoo, solo dal budget delle chiamate Amazon.
    _CAT_MAP_CHUNK_SIZE = 500

    def action_start_apply_category_mapping(self):
        from odoo.exceptions import UserError

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError as exc:
            raise UserError(str(exc)) from exc

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.cat_map_state') == 'running':
            # Non fidarsi ciecamente del parametro: osservato più volte in
            # produzione che resta incastrato su 'running' anche quando il
            # log a cui punta è già concluso (done/error) — tipicamente un
            # riavvio del server a cavallo dell'ultimo tick del cron, che
            # interrompe la sequenza di write() prima che arrivi a ripulire
            # lo stato. Il log stesso (state) è la fonte di verità più
            # affidabile: se non è più 'running' lì, il parametro è solo
            # incoerente, non un giro davvero in corso — lo ripuliamo invece
            # di bloccare per sempre un nuovo avvio.
            stale_log_id = int(params.get_param('ke_amazon_sync.cat_map_log_id', '0') or 0)
            stale_log = self.browse(stale_log_id) if stale_log_id else self.browse()
            if stale_log.exists() and stale_log.state == 'running':
                raise UserError('Mapping categorie già in corso. Controlla il log sincronizzazioni.')
            _log.warning(
                'cat_map_state incoerente (running) ma il log collegato (id=%s) non lo è più '
                '(stato=%s) — ripulisco il parametro e procedo con un nuovo avvio.',
                stale_log_id, stale_log.state if stale_log.exists() else 'non trovato',
            )
            params.set_param('ke_amazon_sync.cat_map_state', 'idle')

        log_rec = self.create({
            'name': f'Aggiornamento da Catalog API — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'apply_cat_mapping',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        params.set_param('ke_amazon_sync.cat_map_state', 'running')
        params.set_param('ke_amazon_sync.cat_map_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.cat_map_offset', '0')
        # Congela il perimetro (id massimo) al momento dell'avvio: apply_category_mapping
        # ricalcola 'total' ad ogni tick con una search live, quindi un import completo/
        # riconciliazione in corso in parallelo che crea nuovi prodotti con ASIN farebbe
        # crescere 'total' durante la corsa — o, sintomo osservato in produzione, se il
        # mapping parte mentre un import è ancora a metà, 'total' visto dai primi tick è
        # molto più basso del reale e il ciclo si segna 'done' non appena il suo offset lo
        # raggiunge, anche se mancano ancora migliaia di prodotti creati poco dopo. Fissare
        # l'id massimo qui congela il perimetro del giro indipendentemente da cosa arriva dopo.
        max_id_rec = self.env['product.template'].search([], order='id desc', limit=1)
        params.set_param('ke_amazon_sync.cat_map_max_id', str(max_id_rec.id if max_id_rec else 0))
        self.env.cr.commit()

        cron = self.env.ref('ke_amazon_sync.cron_apply_category_mapping', raise_if_not_found=False)
        if cron:
            cron.sudo().active = True

        return {
            'type': 'ir.actions.act_window',
            'name': 'Log Mapping Categorie',
            'res_model': 'ke.amazon.sync.log',
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _cron_apply_category_mapping(self):
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.cat_map_state') != 'running':
            self._disable_cat_map_cron()
            return

        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron apply_category_mapping saltato: credenziali SP-API non configurate.')
            return

        log_id = int(params.get_param('ke_amazon_sync.cat_map_log_id', '0') or 0)
        if not log_id:
            params.set_param('ke_amazon_sync.cat_map_state', 'idle')
            self.env.cr.commit()
            self._disable_cat_map_cron()
            return

        log_rec = self.browse(log_id)
        if not log_rec.exists():
            params.set_param('ke_amazon_sync.cat_map_state', 'idle')
            self.env.cr.commit()
            self._disable_cat_map_cron()
            return

        offset = int(params.get_param('ke_amazon_sync.cat_map_offset', '0') or 0)
        max_id = int(params.get_param('ke_amazon_sync.cat_map_max_id', '0') or 0) or None
        try:
            engine = SyncEngine(self.env, client)
            next_offset, total = engine.apply_category_mapping(log_rec, offset, self._CAT_MAP_CHUNK_SIZE, max_id=max_id)
        except Exception as e:
            _log.error('Cron apply_category_mapping fallito: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.cat_map_state', 'idle')
            self.env.cr.commit()
            self._disable_cat_map_cron()
            return

        if next_offset is None:
            log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
            params.set_param('ke_amazon_sync.cat_map_state', 'idle')
            params.set_param('ke_amazon_sync.cat_map_offset', '')
            params.set_param('ke_amazon_sync.cat_map_max_id', '')
            self.env.cr.commit()
            self._disable_cat_map_cron()
            _log.info('apply_category_mapping completato: %d prodotti totali.', total)
        else:
            params.set_param('ke_amazon_sync.cat_map_offset', str(next_offset))
            _log.info('apply_category_mapping: %d/%d prodotti elaborati.', next_offset, total)

    def _disable_cat_map_cron(self):
        cron = self.env.ref('ke_amazon_sync.cron_apply_category_mapping', raise_if_not_found=False)
        if cron:
            try:
                cron.sudo().active = False
            except Exception:
                _log.warning('_disable_cat_map_cron: impossibile disabilitare da dentro il cron, '
                             'sarà idle al prossimo ciclo.')

    def _cron_sync_prices_quantities(self):
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron sync_prices_quantities saltato: credenziali SP-API non configurate.')
            return
        marketplaces = self.env['ke.amazon.marketplace'].search([('active', '=', True)])
        if not marketplaces:
            _log.warning('Cron sync_prices_quantities saltato: nessun marketplace attivo.')
            return
        log_rec = self.create({
            'name': f'Sync prezzi/quantità — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'price_qty',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        try:
            # Fase 3 multi-marketplace: un giro per ogni marketplace attivo, stesso
            # schema del thread manuale (vedi _thread_sync_price_qty).
            total_ok = total_errors = 0
            for marketplace in marketplaces:
                client = SpApiClient(self.env, marketplace_id=marketplace.marketplace_id)
                engine = SyncEngine(self.env, client)
                ok, errors = engine.sync_prices_and_quantities(log_rec)
                total_ok += ok
                total_errors += errors
            log_rec.write({
                'state': 'error' if total_errors and not total_ok else 'done',
                'end_date': fields.Datetime.now(),
            })
            _log.info('Sync prezzi/quantità: %d ok, %d errori', total_ok, total_errors)
        except Exception as e:
            _log.error('Cron sync_prices_quantities fallito: %s', e, exc_info=True)
            log_rec.write({'state': 'error', 'error_summary': str(e), 'end_date': fields.Datetime.now()})

    # ------------------------------------------------------------------
    # Import Ordini Amazon
    # ------------------------------------------------------------------

    def _cron_import_orders_store(self):
        """
        Avvia automaticamente un nuovo import ordini, se non ce n'è già uno in
        corso. Schedulato periodicamente per tenere sincronizzati gli ordini senza
        bisogno di avviarlo manualmente ogni volta (stesso pattern di
        action_start_reconcile per i prodotti).
        """
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.orders_import_state') == 'running':
            _log.info('Import ordini già in corso, salto avvio automatico.')
            return
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron import_orders_store saltato: credenziali SP-API non configurate.')
            return
        self.action_start_import_orders(name_prefix='Import Ordini Amazon (automatico)')

    def action_start_import_orders(self, name_prefix='Import Ordini Amazon'):
        """
        Avvia l'import degli ordini Amazon degli ultimi N giorni, su tutti i
        marketplace attivi (Magazzino → Amazon Sync → Marketplace) — un
        marketplace alla volta, in coda (vedi _thread_import_orders).
        """
        from datetime import timedelta, timezone
        from odoo.exceptions import UserError
        params = self.env['ir.config_parameter'].sudo()
        days_back = int(params.get_param('ke_amazon_sync.orders_days_back', '30'))
        created_after = (
            fields.Datetime.now().replace(tzinfo=timezone.utc) - timedelta(days=days_back)
        ).strftime('%Y-%m-%dT%H:%M:%SZ')

        marketplaces = self.env['ke.amazon.marketplace'].search([('active', '=', True)])
        if not marketplaces:
            raise UserError(
                'Nessun marketplace attivo. Usa "Rileva marketplace attivi" '
                '(Impostazioni → Amazon) per attivarli automaticamente, oppure '
                'attivali a mano da Magazzino → Amazon Sync → Marketplace.'
            )

        log_rec = self.create({
            'name': f'{name_prefix} — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'import_orders',
            'state': 'running',
            'start_date': fields.Datetime.now(),
            'log_lines': (
                f'Importazione ordini degli ultimi {days_back} giorni (da {created_after[:10]}) '
                f'su {len(marketplaces)} marketplace: {", ".join(marketplaces.mapped("code"))}.'
            ),
        })
        params.set_param('ke_amazon_sync.orders_import_state', 'running')
        params.set_param('ke_amazon_sync.orders_import_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.orders_import_created_after', created_after)
        params.set_param('ke_amazon_sync.orders_import_marketplace_queue',
                          ','.join(str(i) for i in marketplaces.ids))
        params.set_param('ke_amazon_sync.orders_import_current_marketplace_id', '')
        self.env.cr.commit()

        self._start_thread('_thread_import_orders', log_rec.id)

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.model
    def _thread_import_orders(self, db, uid, ctx, log_id):
        """
        Sostituisce il vecchio cron a tick (_cron_import_orders_chunk, rimosso).
        A differenza delle altre quattro sync a thread, qui non tutto passa da
        un report: getOrders (header/buyer/indirizzo) resta per-marketplace,
        invariato, non è mai stato il collo di bottiglia. Il vero collo di
        bottiglia era getOrderItems (2s/ordine, un ordine alla volta) — questo
        thread lo elimina scaricando UNA volta sola, prima del loop
        marketplace, il report bulk articoli-ordine (GET_FLAT_FILE_ALL_ORDERS
        _DATA_BY_ORDER_DATE_GENERAL, marketplaceIds = tutti gli attivi in
        un'unica richiesta — non uno a testa, vedi progetto-sync-ordini-report.md
        §3) e lo tiene in memoria (items_by_order_id) per tutta la durata del
        thread: mai persistito, non serve sopravvivere a un tick successivo
        perché non c'è più un tick.
        """
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        from datetime import timezone
        import time

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            SyncLog = env['ke.amazon.sync.log']
            log_rec = SyncLog.browse(log_id)
            params = env['ir.config_parameter'].sudo()
            try:
                created_after = params.get_param('ke_amazon_sync.orders_import_created_after', '')
                all_marketplaces = env['ke.amazon.marketplace'].search([('active', '=', True)])
                data_end_time = fields.Datetime.now().replace(tzinfo=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

                report_client = SpApiClient(env, marketplace_id=all_marketplaces[0].marketplace_id)
                report_id = report_client.request_orders_report(
                    created_after, data_end_time,
                    marketplace_ids=all_marketplaces.mapped('marketplace_id'),
                )
                msg = f'Report articoli-ordine richiesto (ID: {report_id}). In attesa...'
                SyncLog._append_log_line(log_rec, msg)

                document_id = None
                deadline = time.monotonic() + SyncLog._REPORT_POLL_MAX_MINUTES * 60
                while time.monotonic() < deadline:
                    time.sleep(SyncLog._REPORT_POLL_INTERVAL_S)
                    document_id = report_client.check_report_ready(report_id)
                    if document_id:
                        break
                if not document_id:
                    raise RuntimeError(
                        f'Report articoli-ordine {report_id} non pronto dopo '
                        f'{SyncLog._REPORT_POLL_MAX_MINUTES} minuti di attesa.'
                    )

                rows = report_client.download_report_rows(document_id)
                items_by_order_id = {}
                for row in rows:
                    order_id = row.get('amazon-order-id', '')
                    if order_id:
                        items_by_order_id.setdefault(order_id, []).append(row)
                msg = (f'Report scaricato: {len(rows)} righe articolo per '
                       f'{len(items_by_order_id)} ordini distinti.')
                SyncLog._append_log_line(log_rec, msg)

                while True:
                    if params.get_param('ke_amazon_sync.orders_import_state') != 'running':
                        _log.info('Thread import_orders: fermato dall\'esterno, esco senza completare.')
                        return

                    marketplace = SyncLog._orders_import_pop_marketplace(params)
                    if not marketplace:
                        break
                    env.cr.commit()

                    client = SpApiClient(env, marketplace_id=marketplace.marketplace_id)
                    engine = SyncEngine(env, client)

                    next_token = None
                    while True:
                        orders, next_token = engine.fetch_orders_page(created_after, next_token)
                        offset = 0
                        while offset is not None and orders:
                            offset = engine.import_orders_chunk(
                                log_rec, orders, offset, items_by_order_id=items_by_order_id,
                            )
                            env.cr.commit()
                        if not next_token:
                            break

                    msg = f'Import completato per {marketplace.name} ({marketplace.code}).'
                    SyncLog._append_log_line(log_rec, msg)
                    params.set_param('ke_amazon_sync.orders_import_current_marketplace_id', '')
                    env.cr.commit()

                SyncLog._finish_import_orders(log_rec)
            except Exception as exc:
                _log.error('Thread import_orders fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.orders_import_state', 'idle')
                env.cr.commit()

    def _orders_import_pop_marketplace(self, params):
        """
        Estrae il prossimo marketplace dalla coda (orders_import_marketplace_queue)
        e lo imposta come corrente. Ritorna il record ke.amazon.marketplace, o
        un recordset vuoto se la coda è esaurita (import completo su tutti i
        marketplace attivi).
        """
        queue_s = params.get_param('ke_amazon_sync.orders_import_marketplace_queue', '')
        queue_ids = [int(i) for i in queue_s.split(',') if i]
        if not queue_ids:
            return self.env['ke.amazon.marketplace']
        next_id, rest = queue_ids[0], queue_ids[1:]
        params.set_param('ke_amazon_sync.orders_import_marketplace_queue',
                          ','.join(str(i) for i in rest))
        params.set_param('ke_amazon_sync.orders_import_current_marketplace_id', str(next_id))
        return self.env['ke.amazon.marketplace'].browse(next_id)

    def _finish_import_orders(self, log_rec):
        """Chiude l'import ordini: coda marketplace esaurita."""
        log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_amazon_sync.orders_import_state', 'idle')
        self.env.cr.commit()

    # ------------------------------------------------------------------
    # Sync Giacenze FBA
    # ------------------------------------------------------------------

    def action_start_sync_fba_quantities(self, name_prefix='Sync Giacenze FBA'):
        """
        Avvia la sincronizzazione delle giacenze FBA da Amazon verso Odoo in un
        thread di background (stesso pattern di _thread_seed_fbm_stock ecc. in
        res_config_settings.py), non tramite cron a pagine: il nextToken
        restituito dall'Inventory API scade in pochi minuti, quindi se le
        pagine venissero richieste a distanza di tick di cron (ogni 2 min) il
        token risulterebbe già scaduto ("Next token is invalid or expired").
        Un thread continua a scorrere le pagine senza pause tra una chiamata e
        l'altra (a parte il rate-limit di ~0.5s), e non è soggetto al limite
        di tempo reale del cron perché non è un thread cron.
        """
        from odoo.exceptions import UserError

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.fba_sync_state') == 'running':
            log_id = int(params.get_param('ke_amazon_sync.fba_sync_log_id', '0') or 0)
            if log_id and self.browse(log_id).exists():
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': self._name,
                    'res_id': log_id,
                    'view_mode': 'form',
                    'target': 'current',
                }

        marketplaces = self.env['ke.amazon.marketplace'].search([('active', '=', True)])
        if not marketplaces:
            raise UserError(
                'Nessun marketplace attivo. Usa "Rileva marketplace attivi" '
                '(Impostazioni → Amazon) o attivane uno a mano.'
            )

        log_rec = self.create({
            'name': f'{name_prefix} — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'sync_fba',
            'state': 'running',
            'start_date': fields.Datetime.now(),
            'log_lines': f'Avvio sync giacenze FBA su {len(marketplaces)} marketplace: '
                         f'{", ".join(marketplaces.mapped("code"))} (inventario Pan-EU pooled, '
                         f'stesso magazzino Odoo per tutti).',
        })
        params.set_param('ke_amazon_sync.fba_sync_state', 'running')
        params.set_param('ke_amazon_sync.fba_sync_log_id', str(log_rec.id))
        self.env.cr.commit()

        self._start_fba_sync_thread(log_rec.id)

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _start_fba_sync_thread(self, log_id):
        import threading
        db = self.env.cr.dbname
        uid = self.env.uid
        ctx = dict(self.env.context)
        t = threading.Thread(
            target=self._thread_sync_fba_quantities,
            args=(db, uid, ctx, log_id),
            daemon=True,
        )
        t.start()

    # Se il nextToken risulta scaduto/non valido nonostante l'elaborazione
    # continua (es. una pagina particolarmente lenta), ripartiamo da capo
    # invece di fallire l'intera sync: l'aggiornamento quantità è idempotente,
    # quindi rielaborare pagine già fatte è solo tempo perso, non un errore.
    # Limitato a poche ripartenze per non girare all'infinito se la causa
    # fosse in realtà un altro problema permanente.
    _FBA_SYNC_MAX_TOKEN_RESTARTS = 5

    def _thread_sync_fba_quantities(self, db, uid, ctx, log_id):
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            log_rec = env['ke.amazon.sync.log'].browse(log_id)
            params = env['ir.config_parameter'].sudo()
            marketplaces = env['ke.amazon.marketplace'].search([('active', '=', True)])
            try:
                # Fase 3 multi-marketplace (inventario FBA Pan-EU pooled, confermato
                # dall'utente): un giro completo di pagine per ogni marketplace attivo,
                # tutti riconciliati sullo stesso, unico magazzino FBA — vedi il
                # commento in sync_fba_quantities_page. page_token/token_restarts
                # sono resettati ad ogni marketplace (ognuno ha la propria paginazione
                # SP-API indipendente).
                for marketplace in marketplaces:
                    client = SpApiClient(env, marketplace_id=marketplace.marketplace_id)
                    engine = SyncEngine(env, client)
                    page_token = None
                    token_restarts = 0
                    # Accumulatore {asin: qty totale} per l'intero marketplace —
                    # sync_fba_quantities_page lo muta in place e riconcilia solo
                    # sull'ultima pagina (vedi il suo commento per il motivo:
                    # aggregare per ASIN, non per SKU). Va azzerato insieme a
                    # page_token quando si riparte da capo per un token scaduto,
                    # altrimenti le pagine già viste prima del riavvio verrebbero
                    # sommate due volte.
                    asin_totals = {}
                    while True:
                        try:
                            new_page_token = engine.sync_fba_quantities_page(log_rec, page_token, asin_totals)
                        except Exception as exc:
                            msg = str(exc).lower()
                            is_token_error = 'token' in msg and ('expired' in msg or 'invalid' in msg)
                            if is_token_error and token_restarts < self._FBA_SYNC_MAX_TOKEN_RESTARTS:
                                token_restarts += 1
                                _log.warning(
                                    'sync_fba [%s]: nextToken scaduto/non valido, riparto dalla '
                                    'prima pagina (tentativo %d/%d): %s',
                                    marketplace.code, token_restarts,
                                    self._FBA_SYNC_MAX_TOKEN_RESTARTS, exc,
                                )
                                page_token = None
                                asin_totals = {}
                                continue
                            raise
                        if not new_page_token:
                            break
                        page_token = new_page_token
                log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
            except Exception as exc:
                _log.error('Thread sync_fba_quantities fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc),
                               'end_date': fields.Datetime.now()})
            finally:
                params.set_param('ke_amazon_sync.fba_sync_state', 'idle')
                params.set_param('ke_amazon_sync.fba_sync_log_id', '')
                cr.commit()

    def _cron_sync_fba_quantities_store(self):
        """
        Avvia automaticamente una nuova sync giacenze FBA, se non ce n'è già una
        in corso. Schedulata periodicamente (ogni N ore) — stesso pattern di
        action_start_reconcile/_cron_import_orders_store, ma qui la sync vera e
        propria gira in un thread di background (vedi action_start_sync_fba_quantities),
        non in ulteriori cron a pagine.
        """
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.fba_sync_state') == 'running':
            _log.info('Sync giacenze FBA già in corso, salto avvio automatico.')
            return
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron sync_fba_store saltato: credenziali SP-API non configurate.')
            return
        if not self.env['ke.amazon.marketplace'].search([('active', '=', True)]):
            _log.warning('Cron sync_fba_store saltato: nessun marketplace attivo.')
            return
        self.action_start_sync_fba_quantities(name_prefix='Sync Giacenze FBA (automatico)')

    # ------------------------------------------------------------------
    # Download immagini mancanti
    # ------------------------------------------------------------------

    def _cron_download_images_store(self):
        """
        Avvia automaticamente il download delle immagini mancanti, se non ce
        n'è già uno in corso. Schedulata periodicamente — stesso pattern di
        _cron_sync_fba_quantities_store, ma qui il lavoro vero e proprio gira
        nel thread già usato dal pulsante manuale "Scarica immagini mancanti"
        (Impostazioni → Amazon Seller Central), riusato tale e quale.
        Scarica solo per i prodotti che ancora non hanno un'immagine — non
        ricontrolla quelle già scaricate (vedi download_missing_images).
        """
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.download_images_state') == 'running':
            _log.info('Download immagini già in corso, salto avvio automatico.')
            return
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron download_images_store saltato: credenziali SP-API non configurate.')
            return

        log_rec = self.create({
            'name': f'Download immagini mancanti (automatico) — '
                    f'{fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'download_images',
            'state': 'running',
            'start_date': fields.Datetime.now(),
        })
        params.set_param('ke_amazon_sync.download_images_state', 'running')
        self.env.cr.commit()
        self.env['res.config.settings']._start_thread('_thread_download_images', log_rec.id)

    # ------------------------------------------------------------------
    # Import Resi Amazon
    # ------------------------------------------------------------------

    def action_start_returns_report(self):
        """Avvia l'import dei resi cliente FBA degli ultimi N giorni, uno per
        ogni marketplace attivo (stessa coda del full import/riconciliazione:
        il report resi è per-marketplace su SP-API)."""
        from datetime import timedelta, timezone
        from odoo.exceptions import UserError

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.returns_state') == 'running':
            log_id = int(params.get_param('ke_amazon_sync.returns_log_id', '0') or 0)
            if log_id and self.browse(log_id).exists():
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': self._name,
                    'res_id': log_id,
                    'view_mode': 'form',
                    'target': 'current',
                }

        marketplaces = self.env['ke.amazon.marketplace'].search([('active', '=', True)])
        if not marketplaces:
            raise UserError(
                'Nessun marketplace attivo. Usa "Rileva marketplace attivi" '
                '(Impostazioni → Amazon) o attivane uno a mano.'
            )

        days_back = int(params.get_param('ke_amazon_sync.returns_days_back', '30'))
        data_start_time = (
            fields.Datetime.now().replace(tzinfo=timezone.utc) - timedelta(days=days_back)
        ).strftime('%Y-%m-%dT%H:%M:%SZ')

        log_rec = self.create({
            'name': f'Import Resi Amazon — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'returns',
            'state': 'running',
            'start_date': fields.Datetime.now(),
            'log_lines': f'Richiesta report resi degli ultimi {days_back} giorni (da {data_start_time[:10]}) '
                         f'su {len(marketplaces)} marketplace: {", ".join(marketplaces.mapped("code"))}.',
        })
        params.set_param('ke_amazon_sync.returns_state', 'running')
        params.set_param('ke_amazon_sync.returns_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.returns_data_start_time', data_start_time)
        params.set_param('ke_amazon_sync.returns_marketplace_queue',
                          ','.join(str(i) for i in marketplaces.ids))
        params.set_param('ke_amazon_sync.returns_current_marketplace_id', '')
        self.env.cr.commit()

        self._start_thread('_thread_returns', log_rec.id)

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    @api.model
    def _thread_returns(self, db, uid, ctx, log_id):
        """
        Sostituisce il vecchio cron a tick (_cron_returns_chunk, rimosso):
        un unico thread continuo che scorre tutti i marketplace in coda,
        stesso schema di _thread_full_import/_thread_reconcile (crea report
        → poll stretto → scarica → elabora, senza attendere nulla tra un
        marketplace e l'altro). 'data_start_time' è calcolato una volta sola
        da action_start_returns_report (relativo a "adesso", non a un
        marketplace specifico) e riletto qui dal parametro.
        """
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        import time

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            SyncLog = env['ke.amazon.sync.log']
            log_rec = SyncLog.browse(log_id)
            params = env['ir.config_parameter'].sudo()
            try:
                data_start_time = params.get_param('ke_amazon_sync.returns_data_start_time', '')

                while True:
                    if params.get_param('ke_amazon_sync.returns_state') != 'running':
                        _log.info('Thread returns: fermato dall\'esterno, esco senza completare.')
                        return

                    marketplace = SyncLog._returns_pop_marketplace(params)
                    if not marketplace:
                        break
                    env.cr.commit()

                    client = SpApiClient(env, marketplace_id=marketplace.marketplace_id)
                    engine = SyncEngine(env, client)

                    report_id = client.request_returns_report(data_start_time)
                    msg = f'[{marketplace.code}] Report resi richiesto (ID: {report_id}). In attesa...'
                    SyncLog._append_log_line(log_rec, msg)

                    document_id = None
                    deadline = time.monotonic() + SyncLog._REPORT_POLL_MAX_MINUTES * 60
                    while time.monotonic() < deadline:
                        time.sleep(SyncLog._REPORT_POLL_INTERVAL_S)
                        document_id = client.check_report_ready(report_id)
                        if document_id:
                            break
                    if not document_id:
                        raise RuntimeError(
                            f'Report resi {report_id} ({marketplace.code}) non pronto dopo '
                            f'{SyncLog._REPORT_POLL_MAX_MINUTES} minuti di attesa.'
                        )

                    rows = client.download_report_rows(document_id)
                    msg = f'[{marketplace.code}] Report scaricato: {len(rows)} resi totali. Abbinamento...'
                    SyncLog._append_log_line(log_rec, msg)

                    matched, not_found, errors = engine.process_returns_report_rows(log_rec, rows)
                    _log.info('[%s] Import resi: %d abbinati, %d ordini non trovati, %d errori',
                              marketplace.code, matched, not_found, errors)

                    msg = f'Import resi completato per {marketplace.name} ({marketplace.code}).'
                    SyncLog._append_log_line(log_rec, msg)
                    params.set_param('ke_amazon_sync.returns_current_marketplace_id', '')
                    env.cr.commit()

                SyncLog._finish_returns(log_rec)
            except Exception as exc:
                _log.error('Thread returns fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.returns_state', 'idle')
                env.cr.commit()

    def _returns_pop_marketplace(self, params):
        """
        Estrae il prossimo marketplace dalla coda (returns_marketplace_queue)
        e lo imposta come corrente. Ritorna il record ke.amazon.marketplace,
        o un recordset vuoto se la coda è esaurita (import resi completo su
        tutti i marketplace attivi).
        """
        queue_s = params.get_param('ke_amazon_sync.returns_marketplace_queue', '')
        queue_ids = [int(i) for i in queue_s.split(',') if i]
        if not queue_ids:
            return self.env['ke.amazon.marketplace']
        next_id, rest = queue_ids[0], queue_ids[1:]
        params.set_param('ke_amazon_sync.returns_marketplace_queue',
                          ','.join(str(i) for i in rest))
        params.set_param('ke_amazon_sync.returns_current_marketplace_id', str(next_id))
        return self.env['ke.amazon.marketplace'].browse(next_id)

    def _finish_returns(self, log_rec):
        """Chiude l'import resi: coda marketplace esaurita."""
        log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_amazon_sync.returns_state', 'idle')
        params.set_param('ke_amazon_sync.returns_log_id', '')
        self.env.cr.commit()

    # ------------------------------------------------------------------
    # Import FNSKU
    # ------------------------------------------------------------------

    def action_start_fnsku_import(self, name_prefix='Import FNSKU'):
        """
        Avvia l'import FNSKU (report GET_FBA_MYI_ALL_INVENTORY_DATA), uno per
        ogni marketplace attivo — stessa coda del full import/riconciliazione/
        resi. L'FNSKU non è un dato fisso: Amazon ne genera uno nuovo ogni
        volta che cambia lo SKU venditore collegato — cosa che succede qui ad
        ogni riassegnazione SKU al rifornimento (es. 'PFS-1234' → 'PFS-1234+',
        vedi reconcile_from_report_rows). Per questo esiste anche
        _cron_fnsku_import_store, per tenerlo aggiornato da solo invece di
        doverlo rilanciare a mano dopo ogni riconciliazione.
        Come per la sync giacenze FBA (inventario Pan-EU pooled, confermato
        dall'utente), l'FNSKU è legato alla stessa etichetta fisica pooled:
        nessun gating primario/secondario, ogni marketplace attivo la
        riconferma (vedi process_fnsku_report_rows). Stesso rischio noto per
        UK/US non pooled, vedi TASK.md.
        """
        from odoo.exceptions import UserError

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.fnsku_import_state') == 'running':
            log_id = int(params.get_param('ke_amazon_sync.fnsku_import_log_id', '0') or 0)
            if log_id and self.browse(log_id).exists():
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': self._name,
                    'res_id': log_id,
                    'view_mode': 'form',
                    'target': 'current',
                }

        marketplaces = self.env['ke.amazon.marketplace'].search([('active', '=', True)])
        if not marketplaces:
            raise UserError(
                'Nessun marketplace attivo. Usa "Rileva marketplace attivi" '
                '(Impostazioni → Amazon) o attivane uno a mano.'
            )

        log_rec = self.create({
            'name': f'{name_prefix} — {fields.Datetime.now().strftime("%d/%m/%Y %H:%M")}',
            'sync_type': 'import_fnsku',
            'state': 'running',
            'start_date': fields.Datetime.now(),
            'log_lines': f'Richiesta report inventario FBA (GET_FBA_MYI_ALL_INVENTORY_DATA) '
                         f'su {len(marketplaces)} marketplace: {", ".join(marketplaces.mapped("code"))}.',
        })
        params.set_param('ke_amazon_sync.fnsku_import_state', 'running')
        params.set_param('ke_amazon_sync.fnsku_import_log_id', str(log_rec.id))
        params.set_param('ke_amazon_sync.fnsku_import_marketplace_queue',
                          ','.join(str(i) for i in marketplaces.ids))
        params.set_param('ke_amazon_sync.fnsku_import_current_marketplace_id', '')
        self.env.cr.commit()

        self._start_thread('_thread_fnsku_import', log_rec.id)

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': log_rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _cron_fnsku_import_store(self):
        """
        Avvia automaticamente un nuovo import FNSKU, se non ce n'è già uno in
        corso. Schedulata periodicamente — stesso pattern di
        _cron_sync_fba_quantities_store: qui c'è in più il motivo per cui serve
        essere periodica e non solo manuale, vedi action_start_fnsku_import.
        """
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param('ke_amazon_sync.fnsku_import_state') == 'running':
            _log.info('Import FNSKU già in corso, salto avvio automatico.')
            return
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError:
            _log.warning('Cron fnsku_import_store saltato: credenziali SP-API non configurate.')
            return
        if not self.env['ke.amazon.marketplace'].search([('active', '=', True)]):
            _log.warning('Cron fnsku_import_store saltato: nessun marketplace attivo.')
            return
        self.action_start_fnsku_import(name_prefix='Import FNSKU (automatico)')

    @api.model
    def _thread_fnsku_import(self, db, uid, ctx, log_id):
        """
        Sostituisce il vecchio cron a tick (_cron_fnsku_import_chunk, rimosso):
        un unico thread continuo che scorre tutti i marketplace in coda,
        stesso schema di _thread_full_import/_thread_reconcile/_thread_returns.
        """
        from odoo import api as odoo_api
        from odoo.modules.registry import Registry as odoo_registry
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        import time

        with odoo_registry(db).cursor() as cr:
            env = odoo_api.Environment(cr, uid, ctx)
            SyncLog = env['ke.amazon.sync.log']
            log_rec = SyncLog.browse(log_id)
            params = env['ir.config_parameter'].sudo()
            try:
                while True:
                    if params.get_param('ke_amazon_sync.fnsku_import_state') != 'running':
                        _log.info('Thread fnsku_import: fermato dall\'esterno, esco senza completare.')
                        return

                    marketplace = SyncLog._fnsku_import_pop_marketplace(params)
                    if not marketplace:
                        break
                    env.cr.commit()

                    client = SpApiClient(env, marketplace_id=marketplace.marketplace_id)
                    engine = SyncEngine(env, client)

                    report_id = client.request_fnsku_report()
                    msg = f'[{marketplace.code}] Report FNSKU richiesto (ID: {report_id}). In attesa...'
                    SyncLog._append_log_line(log_rec, msg)

                    document_id = None
                    deadline = time.monotonic() + SyncLog._REPORT_POLL_MAX_MINUTES * 60
                    while time.monotonic() < deadline:
                        time.sleep(SyncLog._REPORT_POLL_INTERVAL_S)
                        document_id = client.check_report_ready(report_id)
                        if document_id:
                            break
                    if not document_id:
                        raise RuntimeError(
                            f'Report FNSKU {report_id} ({marketplace.code}) non pronto dopo '
                            f'{SyncLog._REPORT_POLL_MAX_MINUTES} minuti di attesa.'
                        )

                    rows = client.download_report_rows(document_id)
                    msg = f'[{marketplace.code}] Report scaricato: {len(rows)} righe totali. Abbinamento per SKU...'
                    SyncLog._append_log_line(log_rec, msg)

                    matched, not_found, errors = engine.process_fnsku_report_rows(log_rec, rows)
                    _log.info('[%s] Import FNSKU: %d abbinati, %d SKU non trovati, %d errori',
                              marketplace.code, matched, not_found, errors)

                    msg = f'Import FNSKU completato per {marketplace.name} ({marketplace.code}).'
                    SyncLog._append_log_line(log_rec, msg)
                    params.set_param('ke_amazon_sync.fnsku_import_current_marketplace_id', '')
                    env.cr.commit()

                SyncLog._finish_fnsku_import(log_rec)
            except Exception as exc:
                _log.error('Thread fnsku_import fallito: %s', exc, exc_info=True)
                log_rec.write({'state': 'error', 'error_summary': str(exc), 'end_date': fields.Datetime.now()})
                params.set_param('ke_amazon_sync.fnsku_import_state', 'idle')
                env.cr.commit()

    def _fnsku_import_pop_marketplace(self, params):
        """
        Estrae il prossimo marketplace dalla coda (fnsku_import_marketplace_queue)
        e lo imposta come corrente. Ritorna il record ke.amazon.marketplace, o
        un recordset vuoto se la coda è esaurita (import FNSKU completo su
        tutti i marketplace attivi).
        """
        queue_s = params.get_param('ke_amazon_sync.fnsku_import_marketplace_queue', '')
        queue_ids = [int(i) for i in queue_s.split(',') if i]
        if not queue_ids:
            return self.env['ke.amazon.marketplace']
        next_id, rest = queue_ids[0], queue_ids[1:]
        params.set_param('ke_amazon_sync.fnsku_import_marketplace_queue',
                          ','.join(str(i) for i in rest))
        params.set_param('ke_amazon_sync.fnsku_import_current_marketplace_id', str(next_id))
        return self.env['ke.amazon.marketplace'].browse(next_id)

    def _finish_fnsku_import(self, log_rec):
        """Chiude l'import FNSKU: coda marketplace esaurita."""
        log_rec.write({'state': 'done', 'end_date': fields.Datetime.now()})
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('ke_amazon_sync.fnsku_import_state', 'idle')
        params.set_param('ke_amazon_sync.fnsku_import_log_id', '')
        self.env.cr.commit()
