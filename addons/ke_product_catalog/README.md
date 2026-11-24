# KE Product Catalog

Dati di catalogo comuni a tutti i canali (Amazon, cataloghi fornitore, PrestaShop),
indipendenti da ciascuno di essi. Gli altri moduli li compilano (`ke_amazon_sync`,
`ke_inventory_import`) o li usano (`ke_prestashop_sync`).

## Funzionalità

### Marca
Un solo nome in Odoo per ogni marca, con i nomi alternativi con cui arriva da Amazon, dai
fornitori o dalle fatture. Gli import la riconoscono o la creano "Da verificare"; unione di
marche e import della tabella delle corrispondenze.

### Galleria immagini
Le immagini come indirizzi (`ke.product.media`), una serie per fonte (Amazon, fornitore,
manuale); in Odoo resta scaricata solo l'immagine principale.

### Prezzo per variante
Se compilato vale al posto del prezzo della famiglia (listini, ordini, canali).

### Testi del sito (scheda "Sito web")
Nome sul sito, meta title/description, URL, descrizione breve e completa, per lingua.
"Proponi testi sito" li compila dai testi del prodotto. Ordine delle fonti per la proposta:
1. testi scritti nella scheda "Sito web";
2. **testi del catalogo fornitore** nella lingua (vedi sotto);
3. descrizione di vendita (Amazon o scritta a mano).

**Nome sul sito** (formato scelto il 2026-11-17): marca + nome commerciale del fornitore +
tipo di prodotto nella lingua, es. "Berkley Sick Braid – Filo trecciato" / "– Geflochtene
Schnur". Il tipo viene dalla sottocategoria del fornitore (`spec_rules.PRODUCT_KINDS`) e non si
aggiunge se il nome lo contiene già ("… Low Profile Reel"). Un "Nome sul sito" scritto a mano
vale sempre per primo. Il nome Odoo non cambia (lo usano Amazon e le pubblicazioni).

### Catalogo fornitore (scheda "Catalogo fornitore")
Nome commerciale, codice, categoria, testo e punti elenco del fornitore per lingua
(`ke_supplier_*`). Li scrive l'import CSV di `ke_inventory_import` in modalità
"Solo arricchimento prodotti esistenti" (preset "Pure Fishing EMEA (arricchimento)").
La descrizione di vendita resta com'è.

### Specifiche tecniche (scheda "Specifiche")
Valori per prodotto o per variante (nelle famiglie diametro, carico, misura cambiano da
variante a variante), tradotti, con la fonte: Amazon, Fornitore, Manuale.
- **Priorità: manuale > fornitore > Amazon.** Una fonte non sovrascrive mai i valori di una
  più affidabile e toglie solo i valori suoi.
- Elenco delle specifiche e nomi in 5 lingue, unità uniformate (g/kg, mm, m), valori fissi
  riconosciuti in qualsiasi lingua e tradotti (tecnica, mano, tipo di filo, acqua,
  galleggiamento…): `spec_rules.py`, usato da Amazon e dai cataloghi fornitore.
- Inventario → Configurazione → Prodotti → **Specifiche tecniche**: ordine, nome tradotto e
  "Mostra sul sito" (per nascondere una specifica senza cancellarla).

### Classificazione dei prodotti in Odoo
Per mettere i prodotti nella categoria giusta (il menu del sito ricalca le categorie Odoo).

1. **Regole di classificazione** (Inventario → Configurazione → Prodotti), modificabili:
   - *Guarda*: categoria del fornitore ("Categoria / Sottocategoria"), tecnica (dalle
     specifiche), categoria Amazon o parole nel nome;
   - *Testo da trovare*: espressione regolare, senza maiuscole/minuscole (alternative con `|`);
   - *Tranne se il nome contiene*: eccezione (es. un mulinello con il filo incluso non è un filo);
   - *Si applica a*: solo i prodotti di certe categorie e sottocategorie (vuoto: tutti);
   - *Categoria proposta*, *Priorità*;
   - *Debole*: cede a qualsiasi altra regola che trova una categoria diversa (es. la tecnica
     "Spinning" o "Casting" di Amazon, data anche ai mulinelli da surf).

   "**Carica regole predefinite**" crea quelle messe a punto su mulinelli e canne (circa 90),
   e le sottocategorie che servono, senza doppioni.
2. **Proponi categorie** (Inventario → Configurazione → Prodotti, o dalla lista prodotti: Azioni): applica le
   regole ai prodotti di alcune categorie o a quelli selezionati.
3. **Proposte di categoria** (Inventario → Configurazione → Prodotti): categoria attuale, **categoria proposta
   (modificabile)**, motivo, *Sicura*, *Sul sito*, *Nome come categoria Amazon*. Filtri
   "Da verificare" / "Sicure", raggruppamento per categoria; **Applica** o **Scarta** sulle
   righe selezionate. Applicare cambia solo la categoria Odoo del prodotto.

Come si sceglie la categoria: vince la prima regola non debole (in ordine di priorità); due
regole non deboli in disaccordo → *da verificare*, e il motivo dice quale regola dice altro.
Una categoria e una sua sottocategoria non sono in disaccordo: vale la più specifica.

Effetti sugli altri moduli:
- **Amazon**: le categorie Amazon e le "Corrispondenze categorie" non cambiano. La sync Amazon
  sposta un prodotto solo dalla categoria predefinita o verso una sottocategoria di quella in
  cui è; un prodotto messo in una sottocategoria più precisa non torna indietro. Fa eccezione
  una categoria che si chiama come una categoria Amazon e non è usata dalle corrispondenze:
  la proposta lo segnala ("Nome come categoria Amazon").
