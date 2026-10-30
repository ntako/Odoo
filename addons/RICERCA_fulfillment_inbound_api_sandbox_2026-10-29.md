# Ricerca: Fulfillment Inbound API (spedizioni FBA) — sandbox e limiti di sicurezza

Data: 2026-10-29. Obiettivo: capire come testare la comunicazione delle spedizioni inbound FBA (non ancora implementata nel modulo `ke_amazon_sync`, vedi `progetto-odoo-amazon-fba-etichettatura.md` §10 Priorità 3) senza credenziali sandbox (non disponibili) e senza creare una spedizione reale sull'account di produzione.

Fonti consultate (pagine ufficiali Amazon SP-API, lette il 2026-10-29):
- Riferimento API `createInboundPlan`: https://developer-docs.amazon.com/sp-api/reference/createinboundplan
- Guida concettuale Fulfillment Inbound API: https://developer-docs.amazon.com/sp-api/docs/fulfillment-inbound-api
- Guida use-case "Create an inbound shipment": https://developer-docs.amazon.com/sp-api/docs/create-an-inbound-shipment
- Guida "FBA Inventory API v1 Dynamic Sandbox": https://developer-docs.amazon.com/sp-api/docs/fba-inventory-api-v1-dynamic-sandbox-guide

Nota: il dominio ufficiale `developer-docs.amazon.com` fa redirect 301 verso `developer-docs.amazon` (senza `.com`) — entrambe le forme funzionano, è la stessa pagina.

## 1. Come funziona il sandbox SP-API in generale

- **Non esiste un "account sandbox" separato.** Stessa app SP-API, stesso Client ID/Secret LWA della produzione. Cambia solo il *refresh token* (uno specifico per il sandbox, generato da Amazon in Seller Central sulla pagina della propria app) e l'URL di destinazione delle chiamate.
- Libreria usata dal progetto (`python-amazon-sp-api` / pacchetto `sp_api`): legge la variabile d'ambiente `AWS_ENV`. Se vale `SANDBOX`, tutti gli endpoint passano da `sellingpartnerapi-*.amazon.com` a `sandbox.sellingpartnerapi-*.amazon.com` (verificato leggendo `sp_api/base/marketplaces.py` installato nel progetto). **Va impostata PRIMA di qualunque `import sp_api`**, perché è una costante calcolata una sola volta al primo import del modulo, non per singola chiamata.
- **Il livello di sandbox non è uniforme fra le API.** Ogni famiglia di endpoint ha un proprio livello, documentato nella sezione "Sandbox" della sua pagina di riferimento:
  - **Fulfillment Inbound API (v2024-03-20)**: sandbox **"Static"** — risposte fisse/precampionate, non riflettono scenari di test personalizzati. Poco utile per verificare la logica applicativa.
  - **FBA Inventory API (v1)**: sandbox **"Dynamic"** — si creano articoli virtuali (`createInventoryItem`), si aggiunge/toglie inventario (`addInventory`), si leggono i riepiloghi (`getInventorySummaries`) con ASIN/FNSKU/quantità generati dal sandbox stesso, completamente isolato dalla produzione. Le quantità (`fulfillableQuantity`, `totalReservedQuantity`, `pendingCustomerOrderQuantity`) cambiano solo tramite ordini di evasione **virtuali** creati nel sandbox collegato di Fulfillment Outbound. Questo sandbox **non c'entra con le spedizioni inbound**, ma è rilevante per un'eventuale futura verifica sandbox dei fix di sincronizzazione giacenze FBA fatti questa settimana (vedi TASK.md "Giacenze").
  - Nessuna delle due pagine indica che il meccanismo "dynamic" si estenda ad altre API: è specifico per famiglia di endpoint.

## 2. Fulfillment Inbound API — sequenza delle chiamate e confine di sicurezza

Senza un sandbox utile per questa API, l'unica via per capire il comportamento reale è la guida use-case ufficiale "Create an inbound shipment" — non uno schema API, ma la spiegazione testuale del flusso. Riassunto fedele (il testo originale è in inglese, non riprodotto qui integralmente per via del copyright).

**Flusso con contenuto dei cartoni GIÀ noto (il percorso consigliato, mantiene il confine di sicurezza più lontano):**

1. `createInboundPlan` — crea il piano (indirizzo, articoli MSKU/quantità, chi prepara/etichetta). Nessun impegno dichiarato.
2. `getInboundOperationStatus` — sola lettura.
3. `generatePackingOptions`, `listPackingOptions`, `listPackingGroupItems` — esplorazione di come imballare gli SKU insieme. Nessun impegno dichiarato.
4. `confirmPackingOption` — seleziona un'opzione di imballaggio. Il testo non la dichiara irreversibile.
5. `setPackingInformation` — contenuto/dimensioni/peso delle scatole. Attenzione: *"Discarding packing information is not currently supported"* — modificarla dopo aver generato le opzioni di collocazione richiede di rigenerarle.
6. `generatePlacementOptions`, `listPlacementOptions`, `getShipment` — propone i centri di evasione, con eventuali commissioni/sconti. Le opzioni scadono e si rigenerano. Nessun impegno dichiarato.
7. `listShipmentItems` — sola lettura.
8. `generateTransportationOptions`, `listTransportationOptions` — preventivi di trasporto con costo, periodo di annullamento, scadenza. Nessun impegno dichiarato in questa fase.
9. `generateDeliveryWindowOptions`, `listDeliveryWindowOptions` — finestre di consegna proposte, con `validUntil`; se non confermate entro quella data vanno rigenerate.

