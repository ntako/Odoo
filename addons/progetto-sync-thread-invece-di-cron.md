# Piano: sync a report (full import/riconciliazione/resi/FNSKU) su thread invece che su cron a tick

> Bozza per discussione, nessun codice scritto. Nasce dalla domanda: "il full
> import ha poche possibilità di velocizzarlo?" seguita da "qualcosa diverso
> dal cron su cui fare affidamento?" — complementare a
> `progetto-sync-feeds-api.md` (che riguarda invece prezzo/quantità), tocca un
> meccanismo diverso (thread vs feed) quindi tenuto separato.

## 1. Il problema: il cron a tick aggiunge attesa dove non serve

Full import (Fase 1-3), riconciliazione, resi e import FNSKU condividono la
stessa state machine a 3 fasi, eseguita da un `ir.cron` che gira **ogni 2
minuti** (`interval_number=2, interval_type=minutes` in `data/ir_cron.xml`):

```
Fase 1 — crea il report ad Amazon
Fase 2 — poll: il report è pronto? (altrimenti aspetta il prossimo tick)
Fase 3 — scarica ed elabora le righe A CHUNK (200 righe per tick)
```

Due punti in cui il tick da 2 minuti aggiunge attesa **non giustificata da un
rate limit reale**:

- **Poll dello stato report**: si controlla una volta ogni 2 minuti anche se
  Amazon a volte risponde "pronto" molto prima — nessun limite ci impedisce
  di controllare più spesso (il rate limit di `get_report`/`get_reports` è
  generoso, non è il collo di bottiglia).
- **Elaborazione righe (Fase 3)**: verificato nel codice — `full_import_report_rows`
  e le funzioni analoghe per resi/riconciliazione/FNSKU **non chiamano mai
  Amazon**, sono solo scritture locali su Odoo. Eppure processano un chunk
  fisso (200 righe) e poi **aspettano comunque il prossimo tick di cron**
  prima di continuare. Su un catalogo di ~6000 prodotti: 30 chunk × 2 minuti
  = **un'ora**, quasi tutta di puro attendere il tick successivo, non di
  lavoro reale.

## 2. Il precedente: esiste già in questo modulo

La sync giacenze FBA (`sync_fba_quantities_page`) **era** anch'essa a cron-a-tick,
ed è stata riscritta a **thread di background continuo**
(`_start_fba_sync_thread`/`_thread_sync_fba_quantities`) per un problema
analogo nello spirito (anche se diverso nella causa): i token di paginazione
SP-API scadevano più in fretta del tick del cron. Lo stesso pattern è già
usato anche per il download immagini mancanti (`_thread_download_images`) e
per la sync prezzo/quantità (`_thread_sync_price_qty`).

Non è quindi un meccanismo nuovo da introdurre nel modulo: è lo **stesso
pattern già consolidato**, applicato a quattro sync che oggi non lo usano
ancora.

## 3. Proposta

Convertire le quattro sync a report (full import, riconciliazione, resi,
import FNSKU) dal cron a stati a un thread continuo, stesso schema di
`_thread_sync_fba_quantities`:

```
per ogni marketplace attivo (stessa coda già in uso):
    crea report
    loop: poll ogni 15-30s (non 2 minuti) finché non è pronto
    scarica il report
    elabora TUTTE le righe (o a batch generosi, es. 1000-2000, solo per
    tenere le singole transazioni Odoo a una dimensione ragionevole — non
    per rispettare un rate limit, che qui non esiste)
    commit periodico ogni N righe (non solo a fine batch): se il thread
    venisse interrotto a metà, non si perde tutto il lavoro fatto fin lì
```

Tempo stimato dopo la conversione: quasi solo il tempo che **Amazon** impiega
a generare il report (1-10 minuti, fuori dal nostro controllo) più pochi
secondi/minuti di elaborazione locale — contro l'ora attuale dominata
dall'attesa artificiale dei tick.

### Un helper condiviso, non 4 implementazioni copiate

Le quattro sync hanno la stessa identica forma (crea report → poll → scarica
→ elabora righe → prossimo marketplace in coda). Ha senso scrivere **una
funzione di supporto generica** (qualcosa come
`_run_report_sync_thread(sync_type, request_fn, process_fn, ...)`) invece di
duplicare la stessa logica di poll/commit/gestione-coda quattro volte — più
facile da testare una volta sola, meno superficie per bug futuri.
Da progettare con attenzione: le quattro sync differiscono nei dettagli
(full import ha anche la Fase 0 a pagine per cataloghi piccoli; l'import
FNSKU si incatena automaticamente dopo il full import; ognuna scrive un
tipo di riga diverso) — l'helper deve lasciare abbastanza spazio a queste
differenze senza diventare esso stesso troppo complicato da seguire.

## 4. Il compromesso da non nascondere: niente ripresa dopo un riavvio

Un cron a stati **sopravvive** a un riavvio del server Odoo: i parametri di
stato (`import_state`, `import_report_id`, coda marketplace, ecc.) sono
salvati su `ir.config_parameter`, quindi al tick successivo riprende da dove
si era fermato. Un thread **no**: se il processo Odoo si riavvia a metà
(deploy, crash, riavvio manuale come quello fatto oggi per applicare il fix
di `check_report_ready`), il lavoro fatto fino a quel momento si perde e va
rilanciato manualmente da capo.

