# Analisi del connettore Amazon per Odoo 19 — 27 ottobre 2026

## Ricontrollo dopo il commit c7995a0 — esito

**I quattro casi specifici F1–F4 della verifica precedente risultano
corretti nel codice e nelle riproduzioni isolate ripetute.** Nessun nuovo
blocco individuato nel perimetro di questi fix, per la configurazione
concordata con un unico pool FBA europeo. Questo esito aggiorna le sezioni
storiche sotto; non chiude gli altri limiti della revisione generale.

- **F1:** confronto con lo SKU precedente della listing corrente. Verificati
  entrambi i casi: FR cambia B→A uguale al primario → FNSKU invalidato;
  FR resta B diverso dal primario → FNSKU preservato. Il ramo varianti
  applica lo stesso confronto separato da quello dei campi condivisi.
- **F2:** sul cambio SKU di una famiglia con un solo figlio vengono
  invalidati sia codice variante sia fallback template. Ripetuto il ramo
  reale di aggiornamento variante e il resolver: codice stampabile vuoto.
- **F3:** snapshot dedicati separati dai dati di visualizzazione e uno
  snapshot condiviso per variante sul pool. Riprodotti aumento 10→15 su
  IT+FR (consumo totale 5, non 10) e upsert qty=0 fra due sync a inventario
  invariato (nessun nuovo consumo). I nuovi campi non sono scritti dagli
  altri flussi di import/riconciliazione.
- **F4:** il reinvio aggiorna sent_date anche per un lavoro già sent.
  Ripetuti recupero dopo timeout e poll immediatamente successivo: un solo
  invio, poi nessun lavoro restituito. Non è un test di concorrenza HTTP o
  di crash/ack sulla stampante reale.

I nuovi test Odoo coprono i casi principali. Miglioramento consigliato,
non bloccante: nel test di contaminazione dello snapshot aggiungere anche
qty report=0 (attualmente usa 999, che col vecchio codice avrebbe prodotto
un delta negativo senza consumo). Il caso con 0 è stato comunque verificato
qui con i metodi reali e dipendenze simulate.

**Verifiche locali:** 52 Python validi via AST, 29 XML ben formati, file
manifest presenti, `git diff --check` superato; 94 metodi di test presenti,
non eseguiti qui perché Odoo non importabile. Script temporaneo:
`/tmp/recheck_amazon_c7995a0.py`. Nessuna API Amazon, modifica al DB o al
codice applicativo durante questo controllo.

**Aggiornamento:** occorre `-u ke_amazon_sync` per creare i nuovi campi
snapshot, oltre al caricamento del nuovo codice. Non è presente un
backfill degli snapshot precedenti: i nuovi campi partono da zero e la
prima sync FBA li inizializza. Se il transito contiene merce, il primo
giro può trattare lo stock Amazon già esistente come aumento: prima di
usarlo operativamente serve un'inizializzazione controllata. Non viene
assunto che la giacenza attuale del transito sia zero. La futura attivazione
esplicita del consumo inbound e la stampa fisica restano da collaudare.

## Verifica successiva dei fix — HEAD dda1b03

Esaminati i sette commit da `f6f77af` a `dda1b03`: stampa autonoma,
coda agente, PDF, immagini varianti, snapshot FBA, invalidazione FNSKU e
prezzo iniziale. **I fix migliorano i casi di base, ma la verifica rileva
ancora i seguenti casi combinati non coperti dai nuovi test.** I rilievi
nelle sezioni successive descrivono la situazione precedente ai fix e
vanno letti insieme a questo aggiornamento.

### F1 — P1: confronto SKU per invalidazione FNSKU non scoped al marketplace

`services/sync_engine.py:1636` confronta il nuovo SKU con
`product.x_amazon_sku` (primario), poi usa il risultato per invalidare
l'FNSKU della listing corrente. Il confronto deve invece usare lo SKU
precedente della specifica listing; i campi condivisi del prodotto hanno
una responsabilità diversa.

