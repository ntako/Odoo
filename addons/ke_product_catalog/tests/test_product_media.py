from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProductMedia(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Media = self.env['ke.product.media']
        self.tmpl = self.env['product.template'].create({'name': 'ZZT galleria', 'type': 'consu'})
        self.variant = self.tmpl.product_variant_id

    def _urls(self, source='amazon'):
        return self.Media.search([
            ('product_tmpl_id', '=', self.tmpl.id), ('source', '=', source),
        ]).sorted('sequence').mapped('url')

    def test_set_creates_in_order(self):
        changed = self.Media._set_source_images(self.tmpl, self.variant, 'amazon', [
            {'url': 'https://x/main.jpg', 'role': 'MAIN', 'width': 1000, 'height': 1000},
            {'url': 'https://x/pt01.jpg', 'role': 'PT01'},
        ], source_ref='Amazon IT')
        self.assertTrue(changed)
        self.assertEqual(self._urls(), ['https://x/main.jpg', 'https://x/pt01.jpg'])
        main = self.Media.search([('url', '=', 'https://x/main.jpg')])
        self.assertEqual((main.role, main.width, main.product_id, main.source_ref),
                         ('MAIN', 1000, self.variant, 'Amazon IT'))
        self.assertEqual(self.variant.ke_media_ids, self.tmpl.ke_media_ids)

    def test_set_replaces_only_its_source_and_is_idempotent(self):
        self.Media.create({'product_tmpl_id': self.tmpl.id, 'url': 'https://f/a.jpg', 'source': 'supplier'})
        images = [{'url': 'https://x/1.jpg', 'role': 'MAIN'}, {'url': 'https://x/2.jpg', 'role': 'PT01'}]
        self.Media._set_source_images(self.tmpl, self.variant, 'amazon', images)
        self.assertFalse(self.Media._set_source_images(self.tmpl, self.variant, 'amazon', images),
                         'stesso elenco: nessuna scrittura')
        self.Media._set_source_images(self.tmpl, self.variant, 'amazon', [{'url': 'https://x/2.jpg', 'role': 'MAIN'}])
        self.assertEqual(self._urls(), ['https://x/2.jpg'])
        self.assertEqual(self.Media.search([('url', '=', 'https://x/2.jpg')]).role, 'MAIN')
        self.assertEqual(self._urls('supplier'), ['https://f/a.jpg'], 'le altre fonti non si toccano')

    def test_empty_list_keeps_existing(self):
        self.Media._set_source_images(self.tmpl, self.variant, 'amazon', [{'url': 'https://x/1.jpg'}])
        self.assertFalse(self.Media._set_source_images(self.tmpl, self.variant, 'amazon', []))
        self.assertEqual(self._urls(), ['https://x/1.jpg'])

    def test_create_from_variant_fills_template(self):
        media = self.Media.create({'product_id': self.variant.id, 'url': 'https://m/1.jpg'})
        self.assertEqual(media.product_tmpl_id, self.tmpl)
        self.assertEqual(media.source, 'manual')
