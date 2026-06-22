# KE Inventory Import

Modulo Odoo 19 per l'importazione massiva di prodotti da file CSV, con supporto a mapping personalizzato delle colonne, conversione automatica di unità di misura, aggiornamento giacenze e prezzi, e importazione immagini.

**Versione:** 19.0.1.0
**Dipendenze:** `stock`, `product`
**Autore:** Matteo Nuzzachi

---

## Struttura del modulo

```
ke_inventory_import/
├── models/
│   ├── csv_file.py          # Libreria file CSV (inventory.csv.file)
│   ├── field_mapping.py     # Costanti mapping e modello riga mapping
│   ├── import_job.py        # Motore di importazione (inventory.import.job)
│   ├── mapping_preset.py    # Preset mapping riutilizzabili
│   └── product_template.py  # Eventuali estensioni al prodotto
├── views/
│   └── import_inventory_views.xml
├── static/src/
│   ├── js/csv_upload_widget.js
│   └── xml/csv_upload_widget.xml
└── security/
    └── ir.model.access.csv
```

---

## Modelli

### `inventory.csv.file` — Libreria CSV

Archivio centralizzato di file CSV riutilizzabili. Ogni file ha:

| Campo | Descrizione |
|---|---|
| `name` | Nome descrittivo del file |
| `file_data` | Dati binari del CSV (attachment) |
| `delimiter` | Separatore: `;` `,` o `\t` |
| `notes` | Note libere |

**Azioni disponibili:**
- **Crea job da questo file** — apre un nuovo `inventory.import.job` pre-compilato
- **Crea preset da questo file** — legge le intestazioni e apre un nuovo preset con rilevamento automatico delle colonne

---

### `inventory.mapping.preset` — Preset di mapping

Configurazione riutilizzabile che associa le colonne CSV ai campi Odoo. Può essere caricata su qualsiasi job di importazione.

| Campo | Descrizione |
|---|---|
| `name` | Nome del preset |
| `csv_file_id` | File CSV di riferimento (per auto-rilevamento colonne) |
| `delimiter` | Delimitatore CSV |
| `weight_unit` | Unità peso di fallback (g / kg / oz / lb) |
| `volume_unit` | Unità dimensioni di fallback (mm / cm / dm / m / in / ft) |
| `line_ids` | Righe di mapping (colonna CSV → campo destinazione) |

**Azione disponibile:**
- **Rileva colonne da CSV** — legge l'intestazione del file associato e popola le righe con auto-rilevamento

I preset possono essere salvati direttamente da un job tramite **"Salva come preset"**.

---

### `inventory.import.job` — Job di importazione

Il record principale che orchestra l'importazione. L'esecuzione avviene in **background thread** per non bloccare l'interfaccia.

#### Opzioni generali

| Campo | Descrizione |
|---|---|
| `csv_file_id` | File CSV dalla libreria |
| `delimiter` | Separatore CSV |
| `update_existing` | Se `True`, aggiorna i prodotti già esistenti; se `False` li salta |
| `import_images` | Scarica e salva immagini dalle URL nel CSV |
| `images_only` | Solo aggiornamento immagini (non tocca altri campi) |
| `image_delay` | Pausa in secondi tra un download e l'altro |
| `import_attributes` | Importa colonne non mappate come attributi prodotto |
| `import_descriptions` | Importa descrizioni multilingua |
| `import_tracking` | Imposta tracking per lotto su tutti i prodotti importati |
| `import_prices` | Importa prezzo di vendita (`list_price`) in modalità auto |
| `import_cost` | Importa prezzo di costo (`standard_price`) |
| `import_qty` | Aggiorna giacenze tramite rettifica inventario |
| `location_id` | Location magazzino per le giacenze |

#### Unità di misura (fallback)

| Campo | Descrizione |
|---|---|
| `weight_unit` | Unità peso di default: `g` `kg` `oz` `lb` |
| `volume_unit` | Unità dimensioni di default: `mm` `cm` `dm` `m` `in` `ft` |

