"""
Test per le immagini dei prodotti importati da Amazon.

Storia: il 2026-10-27 l'utente aveva segnalato varianti senza la propria
immagine (image_variant_1920): _add_variant_to_template non la scaricava per i
figli successivi al primo. Dal 2026-11-03 il Full Import non scarica più
nessuna immagine (era circa metà del lavoro di creazione di un prodotto):
salva l'indirizzo dell'immagine principale (product.product.
x_amazon_image_url) e "Scarica immagini mancanti", che parte da solo a fine
catena, la scarica senza richiamare il Catalog API.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, no_commit

# _fetch_image_b64 restituisce byte GIÀ base64-codificati (base64.b64encode
# del contenuto scaricato) — image_1920/image_variant_1920 validano non solo
# che sia base64 valido ma anche che decodifichi a un'immagine vera (PIL),
# quindi il finto ritorno dev'essere un PNG 1x1 reale, non testo qualsiasi.
_FAKE_IMAGE_B64 = (
    b'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
    b'+A8AAQUBAScY42YAAAAASUVORK5CYII='
)


class _CountingClient(FakeSpApiClient):
    """FakeSpApiClient che conta le chiamate al Catalog API per singolo ASIN."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.catalog_calls = 0

    def get_catalog_item(self, asin):
        self.catalog_calls += 1
        return super().get_catalog_item(asin)


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestVariantImages(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services import sync_engine as sync_engine_module
        self.sync_engine_module = sync_engine_module
        self.SyncEngine = sync_engine_module.SyncEngine
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', '')

    def _patched_fetch(self, fake_bytes=_FAKE_IMAGE_B64):
        return patch.object(
            self.sync_engine_module, '_fetch_image_b64',
            lambda url: fake_bytes if url else None,
        )

    def _process(self, eng, sku, asin, parent, color, image_url):
        from odoo.addons.ke_amazon_sync.services.sync_engine import _MatchMaps
        product = eng._parse_listing_report({
            'seller-sku': sku, 'asin1': asin, 'item-name': 'Test prodotto',
            'price': '9.99', 'quantity': '3', 'fulfillment-channel': 'DEFAULT',
            'product-id-type': '', 'product-id': '', 'status': 'Active',
        })
        product['parent_asin'] = parent
        product['variation_attr'] = ('ZZT Colore', color)
        product['image_url'] = image_url
        maps = _MatchMaps(self.env, eng._build_barcode_map())
        return eng._process_variant_listing(product, parent, 'consu', maps, True)

    def test_import_stores_each_variant_image_url_without_downloading(self):
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        parent = 'B0ZZIMGPARENT'
        with patch.object(self.sync_engine_module, '_fetch_image_b64') as fetch, no_commit(self.env):
            res1, _ = self._process(eng, 'ZZT-IMG-1', 'B0ZZIMG01', parent, 'ZZT-IMG-1',
                                    'http://example.invalid/first.jpg')
            res2, _ = self._process(eng, 'ZZT-IMG-2', 'B0ZZIMG02', parent, 'ZZT-IMG-2',
                                    'http://example.invalid/second.jpg')
            fetch.assert_not_called()

        self.assertEqual((res1, res2), ('created', 'created'))
        fam = self.env['product.template'].search([('x_amazon_parent_asin', '=', parent)])
        v1 = fam.product_variant_ids.filtered(lambda v: v.x_amazon_asin == 'B0ZZIMG01')
        v2 = fam.product_variant_ids.filtered(lambda v: v.x_amazon_asin == 'B0ZZIMG02')
        self.assertEqual(v1.x_amazon_image_url, 'http://example.invalid/first.jpg')
        self.assertEqual(v2.x_amazon_image_url, 'http://example.invalid/second.jpg',
                         'il secondo figlio ha il SUO indirizzo, non quello del primo')

        # Poi "Scarica immagini mancanti": ogni variante la sua immagine, e
        # anche il template della famiglia, senza chiamare il Catalog API.
        client = _CountingClient()
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT img', 'sync_type': 'download_images'})
        with self._patched_fetch(), no_commit(self.env):
            self.SyncEngine(self.env, client).download_missing_images(log)
        self.assertTrue(v1.image_variant_1920)
        self.assertTrue(v2.image_variant_1920)
        self.assertTrue(fam.image_1920, 'il template della famiglia prende l\'immagine di una sua variante')
        self.assertEqual(client.catalog_calls, 0, 'indirizzi già salvati: nessuna chiamata al catalogo')

    def test_existing_variant_without_image_remembers_url_on_update(self):
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        parent = 'B0ZZIMGBACKFILL'
        with no_commit(self.env):
            # Creato SENZA immagine (es. riga senza immagine disponibile la prima volta).
            res1, _ = self._process(eng, 'ZZT-IMG-BF', 'B0ZZIMGBF01', parent, 'Rosso', None)
        self.assertEqual(res1, 'created')
        variant = self.env['product.template'].search([('x_amazon_parent_asin', '=', parent)]).product_variant_ids
        self.assertFalse(variant.x_amazon_image_url)

        with no_commit(self.env):
            # Un giro successivo porta finalmente l'immagine: l'indirizzo va salvato ora.
            res2, _ = self._process(eng, 'ZZT-IMG-BF', 'B0ZZIMGBF01', parent, 'Rosso',
                                    'http://example.invalid/backfill.jpg')
        self.assertEqual(res2, 'updated')
        self.assertEqual(variant.x_amazon_image_url, 'http://example.invalid/backfill.jpg')

    def test_existing_variant_with_image_is_left_alone(self):
        """Una variante che ha già l'immagine non va toccata da un nuovo giro."""
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        parent = 'B0ZZIMGNOREFETCH'
        with no_commit(self.env):
            self._process(eng, 'ZZT-IMG-NR', 'B0ZZIMGNR01', parent, 'Blu', 'http://example.invalid/first.jpg')
        variant = self.env['product.template'].search([('x_amazon_parent_asin', '=', parent)]).product_variant_ids
        variant.image_variant_1920 = _FAKE_IMAGE_B64

        with patch.object(self.sync_engine_module, '_fetch_image_b64') as fetch, no_commit(self.env):
            self._process(eng, 'ZZT-IMG-NR', 'B0ZZIMGNR01', parent, 'Blu', 'http://example.invalid/second.jpg')
            fetch.assert_not_called()
        self.assertEqual(variant.x_amazon_image_url, 'http://example.invalid/first.jpg')


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestDownloadMissingImages(TransactionCase):
    """
    download_missing_images copre prodotti semplici, varianti di famiglia e
    template di famiglia (prima solo i semplici: le famiglie hanno
    x_amazon_parent_asin, non x_amazon_asin, sul template). Il database dei
    test può contenere altri prodotti senza immagine: si verificano i record
    del test, non i totali.
    """

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services import sync_engine as sync_engine_module
        self.sync_engine_module = sync_engine_module
        self.SyncEngine = sync_engine_module.SyncEngine

    def _make_log(self):
        return self.env['ke.amazon.sync.log'].create({
            'name': 'test download immagini', 'sync_type': 'download_images',
        })

    def _run(self, client):
        log = self._make_log()
        with patch.object(self.sync_engine_module, '_fetch_image_b64',
                          lambda url: _FAKE_IMAGE_B64 if url else None):
            with no_commit(self.env):
                result = self.SyncEngine(self.env, client).download_missing_images(log)
        return result, log

    def test_covers_simple_products_family_variants_and_family_template(self):
        simple_tmpl = self.env['product.template'].create({
            'name': 'ZZT Semplice', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-DLIMG-SIMPLE', 'x_amazon_asin': 'B0ZZDLIMGSIMPLE',
        })
        fam_tmpl = self.env['product.template'].create({
            'name': 'ZZT Famiglia', 'type': 'consu', 'is_storable': True,
            'x_amazon_parent_asin': 'B0ZZDLIMGPARENT',
        })
        attr = self.env['product.attribute'].create({'name': 'ZZT Taglia', 'create_variant': 'always'})
        val1 = self.env['product.attribute.value'].create({'attribute_id': attr.id, 'name': 'S'})
        val2 = self.env['product.attribute.value'].create({'attribute_id': attr.id, 'name': 'M'})
        self.env['product.template.attribute.line'].create({
            'product_tmpl_id': fam_tmpl.id, 'attribute_id': attr.id,
            'value_ids': [(6, 0, [val1.id, val2.id])],
        })
        variants = fam_tmpl.product_variant_ids
        variants[0].write({'x_amazon_asin': 'B0ZZDLIMGV1'})
        variants[1].write({'x_amazon_asin': 'B0ZZDLIMGV2'})

        # Nessun indirizzo salvato (prodotti precedenti al 2026-11-03): ripiego sul catalogo.
        client = _CountingClient(image_url_by_asin={
            'B0ZZDLIMGSIMPLE': 'http://example.invalid/simple.jpg',
            'B0ZZDLIMGV1': 'http://example.invalid/v1.jpg',
            'B0ZZDLIMGV2': 'http://example.invalid/v2.jpg',
        })
        (ok, skipped, errors), log = self._run(client)

        self.assertEqual(errors, 0)
        self.assertTrue(simple_tmpl.image_1920)
        self.assertTrue(variants[0].image_variant_1920)
        self.assertTrue(variants[1].image_variant_1920)
        self.assertTrue(fam_tmpl.image_1920)
        self.assertEqual(variants[0].x_amazon_image_url, 'http://example.invalid/v1.jpg',
                         'indirizzo ricavato dal catalogo salvato per la prossima volta')
        self.assertIn('Chiamate al Catalog API per indirizzi mancanti', log.log_lines)

    def test_stored_url_is_used_without_catalog_call(self):
        tmpl = self.env['product.template'].create({
            'name': 'ZZT Con indirizzo', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-DLIMG-URL', 'x_amazon_asin': 'B0ZZDLIMGURL',
        })
        tmpl.product_variant_id.x_amazon_image_url = 'http://example.invalid/stored.jpg'
        client = _CountingClient()
        (ok, skipped, errors), _log = self._run(client)
        self.assertTrue(tmpl.image_1920)
        self.assertEqual(client.catalog_calls, 0)

    def test_single_variant_template_not_duplicated_via_variant_pass(self):
        """Un template con UNA sola variante usa solo image_1920 — non deve
        comparire anche tra le varianti di famiglia."""
        tmpl = self.env['product.template'].create({
            'name': 'ZZT Singola', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-DLIMG-SINGLE', 'x_amazon_asin': 'B0ZZDLIMGSINGLE',
        })
        client = _CountingClient(image_url_by_asin={'B0ZZDLIMGSINGLE': 'http://example.invalid/single.jpg'})
        (ok, skipped, errors), log = self._run(client)
        self.assertTrue(tmpl.image_1920)
        self.assertEqual(log.log_lines.count('ZZT-DLIMG-SINGLE'), 1, 'elaborato una volta sola')


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestBestImageUrl(TransactionCase):
    """_best_image_url: ruolo e dimensione migliori (dati reali del 2026-11-04)."""

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services.sync_engine import _best_image_url
        self.best = _best_image_url

    @staticmethod
    def _catalog(*images):
        return {'images': [{'marketplaceId': 'X', 'images': [
            {'variant': role, 'width': w, 'height': h, 'link': link} for role, w, h, link in images
        ]}]}

    def test_largest_main_whatever_the_order(self):
        # Stesso ASIN (B0FBXZ6PLG): Amazon cambia l'ordine delle dimensioni.
        cat = self._catalog(
            ('MAIN', 500, 500, 'main-500.jpg'), ('MAIN', 75, 75, 'main-75.jpg'),
            ('MAIN', 1000, 1000, 'main-1000.jpg'), ('PT01', 1400, 1400, 'pt01-1400.jpg'),
        )
        self.assertEqual(self.best(cat), 'main-1000.jpg', 'la MAIN più grande, anche se una PT01 è più grande')

    def test_fallback_to_gallery_then_swatch(self):
        # B0CQ3ZZ3S5: nessuna MAIN, solo galleria.
        cat = self._catalog(
            ('PT01', 436, 500, 'pt01-500.jpg'), ('PT01', 65, 75, 'pt01-75.jpg'),
            ('PT01', 2232, 2560, 'pt01-2560.jpg'), ('PT02', 3000, 3000, 'pt02.jpg'),
        )
        self.assertEqual(self.best(cat), 'pt01-2560.jpg')
        self.assertEqual(self.best(self._catalog(('SWCH', 30, 30, 'swatch.jpg'))), 'swatch.jpg')

    def test_no_usable_image(self):
        self.assertEqual(self.best({}), '')
        self.assertEqual(self.best(self._catalog(('PT05', 500, 500, 'altro.jpg'))), '')


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestSimpleImagesSecondaryOnly(TransactionCase):
    """Prodotto semplice presente solo su un marketplace secondario: niente
    ASIN sul prodotto, solo sugli annunci (PFS-1530586, 2026-11-05)."""

    def test_simple_product_without_template_asin_gets_image(self):
        from odoo.addons.ke_amazon_sync.services import sync_engine as se
        mp = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT ES img', 'code': 'ZZIE', 'marketplace_id': 'ZZ_IMG_ES', 'active': True,
        })
        tmpl = self.env['product.template'].create({'name': 'ZZT solo ES', 'type': 'consu'})
        tmpl.product_variant_id.x_amazon_image_url = 'http://example.invalid/es.jpg'
        self.env['ke.amazon.listing'].create({
            'product_id': tmpl.product_variant_id.id, 'marketplace_id': mp.id,
            'sku': 'ZZT-IMG-ES', 'asin': 'B0ZZIMGES1',
        })
        self.assertFalse(tmpl.x_amazon_asin)
        eng = se.SyncEngine(self.env, FakeSpApiClient())
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT img es', 'sync_type': 'download_images'})
        with patch.object(se, '_fetch_image_b64', lambda url: _FAKE_IMAGE_B64 if url else None), \
                no_commit(self.env):
            eng.download_missing_images(log)
        self.assertTrue(tmpl.image_1920)