Riprodotto con i metodi reali `_update_product` e `_upsert_listing` e
dipendenze simulate: primario A, listing FR B/FNSKU-B. Se FR cambia B→A,
`sku_changed` è False e la listing finisce A/FNSKU-B. Se invece FR resta B,
`sku_changed` è True e viene cancellato un codice locale ancora valido.
Il ramo varianti confronta analogamente lo SKU globale della variante
(`:1883`). Occorre coprire entrambi i casi con SKU diversi per marketplace;
i nuovi test sui cambi SKU non verificano queste sequenze.

### F2 — P1: FNSKU invalidato sulla variante recuperabile dal template

`services/sync_engine.py:1883-1886` invalida soltanto il codice variante.
`models/product_variant.py:49` permette però di usare quello del template
quando rimane una sola variante. Su una famiglia promossa da prodotto
semplice il vecchio FNSKU template può essere ancora valorizzato.

Riprodotto il resolver reale con variante invalidata e template storico
con un solo figlio: restituisce FNSKU-OLD e la stampa autonoma non blocca.
Il collaudo completo ORM della promozione resta da eseguire, ma il percorso
di lettura non distingue famiglia singola e prodotto semplice. Serve
invalidare anche il fallback pertinente oppure impedirlo quando il
template rappresenta una famiglia con identificativi sulla variante.

### F3 — P1 quando si usa il transito: lo snapshot resta instabile e per marketplace

Il precedente ora è per variante, correggendo la contaminazione fra
sorelle. Restano però due problemi riprodotti con metodi reali simulati:

- `_upsert_listing` (`services/sync_engine.py:704`) riscrive `listing.qty`
  dalla quantità del report listing. La sync FBA (`:3413`) usa lo stesso
  campo come precedente di inventario. Sequenza: sync=15, Full Import o
  riconciliazione con qty listing=0, sync invariata=15 → nuovo consumo
  richiesto dal transito di 15. Serve uno snapshot dedicato che non venga
  modificato dagli aggiornamenti delle offerte.
- Il precedente è distinto per marketplace (`:3393`), ma il pool fisico è
  unico. IT e FR entrambi con precedente 10 e inventario nuovo 15 chiedono
  ciascuno 5 dal medesimo transito: totale 10 anziché 5, se capiente.
  Lo snapshot va riferito a variante + pool/magazzino fisico, oppure il
  consumo deve essere eseguito una sola volta per pool.

In entrambi i casi il quant FBA viene infine imposto al valore assoluto:
il difetto riguarda il transito e la provenienza dei movimenti, non
necessariamente il totale FBA finale. Se il transito è vuoto non perde
pezzi; il processo inbound resta futuro secondo le priorità concordate.

### F4 — P2, prima di attivare l'agente: retry stampa senza nuova scadenza

`models/amazon_print_job.py:79-82` aggiorna sent_date solo sui lavori
queued, non su quelli sent recuperati dopo il timeout. Un lavoro scaduto
rimane quindi immediatamente selezionabile a ogni poll successivo fino
all'ack; un batch lento o due poll sovrapposti possono ristamparlo più
volte. Il test attuale verifica il primo recupero, non il poll immediato
successivo. Serve rinnovare il lease a ogni assegnazione e proteggere la
presa in carico concorrente. Questo è distinto dal compromesso già
documentato della possibile ristampa dopo crash prima dell'ack.

### Parti dei fix coerenti e verifiche

- Stampa autonoma: nessuna scrittura di picking/move/quant nel wizard;
  controlli su prodotto, FNSKU e numero copie presenti.
- PDF: barcode incorporato, formato e separazione pagine affrontati nel
  codice. Nessuna nuova verifica visuale/PDF o stampa Zebra in questa
  sessione; le prove runtime descritte in TASK sono storiche.
- Immagini: scrittura sul campo specifico variante e backfill aggiunti.
- IVA: correzione solo alla creazione, senza riscrittura dei prezzi
  esistenti. Controlli isolati riusciti per aliquota 10% esclusa e
  convenzione IVA inclusa; non equivale al supporto a ogni struttura
  fiscale o valuta.
- Upsert: preservazione FNSKU quando assente nella sorgente e invalidazione
  esplicita funzionano nei casi semplici; il problema è come viene decisa
  l'invalidazione nei percorsi multi-marketplace/fallback.
