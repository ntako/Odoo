# Architettura integrazione Odoo 19 Community ↔ Amazon

## Contesto e obiettivi

- **ERP:** Odoo 19 Community.
- **Situazione iniziale:** catalogo Amazon popolato; Odoo contiene già alcuni prodotti con giacenze, ma senza movimenti storici da preservare secondo le informazioni disponibili.
- **Catalogo:** prodotti propri e di altri marchi, più offerte, account e marketplace.
- **Obiettivo:** importare e riconciliare il catalogo Amazon con i prodotti Odoo esistenti, migrare dove necessario la struttura dei prodotti e rendere **Odoo il master** per anagrafiche interne, prezzi e disponibilità FBM.
- **Eccezioni:** Amazon resta autorevole per ASIN, seller SKU, stato delle inserzioni, ordini Amazon e inventario FBA.

## Principio fondamentale: non esiste una chiave universale

| Identificativo | Significato | Utilizzo |
|---|---|---|
| ID `product.product` | Variante/prodotto fisico interno Odoo | Identità interna e giacenze |
| `default_code` | SKU interno Odoo | Codice operativo interno e possibile segnale di matching |
| Seller SKU | Identificativo dell'offerta del venditore | Chiave operativa della listing nel contesto di account e marketplace |
| ASIN | Prodotto del catalogo Amazon | Collegamento al catalogo, non chiave univoca del prodotto Odoo |
| EAN/GTIN/UPC | Identificativo commerciale | Matching iniziale, previa verifica di confezione e unità di vendita |
| FNSKU | Identificativo legato alla logistica FBA | Riconciliazione dell'inventario FBA, non chiave principale |
| MPN + brand | Codice produttore e marchio | Ulteriore segnale di matching |

**Decisione:** identificare un'offerta con `(account_id, marketplace_id, seller_sku)` e collegarla a `product.product`. Non imporre che `default_code` e seller SKU coincidano, né sovrascrivere automaticamente il primo con il secondo.

## Modello dati proposto

### Modelli Odoo esistenti

- `product.template`: anagrafica generale e attributi condivisi.
- `product.product`: variante fisica, unità vendibile e giacenze.
- `stock.quant` e normali meccanismi di inventario: quantità per prodotto e ubicazione.

### Modelli del connettore

| Modello | Responsabilità |
|---|---|
| `amazon.account` | Account Seller Central, configurazioni e autorizzazioni SP-API |
| `amazon.marketplace` | Marketplace, valuta, lingua e regole di vendita |
| `amazon.catalog.item` | ASIN nel marketplace, attributi e dati catalogo Amazon |
| `amazon.offer` | Listing del venditore, seller SKU, prodotto Odoo, account, marketplace, prezzo, stato e fulfillment |
| `amazon.import.batch` | Esecuzione e riepilogo di una importazione |
| `amazon.import.line` | Snapshot, candidati, esito matching e decisioni di riconciliazione |
| `amazon.sync.job` | Job asincroni, tentativi, retry e stato |
| `amazon.sync.log` | Audit delle richieste e risposte, errori e modifiche |
| `product.identifier` (opzionale ma consigliato) | Identificativi multipli EAN, UPC, GTIN, MPN e relativo brand |

### Relazioni

```text
product.template
    └── product.product (variante fisica Odoo)
          └── amazon.offer [0..N]
                ├── amazon.account
                ├── amazon.marketplace
                └── amazon.catalog.item (ASIN + marketplace)

amazon.import.batch
    └── amazon.import.line [0..N]
          ├── snapshot dati Amazon
          ├── candidati product.product
          └── decisione di riconciliazione
```

Più offerte Amazon possono puntare allo stesso `product.product`; offerte diverse possono riferirsi allo stesso ASIN; un prodotto fisico può essere rappresentato da ASIN diversi nei diversi marketplace.

### Campi indicativi di `amazon.offer`

```python
product_id
catalog_item_id
account_id
marketplace_id
seller_sku
fulfillment_channel  # FBA / FBM; valutare casi misti
listing_status
sync_price
sync_stock
sync_product_data
last_import_at
last_export_at
sync_state
```

Vincolo logico di unicità per offerte attive: `(account_id, marketplace_id, seller_sku)`. Gestire esplicitamente archiviazione e storicizzazione.

## Importazione iniziale: bootstrap Amazon → Odoo

