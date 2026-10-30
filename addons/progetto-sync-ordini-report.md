# Piano: import ordini via report bulk invece di `getOrderItems` per ordine

> Nasce dalla domanda "l'import ordini si può velocizzare?" seguita da "non
> possiamo avere un report come per l'import prodotti?". Verificato con una
> chiamata reale (non solo ipotizzato) prima di scrivere questo piano — vedi
> §2. Complementare a `progetto-sync-thread-invece-di-cron.md` (stesso
> meccanismo thread-invece-di-cron, esteso qui a un quinto flusso) ma tenuto
> come documento a sé perché il problema di fondo è diverso: lì il collo di
> bottiglia era un'attesa artificiale del tick di cron; qui è un **rate limit
> Amazon reale**.

## 1. Il problema: `getOrderItems` è un limite reale, non artificiale

`_cron_import_orders_chunk` oggi, per ogni ordine nuovo o aggiornato, chiama
`client.get_order_items(amazon_id)` — un ordine alla volta. Il rate limit
ufficiale di quell'endpoint è 0.5 req/s sostenute (burst 30): **~2 secondi per
ordine**, un pavimento imposto da Amazon, non dal nostro codice. Sopra a
questo, l'architettura attuale (state machine a cron ogni 2 minuti,
`_ORDERS_CHUNK_SIZE = 15` ordini per tick) aggiunge un'attesa **artificiale**
identica a quella già risolta per le altre quattro sync: un chunk da 15 ordini
costa ~30s di lavoro reale, poi aspetta comunque ~90s il tick successivo.

Risultato: 150 ordini nuovi = ~20 minuti reali, di cui solo ~5 minuti sono
lavoro vero (il resto è attesa del tick + il pavimento dei 2s/ordine).

## 2. La verifica: il report esiste, ma non è un sostituto 1:1

Come per i prodotti, la Reports API ha un tipo report per gli ordini:
`GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL` (confermato presente
nella libreria `python-amazon-sp-api` installata). **Verificato con una
chiamata reale** (account reale, marketplace IT, range ultimi 3 giorni,
2026-09-24): richiesta → pronto dopo ~90s → 455 righe scaricate.

Colonne reali ottenute (una riga = **un articolo ordine**, non un ordine):

```
amazon-order-id, order-item-id, purchase-date, last-updated-date,
order-status, item-status, sku, asin, product-name, quantity,
item-price, item-tax, item-promotion-discount, currency,
shipping-price, shipping-tax, ship-promotion-discount,
gift-wrap-price, gift-wrap-tax, fulfillment-channel, sales-channel,
order-channel, ship-service-level, ship-city, ship-state,
ship-postal-code, ship-country, is-business-order, is-iba,
buyer-company-name, buyer-tax-registration-country,
buyer-tax-registration-type, price-designation, order-invoice-type,
promotion-ids, purchase-order-number, merchant-order-id
```

**Due scoperte concrete, non ipotizzate:**

1. **Copre esattamente ciò che oggi viene da `getOrderItems`**: SKU, ASIN,
   quantità, prezzo, stato — tutto quello che `import_orders_chunk`/
   `_create_amazon_order_line` usano oggi. Sostituirlo con questo report
   elimina il pavimento dei 2s/ordine.

2. **NON copre ciò che oggi viene da `getOrders`**: nessuna colonna
   `buyer-name`/`buyer-email`/`buyer-phone`, nessun indirizzo completo (solo
   città/stato/CAP/paese, non via). Questi dati servono a
   `_find_or_create_amazon_partner` per creare/abbinare il cliente Odoo con
   un indirizzo di spedizione utilizzabile — **non recuperabili da questo
   report**, servono ancora `getOrders`/`BuyerInfo`/`ShippingAddress`.

   Nota positiva: `getOrders` (l'header ordine) **non è il collo di
   bottiglia** — rate limit 1 richiesta/60s ma **una chiamata copre fino a
   100 ordini** (paginazione), non un ordine alla volta. Per i volumi di
   questo account (poche pagine al mese) è già efficiente così com'è.

**Conclusione: non è un sostituto completo di tutto l'import ordini, ma un
sostituto perfetto della SOLA parte che oggi costa 2s/ordine.** Disegno
proposto: ibrido, non tutto-report.

## 3. Sui marketplace: la domanda originale, con risposta verificata

**`getOrders` (header/buyer/indirizzo): resta per-marketplace**, come oggi —
è già scoped per `marketplace_id` nella chiamata, è già in coda
(`orders_import_marketplace_queue`), non è il collo di bottiglia, non c'è
motivo di cambiarlo.

**Il report articoli ordine: NON va diviso per marketplace — una sola
richiesta combinata.** Scoperta concreta durante la verifica: la richiesta
report è stata fatta scoped al marketplace **IT**, ma tra le 455 righe
restituite compariva un ordine con `sales-channel: 'Amazon.fr'` (marketplace
FR) — l'endpoint EU (`sellingpartnerapi-eu.amazon.com`, che copre IT/DE/ES/FR/UK
su questo account) restituisce ordini cross-marketplace indipendentemente dal
singolo `marketplaceIds` passato. La raccomandazione ufficiale Amazon per
questo tipo di report è comunque passare **tutti** i marketplaceIds attivi in
un'unica richiesta (non fare affidamento sul comportamento implicito appena
osservato, che potrebbe essere specifico di questo account "Pan-EU"): un solo
report con `marketplaceIds=[id1, id2, ...]` copre tutti i marketplace EU
attivi in un colpo solo, invece di N chiamate ridondanti/sovrapposte.

