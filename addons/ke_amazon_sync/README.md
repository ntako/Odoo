# ke_amazon_sync

Addon Odoo 19.0 per la sincronizzazione bidirezionale tra il database prodotti e Amazon Seller Central (SP-API).

## Funzionalità

### 1. Import Completo (manuale)
Scarica l'intero catalogo dallo store Amazon e crea o aggiorna i prodotti in Odoo.

- Recupera **tutti** i listing attivi tramite **Reports API** (nessun limite di prodotti)
- Gestisce cataloghi di qualsiasi dimensione (testato con ~6.000 prodotti)
- Legge le quantità FBA tramite Inventory API
- Crea `product.template` in Odoo con tutti i campi Amazon valorizzati
- Se il prodotto esiste già (per SKU, ASIN o barcode EAN), lo aggiorna opzionalmente
- Avviabile da **Magazzino → Amazon Sync → Import Completo**

> **Nota tecnica:** la Listings Items API ha un limite fisso di 1.000 prodotti per sessione di paginazione. Per superarlo, l'import usa la **Reports API** (`GET_MERCHANT_LISTINGS_ALL_DATA`), che non ha questo limite.

#### Come funziona l'import per cataloghi grandi (> 1.000 prodotti)

L'import gira in un thread di background continuo (`_thread_full_import`), non più su un cron a tick: crea il report, aspetta che sia pronto con un poll stretto (ogni 20s, non più il tick da 2 minuti), lo scarica ed elabora tutte le righe in batch da 2000, senza attese artificiali tra un batch e l'altro. Un marketplace alla volta, in coda; un watchdog orario (`cron_sync_log_watchdog`) chiude come errore un import rimasto bloccato per un riavvio del server (un thread non sopravvive a un riavvio, a differenza di un vecchio cron a stati).

Per cataloghi con limite ≤ 1.000 prodotti (campo *Limite prodotti* > 0 e ≤ 1.000) viene usata la paginazione diretta della Listings API (avvio più rapido).

### 2. Aggiornamento da Catalog API (manuale)
Arricchisce i prodotti già importati con dati non disponibili nel report TSV.

- **Tipo prodotto Amazon** (`x_amazon_product_type`): necessario per pubblicare e aggiornare listing
- **Categoria Amazon** e **Browse Node ID**: per la categorizzazione corretta su Amazon
- **Dimensioni** (lunghezza, larghezza, altezza in cm): lette dal Catalog API v2022
- Elaborazione a chunk via cron, avviabile da **Magazzino → Amazon Sync → Aggiorna da Catalog API**

### 3. Controllo Nuovi Prodotti (automatico o manuale)
Schedulata ogni N ore (configurabile). Controlla se i prodotti aggiunti manualmente in Odoo sono già presenti su Amazon da parte del produttore/brand.

- Filtra i prodotti senza ASIN ma con barcode EAN, creati nelle ultime N ore
- Cerca su Amazon Catalog per EAN
- Se trovato: collega l'ASIN e imposta il flag *Presente su Amazon (produttore)*
- Se non trovato: imposta lo stato *Non listato*
- Attivabile manualmente da **Impostazioni → Amazon Seller Central → Esegui ora**

### 4. Sincronizzazione Prezzi e Quantità (automatica o manuale)
Schedulata ogni N minuti (configurabile, disabilitata di default). Aggiorna su Amazon i prodotti per cui la sync è abilitata.

- **Prezzo**: aggiorna via Listings PATCH API per tutti i prodotti con *Sync prezzo* attivo
- **Quantità FBM**: usa la giacenza del **magazzino FBM** configurato (se non configurato, usa il totale globale)
- I prodotti **FBA** saltano automaticamente la sync quantità (gestita da Amazon)
- Log dettagliato di ogni operazione
- Attivabile manualmente da **Impostazioni → Amazon Seller Central → Esegui ora**