- AST: **52 Python** validi; **29 XML** ben formati; file manifest presenti.
  **88 metodi test** nel codice, non eseguiti qui. I conteggi di test passati
  in TASK appartengono alle sessioni precedenti, non a questa verifica.
- Script di riproduzione isolata:
  `/tmp/review_amazon_fixes_20261027.py`. Nessun DB/API Amazon utilizzato;
  Odoo non importabile nel Python disponibile. Nessuna correzione al codice
  applicativo applicata durante questa revisione.

## Ambito e metodo

Revisione del codice presente nel working tree, comprese le modifiche non
committate a `services/sync_engine.py` e `tests/test_full_import_matching.py`.
Esaminati entrambi gli addon, manifest, modelli, servizi, wizard, viste,
cron, permessi, migrazione e test; confrontati README, TASK, precedente
REVIEW e documenti di progetto. Nessuna correzione al codice applicativo,
chiamata Amazon o modifica al database.

Le indicazioni storiche di TASK non sono nuove verifiche. I piani Markdown
descrivono obiettivi, non funzionalità necessariamente implementate.
`PREVISIONE_architettura_integrazione_odoo19_amazon.md` è una proposta:
non viene assunto come autorizzazione a cambiare le decisioni precedenti.

Livelli di evidenza usati: **riprodotto isolatamente** significa esecuzione
del metodo Python reale estratto via AST con dipendenze simulate;
**lettura del codice** significa rilievo statico; **da verificare su Odoo**
indica una dipendenza dal comportamento ORM/runtime non collaudata qui.

## Valutazione complessiva

Il connettore contiene soluzioni utili e già articolate: report bulk,
arricchimento Catalog, matching centralizzato, traduzioni, varianti,
magazzini FBA/FBM, etichette e audit degli import. Il refactor delle
inserzioni a `product.product` corregge una lacuna della prima analisi.
La separazione di `ke_product_dimensions` è corretta.

La maggiore fragilità attuale è la convivenza fra tre rappresentazioni dei
dati Amazon: campi sul template, campi sulla variante e righe listing per
marketplace. I percorsi non usano sempre la stessa rappresentazione.
Importare correttamente un catalogo non dimostra ancora la correttezza
dell'intero ciclo stock → ordine → consegna → fattura → riconciliazione.

## Contesto operativo e priorità chiariti dopo la revisione

L'utente utilizza attualmente **Full Import, Import FNSKU e import giacenze
FBA**; non invia prezzi, quantità o pubblicazioni ad Amazon. L'import
ordini non rientra nei flussi attuali indicati. I rilievi su quei percorsi
restano validi come backlog, ma non hanno la stessa urgenza operativa.

Il **magazzino FBA europeo è unico**: i marketplace del pool condividono
quel magazzino Odoo. L'ubicazione di transito è stata predisposta per una
futura automazione della comunicazione dei rifornimenti ad Amazon; tale
processo non è la priorità corrente. La sua giacenza effettiva non è stata
confermata in questa discussione: se vuota, il consumo spurio descritto
nel rilievo 1 non sottrae pezzi dal transito, ma il difetto di confronto
fra varianti resta da correggere.

Le priorità esplicite sono **giacenze, prezzi e stampa etichette**:

- Rendere idempotente la riconciliazione FBA per variante nel pool unico,
  senza coinvolgere implicitamente un processo inbound non ancora attivo.
- Rendere coerente la validità dell'FNSKU: preservarlo quando una sorgente
  non lo fornisce, invalidarlo al cambio SKU e verificarlo prima della stampa.
- Predisporre una stampa autonoma per variante e numero di copie, senza
  trasferimenti di magazzino obbligatori. È la direzione proposta per il
  prossimo sviluppo, non una funzione già implementata.
- Distinguere prezzo Amazon importato e prezzo Odoo; verificare le regole
  di calcolo prima di un'eventuale futura attivazione dell'invio ad Amazon.