1. **Backup e snapshot** di database, prodotti Odoo, identificativi, giacenze per ubicazione e configurazioni.
2. **Acquisizione Amazon** tramite SP-API: preferire report delle listing per estrazioni massive; integrare con Listings Items API e Catalog Items API dove necessario.
3. **Staging:** salvare i payload originali, i marketplace, la data di acquisizione e gli eventuali errori, senza creare subito prodotti.
4. **Matching:** cercare prodotti Odoo esistenti, proporre candidati e segnalare ambiguità.
5. **Revisione:** approvare, rifiutare, correggere associazioni, decidere se creare un nuovo prodotto, una variante, un kit o un multipack.
6. **Migrazione controllata:** arricchire i prodotti esistenti, creare i mancanti e strutturare le varianti quando necessario.
7. **Mapping:** creare le offerte Amazon collegate ai prodotti Odoo confermati.
8. **Verifica:** controllare quantità per prodotto e ubicazione, numero di prodotti, duplicati, varianti e offerte non associate.
9. **Abilitazione graduale:** attivare prima l'esportazione prezzi su un sottoinsieme di offerte, poi le disponibilità FBM e infine gli altri flussi.

Durante bootstrap e migrazione mantenere una **modalità protetta/read-only verso Amazon**: nessun job deve scrivere sulle listing fino alla conferma finale.

## Matching e riconciliazione

Priorità e segnali:

1. Mapping `(account, marketplace, seller SKU)` già validato: riutilizzarlo.
2. EAN/GTIN/UPC coincidente, con verifica di unicità, marca, quantità per confezione e unità vendibile.
3. Seller SKU uguale a `default_code` Odoo, nel rispetto delle convenzioni di codifica.
4. Brand + MPN e altri identificativi produttore.
5. ASIN già collegato a un prodotto Odoo, come indizio da verificare.
6. Titolo, attributi, taglia, colore e altri dati descrittivi, solo come segnali di supporto.

Il matching può assegnare un punteggio di confidenza, ma **non deve automatizzare associazioni ambigue**. Stati consigliati: `matched`, `suggested`, `ambiguous`, `new_product`, `rejected`, `approved`, `applied`, `error`.

### Schermata di riconciliazione

Mostrare affiancati dati Amazon e Odoo: seller SKU, ASIN, EAN, marca, MPN, nome, attributi, confezione, prezzo, disponibilità, candidati e motivi del match. Azioni: approva, cambia prodotto, crea nuovo prodotto, crea variante, identifica kit/multipack, ignora.

### Idempotenza

Rieseguire un import non deve duplicare prodotti, offerte o associazioni. Usare chiavi stabili per staging e mapping, vincoli DB e job riavviabili. Conservare audit e motivazioni delle decisioni.

## Migrazione dei prodotti Odoo esistenti

Preferire una migrazione **in place** per conservare gli ID dei prodotti e le giacenze. La mancanza di movimenti storici riduce i vincoli, ma non autorizza modifiche SQL dirette a `stock.quant`.

Distinguere tre operazioni:

- **Arricchimento:** aggiornare campi e identificativi senza cambiare l'identità del prodotto.
- **Ristrutturazione varianti:** raggruppare prodotti semplici sotto template con attributi, verificando il comportamento di Odoo 19 su generazione, archiviazione o sostituzione delle varianti e la destinazione delle giacenze.
- **Deduplicazione:** selezionare un prodotto canonico, trasferire le quantità tramite procedure di inventario supportate, controllare le ubicazioni e archiviare i duplicati solo dopo verifica.

Per i prodotti già a magazzino, riconciliare le quantità **per `product.product` e ubicazione**, non solo il totale globale. Eseguire la migrazione su una copia del database e confrontare snapshot prima/dopo.

## Casi particolari

| Caso | Strategia |
|---|---|
| Stesso prodotto in più marketplace | Un prodotto Odoo, più `amazon.offer` |
| Seller SKU diversi per la stessa unità | Più offerte collegate al medesimo prodotto Odoo |
| ASIN diversi per lo stesso prodotto fisico | Catalog item distinti, possibile stesso prodotto Odoo dopo verifica |
| Varianti taglia/colore | Un `product.product` per variante, con template condiviso se appropriato |
| Multipack | Unità vendibile distinta; distinta base o logica di composizione |
| Bundle/kit | Prodotto vendibile con componenti Odoo e gestione dedicata delle quantità |
| FBA e FBM | Offerte/canali distinti; quantità FBA lette da Amazon, stock FBM gestito da Odoo |
| EAN mancante o multiplo | Identificativi facoltativi e modello `product.identifier` |
| Offerte chiuse/inattive | Importabili in staging, con filtro e decisione esplicita di attivazione |