Queste unità vengono usate come **fallback** quando il valore CSV non contiene un'unità esplicita. Se il valore contiene già l'unità (es. `"261 g"`, `"25 cm"`) questa impostazione viene ignorata.

#### Mapping personalizzato

| Campo | Descrizione |
|---|---|
| `use_custom_mapping` | Attiva il mapping manuale al posto del rilevamento automatico |
| `preset_id` | Carica un preset (copia le righe nel job) |
| `mapping_line_ids` | Tabella di associazione colonna CSV → campo destinazione |

**Azioni disponibili nel form:**
- **Rileva intestazioni CSV** — legge il CSV e popola la tabella di mapping con auto-rilevamento
- **Salva come preset** — salva il mapping corrente come preset riutilizzabile
- **Avvia importazione** — lancia il job in background
- **Aggiorna** — ricarica la pagina per vedere il progresso
- **Scarica report** — scarica il CSV di riepilogo al termine

#### Stato e contatori (readonly)

`state`: `queued` → `running` → `done` / `error`
Contatori: `total_count`, `processed_count`, `import_count`, `updated_count`, `skipped_count`
Al termine viene generato un allegato CSV con il dettaglio riga per riga.

---

### `inventory.field.mapping.line` — Riga di mapping

Ogni riga associa una colonna del CSV a un campo Odoo (o a una funzione speciale).

| Campo | Descrizione |
|---|---|
| `csv_column` | Nome esatto dell'intestazione nel CSV |
| `target` | Campo destinazione (vedi tabella sotto) |
| `default_value` | Valore fisso da usare se la cella è vuota |
| `sequence` | Ordine di elaborazione |

---

## Target di mapping disponibili

### Target speciali (prefisso `_`)

| Target | Descrizione |
|---|---|
| `_skip` | Ignora la colonna |
| `_image_url` | URL immagine da scaricare e salvare su `image_1920` |
| `_attribute` | Crea/aggiorna un attributo prodotto (`no_variant`) |
| `_category` | Categoria prodotto — trova per nome o crea |
| `_supplier` | Fornitore — trova partner per nome o crea, aggiunge come vendor |
| `_qty` | Quantità giacenza — aggiornata tramite rettifica inventario post-import |
| `_lot` | Numero lotto/seriale (usato insieme a `_qty` con tracking abilitato) |
| `_product_type` | Tipo prodotto Odoo: `consu` / `service` / `combo` |
| `_volume_l` | Dimensione Lunghezza per calcolo volume L×W×H |
| `_volume_w` | Dimensione Larghezza per calcolo volume L×W×H |
| `_volume_h` | Dimensione Altezza per calcolo volume L×W×H |
| `_weight_unit` | **Colonna unità peso per riga** — sovrascrive il selettore globale per quella riga |
| `_dim_unit` | **Colonna unità L/W/H per riga** — sovrascrive il selettore globale per quella riga |
| `_volume_unit` | **Colonna unità volume diretto per riga** — sovrascrive il selettore globale per quella riga |

### Target campi standard Odoo

| Target | Campo Odoo | Tipo |
|---|---|---|
| `name` | Nome prodotto | testo |
| `default_code` | Codice interno | testo |
| `barcode` | Barcode EAN/UPC | testo |
| `list_price` | Prezzo di vendita | decimale |
| `standard_price` | Prezzo di costo | decimale (via post-import) |
| `weight` | Peso [kg] | decimale con conversione unità |
| `volume` | Volume diretto [m³] | decimale con conversione unità |
| `description_sale` | Descrizione vendita | testo |

> **Nota su `standard_price`:** richiede l'opzione **"Importa prezzo di costo"** abilitata nel job.

---

## Parsing delle unità di misura

### Peso (`weight`)

La conversione avviene sempre in **kg** (unità nativa di Odoo).

**Priorità di determinazione dell'unità per ogni riga:**

