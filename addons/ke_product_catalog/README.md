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
2. **Proponi categorie** (Inventario → Prodotti, o dalla lista prodotti: Azioni): applica le
   regole ai prodotti di alcune categorie o a quelli selezionati.
3. **Proposte di categoria** (Inventario → Prodotti): categoria attuale, **categoria proposta
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

### Esporta / Importa classificazione
Per riportare la classificazione da un database all'altro (es. dal locale alla produzione):
file CSV Codice;EAN;Prodotto;Categoria (percorso completo). Prodotti riconosciuti per codice
(del prodotto o di una variante, senza il "+" finale) o EAN; con più candidati vince l'attivo,
poi il codice identico; categorie mancanti create; "Solo verifica" di default.

## Chi scrive cosa
Le regole tra Amazon, catalogo fornitore e valori manuali (testi, specifiche, peso e
dimensioni) sono nel README di `ke_amazon_sync`, sezione "Chi scrive cosa".
