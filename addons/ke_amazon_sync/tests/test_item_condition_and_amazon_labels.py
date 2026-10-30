"""
Test per la condizione Amazon sull'etichetta FNSKU e per l'etichetta
scaricata da Amazon (richiesti dall'utente il 2026-10-30):
- la sync giacenze FBA salva InventorySummary.condition per SKU
  (SyncEngine._store_item_conditions) e l'etichetta Odoo/ZPL la stampa;
- con "Etichette FNSKU (PDF): Scaricata da Amazon" il PDF viene da
  createMarketplaceItemLabels invece che dal report Odoo.
Nessuna chiamata reale: il client delle etichette è sostituito tramite il
metodo di wiring _build_label_client, mai mock.patch su SpApiClient.
"""
import io
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
from odoo.addons.ke_amazon_sync.services.zpl_builder import build_fnsku_zpl


def _tiny_pdf():
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(10, 10, 'etichetta')
    pdf.save()
    return buffer.getvalue()


class FakeLabelClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.requests = []

    def create_item_labels(self, msku_quantities, width_mm, height_mm, locale_code='it_IT'):
        if self.fail:
            raise RuntimeError('Amazon non raggiungibile (simulato)')
        self.requests.append((msku_quantities, width_mm, height_mm))
        return _tiny_pdf()


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestItemConditionAndAmazonLabels(TransactionCase):

    def setUp(self):
        super().setUp()
        self.marketplace = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT IT', 'code': 'ZZTCO', 'marketplace_id': 'ZZT_MKT_CO', 'active': True,
        })
        self.product = self.env['product.product'].create({
            'name': 'ZZT Prodotto condizione', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-COND-1', 'x_amazon_sku': 'ZZT-COND-1', 'x_amazon_fnsku': 'X0ZZCOND01',
        })
        self.listing = self.env['ke.amazon.listing'].create({
            'product_id': self.product.id, 'marketplace_id': self.marketplace.id,
        })

    def test_inventory_condition_is_stored_per_sku(self):
        engine = SyncEngine(self.env, None)
        engine._store_item_conditions([
            {'sku': 'ZZT-COND-1', 'asin': 'B0ZZ', 'qty': 1, 'condition': 'UsedVeryGood'},
            {'sku': 'ZZT-SCONOSCIUTO', 'asin': 'B0YY', 'qty': 1, 'condition': 'NewItem'},
            {'sku': 'ZZT-COND-1', 'asin': 'B0ZZ', 'qty': 1, 'condition': 'Strano'},
        ], self.marketplace)
        self.assertEqual(self.listing.condition, 'used_very_good')
        self.assertEqual(self.product.product_tmpl_id.x_amazon_condition, 'used_very_good',
                         'variante singola: anche sul template')
        lines = engine._condition_log_lines('[IT] ')
        self.assertIn('[IT] Condizione Amazon aggiornata per 1 SKU.', lines)
        self.assertTrue(any('Strano' in line for line in lines), 'valore non riconosciuto segnalato, non salvato')

    def test_new_variants_map_to_nuovo(self):
        engine = SyncEngine(self.env, None)
        self.listing.condition = 'used_good'
        engine._store_item_conditions([{'sku': 'ZZT-COND-1', 'condition': 'New Item'}], self.marketplace)
        self.assertEqual(self.listing.condition, 'new_new')

    def test_condition_label_text(self):
        self.assertEqual(self.product._get_amazon_condition_label(), 'Nuovo')
        self.listing.condition = 'used_like_new'
        self.assertEqual(self.product._get_amazon_condition_label(), 'Usato — Come nuovo')

    def test_odoo_label_and_zpl_print_condition(self):
        log = self.env['ke.amazon.fba.label.log'].create({
            'product_id': self.product.id, 'fnsku': 'X0ZZCOND01',
        })
        html, _ = self.env['ir.actions.report']._render_qweb_html(
            'ke_amazon_sync.report_amazon_fba_label_document', log.ids,
        )
        html = html.decode() if isinstance(html, bytes) else html
        self.assertIn('Nuovo', html)
        self.assertIn('^FDNuovo^FS', build_fnsku_zpl('X0ZZCOND01', 'ZZT-COND-1', 'Nome', 'Nuovo'))
        self.assertNotIn('^FO20,172', build_fnsku_zpl('X0ZZCOND01', 'ZZT-COND-1', 'Nome'))

    def test_labels_from_amazon_when_configured(self):
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.fnsku_label_source', 'amazon')
        client = FakeLabelClient()
        Log = self.env['ke.amazon.fba.label.log']
        with patch.object(type(Log), '_build_label_client', lambda self: client):
            result = Log._create_and_dispatch_many([(self.product, 'X0ZZCOND01', 3, False)])
        action = result['report_action']
        self.assertEqual(action['type'], 'ir.actions.act_url', 'PDF di Amazon, non il report Odoo')
        self.assertIn('/web/content/', action['url'])
        msku_quantities, width, height = client.requests[0]
        self.assertEqual(msku_quantities, [{'msku': 'ZZT-COND-1', 'quantity': 3}])
        self.assertEqual((width, height), (40, 30), 'stesso formato del report Odoo')
        self.assertEqual(Log.search_count([('product_id', '=', self.product.id)]), 3)

    def test_amazon_label_failure_is_explicit(self):
        self.env['ir.config_parameter'].sudo().set_param('ke_amazon_sync.fnsku_label_source', 'amazon')
        Log = self.env['ke.amazon.fba.label.log']
        with patch.object(type(Log), '_build_label_client', lambda self: FakeLabelClient(fail=True)):
            with self.assertRaises(UserError):
                Log._create_and_dispatch_many([(self.product, 'X0ZZCOND01', 1, False)])

    def test_odoo_labels_by_default(self):
        result = self.env['ke.amazon.fba.label.log']._create_and_dispatch_many(
            [(self.product, 'X0ZZCOND01', 1, False)],
        )
        self.assertEqual(result['report_action']['type'], 'ir.actions.report')