La colonna `sales-channel` (es. `Amazon.fr`, `Amazon.it`) permette comunque,
se mai servisse, di risalire al marketplace di origine di una riga — ma per
questo disegno non serve nemmeno: l'abbinamento articoli↔ordine è per
`amazon-order-id`, e il marketplace dell'ordine è già noto dalla chiamata
`getOrders` che lo ha trovato (che resta per-marketplace, vedi sopra).

**Nota per il futuro** (non applicabile oggi): se mai venisse attivato un
marketplace non-EU (US, seedato ma non attivo — vedi `data/amazon_marketplaces.xml`),
servirebbe una **seconda** richiesta report separata per quella regione
(`sellingpartnerapi-na.amazon.com` è un endpoint diverso) — un report per
regione, non per singolo marketplace.

## 4. Disegno proposto

```
_thread_import_orders (nuovo, sostituisce _cron_import_orders_chunk):
    1. UNA volta sola, prima del loop marketplace:
       - richiede il report articoli (marketplaceIds = tutti gli attivi,
         dataStartTime/dataEndTime = stessa finestra "ultimi N giorni")
       - poll stretto (stesso schema _REPORT_POLL_INTERVAL_S delle altre 4 sync)
       - scarica, raggruppa le righe per amazon-order-id in un dict
         {amazon_order_id: [righe articolo, ...]} — SOLO in memoria per
         questo giro di thread, non persistito
    2. per ogni marketplace attivo (stessa coda già in uso, invariata):
       loop: get_orders(pagina) — invariato, stesso rate limit, già efficiente
             per ogni ordine nella pagina:
                 crea/aggiorna sale.order da BuyerInfo/ShippingAddress (invariato)
                 righe ordine: dal dict in memoria (items_by_order_id.get(id, [])),
                 NIENTE PIÙ get_order_items() per ordine
    3. chiude il log, stesso schema delle altre 4 sync
```

Risultato atteso: il pavimento dei 2s/ordine sparisce del tutto (non più
"ridotto", **eliminato** — zero chiamate `getOrderItems`). Il tempo residuo è
dominato da: 1 report (crea+poll+scarica, qualche decina di secondi, una
volta sola) + N pagine `getOrders` (già veloce) + scritture Odoo pure
(create/write, nessuna chiamata di rete, quindi nessun collo di bottiglia).

## 5. Cosa cambia, file per file (stessa struttura delle 4 conversioni già fatte)

- `sp_api_client.py`: `request_orders_report(data_start_time, data_end_time,
  marketplace_ids=None)` — **già aggiunto e verificato con una chiamata
  reale** (vedi §2). Accetta una lista esplicita di marketplace invece del
  singolo `self.marketplace_id` degli altri `request_*_report`, per poter
  passare tutti gli attivi in un colpo solo (vedi §3).
- `sync_engine.py`:
  - `import_orders_chunk`/`_create_amazon_sale_order`/
    `_fetch_and_create_order_lines` modificati per accettare
    `items_by_order_id` (dict pre-costruito) invece di chiamare
    `client.get_order_items()` — la logica di creazione riga
    (`_create_amazon_order_line`) resta la stessa, cambia solo la fonte dei
    dati riga (dict locale invece di una chiamata Amazon).
  - Nuovo parsing: raggruppare le righe report per `amazon-order-id`
    (equivalente report-based di `_orders_import_pop_marketplace`, ma qui non
    serve una coda — un dict costruito una volta).
- `amazon_sync_log.py`: `_cron_import_orders_chunk` → `_thread_import_orders`
  (stesso schema `_thread_returns`/`_thread_fnsku_import`), `_start_thread`
  invece di attivazione cron in `action_start_import_orders`. Rimossi
  `_disable_orders_import_cron`, la logica "pagina in cache su ir.attachment"
  (non serve più: senza il costo di 2s/ordine, un'intera finestra di ordini
  si elabora in un solo giro di thread, non serve più sopravvivere a un tick
  successivo).