### 5. Sync Giacenze FBA (manuale o automatica)
Legge le giacenze reali da Amazon (Inventory API) e aggiorna lo stock Odoo nel magazzino FBA configurato.

- Chiama `FBA Inventory API` e ottiene la mappa `{sku: fulfillableQuantity}`
- Per ogni SKU trovato in Odoo aggiorna il `stock.quant` nella location del magazzino FBA
- Aggiorna anche il campo informativo `x_amazon_qty` sul prodotto
- Avviabile da **Magazzino → Amazon Sync → Sync Giacenze FBA**
- Il cron può essere schedulato ogni N ore dalle impostazioni
- **Marketplace Pan-EU pooled** (IT/DE/ES/FR): tutti condividono lo stesso magazzino FBA Odoo, stesso stock fisico. Un marketplace con **magazzino FBA dedicato** impostato (`ke.amazon.marketplace.fba_warehouse_id` — pensato per UK/US, inventario non pooled) usa invece quel magazzino e traccia la quantità su `ke.amazon.listing.qty` invece che su `x_amazon_qty` (che resta il valore del pool EU). `ke.amazon.listing` è scoped per **variante** (`product.product`), non per template: ogni variante di una famiglia ha la propria riga SKU/ASIN/FNSKU/prezzo/quantità per marketplace (refactor 2026-10-23, vedi TASK.md) — per un prodotto semplice non cambia nulla in pratica.

### 6. Pubblicazione su Amazon (dal form prodotto)
Crea o aggiorna un listing su Amazon direttamente dal form del prodotto Odoo.

**Scenario A – ASIN già noto** (prodotto già presente su Amazon da un altro venditore):
- Usa `requirements='LISTING_OFFER_ONLY'`
- Invia condizione, prezzo e disponibilità
- Applicabile a prodotti di rivendita già catalogati su Amazon

**Scenario B – Nuovo prodotto** (nessun ASIN):
- Usa `requirements='LISTING'`
- Invia nome, brand, condizione, prezzo, immagine principale, descrizione, EAN e disponibilità
- Dopo la pubblicazione, usa **Verifica ASIN** per recuperare l'ASIN assegnato da Amazon

Pulsanti disponibili nel tab Amazon del form prodotto:
- **Cerca tipo prodotto...** — wizard per cercare il `productType` Amazon corretto
- **Cerca categoria...** — wizard per cercare browse node e categoria Amazon
- **Pubblica su Amazon** — avvia la pubblicazione (Scenario A o B)
- **Verifica ASIN** — recupera l'ASIN assegnato dopo Scenario B

### 7. Riconciliazione Store (automatica o manuale)
Confronta i listing attivi su Amazon con i prodotti in Odoo, senza modificare nulla su Amazon.

- Usa Reports API per scaricare il listing completo (nessun limite di prodotti)
- Aggiorna `x_amazon_status` e `x_amazon_price` per i prodotti già collegati
- Gestisce SKU riassegnati (es. `PFS-1234` → `PFS-1234+`) aggiornando `x_amazon_sku` — lo SKU sostituito resta visibile in `x_amazon_sku_previous` (tab Amazon del prodotto), solo indicazione informativa
- Segnala nel log i prodotti presenti su Amazon ma non ancora importati in Odoo
- Schedulabile ogni N ore

### 8. Import Ordini Amazon (manuale)
Importa gli ordini Amazon in `sale.order` di Odoo.