Un dettaglio che peggiora le cose rispetto al cron, non solo "diverso":
`_start_thread` crea thread **daemon** — quando il processo Odoo termina
(riavvio, crash), un thread daemon viene interrotto di colpo, **senza**
eseguire il proprio blocco `finally` (quello che oggi aggiorna
`log_rec.state`/i config parameter a 'idle' o 'error'). Il log resta
silenziosamente `running` per sempre, senza che nessuno se ne accorga — non
un'ipotesi: è esattamente quello che abbiamo trovato con un log FNSKU fermo
in `running` da una settimana, scoperto solo controllando a mano.

Mitigazioni, la prima delle quali **non è opzionale** (fa parte del piano,
non un "magari"):

- **Watchdog di igiene** (cron leggero, es. ogni ora): controlla
  `ke.amazon.sync.log` con `state='running'` da più tempo del ragionevole per
  quel tipo di sync, e lo segnala — minimo: reso visibile in lista (badge/
  decorazione); se si vuole andare oltre, marcato `error` automaticamente con
  un messaggio esplicito ("nessun aggiornamento da N ore, probabile thread
  interrotto"). Vale anche per i thread GIÀ esistenti (FBA, download immagini)
  e per i cron, non solo per le quattro sync di questo piano — il gap è
  generale, va chiuso una volta per tutte indipendentemente da cosa lo causa.
- Con un import che dura minuti invece di ore, il rischio di "riavvio proprio
  a metà" è comunque più basso in valore assoluto rispetto a oggi.
- Il commit periodico (vedi sopra) limita il danno pratico: si riparte da
  quello che manca, non da zero, essendo la logica di matching/creazione già
  idempotente (full import/riconciliazione trovano i prodotti già esistenti
  per SKU/ASIN e li aggiornano, non li duplicano).

## 5. Cosa NON cambia

- La logica di business (matching prodotti, gating campi legacy/`ke.amazon.listing`,
  contatori) resta identica — cambia solo *come* viene invocata (loop di
  thread invece di tick di cron), non *cosa* fa.
- Fase 0 del full import (catalogo piccolo, a pagine) resta separata, non
  è un problema di attesa artificiale (già scorre senza aspettare cron tra
  una pagina e l'altra, essendo dentro `full_import_chunk` chiamata in loop
  quando serve).
- Il pulsante manuale "Import Completo"/"Riconciliazione"/ecc. resta
  identico all'utente: click → parte in background → log aggiornato in
  tempo reale. Cambia solo la rapidità con cui il log si aggiorna.

## 6. Effort e ordine consigliato

1. **Watchdog per i log "running" orfani** — piccolo, indipendente, utile da
   subito anche senza il resto del piano (chiude un gap che esiste già oggi).
2. **Timeout HTTP esplicito sui client SP-API** — verificato: `httpx` (libreria
   sottostante `python-amazon-sp-api`) non ha **nessun timeout configurato**
   da nessuna parte nel modulo. Una connessione che si blocca (non un errore,
   una che non risponde né chiude) farebbe restare appesa la chiamata
   indefinitamente — rischio che esiste già oggi sotto cron, ma diventa più
   serio con un thread continuo (nessun limite di tempo reale imposto da
   Odoo a fargli da rete di sicurezza). Fix piccolo e a sé (passare
   `timeout=` alla costruzione dei client `Reports`/`ListingsItems`/ecc. in
   `sp_api_client.py`), da fare comunque prima di questa conversione.
3. Full import (Fase 1-3) — è quello di cui abbiamo già i numeri reali
   (6000+ prodotti, 4 marketplace) per misurare il prima/dopo.
4. Se l'helper condiviso regge bene su full import, applicarlo a
   riconciliazione/resi/import FNSKU — dovrebbe essere quasi solo
   collegare le rispettive funzioni di richiesta/elaborazione riga
   all'helper, non riscrivere la logica di business.
4. Testare col solito schema già consolidato in questo modulo: `odoo shell`
   con client finti scritti a mano (mai `mock.patch` sulla classe reale),
   verificando sia il caso "tutto fila liscio" sia un riavvio simulato a
   metà thread (per controllare cosa succede davvero al log rimasto
   "running" — con il watchdog del punto 1 già in piedi, verificare che lo
   intercetti davvero).

## 7. Domande aperte da chiarire insieme prima di partire

1. ~~Poll interval: 15-30s è ragionevole?~~ — **verificato**: `get_report`
   (quello usato per il poll di stato) permette 2 richieste/secondo, burst
   15 — un poll ogni 15-30s ne usa 30-60 volte meno, nessun rischio di
   auto-throttling. Verificato anche che nessuna chiamata della pipeline
   report ha retry/backoff interno che potrebbe bloccare il thread per
   minuti (non usano `_with_retry`) — il rischio reale trovato è un altro,
   vedi timeout HTTP mancante al punto 2 di §6.
2. **Dimensione del batch di commit** durante l'elaborazione righe: quanto è
   "generoso ma sicuro"? Da misurare, non da indovinare — stesso principio
   già seguito per `_REPORT_CHUNK_SIZE` (era una scelta prudente mai
   ri-misurata, da qui il problema).
3. ~~Il watchdog per i log rimasti "running" è dentro lo scope?~~ — **deciso**:
   sì, fa parte del piano (vedi §4), e visto che il gap è generale (vale
   anche per i thread/cron già esistenti oggi) conviene farlo **per primo**,
   prima ancora della conversione a thread — utile comunque da subito, non
   solo in previsione di questo lavoro.
4. **Ordine rispetto al piano feed** (`progetto-sync-feeds-api.md`): sono
   cambi indipendenti e non si escludono a vicenda — ma quale viene prima?
   Questo piano è più semplice/rischioso in modo diverso (nessuna nuova
   integrazione Amazon, solo un cambio di "chi orchestra" il lavoro già
   esistente) — probabilmente il più veloce da mettere a terra dei due.