## Proprietà dei dati dopo la migrazione

| Dato | Fonte autorevole |
|---|---|
| Identità e SKU interno del prodotto | Odoo |
| Brand, attributi e anagrafica interna | Odoo |
| Identificativi commerciali validati | Odoo, con verifica dei dati esterni |
| Prezzi di vendita | Odoo, con listini/regole per marketplace |
| Stock FBM | Odoo |
| Stock FBA | Amazon |
| ASIN e seller SKU | Amazon, conservati nei mapping |
| Stato e dati pubblicati della listing | Amazon per stato effettivo; Odoo per le modifiche che decide di inviare |
| Ordini Amazon | Amazon → Odoo |

Evitare una sincronizzazione bidirezionale indiscriminata degli stessi campi: definire la direzione per ciascun dato e gestire eventuali conflitti.

## Flussi di sincronizzazione

- **Prezzi:** Odoo → Amazon per seller SKU, account e marketplace; listini dedicati e conversioni valuta dove necessario.
- **Stock FBM:** Odoo → Amazon, con regole di disponibilità e riserve appropriate.
- **Stock FBA:** Amazon → Odoo come dato di disponibilità logistica separato; non sovrascrivere automaticamente lo stock fisico interno.
- **Ordini:** Amazon → Odoo, risolvendo le righe tramite mapping dell'offerta.
- **Dati prodotto:** Odoo master interno; aggiornamenti al catalogo Amazon soltanto dove consentiti dalle API, dai permessi e dalle regole di contribuzione del marketplace.

Prevedere code asincrone, rate limiting SP-API, retry con backoff, deduplicazione, log di richieste/risposte e controlli di errore.

## Organizzazione del modulo (indicativa)

```text
amazon_connector/
├── models/
│   ├── amazon_account.py
│   ├── amazon_marketplace.py
│   ├── amazon_catalog_item.py
│   ├── amazon_offer.py
│   ├── amazon_import_batch.py
│   ├── amazon_import_line.py
│   ├── amazon_sync_job.py
│   └── product_identifier.py
├── services/
│   ├── amazon_client.py
│   ├── listing_importer.py
│   ├── product_matcher.py
│   ├── reconciliation_service.py
│   ├── listing_exporter.py
│   └── inventory_sync.py
├── jobs/
│   ├── import_listings.py
│   ├── export_prices.py
│   └── export_stock.py
├── views/
├── security/
└── tests/
```

## Roadmap di implementazione

1. Modelli account, marketplace, catalog item e offer.
2. Importazione SP-API e staging, senza scritture verso Amazon.
3. Motore di matching e interfaccia di riconciliazione.
4. Migrazione controllata dei prodotti, delle varianti e delle giacenze Odoo.
5. Esportazione prezzi su offerte selezionate.
6. Sincronizzazione disponibilità FBM e lettura inventario FBA.
7. Import ordini e automazioni robuste.

## Test di accettazione minimi

- Import ripetuto senza duplicati.
- Stesso prodotto su due marketplace collegato a un solo prodotto Odoo.
- Due seller SKU diversi riconciliati correttamente.
- ASIN uguale ma confezioni differenti **non** unite erroneamente.
- Varianti e giacenze preservate per ubicazione.
- Nessuna scrittura Amazon durante la modalità protetta.
- Retry e fallimenti API tracciati senza applicazioni parziali non riconciliabili.
- Stock FBA non trattato come stock FBM.
- Esportazione prezzo e stock soltanto per le offerte abilitate.

## Decisioni aperte

- Numero di account Seller Central e marketplace effettivamente utilizzati.
- Volumi di prodotti, varianti e offerte.
- Presenza di bundle, multipack, prodotti senza EAN e codici ripetuti.
- Regole di prezzo per paese, tasse, valute e promozioni.
- Strategia di gestione dell'inventario FBA in Odoo.
- Modalità di creazione/trasformazione delle varianti in Odoo 19 da verificare in ambiente di test.
- Politiche di archiviazione e storicizzazione delle listing Amazon.

---

*Documento di progettazione iniziale basato sulle decisioni discusse. I dettagli specifici delle API SP-API e del comportamento ORM di Odoo 19 vanno verificati sulla documentazione della versione utilizzata prima dell'implementazione.*
