"""Giacenze e costi dal file del magazzino; il costo manuale vince sul listino (2026-11-24)."""
import csv
import io
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.ke_inventory_import.models.enrich import do_enrich
from odoo.addons.ke_inventory_import.models.stock_cost import do_stock

MAPPING = [('MPN', 'default_code'), ('EAN', 'barcode'), ('ASIN', '_asin'), ("Q.TA'", '_qty'),
           ('UBICAZIONE', '_location'), ('PREZZO UNITARIO', 'standard_price')]


@tagged('post_install', '-at_install')
class TestStockCost(TransactionCase):

    def setUp(self):
        super().setUp()
        patcher = patch.object(self.env.cr, 'commit', lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1).lot_stock_id
        Tmpl = self.env['product.template']
        mk = lambda name, **kw: Tmpl.create(dict(name=name, is_storable=True, **kw)).product_variant_id
        self.reel = mk('ZZS Reel', default_code='LNF-3001015', barcode='8032895101731')
        self.lure = mk('ZZS Lure', default_code='SBR-1', barcode='8059016722873')
        self.pf = mk('ZZS Berkley', default_code='PFS-1561222', barcode='0024099037303')
        self.other = mk('ZZS fuori dal file', default_code='ZZS-9', barcode='8000000000999')
        self.swapped_a = mk('ZZS A', default_code='TUB-21010', barcode='8053831129862')
        self.swapped_b = mk('ZZS B', default_code='TUB-21011', barcode='8053831129855')
        self.old_loc = self.env['stock.location'].create({'name': 'ZZS-VECCHIA', 'location_id': self.root.id,
                                                          'usage': 'internal'})
        Quant = self.env['stock.quant']
        Quant._update_available_quantity(self.reel, self.old_loc, 7)
        Quant._update_available_quantity(self.other, self.root, 5)
        Quant._update_available_quantity(self.pf, self.root, 3)

    def _run(self, rows, dry_run=False, skip=''):
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=[c for c, _t in MAPPING] + ['FORNITORE'], delimiter=',')
        writer.writeheader()
        writer.writerows(rows)
        job = self.env['inventory.import.job'].create({'stock_mode': True, 'delimiter': ',', 'dry_run': dry_run})
        do_stock(job, self.env, out.getvalue(), dict(
            delimiter=',', stock_mode=True, dry_run=dry_run, location_id=self.root.id, sublocations=True,
            qty_skip_values=skip,
            stock_mapping=[{'column': c, 'target': t, 'default': ''} for c, t in MAPPING]), self.env.cr)
        return job

    def _qty(self, variant, location=None):
        quants = self.env['stock.quant'].search([('product_id', '=', variant.id),
                                                 ('location_id', 'child_of', (location or self.root).id)])
        return sum(quants.mapped('quantity'))

    def _row(self, mpn, ean='', asin='', qty='', loc='', price='', supplier=''):
        return {'MPN': mpn, 'EAN': ean, 'ASIN': asin, "Q.TA'": qty, 'UBICAZIONE': loc, 'PREZZO UNITARIO': price,
                'FORNITORE': supplier}

    def test_quantities_costs_and_exclusions(self):
        rows = [
            self._row('3001015', '8032895101731', '', '0', 'M1', '11,97', 'LINEAEFFE'),
            self._row('3001015', '8032895101731', '', '4', 'M1-R12-S5-DX', '11,97', 'LINEAEFFE'),
            self._row('0000100300030', '8059016722873', '', '160', 'M2-R1-S1-CS', '', 'MA.GIA. PESCA'),
            self._row('0000001561222', '', '', '9', 'M1', '5,85', 'PURE FISHING'),
            self._row('ZZS-NONC', '8000000000001', '', '1', 'M1', '1', 'TEMU'),
        ]
        check = self._run(rows, dry_run=True, skip='PURE FISHING\n0000100300030')
        self.assertIn('SOLO VERIFICA', check.error_log)
        self.assertEqual(self._qty(self.reel), 7, 'solo verifica')

        job = self._run(rows, skip='PURE FISHING\n0000100300030')
        m1_r12 = self.env['stock.location'].search([('name', '=', 'M1-R12-S5-DX'), ('location_id', '=', self.root.id)])
        self.assertTrue(m1_r12, 'ubicazione creata come sotto-posizione')
        self.assertEqual(self._qty(self.reel, m1_r12), 4)
        self.assertEqual(self._qty(self.reel, self.old_loc), 0, 'altre sotto-posizioni a zero')
        self.assertEqual(self._qty(self.reel), 4)
        self.assertEqual(self._qty(self.lure), 0, 'codice in "Niente quantità per"')
        self.assertEqual(self._qty(self.pf), 3, 'Pure Fishing: quantità non toccata')
        self.assertEqual(self.pf.standard_price, 5.85, 'Pure Fishing: costo sì (codice senza zeri e prefisso)')
        self.assertEqual(self._qty(self.other), 5, 'prodotto fuori dal file non toccato')
        self.assertEqual((self.reel.standard_price, self.reel.ke_cost_source), (11.97, 'manual'))
        self.assertIn('non trovate: 1', job.error_log)

    def test_ean_wins_over_swapped_asin(self):
        if 'x_amazon_asin' not in self.env['product.product']._fields:
            self.skipTest('senza ke_amazon_sync')
        self.swapped_a.product_tmpl_id.x_amazon_asin = 'B0D36DFGWC'
        self.swapped_b.product_tmpl_id.x_amazon_asin = 'B0D36F9YQX'
        job = self._run([self._row('0000000021010', '8053831129862', 'B0D36F9YQX', '2', '', '3')])
        self.assertEqual(self._qty(self.swapped_a), 2)
        self.assertEqual(self._qty(self.swapped_b), 0)
        self.assertIn('ASIN indica un altro prodotto, vale EAN', job.error_log)

    def test_supplier_price_list_keeps_manual_cost(self):
        self._run([self._row('3001015', '8032895101731', '', '', '', '9,23')])
        self.assertEqual(self.reel.ke_cost_source, 'manual')
        self.other.standard_price = 0
        self.other.ke_cost_source = False
        self.lure.with_context(ke_cost_source='supplier').standard_price = 2.0
        self.swapped_a.product_tmpl_id.standard_price = 8.0   # scritto a mano dal prodotto
        self.assertEqual(self.swapped_a.ke_cost_source, 'manual')
        job = self.env['inventory.import.job'].create({'enrich_only': True, 'delimiter': ';', 'dry_run': False})
        listino = 'Codice;Prezzo\nLNF-3001015;12,92\nSBR-1;2,50\nZZS-9;4\nTUB-21010;9\n'
        do_enrich(job, self.env, listino, dict(
            delimiter=';', enrich_only=True, dry_run=False, match_ean=False, spec_text_lang='en_GB',
            weight_unit='kg', volume_unit='cm', supplier_tag='ZZS',
            enrich_mapping=[{'column': c, 'target': t, 'default': '', 'spec': '', 'lang': ''}
                            for c, t in (('Codice', 'default_code'), ('Prezzo', 'standard_price'))]), self.env.cr)
        self.assertEqual(self.reel.standard_price, 9.23, 'costo del magazzino: il listino non lo cambia')
        self.assertEqual(self.swapped_a.standard_price, 8.0, 'costo scritto a mano: il listino non lo cambia')
        self.assertEqual(self.lure.standard_price, 2.5, 'costo del listino: aggiornato')
        self.assertEqual((self.other.standard_price, self.other.ke_cost_source), (4.0, 'supplier'), 'costo mancante')
