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
        con lo stesso SKU: fondere automaticamente è troppo rischioso
        (sposterebbe stock/ordini) -> deve solo segnalare, non toccare nulla.
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
        self.assertIn('conflitto', msg.lower())
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