1. Unità embedded nel valore della cella (es. `"261 g"`, `"1.5 kg"`, `"9.2 oz"`)
2. Colonna `_weight_unit` mappata (valore per riga, es. `"g"`)
3. Selettore `weight_unit` del job/preset (fallback globale)

**Formati supportati nel valore:**
- Numero puro: `"261"` → usa unità dal livello 2 o 3
- Numero + unità: `"261 g"`, `"261g"`, `"1.5 kg"`, `"2 lb"`, `"3 oz"`
- Pipe-separated (es. cataloghi che riportano più sistemi): `"9.2 oz|261 g"` → prende l'ultimo segmento

**Alias accettati:** `g`, `gr`, `gram`, `grams` → g; `kg` → kg; `oz` → oz; `lb`, `lbs`, `pound`, `pounds` → lb

**Fattori di conversione:**

| Unità | → kg |
|---|---|
| g | × 0.001 |
| kg | × 1 |
| oz | × 0.028349523 |
| lb | × 0.45359237 |

---

### Volume tramite dimensioni L×W×H (`_volume_l`, `_volume_w`, `_volume_h`)

Ogni dimensione viene parsata individualmente in **metri**, poi moltiplicata: `volume = L × W × H` (risultato in m³).

**Priorità unità per ogni riga:**
1. Unità embedded nel valore della cella (es. `"25 cm"`, `"10 mm"`)
2. Colonna `_dim_unit` mappata (valore per riga)
3. Selettore `volume_unit` del job/preset

**Formati supportati:**
- `"25"`, `"25 cm"`, `"25.5cm"`, `"1/4 in"`, `"10mm"`, ecc.

**Unità lineari accettate:** `mm`, `cm`, `dm`, `m`, `in`/`inch`/`"`, `ft`/`foot`/`feet`/`'`

---

### Volume diretto (`volume`)

La cella viene parsata direttamente in **m³**.

**Priorità unità per ogni riga:**
1. Unità embedded nel valore della cella
2. Colonna `_volume_unit` mappata (valore per riga)
3. Selettore `volume_unit` del job/preset (interpretato come unità cubica)

**Unità accettate:**

| Suffisso | Interpretazione |
|---|---|
| `mm`, `mm3`, `mm³` | mm³ → m³ |
| `cm`, `cm3`, `cm³` | cm³ → m³ |
| `dm`, `dm3`, `dm³` | dm³ → m³ |
| `m`, `m3`, `m³` | m³ (nessuna conversione) |
| `in`, `in3`, `in³` | in³ → m³ |
| `ft`, `ft3`, `ft³` | ft³ → m³ |
| `l`, `litre`, `liter` | litri → m³ |
| `ml` | millilitri → m³ |
| `cl`, `dl` | centilitri / decilitri → m³ |

---

## Modalità di importazione

### Modalità automatica (default)

Usata quando `use_custom_mapping = False`. Le colonne vengono riconosciute per nome tramite `_AUTO_DETECT_MAP`:

| Colonne CSV riconosciute | → Campo |
|---|---|
| `SAP` | `default_code` |
| `SAP Description` | `name` |
| `Category` | categoria prodotto |
| `EAN/UPC` | `barcode` |
| `Net Weight` | `weight` |
| `List Price`, `Sales Price`, `Sale Price` | `list_price` |
| `Cost`, `Standard Price`, `Costo` | `standard_price` |
| `Qty`, `Quantity`, `Stock`, `On Hand`, `Quantità` | giacenza |
| `Lot`, `Lot Number`, `Serial`, `Lotto` | lotto |
| `Material Image ID URL`, `Master Image ID URL` | immagine |
| `Supplier` | fornitore |
| Tutte le altre colonne non riconosciute | attributi prodotto |

### Modalità mapping personalizzato

Attivata con `use_custom_mapping = True`. Ogni colonna CSV viene associata manualmente a un target tramite la tabella `mapping_line_ids`.

Le colonne non esplicitamente assegnate a nessun campo vengono **automaticamente trattate come attributi prodotto** (a meno che non siano mappate su `_skip`).

---

