# ke_amazon_sync

Addon Odoo 17.0 per la sincronizzazione bidirezionale tra il database prodotti e Amazon Seller Central (SP-API).

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

L'import usa una state machine a 3 fasi, orchestrata dal cron `cron_full_import_chunk`:

| Fase | Cosa succede |
|------|-------------|
| **1 – Creazione report** | Amazon genera il report (in background). Il cron salva il `reportId` e termina. |
| **2 – Attesa report** | Il cron verifica ogni invocazione se il report è pronto (di solito 30s–5min). |
| **3 – Elaborazione** | Il report TSV viene scaricato, salvato come allegato Odoo e processato in chunk da 200 righe per ogni invocazione del cron. |

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
- Gestisce SKU riassegnati (es. `PFS-1234` → `PFS-1234+`) aggiornando `x_amazon_sku`
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
- Elaborazione a chunk via cron, avviabile da **Magazzino → Amazon Sync → Import Ordini Amazon**
- Gli ordini importati sono visibili in **Magazzino → Amazon Sync → Ordini Amazon**

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
| Magazzino FBA | Magazzino Odoo per lo stock Amazon FBA. Le giacenze vengono aggiornate dalla "Sync Giacenze FBA". Gli ordini AFN vengono assegnati a questo magazzino. |
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
| `x_amazon_length` | Lunghezza (cm) letta dal Catalog API |
| `x_amazon_width` | Larghezza (cm) letta dal Catalog API |
| `x_amazon_height` | Altezza (cm) letta dal Catalog API |
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