Non sono necessari cambiamenti strutturali ai magazzini per compensare i
difetti rilevati. Automazione rifornimenti/inbound, ordini e casi di reti
FBA dedicate restano sviluppi successivi. Il dettaglio operativo e le
attività pendenti sono registrati all'inizio di `TASK.md`.

## Rilievi prioritari

### 1. P1 — Lo snapshot FBA pooled resta condiviso fra varianti

**Riferimento:** `services/sync_engine.py:3250-3278`.

Sul marketplace dedicato la quantità precedente è letta dalla listing
della variante. Sul pool viene ancora letta da
`product_tmpl.x_amazon_qty` e riscritta sullo stesso template dopo ciascun
ASIN. Lo snapshot di una sorella diventa quindi il precedente dell'altra.

**Riprodotto isolatamente:** due varianti A=10 e B=20, dati Amazon
invariati, snapshot condiviso iniziale 20. A scrive 10; B confronta 20 con
quel 10 e richiede un trasferimento di 10 pezzi dal transito. Il quant FBA
viene poi imposto al totale assoluto corretto, ma il transito disponibile
può essere consumato senza una nuova ricezione. Il problema può ripetersi
nei successivi cicli e marketplace pooled.

Serve una rilevazione precedente per variante e pool fisico, separata da
quantità lette dai report listing. Anche su un prodotto semplice,
l'aumento di disponibilità vendibile non prova da solo una ricezione
inbound: può derivare da sblocco delle riserve o resi.

### 2. P1 — Magazzini FBA dedicati, transito ed etichetta non sono coordinati

**Riferimenti:** `services/sync_engine.py:2950`, `:3022`;
`wizard/amazon_fba_prep_wizard.py:66`;
`models/product_variant.py:38`.

`_get_or_create_transit_location` usa un solo parametro globale
`ke_amazon_sync.transit_location_id`: una volta creata la location, ignora
il magazzino FBA richiesto. **Riprodotto isolatamente:** EU e UK ricevono
lo stesso ID transito. Un incremento UK può quindi consumare pezzi
destinati all'EU dello stesso prodotto.

Il wizard di spedizione permette di scegliere un marketplace, ma sul
picking non viene salvato quel riferimento. Il wizard di stampa chiama
`product._get_amazon_fnsku()` senza marketplace; l'helper restituisce
l'FNSKU condiviso della variante/template e non legge la listing dedicata.
Lettura del codice e helper riprodotto isolatamente: con FNSKU EU presente
viene restituito quello, anche se la spedizione era stata richiesta per
UK. Se manca il codice pooled, il codice dedicato presente sulla listing
non viene usato.

Servono destinazione/pool persistenti sul picking, transito distinto per
rete fisica e risoluzione dell'FNSKU coerente con la spedizione.

### 3. P1 — Quantità FBM diverse secondo il percorso usato

**Riferimenti:** `services/sync_engine.py:2288`;
`models/product_template.py:433`, `:513`.

Il motore periodico filtra correttamente per ubicazioni del magazzino,
ma somma la quantità lorda senza sottrarre le riserve. **Riprodotto
isolatamente:** 10 presenti e 7 prenotati restituiscono 10, non 3.
Manca inoltre un limite inferiore a zero prima dell'invio.

Il pulsante di sincronizzazione del singolo prodotto e la pubblicazione
leggono invece `with_context(warehouse=False).qty_available`: non usano
il magazzino FBM configurato. Per un prodotto con stock distribuito fra
FBM e FBA possono esporre anche pezzi non spedibili dal magazzino locale.

Va condiviso un unico calcolo della quantità vendibile fra cron, pulsanti
e pubblicazione. La scelta già concordata di condividere il magazzino
Generale/FBM con altri canali resta valida; non risolve però il conteggio
delle riserve e richiede una latenza di aggiornamento accettabile.

### 4. P1 — Gli ordini non vengono riconciliati riga per riga

**Riferimenti:** `services/sync_engine.py:3353-3410`, `:3520`, `:3659`.

Il recupero delle righe viene eseguito soltanto se `existing.order_line`
è vuoto. Le righe NON TROVATO sono note reali di ordine, quindi bastano a
impedire qualsiasi recupero successivo. **Riprodotto isolatamente:** un
ordine con una nota è contato come aggiornato senza richiedere le righe.

