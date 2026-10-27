"""
Test per il report etichetta FNSKU (report/amazon_fba_label_report.xml) —
trovati e corretti dal vivo il 2026-10-27 analizzando un PDF reale stampato
dall'utente: barcode assente (fetch HTTP relativo verso /report/barcode/...
non risolvibile da wkhtmltopdf), formato pagina ruotato (50x25mm diventava
25x50mm), più copie impilate su un'unica pagina invece di una per etichetta.
Non testiamo qui la resa wkhtmltopdf vera e propria (Odoo la salta durante i
test, vedi ir_actions_report._run_wkhtmltopdf) — solo l'HTML che le genera,
che è la parte sotto il nostro controllo.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install', 'ke_amazon_sync_matching')
class TestLabelReport(TransactionCase):

    def setUp(self):
        super().setUp()
        self.product = self.env['product.product'].create({
            'name': 'ZZT Prodotto Report', 'type': 'consu', 'is_storable': True,
            'default_code': 'ZZT-REPORT-1', 'x_amazon_fnsku': 'B0ZZREPORT1',
        })

    def test_barcode_data_uri_has_no_network_dependency(self):
        log = self.env['ke.amazon.fba.label.log'].create({
            'product_id': self.product.id, 'fnsku': 'B0ZZREPORT1',
        })
        uri = log._get_barcode_data_uri()
        self.assertTrue(uri.startswith('data:image/png;base64,'))

    def test_report_html_embeds_barcode_as_data_uri_not_http(self):
        log = self.env['ke.amazon.fba.label.log'].create({
            'product_id': self.product.id, 'fnsku': 'B0ZZREPORT1',
        })
        html, _ = self.env['ir.actions.report']._render_qweb_html(
            'ke_amazon_sync.report_amazon_fba_label_document', log.ids,
        )
        html = html.decode() if isinstance(html, bytes) else html
        self.assertIn('data:image/png;base64,', html)
        self.assertNotIn('/report/barcode/', html, 'il barcode non deve dipendere da un fetch HTTP')

    def test_report_html_no_external_assets(self):
        """Niente t-call="web.html_container": nessun <link>/<script> esterno
        — vedi il commento nel template per il perché (bug di encoding su
        wkhtmltopdf quando quella richiesta di rete fallisce)."""
        log = self.env['ke.amazon.fba.label.log'].create({
            'product_id': self.product.id, 'fnsku': 'B0ZZREPORT1',
        })
        html, _ = self.env['ir.actions.report']._render_qweb_html(
            'ke_amazon_sync.report_amazon_fba_label_document', log.ids,
        )
        html = html.decode() if isinstance(html, bytes) else html
        self.assertNotIn('<link', html)
        self.assertNotIn('<script', html)
        self.assertIn('<main>', html, "ir.actions.report._prepare_html cerca '//main'")

    def test_report_html_page_break_on_all_but_last(self):
        logs = self.env['ke.amazon.fba.label.log'].create([{
            'product_id': self.product.id, 'fnsku': 'B0ZZREPORT1',
        } for _ in range(3)])
        html, _ = self.env['ir.actions.report']._render_qweb_html(
            'ke_amazon_sync.report_amazon_fba_label_document', logs.ids,
        )
        html = html.decode() if isinstance(html, bytes) else html
        self.assertEqual(
            html.count('page-break-after:always;'), 2,
            'con N etichette, N-1 devono avere il page-break-after (non la copia finale, '
            'altrimenti wkhtmltopdf produce una pagina vuota di troppo)',
        )

    def test_report_paperformat_is_wide_not_tall(self):
        paperformat = self.env.ref('ke_amazon_sync.paperformat_amazon_fba_label')
        self.assertEqual(paperformat.page_width, 50)
        self.assertEqual(paperformat.page_height, 25)
        self.assertEqual(
            paperformat.orientation, 'Portrait',
            "'Landscape' qui farebbe ruotare page_width/page_height già larghi, "
            'producendo un\'etichetta 25x50mm invece di 50x25mm (verificato dal vivo via pdfinfo)',
        )
