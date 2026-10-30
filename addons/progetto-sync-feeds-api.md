# Piano: sync prezzo/quantità via Feeds API (bulk) invece di PATCH per-SKU

> Bozza per discussione, nessun codice scritto. Nasce dalla domanda: "la sync di
> tutti i marketplace ci mette tanto tempo, ci sono alternative?" — vedi anche
> TASK.md per lo stato generale del modulo.

## 1. Il problema, in numeri

Tre meccanismi diversi convivono nel modulo, con caratteristiche di tempo molto diverse:

| Sync | Meccanismo attuale | Scala con | Tempo indicativo (~6000 prodotti, 4 marketplace) |
|---|---|---|---|
| Giacenze FBA (lettura) | `getInventorySummaries`, paginato | N. pagine (non N. prodotti) | ~2 minuti totali — **non è il collo di bottiglia** |
| Full import / riconciliazione | Reports API (report unico per marketplace) | Tempo di elaborazione report lato Amazon (1-10 min) × marketplace, **in sequenza** | Da 30-40 minuti a qualche ora, dipende da Amazon |
| **Prezzo/quantità FBM (scrittura)** | `patchListingsItem`, **una PATCH per SKU** (0,3s/chiamata, 2 chiamate se prezzo+quantità attivi) | **N. prodotti × N. marketplace**, linearmente | **Ore**, nel caso di molti prodotti con sync attiva su più marketplace |

Il terzo è quello che rende impraticabile una cadenza oraria (e stressa anche quella giornaliera). È l'unico dei tre a scalare linearmente con `prodotti × marketplace` invece che con `pagine` o `report`.

## 2. Proposta: Feeds API per prezzo/quantità in bulk

