# KE PrestaShop Sync

Modulo Odoo 19 per la sincronizzazione bidirezionale tra Odoo e PrestaShop
tramite la **WebService API** di PrestaShop.

## Funzionalità

| Flusso | Direzione | Attivazione |
|---|---|---|
| Import prodotti | PS → Odoo | Wizard (`PrestaShop ▸ Import prodotti da PS`) |
| Export prodotti (semplici e configurabili) | Odoo → PS | Wizard (`PrestaShop ▸ Export prodotti verso PS`) |
| Sync quantità disponibili | Odoo → PS | Cron + pulsante manuale |
| Sync prezzi (listino + impatto prezzo combinazioni) | Odoo → PS | Cron + pulsante manuale |
| Sync immagine principale | Odoo → PS | Cron + pulsante manuale (+ re-upload forzato) |
| Import ordini | PS → Odoo | Cron + pulsante manuale |
| Disattivazione prodotti archiviati | Odoo → PS | Durante l'export completo |

Con **Filtra i prodotti per magazzino** attivo, i flussi Odoo → PS operano solo
sull'assortimento del **Magazzino PrestaShop** (giacenza > 0). Vedi *Configurazione*.

Il wizard di **export** ha inoltre l'opzione **"Solo prodotti con disponibilità
libera > 0"** (attiva di default) + un campo **"Magazzino di riferimento"**
(preimpostato sul Magazzino PrestaShop): limita l'export ai prodotti con
`free_qty` (giacenza meno quantità riservata) maggiore di zero **in quel
magazzino**, indipendentemente dal filtro globale. Il wizard mostra in anteprima
quanti prodotti verranno esportati e con quale criterio.

Ogni operazione produce un record in **`PrestaShop ▸ Log sincronizzazioni`** con
conteggi (creati / aggiornati / saltati / errori), durata e log dettagliato riga per riga.

### Coda ed esecuzione

Le sincronizzazioni **non** girano nel processo web: vengono **accodate** (stato
*In coda*) ed eseguite dal cron **`PS Sync: Runner coda job`** (`cron_run_queue`,
attivo di default, ogni 5 min + innesco immediato all'accodamento), un job per
volta. Questo le rende robuste: non muoiono se il worker HTTP viene riciclato.

> Richiede il job scheduler attivo (`--max-cron-threads` ≥ 1, default). Senza,
> i job restano *In coda*.

I cron periodici (stock/prezzi/immagini/ordini) accodano anch'essi e vengono
saltati se un job dello stesso tipo è già in coda o in corso.

### Avanzamento e interruzione

Durante l'esecuzione il log viene aggiornato ogni pochi elementi con
`Avanzamento` (`N / totale`), contatori e ultime righe di log: premere **Aggiorna**
sulla form per vedere lo stato corrente. L'avanzamento è scritto anche nel log del
server Odoo a livello `INFO` (`PS sync <tipo>: N/totale`) — utile con `tail -f`.

- **Interrompi** (stato *In coda* o *In corso*): se in coda annulla subito; se in
  corso il worker si ferma entro pochi elementi e il log passa a *Interrotto*.
- **Forza chiusura**: chiude comunque un job che risulta bloccato (nessun
  avanzamento). Un job *In corso* senza aggiornamenti da oltre 1 ora viene inoltre
  chiuso automaticamente in errore dal runner della coda.

## Requisiti

- Odoo 19
- Python: `requests`
- In PrestaShop: WebService abilitato (`Parametri Avanzati ▸ WebService`) e una
  API key con i permessi sulle risorse: `products`, `combinations`, `product_options`,
  `product_option_values`, `stock_availables`, `images`, `orders`, `customers`,
  `addresses`, `countries`.

## Configurazione

`Impostazioni ▸ PrestaShop`:

- **Connessione**: URL negozio (senza slash finale), API key, ID lingua PS,
  ID shop, categoria PS e gruppo regole IVA di default per i prodotti creati.
  Il pulsante *Testa connessione* verifica le credenziali.
- **Magazzino PrestaShop**: magazzino Odoo usato come sorgente delle quantità e
  assegnato agli ordini importati (`warehouse_id`). Con **Filtra i prodotti per
  magazzino** attivo, i flussi Odoo → PS (stock, prezzi, immagini, export) e
  l'aggiornamento di prodotti esistenti in import considerano **solo i prodotti
  con giacenza disponibile > 0** in quel magazzino. La *creazione* di nuovi
  prodotti in import non è filtrabile per giacenza (il prodotto non esiste ancora).
- **Sync stock / prezzi / immagini**: attivazione del cron e intervallo in minuti
  (l'intervallo scrive direttamente sul record `ir.cron`).
- **Import ordini**: intervallo cron, finestra oraria (`ultime N ore`) e
  *Conferma automatica* (conferma il `sale.order` invece di lasciarlo come preventivo).
- **Opzioni import prodotti**: aggiornamento dei prodotti esistenti e tipo prodotto
  Odoo da creare.

Tutti i cron sono **disabilitati per default** e vanno attivati dalle impostazioni.

## Modello dati

- `product.template`: `ps_product_id`, `ps_sync_enabled`, `ps_last_sync_date`, `ps_image_synced`
- `product.product`: `ps_combination_id`
- `sale.order`: `ps_order_id`, `ps_order_state`, `ps_order_reference`
- `ke.prestashop.sync.log`: storico delle sincronizzazioni

## Logica di matching

- **Import prodotti**: `ps_product_id` → `default_code` (reference) → `barcode` (ean13)
- **Export prodotti**: `ps_product_id` → ricerca su PS per `reference`
- **Import ordini**: cliente per `email`, prodotto per `ps_product_id` → nome;
  gli ordini già importati (stesso `ps_order_id`) vengono saltati (idempotente).
  Le righe senza corrispondenza usano il prodotto generico `PS-GENERIC`.

## Manutenzione

`Impostazioni ▸ PrestaShop ▸ Manutenzione`:

- **Diagnostica link PS**: elenca i `ps_product_id` duplicati (più prodotti Odoo
  collegati allo stesso prodotto PS).
- **Reset link orfani**: azzera `ps_product_id` sui prodotti il cui prodotto PS
  non esiste più.

## Note tecniche

- Letture in JSON (`output_format=JSON`), scritture in XML con round-trip GET→PUT.
- Autenticazione via `ws_key` in query string (più robusta con proxy che filtrano
  l'header `Authorization`).
- Pulsanti manuali, wizard e cron periodici **accodano** un job; l'esecuzione
  avviene nel cron worker (vedi *Coda ed esecuzione*).
- EAN accettato solo se 13 cifre numeriche; `link_rewrite` generato come slug ASCII.

## Test

```
odoo -i ke_prestashop_sync --test-enable --stop-after-init
```

I test (`tests/test_sync_engine.py`) usano un client PrestaShop finto in memoria,
senza chiamate di rete.