Lo stesso difetto riguarda ordini Pending importati con righe economiche
incomplete: i passaggi successivi aggiornano stato/data, non quantità e
prezzi. `x_amazon_order_item_id` viene memorizzato ma non usato come chiave
per riconciliare le righe.

La conferma automatica FBA richiede almeno una riga prodotto con prezzo
positivo, non che tutte le righe Amazon siano risolte: un ordine misto
può essere confermato e generare una fattura parziale, lasciando gli altri
articoli nelle note. Inoltre `_ORDER_STATUSES` esclude `Canceled` e la
ricerca usa CreatedAfter, non un cursore LastUpdatedAfter: cancellazioni e
modifiche di ordini fuori dalla finestra possono sfuggire.

Serve una riconciliazione idempotente per articolo, uno stato esplicito di
completezza e un blocco della conferma automatica quando mancano articoli.

### 5. P1 per mercati non EUR — Importi, imposte e totali non sono riconciliati

**Riferimenti:** `services/sync_engine.py:3427`, `:3500`, `:3659`;
`models/product_template.py:37-106`, `:513`.

La creazione ordine non sceglie il listino/valuta dal marketplace o da
Amazon; il partner e le impostazioni Odoo determinano quindi la valuta
effettiva senza una verifica. Un importo GBP può essere interpretato come
EUR se il listino risultante è in EUR. La sync prezzi secondaria riusa
lo stesso prezzo calcolato senza conversione valuta; la pubblicazione
usa esplicitamente EUR.

Il netto articolo è calcolato sottraendo ItemTax, ma le imposte applicate
da Odoo non vengono riconciliate con il paese e con i totali Amazon.
Spedizione, confezione regalo, promozioni e relativi importi fiscali non
sono trasformati in righe/aggiustamenti dal parser corrente. Non c'è un
confronto conclusivo con il totale ordine Amazon.

Il listino Amazon è interpretato da codice personalizzato, non dal motore
completo dei listini Odoo: non vengono valutati data di validità, quantità
minima, base alternativa, supplementi e margini. Non va quindi presentato
come supporto integrale a qualsiasi regola configurabile nell'interfaccia.

### 6. P1/P2 — I mapping marketplace non sono ancora la fonte unica

**Riferimenti:** `models/amazon_listing.py:15-81`;
`services/sync_engine.py:456-530`, `:3612`, `:3996`.

Il passaggio a `product.product` è corretto, ma resta il vincolo
`unique(product_id, marketplace_id)`: non rappresenta due offerte con SKU
diversi dello stesso prodotto nello stesso marketplace. Manca anche un
vincolo di unicità dello SKU nel marketplace.

Il matching ordini/FNSKU cerca i campi legacy SKU/ASIN, non la listing del
marketplace corrente. Se uno SKU è noto soltanto lì, l'ordine può non
essere abbinato; se uno SKU/ASIN compare su più prodotti, `limit=1` sceglie
un record senza segnalare l'ambiguità. Il fallback ASIN e la convenzione
`+` sono decisioni esistenti da preservare, ma non sostituiscono un mapping
verificato per marketplace.

Le mappe di import sono globali a tutti i marketplace e conservano un solo
record per SKU/ASIN/EAN. `_build_barcode_map` avvisa sulle collisioni ma
prosegue sostituendo il candidato precedente: con barcode equivalenti
duplicati può partire anche una fusione automatica sul candidato scelto.

La riconciliazione ricava inoltre mappe per template da listing per
variante; il dominio di `simple_products` non esclude le famiglie con
listing. Le famiglie possono quindi passare anche dal ramo dei semplici.
Da coprire con test mirati su famiglie con SKU diversi per marketplace.

### 7. P1/P2 — Validità dell'FNSKU gestita in modo incoerente

**Riferimenti:** `services/sync_engine.py:668-702`, `:1567`,
`:1787-1801`, `:3790-3877`.