## Flusso di importazione

```
action_run()
    └─ decodifica CSV
    └─ costruisce options{}
    └─ lancia _run_in_background() in thread daemon
           └─ _do_import()
                  ├─ legge tutte le righe CSV
                  ├─ pre-carica prodotti esistenti per default_code
                  ├─ per ogni riga:
                  │    ├─ determina unità peso/volume/dim per riga
                  │    ├─ _prepare_product_vals() → vals{}
                  │    ├─ calcola volume L×W×H se mappato
                  │    ├─ raccoglie qty/cost in qty_queue
                  │    ├─ raccoglie supplier in supplier_queue
                  │    ├─ scarica immagine se necessario
                  │    ├─ se esiste → write(vals) oppure skip
                  │    └─ se nuovo → aggiungi a create_batch
                  ├─ _flush_batch() ogni 100 prodotti nuovi
                  ├─ _apply_post_import() → aggiorna giacenze e costi
                  ├─ _apply_supplier_info() → crea/aggiorna vendor
                  └─ genera report CSV allegato
```

### Gestione prodotti esistenti vs nuovi

- **Prodotti esistenti** (trovati per `default_code`): aggiornati con `write(vals)` se `update_existing = True`, altrimenti saltati. I contatori (`updated_count` / `skipped_count`) riflettono questo.
- **Prodotti nuovi**: creati in batch da 100 per efficienza. Il tipo prodotto (`consu` / `service` / `combo`) viene determinato dalla colonna `_product_type` o dal default `consu`.

### Post-processing giacenze e costi

Qty e costi vengono raccolti durante il loop principale e applicati **dopo** che tutti i prodotti sono stati creati/aggiornati, tramite `_apply_post_import()`. Questo garantisce che anche i prodotti appena creati abbiano il record `product.variant` disponibile.

Le giacenze vengono aggiornate tramite `stock.quant` con `action_apply_inventory()`, raggruppando tutte le rettifiche in una singola chiamata per efficienza.

---

## Recupero da crash

Al caricamento del modulo (`_register_hook`), il sistema cerca eventuali job rimasti in stato `running` dopo un riavvio del server e li porta automaticamente in stato `error` con un messaggio esplicativo.

---

## Precisione decimale volume

All'avvio di ogni importazione, il sistema verifica che la precisione decimale Odoo per il campo `Volume` sia almeno **6 cifre** e la aggiorna automaticamente se necessario, per evitare arrotondamenti su valori in m³ molto piccoli (es. oggetti di pochi cm).

---

## Esempi di mapping CSV

### Import prezzi-only su prodotti esistenti

| Colonna CSV | Target |
|---|---|
| `SKU` | `default_code` |
| `Prezzo` | `list_price` |
| *(tutte le altre)* | `_skip` |

Impostazioni: `update_existing = True`, tutto il resto disabilitato.

### Import giacenze con lotto

| Colonna CSV | Target |
|---|---|
| `Codice` | `default_code` |
| `Quantità` | `_qty` |
| `Lotto` | `_lot` |
| *(tutte le altre)* | `_skip` |

Impostazioni: `import_qty = True`, `import_tracking = True`, location magazzino selezionata.

### Import peso con unità per riga

| Colonna CSV | Target |
|---|---|
| `SKU` | `default_code` |
| `Net Weight` | `weight` |
| `Weight Unit` | `_weight_unit` |

Il valore nella colonna `Weight Unit` (es. `g`, `kg`, `oz`) sovrascrive il selettore globale per ogni riga. Se la cella è vuota, si usa il selettore `weight_unit` del job.

### Import dimensioni L×W×H con unità per riga

| Colonna CSV | Target |
|---|---|
| `SKU` | `default_code` |
| `Length` | `_volume_l` |
| `Width` | `_volume_w` |
| `Height` | `_volume_h` |
| `Unit of Dimension` | `_dim_unit` |

Il volume finale in m³ viene calcolato come `L × W × H` dopo conversione di ogni dimensione in metri.
