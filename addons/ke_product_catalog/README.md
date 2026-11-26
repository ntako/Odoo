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
   quelle degli ami (2026-11-24) e le sottocategorie che servono, senza doppioni. Si può
   ricaricare dopo un aggiornamento del modulo: aggiunge solo le regole nuove.

   **Ami** (Pesca / Ami, prima Pesca / Minuteria / Ami): 12 sottocategorie come Nencini Sport (Acque interne, Ami
   legati, Mare, Drifting bolentino e traina, Assist hook, Ancorette, Spinning, Jig head, Sabiki,
   Siluro, Carpfishing, Trota), nell'ordine delle loro regole (`docs/nencini_ami_regole.md`):
   legato → sabiki → testa piombata → assist → ancoretta → per artificiali → siluro/carpa/trota e
   serie note → circle/girella/traina → acque interne o mare dalle parole del nome. Categoria del
   fornitore per tutti (Tubertini "Ami Legati", "Ancorette"…; Trabucco `002/…_L` = legati,
   `002/…_P` = ami sciolti, `019/KK-110` = K-Karp); parole nel nome solo per i prodotti in Ami
   (un artificiale "con ancorette" non diventa un'ancoretta). Serie Tubertini come le classifica
   Nencini (letto dal loro sito il 24/11/2026); Trabucco Pro Sword, XPS e Lucifer = acque interne.
   **Albero come Nencini Sport (2026-11-26, una categoria alla volta).** *Carica regole
   predefinite* porta anche l'albero: una categoria che cambia nome si rinomina (tiene prodotti,
   regole e corrispondenze Amazon); una che sparisce si unisce alla nuova (ogni riferimento passa
   alla nuova, poi si cancella); le regole predefinite di prima si disattivano. Fatti:
   - **Mulinelli**: Frizione anteriore, Frizione posteriore, Casting, Surfcasting, Carpfishing,
     Traina e drifting, Jigging, Elettrici, Bobine, Mosca, Manuali, Salpapalamito. Spinning →
     Frizione anteriore, Ricambi e bobine → Bobine, Carpfishing e feeder → Carpfishing, Traina
     jigging e big game → Traina e drifting; Bolognese e match e Trota e area unite in Frizione
     anteriore. Categoria del fornitore debole dove il nome precisa (un Penn "Spinning Reels" con
     "Surf" nel nome va in Surfcasting).
   - **Canne**: le 22 di Nencini (All round, Bolentino, Bolognesi, Carpfishing, Casting, Da viaggio,
     Eging, Feeder e ledgering, Fisse, Inglesi e match, Jigging e slow pitch, Luccio e siluro, Mosca,
     Per mulinelli elettrici, Spinning, Spondini e roubaisienne, Surfcasting e beach ledgering,
     Traina e drifting, Trota lago, Trota torrente, Trout area, Vette). Rinominate: Surfcasting,
     Carpfishing e feeder → Carpfishing, Traina e big game → Traina e drifting, Bolentino e
     jigging → Bolentino, Bolognese match e fisse → Bolognesi, Trota e area → Trota lago. "Casting"
     cede all'uso (Boat → Bolentino, Trolling → Traina, Vertical → Jigging).
   - **Fili**: Big game, Cavetti in acciaio, Code, Fluorocarbon, Fluorocoated, Nylon, Shock leader,
     Trecciati. Monofilo → Nylon, Terminali e leader → Shock leader.
   - **Ami**: diventa categoria principale come da Nencini (Pesca / Minuteria / Ami → **Pesca / Ami**,
     spostata con le sue sottocategorie: stesso record). Regole sul nome ritoccate sui 2.817 titoli
     Nencini del 26/11 (rig, bait band, leader e cm = legati; baitholder = mare; strong single =
     spinning): concordano con Nencini 1.396 volte su 1.661 dove decidono.
   - **Esche artificiali**: Egi, Jigs, Hard baits, Soft baits, Wire baits (cucchiaini, spinner,
     spinnerbait). Calamaro → Egi, Jig e metal → Jigs, Hard bait e altre → Hard baits, Siliconiche →
     Soft baits, Cucchiaini e spinner → Wire baits; Superficie unita a Hard baits. Pure Fishing dice
     solo "Hard Bait" anche per spinner e ondulanti: decide il nome, più le serie che il nome non dice
     (Droppen, Reflex, Toby = Wire; Seeker, Sillen, Cast Hacker = Jigs).
   - **Esche e pasture** (prima "Esche naturali, aromi e pasturazione"): Inneschi pronti, Pasture
     liquide, Sfarinati, Pellet, Boiles, Mais, Colla. I non-esche finiti qui dalla categoria Amazon
     "Aromi attrattivi" (mulinelli, ami, fili…) escono con le stesse regole di mulinelli e canne.
   - **Piombi**: categoria principale (Pesca / Minuteria / Piombi → **Pesca / Piombi**) con le 21
     sottocategorie di Nencini (Pasturatori, Con anello inox, Plastificati, Foro passante,
     Carpfishing, Pallini spaccati, Screw-in, Torpille e sfere, Sonde, Bullet, Drop shot, Cheburashka,
     Bottom weights, Temolino, Nail, Guardiani, Mascotte, Sgancio rapido, Bolentino, Per affondatore,
     Filo piombo). I pasturatori finiti in altre categorie vengono proposti qui.
   - **Galleggianti**: categoria principale (Pesca / Minuteria / Galleggianti → **Pesca /
     Galleggianti**): Acque interne, Mare, Bombarde, Scorrevoli, Accessori, Trota lago, Inglese,
     Galleggianti pasturatori. Le bombarde finite altrove (es. in Esche e pasture) vengono proposte qui.
   - **Minuteria**: Girelle, Moschettoni, Attacchi, Split ring, Perline, Componenti per il montaggio,
     Anti-tangle, Stopper, Crimps, Solid ring, Terminali. Girelle moschettoni e anellini → Girelle,
     Montature e altro → Terminali.
   - **Reggicanne e supporti** (nuova, prima dentro Accessori / Carpfishing e postazione): Rod pod,
     Reggicanne da sedia, Picchetti e puntali, Tripodi e treppiedi, Poggia kit.
   - **Panieri** (nuova): Panchetti, Moduli, Piatti laterali, Pedane, Bracci, Cassoni surfcasting,
     Carrelli e kit trasporto, Staffe per ombrelloni, Accessori. Trabucco 011/SEAT-3 (famiglie
     "GENIUS", "GNT STATION" che mescolano accessori diversi) → Accessori.
   - **Valigette e scatole** (Contenitori e borse / Cassette e scatole spostata qui): Valigette, Porta
     artificiali, Porta minuteria e varie, Porta mosche, Porta ami, Porta terminali.
   - **Guadini, nasse e bilance** (Accessori / Guadini e reti spostata qui): Guadini, Teste di guadino,
     Pali per guadino, Nasse. Le bilance restano in Accessori, come da Nencini.
   - **Borse e foderi** (prima "Contenitori e borse"): Borse generiche (prima Borse e zaini),
     Portacanne (prima Foderi e portacanne), Borse porta artificiali (prima Borse e secchi per esche),
     Borse EVA, Borse carpfishing (Prologic/JRC: Avenger, Defender, Element…), Borse frigo, Portanasse.
   - **Kit pesca** (prima "Combo canna e mulinello"): 11 sottocategorie. Come Nencini, soprattutto per
     mulinello (Spinning, Casting); per tecnica solo Carpfishing, Feeder e ledgering, Surfcasting,
     Mosca, Traina (combo rotanti), Bolognesi. "Boat", "Trout", "Pike" sono deboli: cedono al tipo di
     combo del fornitore. Un "Combo" finito in Mulinelli o Canne viene proposto qui.
   - **Abbigliamento** (nuova, **Pesca / Abbigliamento**): 13 sottocategorie di Nencini (Giacche e
     pantaloni, T-shirt, Felpe, Pantaloni e salopette, Cappelli, Occhiali, Intimo, Guanti, Completi,
     Gilet, Pile e softshell, Polo, Camicie). Ci va solo l'abbigliamento da pesca riconosciuto dal
     fornitore (Pure Fishing Apparel/Eyewear, Tubertini, Trabucco, K-Karp); quello generico resta in
     "Abbigliamento e calzature" (scelta dell'utente, da rivedere).
   - **Stivali e waders** (nuova, **Pesca / Stivali e waders**): Scarpe da wading, Stivali, Waders
     neoprene, Waders PVC, Waders traspiranti. Come l'abbigliamento: solo da pesca (fornitore "Wading
     Gear", o wader/wading/scafandro nel nome). Il materiale conta solo per un wader (uno stivale in
     neoprene è uno stivale); i cosciali vanno in Stivali; le giacche da wading in Abbigliamento.
   - **Attrezzatura da campo** (nuova): Cura del pescato, Lettini, Sedie, Tende, Ombrelloni, Sacchi a
     pelo, Tavolini; solo l'attrezzatura da pesca (fornitore Furniture, Tents and Shelters, Unhooking
     Mats, Sacks and Slings). Fornelli e affumicatori restano in Campeggio; le borse frigo Pure Fishing
     vanno in Borse e foderi / Borse frigo.
   - **Belly boat** (nuova, senza sottocategorie): belly boat, kayak e barchini da pesca con pinne,
     pompe, supporti per ecoscandaglio e portacanne da barca (Pure Fishing "Boats and Motors" e "Rod
     Holders"; prima in Nautica / Barche e motori).
   - **Accessori**: le 21 sottocategorie di Nencini. Pinze coltelli e attrezzi → Pinze e forbici,
     Segnalatori di abboccata → Avvisatori e segnalatori, Bilance e misure → Bilance; Manutenzione
     mulinelli, Altri accessori e il resto di Carpfishing e postazione unite in Accessori vari, che
     raccoglie (debole) anche tutto quello che non ha un posto più preciso.

   *Carica regole predefinite* aggiunge alle regole già create le categorie "Si applica a" comparse
   dopo (una regola nata quando una di quelle categorie non c'era ancora non resta incompleta).

   Nomi come categorie Amazon ("Guanti", "Accessori", "Sonde"…): dal 2026-11-26 la sync Amazon
   (`ke_amazon_sync` 19.0.1.6) non sposta e non cancella le categorie dell'albero di classificazione,
   quindi le proposte verso di esse non sono più "da verificare" per questo motivo.

   Le regole predefinite di prima restano solo se uguali a una di adesso (priorità, testo, categoria,
   debole, eccezione); prima le regole "non è una canna" (ami, fili, borse… finiti in Canne) non
   venivano create perché uguali a quelle dei mulinelli.

   Gli ami sciolti che il nome non distingue (Hisashi senza "SW", Shinken, Lineaeffe LF) restano
   in Ami, da sistemare a mano o con una regola nuova.
2. **Proponi categorie** (Inventario → Configurazione → Prodotti, o dalla lista prodotti: Azioni): applica le
   regole ai prodotti di alcune categorie o a quelli selezionati.
3. **Proposte di categoria** (Inventario → Configurazione → Prodotti): categoria attuale, **categoria proposta
   (modificabile)**, motivo, *Sicura*, *Sul sito*, *Nome come categoria Amazon*. Filtri
   "Da verificare" / "Sicure", raggruppamento per categoria; **Applica** o **Scarta** sulle
   righe selezionate. Applicare cambia solo la categoria Odoo del prodotto.

Come si sceglie la categoria: vince la prima regola non debole (in ordine di priorità); due
regole non deboli in disaccordo → *da verificare*, e il motivo dice quale regola dice altro.
Una categoria e una sua sottocategoria non sono in disaccordo: vale la più specifica.
Se la categoria trovata ha regole sue (*Si applica a*), si provano subito: un amo appena importato
va direttamente in "Ami / Mare" senza applicare e riproporre. Nessuna proposta verso una categoria
più generica di quella attuale (un amo già in "Ami / Mare" non torna in "Ami").

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
