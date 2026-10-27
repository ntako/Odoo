"""
Test per il download dell'immagine propria di ogni variante
(image_variant_1920) — bug trovato dal vivo il 2026-10-27, segnalato
dall'utente: _add_variant_to_template non scaricava mai l'immagine per i
figli di una famiglia successivi al primo (quello che seeda l'immagine
condivisa del template in _create_configurable_template), quindi ogni
variante mostrava sempre e solo la foto del primo fratello processato.
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

    def test_second_variant_of_family_gets_its_own_image(self):
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        parent = 'B0ZZIMGPARENT'

        def process(sku, asin, image_url):
            from odoo.addons.ke_amazon_sync.services.sync_engine import _MatchMaps
            product = eng._parse_listing_report({
                'seller-sku': sku, 'asin1': asin, 'item-name': 'Test prodotto',
                'price': '9.99', 'quantity': '3', 'fulfillment-channel': 'DEFAULT',
                'product-id-type': '', 'product-id': '', 'status': 'Active',
            })
            product['parent_asin'] = parent
            product['variation_attr'] = ('ZZT Colore', sku)
            product['image_url'] = image_url
            maps = _MatchMaps(self.env, eng._build_barcode_map())
            return eng._process_variant_listing(product, parent, 'consu', maps, True)

        with self._patched_fetch(), no_commit(self.env):
            res1, _ = process('ZZT-IMG-1', 'B0ZZIMG01', 'http://example.invalid/first.jpg')
            res2, _ = process('ZZT-IMG-2', 'B0ZZIMG02', 'http://example.invalid/second.jpg')

        self.assertEqual(res1, 'created')
        self.assertEqual(res2, 'created')
        fam = self.env['product.template'].search([('x_amazon_parent_asin', '=', parent)])
        v1 = fam.product_variant_ids.filtered(lambda v: v.x_amazon_asin == 'B0ZZIMG01')
        v2 = fam.product_variant_ids.filtered(lambda v: v.x_amazon_asin == 'B0ZZIMG02')
        self.assertTrue(v1.image_variant_1920, 'anche il primo figlio deve avere la sua immagine propria')
        self.assertTrue(v2.image_variant_1920, 'il secondo figlio deve avere la SUA immagine, non restare vuoto')

    def test_existing_variant_without_image_gets_backfilled_on_update(self):
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        parent = 'B0ZZIMGBACKFILL'

        from odoo.addons.ke_amazon_sync.services.sync_engine import _MatchMaps

        def process(image_url):
            product = eng._parse_listing_report({
                'seller-sku': 'ZZT-IMG-BF', 'asin1': 'B0ZZIMGBF01', 'item-name': 'Test prodotto',
                'price': '9.99', 'quantity': '3', 'fulfillment-channel': 'DEFAULT',
                'product-id-type': '', 'product-id': '', 'status': 'Active',
            })
            product['parent_asin'] = parent
            product['variation_attr'] = ('ZZT Colore', 'Rosso')
            product['image_url'] = image_url
            maps = _MatchMaps(self.env, eng._build_barcode_map())
            return eng._process_variant_listing(product, parent, 'consu', maps, True)

        with no_commit(self.env):
            # Creato SENZA immagine (es. variante nata prima del fix, o riga
            # senza immagine disponibile la prima volta).
            res1, _ = process(None)
        self.assertEqual(res1, 'created')
        fam = self.env['product.template'].search([('x_amazon_parent_asin', '=', parent)])
        variant = fam.product_variant_ids
        self.assertFalse(variant.image_variant_1920)

        with self._patched_fetch(), no_commit(self.env):
            # Un giro successivo (es. altro marketplace, o rilancio) porta
            # finalmente l'immagine: deve essere recuperata ora.
            res2, _ = process('http://example.invalid/backfill.jpg')
        self.assertEqual(res2, 'updated')
        variant.invalidate_recordset()
        self.assertTrue(variant.image_variant_1920)

    def test_existing_variant_with_image_is_not_refetched(self):
        """Non deve riscaricare a ogni giro se l'immagine c'è già — solo
        backfill per chi ne è privo (vedi commento in sync_engine.py)."""
        eng = self.SyncEngine(self.env, FakeSpApiClient())
        parent = 'B0ZZIMGNOREFETCH'
        from odoo.addons.ke_amazon_sync.services.sync_engine import _MatchMaps

        def process(image_url):
            product = eng._parse_listing_report({
                'seller-sku': 'ZZT-IMG-NR', 'asin1': 'B0ZZIMGNR01', 'item-name': 'Test prodotto',
                'price': '9.99', 'quantity': '3', 'fulfillment-channel': 'DEFAULT',
                'product-id-type': '', 'product-id': '', 'status': 'Active',
            })
            product['parent_asin'] = parent
            product['variation_attr'] = ('ZZT Colore', 'Blu')
            product['image_url'] = image_url
            maps = _MatchMaps(self.env, eng._build_barcode_map())
            return eng._process_variant_listing(product, parent, 'consu', maps, True)

        with self._patched_fetch(), no_commit(self.env):
            process('http://example.invalid/first.jpg')

        with patch.object(self.sync_engine_module, '_fetch_image_b64') as mocked_fetch, no_commit(self.env):
            process('http://example.invalid/second.jpg')
            mocked_fetch.assert_not_called()


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestDownloadMissingImages(TransactionCase):
    """
    Prima download_missing_images guardava solo product.template con
    x_amazon_asin valorizzato — mai vero per una famiglia (che ha solo
    x_amazon_parent_asin sul template, vedi _create_configurable_template),
    quindi non c'era modo di recuperare a mano le immagini mancanti sulle
    varianti già esistenti create prima del fix in _add_variant_to_template.
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

    def test_covers_both_simple_products_and_family_variants(self):
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

        client = FakeSpApiClient(image_url_by_asin={
            'B0ZZDLIMGSIMPLE': 'http://example.invalid/simple.jpg',
            'B0ZZDLIMGV1': 'http://example.invalid/v1.jpg',
            'B0ZZDLIMGV2': 'http://example.invalid/v2.jpg',
        })
        eng = self.SyncEngine(self.env, client)
        log = self._make_log()

        with patch.object(self.sync_engine_module, '_fetch_image_b64', lambda url: _FAKE_IMAGE_B64 if url else None):
            with no_commit(self.env):
                ok, skipped, errors = eng.download_missing_images(log)

        self.assertEqual((ok, skipped, errors), (3, 0, 0))
        simple_tmpl.invalidate_recordset()
        variants.invalidate_recordset()
        self.assertTrue(simple_tmpl.image_1920)
        self.assertTrue(variants[0].image_variant_1920)
        self.assertTrue(variants[1].image_variant_1920)

    def test_single_variant_template_not_duplicated_via_variant_pass(self):
        """Un template con UNA sola variante usa solo image_1920 (già
        coperto dal primo passaggio) — non deve comparire anche nel secondo
        passaggio (product_variant_count > 1 lo esclude)."""
        tmpl = self.env['product.template'].create({
            'name': 'ZZT Singola', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-DLIMG-SINGLE', 'x_amazon_asin': 'B0ZZDLIMGSINGLE',
        })
        client = FakeSpApiClient(image_url_by_asin={'B0ZZDLIMGSINGLE': 'http://example.invalid/single.jpg'})
        eng = self.SyncEngine(self.env, client)
        log = self._make_log()

        with patch.object(self.sync_engine_module, '_fetch_image_b64', lambda url: _FAKE_IMAGE_B64 if url else None):
            with no_commit(self.env):
                ok, skipped, errors = eng.download_missing_images(log)

        self.assertEqual((ok, skipped, errors), (1, 0, 0))
        tmpl.invalidate_recordset()
        self.assertTrue(tmpl.image_1920)