`_upsert_listing` scrive `data.get('fnsku')` anche quando la sorgente non
contiene quel dato. **Riprodotto isolatamente:** una listing con FNSKU
valido lo perde dopo un aggiornamento report privo di `fnsku`, anche senza
cambio SKU. Questo interessa soprattutto la conservazione dei codici dei
marketplace dedicati.

Nel senso opposto, il Full Import può cambiare lo SKU sul prodotto o sulla
variante senza invalidare il relativo FNSKU condiviso. La riconciliazione
lo invalida in alcuni rami, ma `_update_product` e l'aggiornamento di una
variante esistente non applicano la stessa regola. Se l'import FNSKU
incatenato fallisce, rimane un codice precedente potenzialmente usabile in
stampa. Il matching FNSKU usa anche il fallback SKU con/senza `+`: righe
storiche e nuove possono così finire sullo stesso prodotto.

Occorre distinguere dato assente nella sorgente, dato invalidato da un
cambio identità e dato verificato sullo SKU corrente.

## Altri rischi da affrontare

### Consegne FBA automatiche — da verificare su Odoo 19

In `services/sync_engine.py:3552` il metodo degli ordini imposta le quantità
ma non `move.picked = True`; nei trasferimenti interni e nel consumo del
transito quel flag viene impostato esplicitamente, con un bug Odoo 19 già
documentato dal progetto. Manca anche una verifica finale dello stato del
picking; un ordine non più draft non viene ritentato dal ramo successivo.
Il rischio di consegna rimasta aperta è concreto, ma non è stato provato
qui sul runtime Odoo.

### Thread, concorrenza e transazioni

I thread daemon non sono persistenti; il watchdog è una mitigazione, non
una coda con ripresa. I controlli di avvio leggono/scrivono parametri senza
lock atomico. Manca un vincolo univoco su `x_amazon_order_id`.

Lo stop libera subito lo stato globale mentre un vecchio thread può ancora
elaborare il marketplace corrente. Un nuovo avvio può sovrapporsi e
riutilizzare gli stessi parametri; il loop FBA non controlla uno stop a
ogni pagina. La sync prezzi da cron gira ancora in un solo tick su tutti
gli SKU, con PATCH singole e retry lunghi su alcuni endpoint.

Molti cicli intercettano Exception senza savepoint per l'unità elaborata:
un errore SQL può lasciare la transazione inutilizzabile e impedire anche
la registrazione del log finale. Il savepoint sulla creazione ordine è un
miglioramento effettivo, ma non copre l'aggiornamento di ordini esistenti
né tutti gli altri cicli.

### Esiti API e rilevamento marketplace

Le PATCH prezzo/quantità vengono contate come riuscite se non sollevano
eccezioni; i chiamanti non analizzano status/issues del risultato come fa
invece la pubblicazione. Accettazione della richiesta e stato effettivo
dell'offerta restano da distinguere. `_with_retry` ritenta anche errori API
permanenti; il delay per chiamata non è un limite coordinato fra thread.

In `detect_active_marketplaces`, un marketplace nuovo viene creato senza
`active=True`, benché il default del modello sia False e il log dica
«CREATO E ATTIVATO». Inoltre `currency` restituita dal client non viene
usata per valorizzare il nuovo record. I marketplace seedati già esistenti
seguono un altro ramo e possono essere attivati correttamente.

### Etichettatura e audit

La scansione incrementa il conteggio prima di sapere se PDF e stampa sono
riusciti. Lo storico prova la richiesta di etichetta, non la stampa fisica.
Le scansioni concorrenti non hanno una protezione atomica sul conteggio;
le azioni del wizard non verificano esplicitamente picking aperto e
destinazione FBA. Nascondere il pulsante nella vista non costituisce un
vincolo applicativo.

La ristampa forzata funziona solo quando la quantità della riga è già
completa: non risolve la ristampa dell'ultima etichetta fallita a metà
della preparazione senza incrementare il conteggio.

### Migrazione, fusione e multi-azienda

È presente una migrazione `19.0.1.1.0/post-migrate.py`: elimina le listing
di famiglia e ripunta quelle semplici da template a variante. Serve un
collaudo di upgrade con dati preesistenti e ID template/variante diversi,
compresi vincoli FK e unicità; la sola installazione pulita non lo copre.
L'eliminazione perde anche i flag sync delle vecchie listing di famiglia.