Amazon offre un meccanismo alternativo alle PATCH singole: invece di migliaia di
chiamate, si carica **un solo documento** con le modifiche di tutti gli SKU di
un marketplace, e Amazon lo elabora in async (stesso schema "crea → poll →
scarica risultato" già usato per i report in questo modulo).

Il tipo di feed corretto è **`JSON_LISTINGS_FEED`**: accetta una lista di
"messaggi" PATCH, uno per SKU, con la **stessa identica struttura body** già
usata oggi da `patch_price`/`patch_quantity` (`productType` + `patches` con
`op: replace`) — quindi il codice che *costruisce* il payload per singolo
prodotto resta praticamente identico, cambia solo *dove* finisce (dentro un
array nel feed, invece che nel body di una chiamata singola).

### Meccanica SP-API (libreria già installata, nessuna nuova dipendenza)

La libreria `python-amazon-sp-api` già in uso ha un modulo Feeds completo
(`sp_api.api.feeds`), con un helper `submit_feed(feed_type, file, content_type)`
che accorpa "crea documento" + "carica contenuto" + "crea feed" in una chiamata:

```
submit_feed('JSON_LISTINGS_FEED', <contenuto JSON>, 'application/json')
  → crea feedDocumentId, carica il contenuto, crea il feed → ritorna feedId

get_feed(feedId)                          # poll, come check_report_ready oggi
  → processingStatus: IN_QUEUE/IN_PROGRESS/DONE/CANCELLED/FATAL

get_feed_result_document(resultFeedDocumentId)   # scarica l'esito
  → JSON con successo/errore per OGNI SKU del feed (fondamentale: un feed può
    avere alcuni SKU falliti e altri riusciti, va scomposto per prodotto)
```

Rate limit `create_feed`: 0,0083 req/s sostenuto, burst 15 — per 4 marketplace
(1 feed a testa per ciclo) siamo ben dentro il burst, nessun problema anche a
cadenza oraria.

### State machine proposta (stesso pattern già consolidato nel modulo)

Mirror esatto di `_cron_reconcile_chunk`/`_cron_returns_chunk`/
`_cron_fnsku_import_chunk`, con la stessa coda multi-marketplace già in uso
da fase 3:

```
Fase 1 — nessun feed_id: costruisci il JSON (un messaggio per prodotto con
         sync_price/sync_qty attivi su QUESTO marketplace) e submit_feed()
Fase 2 — feed_id presente, nessun risultato: poll get_feed(), se DONE scarica
         get_feed_result_document()
Fase 3 — risultato scaricato: applica gli esiti per-SKU (stesso schema di
         log/errori di sync_prices_and_quantities oggi: OK/ERRORE per riga,
         x_amazon_sync_error popolato sui falliti) → passa al marketplace
         successivo in coda, o chiude il log a coda esaurita
```

### Cosa NON cambia

- **Il pulsante "Sync prezzo/quantità" sul singolo prodotto** (scheda
  prodotto → tab Amazon) resta sul path PATCH attuale: per un solo SKU un
  feed (che comunque richiede il giro crea→poll→scarica, minuti) è più lento
  e più complesso di una PATCH diretta. Il bulk via feed ha senso solo per il
  cron/thread che sincronizza *tutto* il catalogo attivo.
- Full import, riconciliazione, giacenze FBA, resi, FNSKU: **non toccati**,
  già su meccanismi adeguati alla loro scala (report o paginazione).
- La logica di gating primario/secondario (`_is_primary_marketplace`,
  `_sync_prices_and_quantities_secondary`) resta concettualmente identica,
  va solo riadattata al fatto che ora si costruisce un batch invece di
  iterare chiamata per chiamata.

## 3. Cosa cambia nel codice (stima, non ancora scritto)

- `sp_api_client.py`: 3 nuovi metodi (`submit_price_qty_feed`,
  `check_feed_ready`, `download_feed_result`), stesso stile dei metodi
  `request_*_report`/`check_report_ready`/`download_report_rows` già
  esistenti — pattern riconoscibile, nessuna sorpresa architetturale.
- `amazon_sync_log.py`: nuova coda `_cron_price_qty_*` a 3 fasi (mirror di
  `_cron_reconcile_chunk`), sostituisce l'attuale ciclo sincrono in un colpo
  solo di `_thread_sync_price_qty`/`_cron_sync_prices_quantities`. Diventa
  un cron a chunk invece che un thread — coerente con reconcile/resi/FNSKU,
  ma è un cambio di forma (da thread continuo a cron a tick) da verificare
  bene sui tempi di attesa reali dei feed.
- `sync_engine.py`: `sync_prices_and_quantities`/
  `_sync_prices_and_quantities_secondary` riscritte per costruire il JSON
  del batch invece di ciclare chiamata per chiamata; nuova funzione per
  applicare il risultato del feed ai prodotti/listing.
- `action_mark_done`: nuovo branch per il cleanup dei parametri di coda
  (stesso schema già ripetuto per le altre 5 sync).

Effort paragonabile a una delle conversioni multi-marketplace già fatte
(es. riconciliazione), non un progetto enorme — la struttura da copiare
esiste già in più punti del modulo.

## 4. Domande aperte da chiarire insieme prima di partire

1. **Batch size**: quanti SKU in un solo feed? Non risulta un limite basso
   documentato per `JSON_LISTINGS_FEED` (diversamente dai vecchi feed flat-file),
   ma va verificato con un test reale su un batch grande prima di fidarsi in
   produzione — possibile serva comunque uno spezzettamento se il catalogo
   attivo supera qualche migliaio di SKU per marketplace.
2. **Cadenza realistica una volta passati ai feed**: il giro crea→poll→scarica
   di un feed richiede comunque minuti (non è istantaneo), quindi anche con
   i feed una cadenza *oraria* è probabilmente il limite pratico, non una
   sync in tempo reale — da allineare le aspettative.
3. **Cosa fare degli errori per-SKU**: il risultato del feed va scomposto riga
   per riga (un feed può avere 950 successi e 50 errori) — la UX di log resta
   quella già esistente (per prodotto), ma va scritta la logica di parsing
   del documento risultato, che ha un formato specifico da Amazon.
4. **Va convertito anche l'aggiornamento della giacenza FBM in occasione di
   un movimento di magazzino puntuale**, o resta comunque sync periodica?
   (Oggi non esiste un trigger "push immediato alla singola modifica di
   stock" — se serve, è fuori scope di questo piano.)

## 5. Alternative considerate e perché non sono la priorità

- **Parallelizzare i marketplace** (invece che processarli in sequenza) per
  full import/riconciliazione: aiuterebbe, ma quei due meccanismi sono già
  su Reports API (non il collo di bottiglia principale) — l'impatto reale
  sarebbe minore che sistemare prezzo/quantità. Da considerare in un secondo
  momento, non esclude i feed (sono cambi indipendenti).
- **Sync solo sui prodotti effettivamente cambiati** (delta invece che tutto
  il catalogo attivo ad ogni giro): riduce il numero di richieste a
  prescindere dal meccanismo (PATCH o feed), ma richiede tracciare "cosa è
  cambiato dall'ultima sync" — un altro pezzo di lavoro a sé, complementare
  ai feed, non alternativo.
