"""
Galleria immagini (ke_product_catalog, 2026-11-04): il Full Import salva gli
indirizzi di tutte le immagini Amazon della variante (una per ruolo, la più
grande), per la pubblicazione su PrestaShop — senza scaricarle. Durante
l'import le immagini vengono solo annotate sulla variante; la galleria la
scrive save_pending_galleries, all'inizio di "Scarica immagini mancanti".
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from .common import FakeSpApiClient, make_report_row, no_commit


def _img(role, link, size):
    return {'variant': role, 'link': link, 'width': size, 'height': size}


class _GalleryClient(FakeSpApiClient):
    def __init__(self, images_by_asin, **kw):
        super().__init__(**kw)
        self._images_by_asin = images_by_asin

    def get_catalog_items_batch(self, asins):
        out = super().get_catalog_items_batch(asins)
        for asin in asins:
            if asin in self._images_by_asin:
                out[asin]['images'] = [{'images': self._images_by_asin[asin]}]
        return out


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestGalleryImages(TransactionCase):

    def setUp(self):
        super().setUp()
        from odoo.addons.ke_amazon_sync.services import sync_engine
        self.se = sync_engine
        self.it = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT IT gal', 'code': 'ZZGI', 'marketplace_id': 'ZZ_GAL_IT', 'active': True,
        })
        self.es = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT ES gal', 'code': 'ZZGE', 'marketplace_id': 'ZZ_GAL_ES', 'active': True,
        })
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.marketplace_id', 'ZZ_GAL_IT')

    def _run(self, client, rows):
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT galleria', 'sync_type': 'full_import'})
        eng = self.se.SyncEngine(self.env, client)
        with no_commit(self.env):
            eng.full_import_report_rows(log, rows, 0, len(rows), True)

    def _save(self):
        return self.se.SyncEngine(self.env, FakeSpApiClient()).save_pending_galleries(commit=False)

    def _gallery(self, sku):
        tmpl = self.env['product.template'].search([('default_code', '=', sku)])
        return tmpl.ke_media_ids.filtered(lambda m: m.source == 'amazon').sorted('sequence')

    def test_gallery_images_one_per_role_largest_in_order(self):
        catalog = {'images': [{'images': [
            _img('SWCH', 'https://a/swch.jpg', 30),
            _img('PT10', 'https://a/pt10.jpg', 500),
            _img('MAIN', 'https://a/main-75.jpg', 75),
            _img('PT02', 'https://a/pt02.jpg', 500),
            _img('MAIN', 'https://a/main-1000.jpg', 1000),
            _img('PT01', 'https://a/pt01-500.jpg', 500),
            _img('PT01', 'https://a/pt01-1500.jpg', 1500),
        ]}]}
        gallery = self.se._gallery_images(catalog)
        self.assertEqual([g['url'] for g in gallery], [
            'https://a/main-1000.jpg', 'https://a/pt01-1500.jpg', 'https://a/pt02.jpg',
            'https://a/pt10.jpg', 'https://a/swch.jpg',
        ])
        self.assertEqual(gallery[0]['width'], 1000)

    def test_full_import_saves_gallery_and_secondary_does_not_overwrite(self):
        asin, sku = 'B0ZZGAL001', 'ZZT-GAL-1'
        it_images = [_img('MAIN', 'https://it/main.jpg', 1000), _img('PT01', 'https://it/pt01.jpg', 1000)]
        self._run(_GalleryClient({asin: it_images}, marketplace_id='ZZ_GAL_IT'), [make_report_row(sku, asin=asin)])
        self.assertFalse(self._gallery(sku), "durante l'import la galleria non si scrive")
        variant = self.env['product.template'].search([('default_code', '=', sku)]).product_variant_id
        self.assertTrue(variant.x_amazon_gallery_pending)

        # Su ES (prima del salvataggio) il prodotto non è "completo" per lo
        # spagnolo: catalogo richiesto, con immagini diverse — resta quella IT.
        es_images = [_img('MAIN', 'https://es/main.jpg', 1000)]
        self._run(_GalleryClient({asin: es_images}, marketplace_id='ZZ_GAL_ES'), [make_report_row(sku, asin=asin)])

        self.assertEqual(self._save(), 1)
        self.assertFalse(variant.x_amazon_gallery_pending, 'annotazione svuotata dopo il salvataggio')
        gallery = self._gallery(sku)
        self.assertEqual(gallery.mapped('url'), ['https://it/main.jpg', 'https://it/pt01.jpg'])
        self.assertEqual(gallery.mapped('role'), ['MAIN', 'PT01'])
        self.assertEqual(set(gallery.mapped('source_ref')), {'Amazon ZZGI'})
        self.assertTrue(all(gallery.mapped('product_id')), 'immagini sulla variante')

        # Dopo il salvataggio, un altro giro ES non la cambia.
        self._run(_GalleryClient({asin: es_images}, marketplace_id='ZZ_GAL_ES'), [make_report_row(sku, asin=asin)])
        self.assertFalse(variant.x_amazon_gallery_pending)
        self.assertEqual(self._save(), 0)
        self.assertEqual(self._gallery(sku).mapped('url'), ['https://it/main.jpg', 'https://it/pt01.jpg'])

    def test_secondary_marketplace_fills_missing_gallery(self):
        asin, sku = 'B0ZZGAL002', 'ZZT-GAL-2'
        self._run(_GalleryClient({}, marketplace_id='ZZ_GAL_IT'), [make_report_row(sku, asin=asin)])
        self.assertFalse(self._gallery(sku))
        self._run(_GalleryClient({asin: [_img('MAIN', 'https://es/only.jpg', 800)]}, marketplace_id='ZZ_GAL_ES'),
                  [make_report_row(sku, asin=asin)])
        self._save()
        self.assertEqual(self._gallery(sku).mapped('url'), ['https://es/only.jpg'])

    def test_family_variants_get_their_own_gallery(self):
        parent = 'B0ZZGALPAR'
        client = _GalleryClient(
            {'B0ZZGALV1': [_img('MAIN', 'https://v/1.jpg', 1000)], 'B0ZZGALV2': [_img('MAIN', 'https://v/2.jpg', 1000)]},
            parent_asin_by_asin={'B0ZZGALV1': parent, 'B0ZZGALV2': parent}, marketplace_id='ZZ_GAL_IT',
        )
        self._run(client, [make_report_row('ZZT-GALV-1', asin='B0ZZGALV1'), make_report_row('ZZT-GALV-2', asin='B0ZZGALV2')])
        self.assertEqual(self._save(), 2)
        v1 = self.env['product.product'].search([('x_amazon_asin', '=', 'B0ZZGALV1')])
        v2 = self.env['product.product'].search([('x_amazon_asin', '=', 'B0ZZGALV2')])
        self.assertEqual(v1.product_tmpl_id, v2.product_tmpl_id)
        self.assertEqual(v1.ke_media_ids.mapped('url'), ['https://v/1.jpg'])
        self.assertEqual(v2.ke_media_ids.mapped('url'), ['https://v/2.jpg'])
        self.assertEqual(len(v1.product_tmpl_id.ke_media_ids), 2)

    def test_download_missing_images_saves_gallery_first(self):
        asin, sku = 'B0ZZGAL003', 'ZZT-GAL-3'
        self._run(_GalleryClient({asin: [_img('MAIN', 'https://it/3.jpg', 1000)]}, marketplace_id='ZZ_GAL_IT'),
                  [make_report_row(sku, asin=asin)])
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT immagini', 'sync_type': 'download_images'})
        eng = self.se.SyncEngine(self.env, FakeSpApiClient())
        with no_commit(self.env), patch.object(self.se, '_fetch_image_b64', return_value=None):
            eng.download_missing_images(log)
        self.assertEqual(self._gallery(sku).mapped('url'), ['https://it/3.jpg'])
        self.assertIn('Galleria immagini salvata per 1 varianti', log.log_lines)