La fusione manuale→variante è intenzionale e protetta da savepoint,
riserve e movimenti aperti. Tuttavia trasferisce quantità per prodotto e
location senza conservare lotto, proprietario e package; lo stesso limite
vale per la rettifica stock che legge un solo quant. Va limitata ai casi
compatibili con queste precondizioni. Non reindirizza i riferimenti
storici degli ordini al prodotto archiviato.

Configurazioni magazzino e credenziali sono globali su
`ir.config_parameter`, anche quando esposte su `res.company`. I modelli
listing/marketplace/report non separano gli account e non definiscono un
isolamento aziendale dedicato. Il codice attuale è quindi sostanzialmente
mono-account; non va considerato un connettore multi-account/multi-azienda.

## Documentazione e architettura proposta

| Documento | Confronto con il codice |
|---|---|
| README | Corretto il riferimento iniziale da Odoo 17 a 19 durante questa analisi; dipendenze e struttura ancora incomplete; descrizioni FBA/IVA più assolute di quanto garantisca il codice. |
| TASK | Memoria utile, ma mescola stato attuale e cronologia superata; esistono test nonostante AGENTS/CLAUDE dicano il contrario. |
| REVIEW_AMAZON.md | Revisione storica con numeri di riga vecchi; molti rilievi già corretti, snapshot pooled per varianti ancora problematico. |
| Piano thread | Implementazione presente, benché l'intestazione dica «nessun codice scritto»; non è stato introdotto un esecutore condiviso per tutti i flussi. |
| Piano report ordini | Flusso ibrido implementato; manca la riconciliazione completa degli articoli e la separazione per regione se si attivano mercati di regioni diverse. |
| Piano Feeds | Ancora proposta: i prezzi/quantità continuano con PATCH per SKU, senza sincronizzazione solo dei dati cambiati. |
| Piano etichettatura | Implementata la parte PDF/scansione; agente Windows/ZPL, cartoni, piano inbound e riconciliazione inbound non presenti. |
| PREVISIONE architettura | Direzione utile per offerte multiple, staging decisionale e job persistenti; account/catalog item/offer separati e staging per riga non sono l'architettura attuale. |

Il documento PREVISIONE centra il problema della distinzione fra prodotto
fisico, ASIN e offerta. La priorità non è creare subito tutti i modelli
proposti: conviene prima rendere coerenti mapping, stock e ordini nel
connettore esistente, quindi migrare gradualmente. Cambiare il matching
ASIN/SKU `+` o la gestione del codice interno richiede una decisione
esplicita successiva: qui non viene modificato il comportamento concordato.

## Verifiche eseguite e limiti

- Parsing AST: **39 file Python** dei due addon validi.
- Parsing XML: **25 file** ben formati; non equivale a validazione delle
  viste risolte o dello schema Odoo.
- File dichiarati nei manifest: presenti.
- **43 metodi di test Odoo presenti**, principalmente matching, listing
  per variante, FNSKU e log incatenati. Non eseguiti in questa sessione.
- **6 riproduzioni isolate** con metodi reali estratti via AST: snapshot
  FBA fra sorelle, disponibilità FBM, perdita FNSKU da upsert, transito
  globale, recupero ordine con nota e scelta FNSKU senza marketplace.
  Script temporaneo: `/tmp/odoo_amazon_review_20261027.py`.
- Odoo non importabile nel Python disponibile; Docker esistente nel
  filesystem ma socket non accessibile. Nessun test funzionale eseguito.
- Nessuna chiamata Amazon; nessuna verifica corrente dei contratti SP-API.

Prima di automatizzare l'intero flusso: provare inventario invariato con
due sorelle, spedizioni EU/UK separate, ordini misti abbinati/non abbinati,
passaggio Pending→Shipped, valute diverse, cancellazioni, stop/riavvio e
upgrade del modulo con dati storici. I test esistenti non coprono ancora
questo insieme di comportamenti.