> ⚠️ **10. `confirmPlacementOption` — PUNTO DI NON RITORNO, dichiarato esplicitamente dalla documentazione**: *"This option cannot be reversed after it is selected."* Genera gli ID di conferma (es. `FBA1234ABCD`) che compaiono sulle etichette. **Non chiamare questo metodo durante un test esplorativo.**

11. `confirmDeliveryWindowOptions` — impegno parziale (non si rigenerano nuove finestre dopo, ma la selezione è aggiornabile prima della chiusura).
12. `confirmTransportationOptions` — impegna corriere e costo; per corrieri partner Amazon addebita la spedizione. *"After a transportation option is confirmed, new transportation options cannot be generated or confirmed."*
13. `getLabels` — genera etichette (non porta da sola la spedizione a `ready_to_ship`).
14. Spedizione fisica.
15. `updateShipmentTrackingDetails` — tracking, obbligatorio per corrieri non partner.

**Annullamento**: `cancelInboundPlan` è documentato solo per DOPO la conferma del trasporto — finestra di 24 ore (pacco singolo) o 1 ora (LTL/FTL), dopo la quale l'account viene addebitato del costo di spedizione. La guida **non dichiara esplicitamente** che si possa abbandonare un piano prima di `confirmPlacementOption` senza conseguenze — è un'assenza di informazione, non una conferma di sicurezza, anche se nulla nel testo suggerisce impegni prima di quel punto.

**Importante — due varianti di flusso**: se il contenuto dei cartoni **non** è ancora noto al momento della creazione del piano, `confirmPlacementOption` (il punto di non ritorno) arriva molto prima nella sequenza (al passo 3 circa, invece che al 10). Per questo motivo, per un test esplorativo conviene **sempre** seguire la variante "contenuto cartoni noto", che tiene la zona sicura più ampia.

## 3. Piano di test concreto (zona sicura, con le credenziali di produzione esistenti)

Chiamate considerate sicure da eseguire per davvero (nessun impegno dichiarato dalla documentazione):

```
item_guidance                                    (nessun piano coinvolto, il più sicuro)
createInboundPlan
getInboundOperationStatus
generatePackingOptions / listPackingOptions / listPackingGroupItems
confirmPackingOption
setPackingInformation
generatePlacementOptions / listPlacementOptions
listShipmentItems
generateTransportationOptions / listTransportationOptions
generateDeliveryWindowOptions / listDeliveryWindowOptions
cancelInboundPlan                                (pulizia a fine test)
```

**Mai chiamare, in questa fase esplorativa:**

```
confirmPlacementOption        <- punto di non ritorno dichiarato
confirmDeliveryWindowOptions
confirmTransportationOptions  <- impegna corriere e costo
getLabels
updateShipmentTrackingDetails
```

## 4. Limiti di questa ricerca

- Basata sulla lettura delle pagine pubbliche elencate sopra il 2026-10-29, non su una conferma diretta di Amazon Seller Support.
- Nessuna chiamata reale è stata fatta per verificare empiricamente questi limiti.
- La documentazione non garantisce esplicitamente l'assenza di conseguenze prima di `confirmPlacementOption` — è un'assenza di dichiarazioni contrarie, non una garanzia scritta. Da trattare come "ragionevolmente sicuro secondo la documentazione ufficiale", non come "garantito".

## 5. Script esistenti in AMZ_TESTS/02_scritture_esempio/ — attenzione al confine

- `create_inbound_shipment_plan.py` (preesistente, non scritto in questa sessione di ricerca) **supera il confine di sicurezza**: arriva fino a `confirmPlacementOption` incluso (step 8/9, commentato nello script stesso come "IRREVERSIBILE lato Amazon") e oltre, fino a leggere `shipmentConfirmationId`. Se eseguito con dati reali (gli `assert` in cima bloccano solo l'esecuzione a vuoto, non con `ITEMS`/`SOURCE_ADDRESS`/`BOX_DIMENSIONS` compilati), crea un impegno reale e irreversibile sull'account. Da usare solo quando si è pronti a creare davvero una spedizione, non per esplorazione.
- `create_inbound_shipment.py` e `get_inbound_labels.py` proseguono oltre (prenotazione trasporto, etichette) — stesso discorso, non letti/toccati in questa ricerca.
- **Nuovo script `explore_inbound_safe.py`**: copre lo stesso punto di partenza (`createInboundPlan` → `listPlacementOptions`) ma poi **si ferma al confine documentato** — aggiunge `generateTransportationOptions`/`listTransportationOptions` e `generateDeliveryWindowOptions`/`listDeliveryWindowOptions` (preventivi/finestre, nessun impegno dichiarato) e non chiama mai `confirmPlacementOption` né nulla dopo. Ha un cleanup opzionale (`cancelInboundPlan`, disattivato di default tramite `CONFIRM_CLEANUP = False`) per non lasciare il piano aperto se non lo si vuole. Sintassi verificata (`ast.parse`); non eseguito — nessuna chiamata reale fatta finché l'utente non lo autorizza esplicitamente.