- `data/ir_cron.xml`: `cron_import_orders_chunk` rimosso (`<delete>`), resta
  invariato `cron_import_orders_store` (avvio periodico, leggero).
- `action_mark_done`: ramo `import_orders` semplificato come le altre 4
  (libera solo lo stato, non "ferma" un thread già avviato).

## 6. Cosa NON cambia

- `getOrders`/`fetch_orders_page`: invariato, stesso rate limit, stessa
  paginazione, resta per-marketplace in coda.
- `_find_or_create_amazon_partner`: invariato, usa ancora `BuyerInfo`/
  `ShippingAddress` da `getOrders` — il report bulk non tocca questa parte.
- `_confirm_and_validate_fba_order`, gating FBA/FBM, warehouse: invariati.
- Il salvataggio "pagina ordini in ir.attachment tra fasi" per `getOrders`
  stesso non serve toccarlo se resta dentro un singolo giro di thread (non
  più a cavallo di tick di cron separati) — probabilmente eliminabile anche
  lì, da verificare in fase di implementazione se semplifica il codice senza
  rischi.

## 7. Rischi/cose da verificare durante l'implementazione (non ancora fatto)

- ~~Il report items ha un limite di range date?~~ **Verificato**: range di 30
  giorni (`orders_days_back` di produzione) accettato senza errori — chiamata
  reale su IT/ES/FR, report pronto in 42s, 4357 righe / 4029 ordini distinti.
  Conferma anche l'entità del guadagno: solo `getOrderItems` per questo
  volume, al vecchio pavimento di 2s/ordine, avrebbe richiesto ~134 minuti.
- Ordini con `order-status` cambiato ma **nessuna riga articolo** nel report
  (es. ordine cancellato prima che avesse articoli) — gestire il caso
  `items_by_order_id.get(id, [])` vuoto senza errori, stesso principio già
  presente nel codice attuale per ordini senza righe.
- Un ordine può avere lo stesso SKU su più righe report (spedizioni parziali)
  — verificare che raggruppare per `amazon-order-id` mantenga tutte le righe
  (una lista, non un dict per SKU) — già previsto nel disegno sopra.

## 8. Piano di implementazione — COMPLETATO

1. [x] Verifica reale del formato report (fatto, vedi §2).
2. [x] `sp_api_client.request_orders_report` esteso con `marketplace_ids`
   espliciti (default `[self.marketplace_id]` se non passato, come gli altri
   `request_*_report`).
3. [x] `sync_engine.py`: `import_orders_chunk`/`_create_amazon_sale_order`/
   `_fetch_and_create_order_lines` accettano `items_by_order_id` — se
   l'ordine è nel dict, le righe vengono lette da lì (nuovo helper statico
   `_order_report_row_to_item`, converte le colonne piatte del report nel
   formato annidato già atteso da `_create_amazon_order_line`, che resta
   invariato); altrimenti ricade su `get_order_items` (self-healing per
   ordini troppo recenti per essere nel report).
4. [x] `amazon_sync_log.py`: nuovo `_thread_import_orders` (report bulk una
   volta sola, poi loop marketplace con `getOrders` invariato + lettura dal
   dict in memoria), `action_start_import_orders` avvia il thread invece del
   cron, `action_mark_done`/ramo `import_orders` semplificato. Rimossi
   `_cron_import_orders_chunk`/`_orders_import_finish_marketplace`/
   `_disable_orders_import_cron` e tutta la cache a `ir.attachment` (non
   serve più: senza il pavimento di 2s/ordine, un'intera finestra si elabora
   in un solo giro di thread).
5. [x] `data/ir_cron.xml`: `cron_import_orders_chunk` rimosso (`<delete>`),
   invariato `cron_import_orders_store` (avvio periodico, leggero).
6. [x] **Test end-to-end passato** (`odoo shell` su DB reale "odoo", classe
   finta — `FakeSpApiClient` **senza `get_order_items`**, di proposito: se il
   codice lo avesse ancora chiamato, il test sarebbe fallito rumorosamente
   con `AttributeError` invece di passare per sbaglio): report richiesto con
   `marketplace_ids` = entrambi IT+DE in una sola chiamata (asserzione
   esplicita), due ordini creati (uno per marketplace) con la riga prodotto
   corretta (SKU/quantità) letta dal report, zero chiamate `get_order_items`,
   parametri di coda ripuliti, `orders_import_state='idle'`.
7. [x] `TASK.md`/`README.md` aggiornati.