- Scarica gli ordini degli ultimi N giorni (configurabile) tramite Orders API
- Crea o aggiorna `sale.order` con tutti i campi Amazon
- **Assegna automaticamente il magazzino** corretto: FBA (AFN) o FBM (MFN)
- Crea le righe ordine (`sale.order.line`) abbinando i prodotti per SKU poi ASIN
- I prodotti non trovati vengono registrati come nota testuale con tutti i dettagli
- **Prezzi corretti**: usa `ItemTax` restituito da Amazon per calcolare il netto esatto (nessuna IVA doppia)
- Crea automaticamente il partner se non esiste (deduplicazione per email poi nome+città)
- Righe ordine (SKU/quantità/prezzo) lette da un report bulk (`GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL`, un'unica richiesta per tutti i marketplace attivi) invece di una chiamata `getOrderItems` per ordine — elimina il pavimento di 2s/ordine imposto da quell'endpoint. Header ordine/buyer/indirizzo restano da Orders API (`getOrders`, per-marketplace, mai stato il collo di bottiglia)
- Gira su thread di background continuo, avviabile da **Magazzino → Amazon Sync → Import Ordini Amazon**
- Gli ordini importati sono visibili in **Magazzino → Amazon Sync → Ordini Amazon**

### 9. Stampa etichette FNSKU

Due flussi distinti, entrambi basati sullo stesso report (`ir.actions.report` `action_report_amazon_fba_label`, un'etichetta per `ke.amazon.fba.label.log`):

- **Legata a una spedizione** (`ke.amazon.fba.prep.wizard`, bottone "Etichetta FBA (FNSKU)" sul trasferimento): scansione EAN riga per riga, conta quanto è stato etichettato contro la quantità prevista del movimento, chiude la spedizione solo a etichettatura completa.
- **Autonoma per variante** (`ke.amazon.fba.standalone.label.wizard`, **Magazzino → Amazon Sync → Stampa etichetta FNSKU**): nessuna spedizione/trasferimento coinvolto, nessuna giacenza toccata. Identifica il prodotto da EAN scansionato o da selezione diretta, mostra SKU/FNSKU per controllo visivo, poi stampa N copie a scelta. Pensata per ristampe, scorte di sicurezza o etichettatura preventiva fuori da un ciclo di spedizione FBA.

In entrambi i casi l'FNSKU stampato è quello della variante se presente, altrimenti quello del template solo se il template ha un'unica variante (`product.product._get_amazon_fnsku()`) — mai l'FNSKU del template su una famiglia con più varianti, per non etichettare un'unità fisica con un codice che non la identifica univocamente. Se manca, va prima eseguito l'Import FNSKU.

Il PDF (50x25mm, un'etichetta per pagina) è autosufficiente: barcode come data URI generata in Python (nessuna richiesta di rete), nessun asset esterno. **Se il testo dell'etichetta esce con caratteri corrotti** (es. "®" → "Â®") nonostante questo, è un bug noto di wkhtmltopdf quando una richiesta di rete interna fallisce durante la resa (qui: la generazione di header/footer di Odoo, sempre presente anche a contenuto vuoto) — controllare/impostare il parametro di sistema `report.url` (Impostazioni → Tecnico → Parametri di Sistema) a un indirizzo del server raggiungibile da se stesso (es. `http://localhost:8069`), vedi TASK.md.

**Agente di stampa Windows (ZPL)**: lato Odoo esiste già la coda (`ke.amazon.print.station`/`ke.amazon.print.job`, **Magazzino → Amazon Sync → Postazioni agente di stampa** / **Lavori di stampa ZPL**) e l'API HTTP per un agente esterno (`/ke_amazon_sync/print_agent/poll` e `/ack`, autenticata con una chiave API dedicata alla postazione, non una password Odoo) — vedi `progetto-odoo-amazon-fba-etichettatura.md` §6/§8. L'agente Windows vero (il programma che gira sul PC del magazzino e parla con la stampante) non è ancora stato scritto.

Un interruttore in **Impostazioni → Amazon Seller Central → Etichette FNSKU** ("Usa stampa di rete (ZPL)", **disattivo di default**) decide quale canale usano entrambi i wizard di stampa sopra: disattivo (default) → PDF dal browser come sempre; attivo + una postazione scelta → accoda un lavoro ZPL su quella postazione invece del PDF. Se attivo senza una postazione valida, i wizard segnalano l'errore invece di stampare in silenzio il PDF.

### 10. Spedizioni inbound FBA (`ke.amazon.inbound.plan`)

Rifornimento verso Amazon tramite la Fulfillment Inbound API v2024-03-20 — **Magazzino → Amazon Sync → Piani inbound FBA**, o dal bottone "Piano inbound FBA" su un trasferimento "In transito verso Amazon" già validato e già etichettato FNSKU (quel trasferimento resta l'unica fonte di SKU/quantità del piano, il piano non ne definisce di nuovi). Workflow a stati che segue 1:1 la sequenza ufficiale Amazon:

```
draft → plan_created → packing_confirmed → placement_options_ready
     → placement_confirmed (IRREVERSIBILE) → delivery_window_options_ready
     → delivery_window_confirmed → transport_options_ready → transport_confirmed
     → labels_ready → shipped
```

- **Colli** (`ke.amazon.inbound.plan.box`/`.box.line`, scheda "Colli" del piano): si scansionano gli articoli nel collo (wizard EAN-scan, stesso idioma degli altri wizard di stampa FNSKU), si chiude impostando dimensioni/peso. Tutta la quantità del trasferimento deve essere imballata prima di proseguire.
- **`placement_confirmed`** è l'unico stato raggiunto chiamando `confirmPlacementOption` — dichiarato **irreversibile** dalla documentazione ufficiale Amazon ("This option cannot be reversed after it is selected."). Il bottone corrispondente ha una conferma esplicita nella UI; nessun altro percorso nel codice chiama quel metodo.
- Solo **corrieri partner Amazon** in questa prima versione (preventivo/costo mostrato prima di confermare) — il percorso self-ship (corriere proprio) non è ancora implementato.
- **Annullamento** (`cancelInboundPlan`) disponibile solo prima di `transport_confirmed`: dopo quel punto la documentazione garantisce una finestra a costo zero limitata (24h pacco singolo / 1h LTL-FTL), quindi il bottone si disabilita.
- **Limite noto**: la shape esatta della risposta `getLabels` per questa versione dell'API non è stata verificata contro una chiamata reale (la pagina di riferimento trovata documenta solo la v0) — il payload viene salvato grezzo (`ke.amazon.inbound.plan.shipment.labels_raw_response`) finché non si verifica il formato reale, prima di collegarlo a stampa/download automatico.
- Logica di orchestrazione in `services/fba_inbound_service.py` (separato da `sync_engine.py`: dominio di creazione/push multi-step con un punto di non ritorno, non di import/reconcile); wrapper SP-API dedicati in `services/sp_api_client.py` (`DELAY_INBOUND_S`, tutti con retry fail-fast).

---

## Installazione

### Prerequisiti

```bash
pip install python-amazon-sp-api requests
```

### Deploy
Copia la cartella `ke_amazon_sync` nella directory addons di Odoo, poi installa il modulo dall'interfaccia (**Impostazioni → Applicazioni**).

Il modulo dipende da: `stock`, `product`, `base_setup`, `sale`.

---

## Configurazione

Vai in **Impostazioni → Amazon Seller Central** e compila:

### Credenziali SP-API

| Campo | Dove trovarlo |
|---|---|
| Seller ID (Merchant Token) | Seller Central → Impostazioni → Informazioni account |
| Marketplace ID | `APJ6JRA9NG5V4` per Italia (default) |
| LWA App ID | Seller Central → Apps & Services → Develop Apps |
| LWA Client Secret | Seller Central → Apps & Services → Develop Apps |
| Refresh Token | Ottenuto con il tool `Amazon/SyncFromAmazon/get_token.py` |

### Impostazioni import

| Campo | Descrizione |
|---|---|
| Lingua catalogo | Lingua per descrizioni e bullet points (default: `it_IT`) |
| Tipo prodotto Odoo | Tipo assegnato ai nuovi prodotti creati (Consumabile / Stoccabile) |
| Limite prodotti per import | Numero massimo di prodotti da scaricare. **0 = nessun limite**. Utile per test (es. 50). |
| Aggiorna prodotti esistenti | Se attivo, aggiorna anche i prodotti già presenti in Odoo durante l'import completo. |

### Import Ordini

| Campo | Descrizione |
|---|---|
| Giorni da importare | Numero di giorni nel passato da cui recuperare gli ordini ad ogni import manuale (default: 30). |
| Aliquota IVA predefinita (%) | Aliquota usata per convertire i prezzi lordi Amazon in prezzi netti per il listino prodotti (IT: 22, DE: 19, FR: 20, ES: 21). Per le righe ordine viene usato il dato esatto restituito da Amazon (`ItemTax`). |

### Magazzini FBA / FBM

> **Setup necessario prima di usare queste funzionalità:** creare i due magazzini in **Inventario → Configurazione → Magazzini**, poi assegnarli qui.

| Campo | Descrizione |
|---|---|
| Magazzino FBA | Magazzino Odoo per lo stock Amazon FBA **pooled Pan-EU** (IT/DE/ES/FR). Le giacenze vengono aggiornate dalla "Sync Giacenze FBA". Gli ordini AFN vengono assegnati a questo magazzino, a meno che il marketplace dell'ordine non abbia un magazzino FBA dedicato (vedi sotto). |
| Magazzino FBA dedicato (per marketplace) | Campo su **Magazzino → Amazon Sync → Marketplace**, non in Impostazioni: opzionale, da impostare solo per un marketplace con inventario FBA realmente separato dal pool EU (es. UK/US). Se impostato, quel marketplace usa questo magazzino invece del Magazzino FBA condiviso, sia per la sync giacenze sia per gli ordini AFN sia per i wizard di rifornimento/rientro (selezionabile lì). |
| Magazzino FBM (Locale) | Magazzino Odoo locale per gli ordini FBM. Le quantità di questo magazzino vengono spinte su Amazon durante la sync prezzi/qty. Gli ordini MFN vengono assegnati a questo magazzino. |

### Sincronizzazioni automatiche

Ogni riga ha un toggle per abilitare/disabilitare la schedulazione, il campo intervallo e un pulsante **Esegui ora** per il trigger manuale immediato.

| Operazione | Default |
|---|---|
| Controllo nuovi prodotti | Ogni 24 ore — **disabilitato** |
| Sync prezzi/quantità | Ogni 60 minuti — **disabilitato** |
| Riconciliazione store | Ogni 24 ore — **disabilitato** |

> **Nota:** i pulsanti *Esegui ora* avviano l'operazione in background e reindirizzano al log. Salva le impostazioni prima di usarli se hai modificato le credenziali.

### Marketplace ID di riferimento

| Paese | ID |
|---|---|
| Italia | `APJ6JRA9NG5V4` |
| Germania | `A1PA6795UKMFR9` |
| Spagna | `A1RKKUPIHCS9HS` |
| Francia | `A13V1IB3VIYZZH` |
| UK | `A1F83G8C2ARO7P` |
| USA | `ATVPDKIKX0DER` |

---

## Struttura

```
ke_amazon_sync/
├── models/
│   ├── product_template.py      # Campi Amazon su product.template, azioni pubblica/cerca
│   ├── sale_order.py            # Campi Amazon su sale.order e sale.order.line
│   ├── amazon_sync_log.py       # Modello log + orchestrazione cron (state machine)
│   └── res_config_settings.py   # Credenziali, toggle cron, trigger manuali, magazzini
├── services/
│   ├── sp_api_client.py         # Wrapper SP-API: Listings, Catalog, Inventory, Reports, Orders API
│   └── sync_engine.py           # Logica sync: import, check, price/qty, FBA, ordini
├── wizard/
│   └── amazon_product_type_wizard.py  # Wizard ricerca tipo prodotto e browse node
├── data/
│   └── ir_cron.xml              # Azioni schedulate (7 cron, tutti inattivi di default)
├── security/
│   └── ir.model.access.csv
└── views/
    ├── product_template_views.xml          # Tab Amazon nel form prodotto
    ├── amazon_sync_log_views.xml           # Vista log
    ├── res_config_settings_views.xml       # Sezione impostazioni
    ├── amazon_product_type_wizard_views.xml # Vista wizard tipo/categoria
    └── menu.xml
```

### Metodi chiave in `sp_api_client.py`

| Metodo | API usata | Note |
|--------|-----------|------|
| `iter_seller_skus()` | Listings Items | Paginazione classica, max 1.000 prodotti |
| `request_listings_report()` | Reports | Crea il report asincrono |
| `check_report_ready(report_id)` | Reports | Ritorna `documentId` se DONE, `None` se in corso |
| `download_report_rows(document_id)` | Reports | Scarica e parsa il TSV (gestisce BOM UTF-8) |
| `get_catalog_item(asin)` | Catalog v2022 | Brand, EAN, immagini, dimensioni, varianti |
| `get_inventory_map()` | FBA Inventory | Mappa `{sku: fulfillableQuantity}` per prodotti FBA |
| `patch_price(sku, product_type, price)` | Listings PATCH | Aggiorna prezzo su Amazon |
| `patch_quantity(sku, product_type, qty)` | Listings PATCH | Aggiorna qty FBM su Amazon |
| `put_listing(sku, product_type, attributes)` | Listings PUT | Crea/sostituisce un listing completo |
| `get_listing_item(sku)` | Listings GET | Legge il listing esistente (recupero ASIN) |
| `search_product_types(keywords)` | Product Type Definitions | Ricerca tipi prodotto Amazon |
| `search_browse_nodes(keyword)` | Catalog Items | Ricerca browse node/categoria Amazon |
| `get_orders(created_after, ...)` | Orders v0 | Pagina di ordini con filtro stato e data |
| `get_order_items(order_id)` | Orders v0 | Righe di un singolo ordine |

### Metodi chiave in `sync_engine.py`

| Metodo | Descrizione |
|--------|-------------|
| `full_import()` | Import completo con catalog API (senza limite) |
| `full_import_chunk()` | Processa una pagina di listing (paginazione classica) |
| `full_import_report_rows()` | Processa un chunk di righe TSV del report |
| `apply_category_mapping_chunk()` | Arricchisce prodotti con dati Catalog API (tipo, categoria, dimensioni) |
| `check_new_products()` | Cerca prodotti Odoo su Amazon per EAN |
| `sync_prices_and_quantities()` | PATCH prezzi e qty FBM su Amazon |
| `sync_fba_quantities()` | Legge giacenze FBA da Amazon, aggiorna stock.quant nel magazzino FBA |
| `reconcile_from_report_rows()` | Riconcilia listing Amazon con prodotti Odoo |
| `fetch_orders_page()` | Scarica una pagina di ordini Amazon (fino a 100) |
| `import_orders_chunk()` | Crea/aggiorna sale.order per un blocco di _ORDERS_CHUNK_SIZE ordini della pagina |
| `_net_price(gross)` | Converte prezzo lordo Amazon in netto Odoo usando l'aliquota IVA configurata |
| `_get_warehouse_location(param_key)` | Recupera la stock.location interna del magazzino configurato |
| `_set_product_stock(variant, location, qty)` | Imposta giacenza assoluta via stock.quant |

---

## Campi aggiunti a `product.template`

Lunghezza/larghezza/altezza e il volume NON sono più campi Amazon-specifici:
sono forniti dal modulo condiviso `ke_product_dimensions` (dipendenza di
questo modulo) come `product_length`/`product_width`/`product_height`, così
da poter essere riusati da altre integrazioni (es. ecommerce) senza doversi
accoppiare a `ke_amazon_sync`. Questo modulo si limita a scriverli durante
l'import/sync dal Catalog Amazon.

| Campo | Descrizione |
|---|---|
| `x_amazon_asin` | ASIN collegato |
| `x_amazon_sku` | SKU interno Amazon |
| `x_amazon_status` | Stato: Attivo / Inattivo / Non listato / Trovato (produttore) / Errore |
| `x_amazon_fulfillment` | FBA (`AFN`) o FBM (`MFN`) |
| `x_amazon_product_type` | Tipo prodotto Amazon (es. `LUGGAGE`, `SHOES`) — necessario per sync e pubblicazione |
| `x_amazon_category` | Categoria Amazon (displayName del browse node) |
| `x_amazon_browse_node_id` | ID browse node Amazon |
| `x_amazon_brand` | Brand da inviare ad Amazon in fase di pubblicazione |
| `x_amazon_condition` | Condizione (Nuovo, Usato — Come nuovo, ecc.) |
| `x_amazon_publish_state` | Stato pubblicazione: Non pubblicato / In elaborazione / Pubblicato / Errore |
| `x_amazon_publish_error` | Testo dell'errore di pubblicazione (se presente) |
| `x_amazon_found_by_manufacturer` | Prodotto già presente su Amazon da altro venditore |
| `x_amazon_sync_price` | Abilita sync prezzo automatica |
| `x_amazon_sync_qty` | Abilita sync quantità automatica (solo FBM) |
| `x_amazon_price` | Prezzo letto da Amazon (sola lettura) |
| `x_amazon_qty` | Quantità FBA letta da Amazon (sola lettura) |
| `x_amazon_last_sync` | Data/ora ultima sincronizzazione |
| `x_amazon_sync_error` | Ultimo errore di sincronizzazione |

## Campi aggiunti a `sale.order`

| Campo | Descrizione |
|---|---|
| `x_amazon_order_id` | ID ordine Amazon (es. `302-1234567-8901234`) |
| `x_amazon_order_status` | Stato: In attesa / Da spedire / Parzialmente spedito / Spedito / Annullato |
| `x_amazon_fulfillment_channel` | AFN (FBA) o MFN (FBM) |
| `x_amazon_purchase_date` | Data acquisto originale su Amazon |
| `x_amazon_sales_channel` | Canale di vendita Amazon (es. `Amazon.it`) |

## Campi aggiunti a `sale.order.line`

| Campo | Descrizione |
|---|---|
| `x_amazon_sku` | SKU Amazon della riga |
| `x_amazon_asin` | ASIN Amazon della riga |
| `x_amazon_order_item_id` | ID riga ordine Amazon |

---

## Gestione IVA sui prezzi Amazon

I prezzi restituiti dall'API Amazon per i mercati europei sono **IVA inclusa**.

- **Righe ordine**: il modulo sottrae il campo `ItemTax` (restituito da Amazon per ogni riga) per ottenere il prezzo netto esatto. Odoo applica poi le imposte configurate sul prodotto, ottenendo il totale corretto senza doppia tassazione.
- **Listino prodotti** (`list_price`): durante l'import, il prezzo lordo Amazon viene diviso per `(1 + aliquota/100)` usando l'**aliquota IVA predefinita** configurata in impostazioni (default 22% per l'Italia). Il valore risultante è il netto da inserire in Odoo.

---

## Log e monitoraggio

Tutti gli eventi sono tracciati in **Magazzino → Amazon Sync → Log Sincronizzazioni** con:
- Tipo operazione, stato, date inizio/fine e durata
- Contatori: totale, creati, aggiornati, saltati, errori
- Log riga per riga di ogni prodotto/ordine elaborato

---

## Relazione con gli altri moduli

```
Amazon/SyncFromAmazon     →  export CSV manuale (flusso legacy, ancora utilizzabile)
ke_amazon_sync            →  sync diretta SP-API ↔ Odoo (questo modulo)
Odoo-Import-Products      →  import CSV generico da fornitore (invariato)
```