- **PrestaShop**: un prodotto spostato in una sottocategoria più precisa segue la corrispondenza
  della categoria Odoo (Odoo come riferimento dell'albero del sito).

### Famiglie del fornitore (2026-11-20)
Le famiglie nate da Amazon spesso sono sbagliate: la stessa canna in più prodotti separati, o un
prodotto che contiene modelli diversi come varianti (es. "Berkley PowerBait" con 8 esche diverse).
- L'**arricchimento** dal catalogo fornitore salva su ogni **variante** il codice e il nome della
  famiglia del fornitore (Pure Fishing: "Master code" / "Master Description"; destinazioni di
  colonna "Codice famiglia fornitore" e "Nome famiglia fornitore", già nel preset Pure Fishing).
  Sulla variante, non sul prodotto, perché un prodotto Odoo può oggi mescolare più famiglie.
  L'abbinamento per codice vale più di quello per EAN; un EAN che porta a famiglie diverse non
  scrive nulla (conteggiato nel riepilogo).
- **Inventario → Configurazione → Proposte di famiglia**, "Calcola proposte": una proposta per
  ogni famiglia del fornitore che in Odoo non sta in un prodotto solo, da sola — *Unisci*
  (varianti sparse su più prodotti), *Separa* (dentro un prodotto con altre famiglie),
  *Riorganizza* (entrambe). Prodotto di destinazione: quello dove la famiglia è la principale;
  un prodotto va a una sola famiglia, vuoto = prodotto nuovo. "Da sapere": varianti non nel
  file (restano dove sono), più famiglie Amazon coinvolte, prodotti già su PrestaShop che si
  svuoterebbero. Una proposta scartata non ricompare finché le sue varianti non cambiano.
- **Applica** (dalla lista, sulle proposte scelte, o dalla scheda): una proposta alla volta, per intero
  o niente. Prima controlla che le varianti della famiglia siano ancora quelle del calcolo (se no:
  "Calcola proposte"). Le varianti si **spostano**, non si ricreano: giacenze, movimenti, ordini,
  inserzioni Amazon, EAN, codici e costi restano attaccati. Seguono la variante anche le specifiche
  (quelle del vecchio prodotto diventano sue), la galleria e l'immagine di un vecchio prodotto
  semplice, il prezzo (se diverso da quello della famiglia diventa il prezzo della variante) e il
  listino acquisti. La famiglia ha un solo attributo che crea varianti (come quelle della sync
  Amazon): le varianti arrivate prendono come valore la loro etichetta di prima (o il codice).
  Prodotti rimasti vuoti: archiviati, mai cancellati. Famiglia nuova (nessun prodotto libero): nome
  marca + famiglia del fornitore, dati base dal prodotto da cui arriva la maggior parte delle
  varianti. "Nome fornitore" della famiglia = nome della famiglia del fornitore.
- **Nomi** (applicati con la proposta, o da *Nomi dal catalogo fornitore* per tutte le famiglie, con
  "Solo verifica"): per un prodotto le cui varianti sono tutte di una famiglia del fornitore, "Nome
  fornitore" = famiglia; nome del prodotto in ogni lingua = nome sul sito (marca + famiglia + tipo,
  es. "Berkley Flex Carp – Filo monofilo"); nomi delle varianti = le specifiche che le distinguono,
  il meno possibile, in ordine misura, misura amo, diametro, lunghezza, potenza, grammatura… (Flex
  Carp: "0,30 mm"), tradotte come le specifiche; due varianti uguali in una lingua → il codice.
  Attributo della specifica se ne basta una ("Diametro"), altrimenti "Variante"; la vecchia riga
  attributo si archivia (si tiene l'attributo vecchio se varianti archiviate ne usano i valori).
  Nomi tradotti scritti con l'inglese di base per ultimo (scrivere una lingua su un nome che ha solo
  l'inglese riscrive anche l'inglese).
- **Amazon** (ke_amazon_sync): SKU/ASIN/FNSKU di un prodotto semplice passano alla sua variante; la
  famiglia prende il parent ASIN di una famiglia Amazon svuotata o uno interno "ODOO-FAM-<id>" (mai
  mandato ad Amazon); i parent ASIN delle famiglie Amazon svuotate restano come "Altri parent ASIN":
  il Full Import vi porta le loro nuove varianti invece di ricreare la famiglia. Su Amazon non cambia
  niente.
- **PrestaShop**: le varianti spostate perdono la combinazione del vecchio prodotto PS; il prossimo
  export le ricrea sotto la famiglia, disattiva con redirect 301 il vecchio prodotto PS separato della
  variante e (export completo) i prodotti archiviati. Varianti rinominate: `ps_options_dirty`, l'export
  aggiorna i valori attributo della combinazione già creata.

### Esporta / Importa classificazione
Per riportare la classificazione da un database all'altro (es. dal locale alla produzione):
file CSV Codice;EAN;Prodotto;Categoria (percorso completo). Prodotti riconosciuti per codice
(del prodotto o di una variante, senza il "+" finale) o EAN; con più candidati vince l'attivo,
poi il codice identico; categorie mancanti create; "Solo verifica" di default.

## Chi scrive cosa
Le regole tra Amazon, catalogo fornitore e valori manuali (testi, specifiche, peso e
dimensioni) sono nel README di `ke_amazon_sync`, sezione "Chi scrive cosa".
