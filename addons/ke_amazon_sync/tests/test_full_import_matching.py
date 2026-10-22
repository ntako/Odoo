"""
Test di regressione per il matching SKU/ASIN del Full Import
(full_import_report_rows, _process_variant_listing).

Fonte: nono/decimo/undicesimo/dodicesimo bug trovati e corretti il
2026-10-09/12/13 (vedi TASK.md) — ognuno codifica qui lo scenario esatto
che li ha fatti scoprire, per evitare regressioni durante il refactor di
centralizzazione della logica di matching.

Ogni chiamata a full_import_report_rows è avvolta in `no_commit(self.env)`
(vedi common.py) perché quella funzione fa `env.cr.commit()` internamente —
senza questo, Odoo solleva un'eccezione esplicita invece di permettere la
scrittura, per proteggere il rollback automatico di fine test di
TransactionCase. Con `no_commit` il rollback standard pulisce tutto da solo,
nessun cleanup manuale necessario.
"""
from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestFullImportMatching(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        self.SyncEngine = SyncEngine
        # _is_primary_marketplace() con marketplace_id primario vuoto si
        # comporta sempre come "primario" (vedi sync_engine.py) senza dover
        # dare a FakeSpApiClient un attributo marketplace_id — deterministico
        # a prescindere da cosa sia configurato nel DB di test.
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')

    def _make_log(self, name):
        return self.env['ke.amazon.sync.log'].create({'name': name, 'sync_type': 'full_import'})

    def _run(self, eng, log, rows):
        with no_commit(self.env):
            return eng.full_import_report_rows(log, rows, 0, 10, True)

    # ------------------------------------------------------------------
    # Sanity di base
    # ------------------------------------------------------------------

    def test_simple_product_created(self):
        """Riga senza relazioni di variante -> prodotto semplice normale."""
        sku = 'ZZT-SIMPLE-SKU'
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self._make_log('test simple')
        self._run(eng, log, [make_report_row(sku, asin='B0ZZSIMPLE1')])

        tmpl = self.env['product.template'].search([('default_code', '=', sku)])
        self.assertEqual(len(tmpl), 1)
        self.assertFalse(tmpl.x_amazon_parent_asin)

    def test_variant_family_created(self):
        """Riga con relazione VARIATION -> famiglia + 1 variante."""
        sku = 'ZZT-FAMILY-SKU'
        parent = 'B0ZZFAMILYPARENT'
        client = FakeSpApiClient({'B0ZZFAMILYCHILD': parent})
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test famiglia')
        self._run(eng, log, [make_report_row(sku, asin='B0ZZFAMILYCHILD')])

        tmpl = self.env['product.template'].search([('x_amazon_parent_asin', '=', parent)])
        self.assertEqual(len(tmpl), 1)
        self.assertEqual(len(tmpl.product_variant_ids), 1)

    # ------------------------------------------------------------------
    # Nono bug (2026-10-09): semplice creato, poi riconosciuto come
    # variante -> deve promuoversi sul posto, non duplicarsi.
    # ------------------------------------------------------------------

    def test_simple_then_variant_promotes_in_place(self):
        sku = 'ZZT-PROMO-SKU'
        asin = 'B0ZZPROMOCHILD'
        parent = 'B0ZZPROMOPARENT'
        log = self._make_log('test promozione')

        # Primo giro: nessuna relazione nota -> creato come semplice.
        eng_no_cat = self.SyncEngine(self.env, FakeSpApiClient())
        self._run(eng_no_cat, log, [make_report_row(sku, asin=asin)])

        simple_tmpl = self.env['product.template'].search([('default_code', '=', sku)])
        self.assertEqual(len(simple_tmpl), 1)
        simple_id = simple_tmpl.id

        # Secondo giro (altro marketplace/chunk): il Catalog API rivela
        # stavolta la relazione di variante per lo stesso ASIN.
        eng_with_cat = self.SyncEngine(self.env, FakeSpApiClient({asin: parent}))
        self._run(eng_with_cat, log, [make_report_row(sku, asin=asin)])

        count = self.env['product.template'].search_count([('default_code', '=', sku)])
        self.assertEqual(count, 1, 'deve restare un solo template, promosso sul posto')
        promoted = self.env['product.template'].search([('x_amazon_parent_asin', '=', parent)])
        self.assertEqual(promoted.id, simple_id, 'il template promosso deve essere lo stesso id del semplice originale')

    def test_family_with_sibling_conflict_is_skipped_not_merged(self):
        """
        Famiglia già creata da un fratello + un prodotto semplice separato
        con lo stesso SKU. Dal 2026-10-22 il semplice viene unito nella
        variante (vedi test_simple_with_same_sku_merged_when_family_exists),
        ma solo se è sicuro: con giacenza riservata deve ancora solo
        segnalare, senza toccare nulla.
        """
        sku = 'ZZT-RARECASE-SKU'
        parent = 'B0ZZRAREPARENT'

        sibling_tmpl = self.env['product.template'].create({
            'name': 'ZZT Famiglia fratello', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': parent,
        })
        conflicting_simple = self.env['product.template'].create({
            'name': 'ZZT Semplice in conflitto', 'type': 'consu', 'is_storable': True,
            'default_code': sku, 'x_amazon_sku': sku,
        })
        location = self.env.ref('stock.warehouse0').lot_stock_id
        self.env['stock.quant'].sudo()._update_available_quantity(
            conflicting_simple.product_variant_id, location, 3,
        )
        self.env['stock.quant'].sudo().search([
            ('product_id', '=', conflicting_simple.product_variant_id.id), ('location_id', '=', location.id),
        ]).reserved_quantity = 1

        eng = self.SyncEngine(self.env, FakeSpApiClient({'B0ZZRARECHILD': parent}))
        row = make_report_row(sku, asin='B0ZZRARECHILD')

        from odoo.addons.ke_amazon_sync.services.sync_engine import _MatchMaps
        maps = _MatchMaps(self.env, {})
        maps.by_parent_asin[parent] = sibling_tmpl
        maps.by_sku[conflicting_simple.x_amazon_sku] = conflicting_simple
        maps.by_default_code[conflicting_simple.default_code] = conflicting_simple

        product = eng._parse_listing_report(row)
        product['parent_asin'] = parent
        product['variation_attr'] = ('Colore', 'Blu')

        res, msg = eng._process_variant_listing(product, parent, 'consu', maps, True)

        self.assertEqual(res, 'skipped')
        self.assertIn('serve fusione manuale', msg)
        self.assertTrue(conflicting_simple.active)
        sibling_tmpl.invalidate_recordset()
        self.assertEqual(len(sibling_tmpl.product_variant_ids), 1, 'nessuna variante deve essere stata aggiunta alla famiglia del fratello')

    # ------------------------------------------------------------------
    # Decimo bug (2026-10-09, parte 2): riga con ASIN vuoto per uno SKU
    # già-variante -> skip, mai _update_product su un template famiglia.
    # ------------------------------------------------------------------

    def test_empty_asin_row_skips_known_variant_sku(self):
        sku = 'ZZT-EMPTYASIN-SKU'
        parent = 'B0ZZEMPTYASINPARENT'

        fam_tmpl = self.env['product.template'].create({
            'name': 'ZZT Famiglia asin vuoto', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': parent,
        })
        fam_tmpl.product_variant_id.write({
            'x_amazon_sku': sku, 'x_amazon_asin': 'B0ZZEMPTYASINCHILD', 'default_code': sku,
        })

        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self._make_log('test asin vuoto')
        self._run(eng, log, [make_report_row(sku, asin='')])  # quirk noto del report bulk Amazon

        count = self.env['product.template'].search_count([('default_code', '=', sku)])
        self.assertEqual(count, 1, 'non deve creare un duplicato')
        fam_tmpl.invalidate_recordset()
        self.assertEqual(fam_tmpl.x_amazon_parent_asin, parent, 'la famiglia non deve essere stata toccata')
        self.assertFalse(fam_tmpl.x_amazon_asin, 'nessun campo "semplice" deve essere stato scritto sulla famiglia')

    # ------------------------------------------------------------------
    # Undicesimo bug (2026-10-12): la mappa di matching delle varianti va
    # sincronizzata DENTRO lo stesso chunk, non solo tra chunk diversi.
    # ------------------------------------------------------------------

    def test_same_chunk_variant_then_empty_asin_row_skips(self):
        sku = 'ZZT-SAMECHUNK-SKU'
        parent = 'B0ZZSAMECHUNKPARENT'

        eng = self.SyncEngine(self.env, FakeSpApiClient({'B0ZZSAMECHUNKCHILD': parent}))
        log = self._make_log('test stesso chunk')
        row1 = make_report_row(sku, asin='B0ZZSAMECHUNKCHILD')
        row2 = make_report_row(sku, asin='')  # stesso SKU, ASIN vuoto, STESSA chiamata/chunk

        self._run(eng, log, [row1, row2])

        count = self.env['product.template'].search_count([('default_code', '=', sku)])
        self.assertEqual(count, 1, 'la seconda riga nello stesso chunk non deve duplicare')
        fams = self.env['product.template'].search([('x_amazon_parent_asin', '=', parent)])
        self.assertEqual(len(fams), 1)

    # ------------------------------------------------------------------
    # Dodicesimo bug, causa A (2026-10-13): famiglia con parent_asin
    # DIVERSO per lo stesso SKU (report con ASIN non aggiornato) -> skip,
    # mai una terza famiglia creata silenziosamente.
    # ------------------------------------------------------------------

    def test_family_vs_family_different_parent_is_skipped(self):
        sku = 'ZZT-STALEPARENT-SKU'
        parent_old = 'B0ZZSTALEPARENTOLD'
        parent_new = 'B0ZZSTALEPARENTNEW'

        client = FakeSpApiClient({
            'B0ZZSTALECHILDOLD': parent_old,
            'B0ZZSTALECHILDNEW': parent_new,
        })
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test parent diverso')

        self._run(eng, log, [make_report_row(sku, asin='B0ZZSTALECHILDOLD')])
        self._run(eng, log, [make_report_row(sku, asin='B0ZZSTALECHILDNEW')])

        count = self.env['product.template'].search_count([('default_code', '=', sku)])
        self.assertEqual(count, 1, 'non deve creare una seconda/terza famiglia')
        fams = self.env['product.template'].search([('x_amazon_parent_asin', 'in', [parent_old, parent_new])])
        self.assertEqual(fams.x_amazon_parent_asin, parent_old, 'la famiglia originale deve restare quella, non sostituita silenziosamente')

    # ------------------------------------------------------------------
    # Dodicesimo bug, causa B (2026-10-13): guardia "non sovrascrivere un
    # SKU con '+' usando uno senza '+'" mancava nel ramo varianti.
    # ------------------------------------------------------------------

    def test_plus_variant_sku_not_overwritten_by_non_plus(self):
        sku_plus = 'ZZT-PLUSGUARD-SKU+'
        sku_noplus = 'ZZT-PLUSGUARD-SKU'
        asin = 'B0ZZPLUSGUARDCHILD'
        parent = 'B0ZZPLUSGUARDPARENT'

        client = FakeSpApiClient({asin: parent})
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test guardia plus')

        self._run(eng, log, [make_report_row(sku_plus, asin=asin)])
        self._run(eng, log, [make_report_row(sku_noplus, asin=asin)])

        variant = self.env['product.product'].search([('x_amazon_asin', '=', asin)])
        self.assertEqual(len(variant), 1)
        self.assertEqual(variant.x_amazon_sku, sku_plus, 'lo SKU con + non deve essere sovrascritto da uno senza +')

    def test_non_plus_variant_sku_upgraded_to_plus(self):
        """Direzione opposta del test sopra: '+' deve poter sempre sostituire il non-+."""
        sku_plus = 'ZZT-PLUSUPGRADE-SKU+'
        sku_noplus = 'ZZT-PLUSUPGRADE-SKU'
        asin = 'B0ZZPLUSUPGRADECHILD'
        parent = 'B0ZZPLUSUPGRADEPARENT'

        client = FakeSpApiClient({asin: parent})
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test upgrade plus')

        self._run(eng, log, [make_report_row(sku_noplus, asin=asin)])
        self._run(eng, log, [make_report_row(sku_plus, asin=asin)])

        variant = self.env['product.product'].search([('x_amazon_asin', '=', asin)])
        self.assertEqual(len(variant), 1)
        self.assertEqual(variant.x_amazon_sku, sku_plus, 'il + deve sempre poter sostituire la versione senza +')

    # ------------------------------------------------------------------
    # Rilevamento SKU duplicati a fine Full Import (richiesto dall'utente
    # dopo il residuo di 12 duplicati post-refactor, 2026-10-13): solo
    # segnalazione nel log, mai fusione/cancellazione automatica.
    # ------------------------------------------------------------------

    def test_detect_duplicate_skus_reports_without_merging(self):
        sku = 'ZZT-DETECT-SKU'
        t1 = self.env['product.template'].create({
            'name': 'ZZT Detect semplice', 'type': 'consu', 'is_storable': True,
            'default_code': sku, 'x_amazon_asin': 'B0ZZDETECT1',
        })
        t2 = self.env['product.template'].create({
            'name': 'ZZT Detect famiglia', 'type': 'consu', 'is_storable': True,
            'default_code': sku, 'x_amazon_parent_asin': 'B0ZZDETECTPARENT',
        })

        # default_code è un campo correlato/computed-stored sul template
        # (riflette la variante quando è una sola) — detect_duplicate_skus
        # legge via SQL grezzo, serve un flush esplicito prima perché l'ORM
        # lo scriva davvero sulla colonna (in produzione non serve: il
        # metodo gira dopo numerosi env.cr.commit() già fatti nel flusso
        # reale, che fanno flush da soli).
        self.env.flush_all()

        eng = self.SyncEngine(self.env, None)
        log = self._make_log('test detect duplicati')
        with no_commit(self.env):
            n = eng.detect_duplicate_skus(log)

        self.assertEqual(n, 1)
        log.invalidate_recordset()
        self.assertIn(sku, log.log_lines)
        self.assertIn(str(t1.id), log.log_lines)
        self.assertIn(str(t2.id), log.log_lines)
        # Nessuna fusione/cancellazione: entrambi i template devono esistere ancora.
        self.assertTrue(t1.exists())
        self.assertTrue(t2.exists())

    # ------------------------------------------------------------------
    # Tredicesimo bug (2026-10-15): find_simple ritorna correttamente None
    # quando il match è una famiglia, ma se quella famiglia ha la variante
    # senza x_amazon_sku scritto (debito di dati da bug precedenti, solo
    # default_code presente), variant_tmpl_by_sku da solo non la trova più
    # e una riga con ASIN vuoto per quello SKU creava un duplicato.
    # ------------------------------------------------------------------

    def test_empty_asin_row_skips_family_even_without_variant_sku(self):
        sku = 'ZZT-NOVARIANTSKU'
        parent = 'B0ZZNOVARIANTPARENT'

        fam_tmpl = self.env['product.template'].create({
            'name': 'ZZT Famiglia senza variant sku', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': parent,
        })
        # Solo default_code sulla variante, MAI x_amazon_sku/x_amazon_asin —
        # replica esatta del debito di dati trovato dal vivo (es. SKU
        # "034-45-100", id reale 23564): la variante ha default_code ma non
        # x_amazon_sku, quindi non compare in variant_tmpl_by_sku.
        fam_tmpl.product_variant_id.write({'default_code': sku})

        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self._make_log('test famiglia senza variant sku')
        self._run(eng, log, [make_report_row(sku, asin='')])

        count = self.env['product.template'].search_count([('default_code', '=', sku)])
        self.assertEqual(count, 1, 'non deve creare un duplicato anche se la famiglia non ha x_amazon_sku sulla variante')
        fam_tmpl.invalidate_recordset()
        self.assertEqual(fam_tmpl.x_amazon_parent_asin, parent, 'la famiglia non deve essere stata toccata')

    def test_detect_duplicate_skus_no_false_positive_on_unique_sku(self):
        self.env['product.template'].create({
            'name': 'ZZT Unico', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-UNIQUE-SKU', 'x_amazon_asin': 'B0ZZUNIQUE1',
        })
        eng = self.SyncEngine(self.env, None)
        log = self._make_log('test nessun duplicato')
        with no_commit(self.env):
            n = eng.detect_duplicate_skus(log)
        self.assertEqual(n, 0)

    # ------------------------------------------------------------------
    # Ottimizzazione Catalog API (2026-10-15, richiesta dall'utente): salta
    # la chiamata per ASIN già noti e completamente arricchiti — ma solo per
    # prodotti semplici, mai per famiglie/dati incompleti/ASIN nuovi.
    # ------------------------------------------------------------------

    def test_catalog_api_skipped_for_known_complete_simple_product(self):
        asin = 'B0ZZKNOWNCOMPLETE'
        sku = 'ZZT-KNOWNCOMPLETE-SKU'
        tmpl = self.env['product.template'].create({
            'name': 'ZZT Prodotto completo', 'type': 'consu', 'is_storable': True,
            'default_code': sku, 'x_amazon_sku': sku, 'x_amazon_asin': asin,
            'x_amazon_category': 'Categoria test', 'product_length': 10.0,
            'x_amazon_product_type': 'TEST_TYPE', 'x_amazon_browse_node_id': '12345',
            'x_amazon_brand': 'Marca test', 'description_sale': 'Descrizione test',
        })

        requested_asins = []

        class RecordingClient(FakeSpApiClient):
            def get_catalog_items_batch(self, asins):
                requested_asins.extend(asins)
                return super().get_catalog_items_batch(asins)

        eng = self.SyncEngine(self.env, RecordingClient())
        log = self._make_log('test skip catalog api')
        self._run(eng, log, [make_report_row(sku, asin=asin, price='15.50', qty='7')])

        self.assertNotIn(asin, requested_asins, 'non deve richiamare il Catalog API per un prodotto già completo')
        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.x_amazon_price, 15.50, 'il prezzo deve comunque aggiornarsi via _update_product')

    def test_catalog_api_still_called_for_incomplete_existing_product(self):
        asin = 'B0ZZKNOWNINCOMPLETE'
        sku = 'ZZT-KNOWNINCOMPLETE-SKU'
        self.env['product.template'].create({
            'name': 'ZZT Prodotto incompleto', 'type': 'consu', 'is_storable': True,
            'default_code': sku, 'x_amazon_sku': sku, 'x_amazon_asin': asin,
            # Nessun campo Catalog popolato: needs_catalog deve restare True.
        })

        requested_asins = []

        class RecordingClient(FakeSpApiClient):
            def get_catalog_items_batch(self, asins):
                requested_asins.extend(asins)
                return super().get_catalog_items_batch(asins)

        eng = self.SyncEngine(self.env, RecordingClient())
        log = self._make_log('test non skip catalog api incompleto')
        self._run(eng, log, [make_report_row(sku, asin=asin)])

        self.assertIn(asin, requested_asins, 'un prodotto con dati Catalog mancanti deve continuare a essere interrogato')

    def test_catalog_api_still_called_for_unknown_asin(self):
        asin = 'B0ZZNEVERSEEN'
        sku = 'ZZT-NEVERSEEN-SKU'

        requested_asins = []

        class RecordingClient(FakeSpApiClient):
            def get_catalog_items_batch(self, asins):
                requested_asins.extend(asins)
                return super().get_catalog_items_batch(asins)

        eng = self.SyncEngine(self.env, RecordingClient())
        log = self._make_log('test non skip catalog api nuovo')
        self._run(eng, log, [make_report_row(sku, asin=asin)])

        self.assertIn(asin, requested_asins, 'un ASIN mai visto prima deve sempre essere interrogato')

    # ------------------------------------------------------------------
    # Testi/traduzioni multi-marketplace (2026-10-15, richiesta dell'utente
    # in ottica esportazione PrestaShop): name/description_sale sono già
    # campi traducibili in Odoo core — il problema era che le nostre write()
    # non specificavano mai la lingua, quindi ogni marketplace sovrascriveva
    # sempre lo stesso slot di lingua di default.
    # ------------------------------------------------------------------

    def _activate_fr(self):
        """fr_FR esiste già come res.lang ma inattivo in odoo_test1 — lo
        attivo solo per la durata del test (rollback automatico a fine test,
        stesso meccanismo di qualunque altra modifica)."""
        fr = self.env['res.lang'].with_context(active_test=False).search([('code', '=', 'fr_FR')])
        fr.write({'active': True})
        return fr

    def test_create_product_writes_name_in_marketplace_language(self):
        fr = self._activate_fr()
        mkt = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT Francia', 'code': 'ZZFR', 'marketplace_id': 'ZZFRMKTID',
            'lang_id': fr.id, 'active': True,
        })

        class FrClient(FakeSpApiClient):
            marketplace_id = 'ZZFRMKTID'

        sku = 'ZZT-LANG-SKU'
        eng = self.SyncEngine(self.env, FrClient())
        log = self._make_log('test lingua creazione')
        self._run(eng, log, [make_report_row(sku, asin='B0ZZLANGASIN', name='Nom francais du produit')])

        tmpl = self.env['product.template'].search([('default_code', '=', sku)])
        self.assertEqual(len(tmpl), 1)
        self.assertEqual(tmpl.with_context(lang='fr_FR').name, 'Nom francais du produit')

    def test_update_fills_empty_language_slot_without_overwriting_others(self):
        fr = self._activate_fr()
        mkt = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT Francia 2', 'code': 'ZZFR2', 'marketplace_id': 'ZZFRMKTID2',
            'lang_id': fr.id, 'active': True,
        })
        sku = 'ZZT-LANGFILL-SKU'
        asin = 'B0ZZLANGFILLASIN'
        # Scritto esplicitamente in it_IT: senza un contesto lingua esplicito
        # qui, Odoo userebbe la lingua dell'utente/ambiente di test (non
        # necessariamente it_IT), rendendo l'asserzione sotto inaffidabile.
        tmpl = self.env['product.template'].with_context(lang='it_IT').create({
            'name': 'Nome italiano esistente', 'type': 'consu', 'is_storable': True,
            'default_code': sku, 'x_amazon_sku': sku, 'x_amazon_asin': asin,
        })

        class FrClient(FakeSpApiClient):
            marketplace_id = 'ZZFRMKTID2'

        eng = self.SyncEngine(self.env, FrClient())
        log = self._make_log('test riempimento slot lingua')
        self._run(eng, log, [make_report_row(sku, asin=asin, name='Nom francais rempli')])

        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.with_context(lang='fr_FR').name, 'Nom francais rempli',
                          'lo slot francese, vuoto, deve riempirsi')
        self.assertEqual(tmpl.with_context(lang='it_IT').name, 'Nome italiano esistente',
                          'lo slot italiano, già presente, non deve essere toccato')

        # Un secondo giro con un testo francese diverso non deve sovrascrivere
        # quello già presente (stesso principio "mai perdere un dato già
        # scritto" già usato per brand/descrizione in apply_category_mapping).
        self._run(eng, log, [make_report_row(sku, asin=asin, name='Nom francais different')])
        tmpl.invalidate_recordset()
        self.assertEqual(tmpl.with_context(lang='fr_FR').name, 'Nom francais rempli',
                          'lo slot francese già popolato non deve essere sovrascritto da un giro successivo')

    def test_import_does_not_crash_on_marketplace_with_inactive_lang(self):
        """
        Bug reale del 2026-10-16: un marketplace con lang_id configurato ma
        la lingua corrispondente NON attiva in Odoo faceva sollevare
        'Invalid language code', eccezione non catturata che ha fatto
        fallire un intero Full Import dopo 45 minuti di lavoro. Qui fr_FR
        NON viene attivata (a differenza degli altri test lingua sopra) —
        deve degradare a scrivere nella lingua di default, mai sollevare.
        """
        mkt = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT Francia inattiva', 'code': 'ZZFRINACT', 'marketplace_id': 'ZZFRINACTID',
            'active': True,
        })
        fr_inactive = self.env['res.lang'].with_context(active_test=False).search([('code', '=', 'fr_FR')])
        self.assertFalse(fr_inactive.active, 'precondizione del test: fr_FR deve essere inattiva qui')
        mkt.write({'lang_id': fr_inactive.id})

        class FrInactiveClient(FakeSpApiClient):
            marketplace_id = 'ZZFRINACTID'

        sku = 'ZZT-INACTIVELANG-SKU'
        eng = self.SyncEngine(self.env, FrInactiveClient())
        log = self._make_log('test lingua inattiva non blocca import')
        # Non deve sollevare 'Invalid language code' né nessun'altra eccezione.
        self._run(eng, log, [make_report_row(sku, asin='B0ZZINACTIVEASIN', name='Nome con lingua inattiva')])

        tmpl = self.env['product.template'].search([('default_code', '=', sku)])
        self.assertEqual(len(tmpl), 1, 'il prodotto deve comunque essere creato, non bloccare l\'intero import')
        self.assertEqual(tmpl.name, 'Nome con lingua inattiva',
                          'il nome deve scriversi nella lingua di default, non andare perso')

    # ------------------------------------------------------------------
    # Prodotti "fantasma" dei marketplace secondari (2026-10-22): il report
    # di ES/FR/UK contiene ancora il vecchio SKU senza '+' (spesso senza
    # ASIN), e creava un prodotto nuovo accanto a quello vero.
    # ------------------------------------------------------------------

    def _make_simple(self, sku, asin):
        return self.env['product.template'].create({
            'name': f'ZZT {sku}', 'type': 'consu', 'is_storable': True,
            'default_code': sku, 'x_amazon_sku': sku, 'x_amazon_asin': asin,
        })

    def test_old_sku_without_asin_skipped_when_plus_in_odoo(self):
        real = self._make_simple('ZZT-GHOST1+', 'B0ZZGHOST1')
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self._make_log('test fantasma senza asin')
        self._run(eng, log, [make_report_row('ZZT-GHOST1', asin='')])

        self.assertFalse(self.env['product.template'].search([('default_code', '=', 'ZZT-GHOST1')]),
                         'il vecchio SKU senza + non deve creare un prodotto nuovo')
        self.assertIn('sostituito da ZZT-GHOST1+ già in Odoo', log.log_lines)
        self.assertEqual(real.x_amazon_sku, 'ZZT-GHOST1+')

    def test_old_sku_with_asin_does_not_rename_plus_product(self):
        """Report con solo il vecchio SKU (la versione '+' non è listata su
        quel marketplace): il match per ASIN non deve riportare il prodotto
        vero da 'X+' a 'X'."""
        real = self._make_simple('ZZT-GHOST2+', 'B0ZZGHOST2')
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        log = self._make_log('test fantasma con asin')
        self._run(eng, log, [make_report_row('ZZT-GHOST2', asin='B0ZZGHOST2')])

        real.invalidate_recordset()
        self.assertEqual(real.x_amazon_sku, 'ZZT-GHOST2+')
        self.assertEqual(real.default_code, 'ZZT-GHOST2+')
        self.assertFalse(self.env['product.template'].search([('default_code', '=', 'ZZT-GHOST2')]))

    def test_variant_asin_not_recognised_as_variant_skips(self):
        """ASIN di una variante esistente su una riga senza relationships
        (Catalog API fallita o senza dati su quel marketplace): niente
        prodotto semplice duplicato."""
        fam = self.env['product.template'].create({
            'name': 'ZZT Famiglia fantasma', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': 'B0ZZGHOSTPARENT',
        })
        fam.product_variant_id.write({
            'x_amazon_sku': 'ZZT-GHOST3-V', 'x_amazon_asin': 'B0ZZGHOST3', 'default_code': 'ZZT-GHOST3-V',
        })
        eng = self.SyncEngine(self.env, FakeSpApiClient())  # nessuna relazione nota
        log = self._make_log('test fantasma asin variante')
        self._run(eng, log, [make_report_row('ZZT-GHOST3-OLD', asin='B0ZZGHOST3')])

        self.assertFalse(self.env['product.template'].search([('default_code', '=', 'ZZT-GHOST3-OLD')]),
                         "l'ASIN di una variante esistente non deve diventare un prodotto semplice")

    # ------------------------------------------------------------------
    # Prodotto manuale con lo stesso EAN di una variante Amazon
    # (2026-10-22): decisione dell'utente — crea la variante nella famiglia,
    # sposta la giacenza del manuale e archivia il manuale.
    # ------------------------------------------------------------------

    def _make_manual(self, code, ean, qty):
        manual = self.env['product.template'].create({
            'name': f'ZZT manuale {code}', 'type': 'consu', 'is_storable': True,
            'default_code': code, 'barcode': ean,
        })
        location = self.env.ref('stock.warehouse0').lot_stock_id
        if qty:
            self.env['stock.quant'].sudo()._update_available_quantity(
                manual.product_variant_id, location, qty,
            )
        return manual, location

    def _qty(self, variant, location):
        return sum(self.env['stock.quant'].sudo().search([
            ('product_id', '=', variant.id), ('location_id', '=', location.id),
        ]).mapped('quantity'))

    def test_manual_product_with_same_ean_merged_into_variant(self):
        manual, location = self._make_manual('ZZT-SVS-1', '5700000000017', 7)
        manual_variant = manual.product_variant_id
        client = FakeSpApiClient({'B0ZZMERGE1': 'B0ZZMERGEPARENT'}, {'B0ZZMERGE1': '5700000000017'})
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test unione manuale')
        self._run(eng, log, [make_report_row('ZZT-SAV-1', asin='B0ZZMERGE1')])

        fam = self.env['product.template'].search([('x_amazon_parent_asin', '=', 'B0ZZMERGEPARENT')])
        self.assertEqual(len(fam), 1)
        variant = fam.product_variant_ids.filtered(lambda v: v.x_amazon_asin == 'B0ZZMERGE1')
        self.assertEqual(len(variant), 1)
        self.assertEqual(variant.barcode, '5700000000017', "l'EAN passa alla variante")
        self.assertEqual(self._qty(variant, location), 7, 'la giacenza del manuale passa alla variante')
        self.assertEqual(self._qty(manual_variant, location), 0)
        manual.invalidate_recordset()
        self.assertFalse(manual.active, 'il manuale viene archiviato')
        self.assertFalse(manual_variant.barcode)

    def test_existing_duplicate_variant_merged_on_next_run(self):
        """Doppione già creato da un import precedente (variante senza EAN,
        occupato dal manuale): il giro successivo lo unisce."""
        fam = self.env['product.template'].create({
            'name': 'ZZT famiglia doppione', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': 'B0ZZMERGEPARENT2',
        })
        fam.product_variant_id.write({
            'x_amazon_sku': 'ZZT-SAV-2', 'x_amazon_asin': 'B0ZZMERGE2', 'default_code': 'ZZT-SAV-2',
        })
        manual, location = self._make_manual('ZZT-SVS-2', '5700000000024', 4)
        client = FakeSpApiClient({'B0ZZMERGE2': 'B0ZZMERGEPARENT2'}, {'B0ZZMERGE2': '5700000000024'})
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test unione doppione esistente')
        self._run(eng, log, [make_report_row('ZZT-SAV-2', asin='B0ZZMERGE2')])

        variant = fam.product_variant_id
        self.assertEqual(variant.barcode, '5700000000024')
        self.assertEqual(self._qty(variant, location), 4)
        manual.invalidate_recordset()
        self.assertFalse(manual.active)

    def test_manual_with_reserved_stock_not_merged(self):
        """Giacenza riservata (ordine aperto): niente unione automatica e
        nessuna famiglia vuota creata — riga saltata con 'serve fusione manuale'."""
        manual, location = self._make_manual('ZZT-SVS-3', '5700000000031', 5)
        quant = self.env['stock.quant'].sudo().search([
            ('product_id', '=', manual.product_variant_id.id), ('location_id', '=', location.id),
        ])
        quant.reserved_quantity = 2
        client = FakeSpApiClient({'B0ZZMERGE3': 'B0ZZMERGEPARENT3'}, {'B0ZZMERGE3': '5700000000031'})
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test unione bloccata')
        self._run(eng, log, [make_report_row('ZZT-SAV-3', asin='B0ZZMERGE3')])

        self.assertFalse(self.env['product.template'].search([('x_amazon_parent_asin', '=', 'B0ZZMERGEPARENT3')]),
                         'nessuna famiglia vuota deve restare')
        self.assertTrue(manual.active)
        self.assertEqual(self._qty(manual.product_variant_id, location), 5)
        self.assertIn('serve fusione manuale', log.log_lines)

    def test_same_attribute_value_different_asin_creates_distinct_variant(self):
        """Due figli della stessa famiglia con lo stesso valore attributo
        (es. due misure con lo stesso colore): prima il secondo riscriveva
        SKU/ASIN della variante del primo, ora nasce una variante distinta."""
        from odoo.addons.ke_amazon_sync.services.sync_engine import _MatchMaps
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        parent = 'B0ZZCLASHPARENT'

        def process(sku, asin):
            product = eng._parse_listing_report(make_report_row(sku, asin=asin))
            product['parent_asin'] = parent
            product['variation_attr'] = ('ZZT Colore', 'Lemon Back')
            maps = _MatchMaps(self.env, eng._build_barcode_map())
            return eng._process_variant_listing(product, parent, 'consu', maps, True)

        with no_commit(self.env):
            process('ZZT-CLASH-1', 'B0ZZCLASH01')
            res, msg = process('ZZT-CLASH-2', 'B0ZZCLASH02')

        fam = self.env['product.template'].search([('x_amazon_parent_asin', '=', parent)])
        self.assertEqual(res, 'created', msg)
        self.assertEqual(sorted(fam.product_variant_ids.mapped('x_amazon_asin')), ['B0ZZCLASH01', 'B0ZZCLASH02'],
                         'ogni figlio Amazon deve avere la sua variante')

    def test_simple_with_same_sku_merged_when_family_exists(self):
        """Semplice con lo stesso SKU della riga, famiglia già creata da un
        fratello: prima "serve fusione manuale", ora variante + unione."""
        parent = 'B0ZZSKUMERGEPARENT'
        fam = self.env['product.template'].create({
            'name': 'ZZT famiglia unione sku', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': parent,
        })
        fam.product_variant_id.write({
            'x_amazon_sku': 'ZZT-SKUM-SIB', 'x_amazon_asin': 'B0ZZSKUMSIB', 'default_code': 'ZZT-SKUM-SIB',
        })
        simple, location = self._make_manual('ZZT-SKUM-1', False, 6)
        simple_variant = simple.product_variant_id
        client = FakeSpApiClient({'B0ZZSKUM1': parent})
        eng = self.SyncEngine(self.env, client)
        log = self._make_log('test unione per sku')
        self._run(eng, log, [make_report_row('ZZT-SKUM-1', asin='B0ZZSKUM1')])

        variant = fam.product_variant_ids.filtered(lambda v: v.x_amazon_asin == 'B0ZZSKUM1')
        self.assertEqual(len(variant), 1, log.log_lines)
        self.assertEqual(self._qty(variant, location), 6)
        self.assertEqual(self._qty(simple_variant, location), 0)
        simple.invalidate_recordset()
        self.assertFalse(simple.active)
        self.assertNotIn('serve fusione manuale', log.log_lines)
