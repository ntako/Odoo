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

I parametri di stato vengono salvati in `ir.config_parameter`:
- `ke_amazon_sync.import_report_id` — reportId in attesa
- `ke_amazon_sync.import_report_attach_id` — ID allegato con le righe JSON
- `ke_amazon_sync.import_report_row_offset` — prossima riga da elaborare

### 2. Controllo Nuovi Prodotti (automatico o manuale)
Schedulata ogni N ore (configurabile). Controlla se i prodotti aggiunti manualmente in Odoo sono già presenti su Amazon da parte del produttore/brand.

- Filtra i prodotti senza ASIN ma con barcode EAN, creati nelle ultime N ore
- Cerca su Amazon Catalog per EAN
- Se trovato: collega l'ASIN e imposta il flag *Presente su Amazon (produttore)*
- Se non trovato: imposta lo stato *Non listato*
- Attivabile manualmente da **Impostazioni → Amazon Seller Central → Esegui ora**

### 3. Sincronizzazione Prezzi e Quantità (automatica o manuale)
Schedulata ogni N minuti (configurabile, disabilitata di default). Aggiorna su Amazon i prodotti per cui la sync è abilitata.

- **Prezzo**: aggiorna via Listings PATCH API per tutti i prodotti con *Sync prezzo* attivo
- **Quantità**: aggiorna via Listings PATCH API per i prodotti **FBM** con *Sync quantità* attivo (per FBA la quantità è gestita da Amazon)
- Log dettagliato di ogni operazione
- Attivabile manualmente da **Impostazioni → Amazon Seller Central → Esegui ora**

---

## Installazione

### Prerequisiti

```bash
pip install python-amazon-sp-api requests
```

### Deploy
Copia la cartella `ke_amazon_sync` nella directory addons di Odoo, poi installa il modulo dall'interfaccia (**Impostazioni → Applicazioni**).

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

### Sincronizzazioni automatiche

Ogni riga ha un toggle per abilitare/disabilitare la schedulazione, il campo intervallo e un pulsante **Esegui ora** per il trigger manuale immediato.

| Opzione | Default |
|---|---|
| Controllo nuovi prodotti | Ogni 24 ore — **abilitato** |
| Sync prezzi/quantità | Ogni 60 minuti — **disabilitato** |

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
│   ├── product_template.py      # Campi Amazon su product.template
│   ├── amazon_sync_log.py       # Modello log + orchestrazione cron (state machine import)
│   └── res_config_settings.py   # Credenziali, toggle cron, trigger manuali
├── services/
│   ├── sp_api_client.py         # Wrapper SP-API: Listings, Catalog, Inventory, Reports API
│   └── sync_engine.py           # Logica sync: full_import, full_import_report_rows, check, price/qty
├── wizard/
│   └── amazon_full_import.py    # Wizard import completo
├── data/
│   └── ir_cron.xml              # Azioni schedulate (funzione 2 e 3)
├── security/
│   └── ir.model.access.csv
└── views/
    ├── product_template_views.xml     # Tab Amazon nel form prodotto
    ├── amazon_sync_log_views.xml      # Vista log
    ├── res_config_settings_views.xml  # Sezione impostazioni
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
| `get_inventory_map()` | FBA Inventory | Mappa `{sku: qty}` per prodotti FBA |
| `patch_price(sku, product_type, price)` | Listings PATCH | Aggiorna prezzo su Amazon |
| `patch_quantity(sku, product_type, qty)` | Listings PATCH | Aggiorna qty FBM su Amazon |

### Metodi chiave in `sync_engine.py`

| Metodo | Descrizione |
|--------|-------------|
| `full_import()` | Import completo con catalog API (senza limite) |
| `full_import_chunk()` | Processa una pagina di 20 listing (paginazione classica) |
| `full_import_report_rows()` | Processa un chunk di righe TSV del report (senza catalog API) |
| `_parse_listing_report()` | Converte una riga TSV nel dict prodotto interno |
| `check_new_products()` | Cerca prodotti Odoo su Amazon per EAN |
| `sync_prices_and_quantities()` | PATCH prezzi e qty su Amazon |

---

## Campi aggiunti a product.template

| Campo | Descrizione |
|---|---|
| `x_amazon_asin` | ASIN collegato |
| `x_amazon_sku` | SKU interno Amazon |
| `x_amazon_status` | Stato: Attivo / Inattivo / Non listato / Trovato (produttore) / Errore |
| `x_amazon_fulfillment` | FBA o FBM |
| `x_amazon_found_by_manufacturer` | Prodotto già presente su Amazon da altro venditore |
| `x_amazon_sync_price` | Abilita sync prezzo automatica |
| `x_amazon_sync_qty` | Abilita sync quantità automatica (solo FBM) |
| `x_amazon_price` | Prezzo letto da Amazon (sola lettura) |
| `x_amazon_qty` | Quantità FBA letta da Amazon (sola lettura) |
| `x_amazon_last_sync` | Data/ora ultima sincronizzazione |
| `x_amazon_sync_error` | Ultimo errore di sincronizzazione |

---

## Log e monitoraggio

Tutti gli eventi sono tracciati in **Magazzino → Amazon Sync → Log Sincronizzazioni** con:
- Tipo operazione, stato, date inizio/fine e durata
- Contatori: creati, aggiornati, saltati, errori
- Log riga per riga di ogni prodotto elaborato

---

## Relazione con gli altri moduli

```
Amazon/SyncFromAmazon     →  export CSV manuale (flusso legacy, ancora utilizzabile)
ke_amazon_sync         →  sync diretta SP-API ↔ Odoo (questo modulo)
Odoo-Import-Products      →  import CSV generico da fornitore (invariato)
```
