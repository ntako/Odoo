"""
Analisi prezzi e commissioni Amazon (2026-11-06): lettura della risposta di
getMyFeesEstimates, aggiornamento delle commissioni sugli annunci (client
finto, nessuna chiamata reale) e numeri della vista ke.amazon.price.analysis.
"""
from odoo.tests.common import TransactionCase, tagged

from .common import no_commit


def _fee_row(ident, referral, fba=0.0, status='Success'):
    details = [{'FeeType': 'ReferralFee', 'FeeAmount': {'Amount': referral}, 'FinalFee': {'Amount': referral}}]
    if fba:
        details.append({'FeeType': 'FBAFees', 'FeeAmount': {'Amount': fba}, 'FinalFee': {'Amount': fba}})
    return {
        'Status': status,
        'FeesEstimateIdentifier': {'SellerInputIdentifier': str(ident)},
        'FeesEstimate': {'TotalFeesEstimate': {'Amount': referral + fba}, 'FeeDetailList': details}
        if status == 'Success' else None,
        'Error': None if status == 'Success' else {'Code': 'InvalidParameterValue', 'Message': 'SKU sconosciuto'},
    }


class _FeesClient:
    def __init__(self):
        self.calls = []

    def get_fees_estimates(self, items):
        from odoo.addons.ke_amazon_sync.services.sp_api_client import parse_fees_estimates
        self.calls.append(items)
        rows = [_fee_row(it['id'], round(it['price'] * 0.15, 2), 3.5 if it['is_fba'] else 0.0)
                if it['sku'] != 'ZZT-PA-BAD' else _fee_row(it['id'], 0, status='ClientError') for it in items]
        return parse_fees_estimates(rows)


@tagged('post_install', '-at_install')
class TestPriceAnalysis(TransactionCase):

    def setUp(self):
        super().setUp()
        self.mp = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT IT analisi', 'code': 'ZZPA', 'marketplace_id': 'ZZ_PA_IT', 'active': True,
            'currency_id': self.env.company.currency_id.id, 'vat_rate': 22.0,
        })
        self.tmpl = self.env['product.template'].create({
            'name': 'ZZT analisi', 'type': 'consu', 'list_price': 20.0, 'standard_price': 10.0})
        self.variant = self.tmpl.product_variant_id
        Listing = self.env['ke.amazon.listing']
        self.fba = Listing.create({'product_id': self.variant.id, 'marketplace_id': self.mp.id, 'sku': 'ZZT-PA-1',
                                   'status': 'active', 'price': 30.5, 'fulfillment': 'fba'})
        self.bad = Listing.create({'product_id': self.variant.id, 'marketplace_id': self.mp.id, 'sku': 'ZZT-PA-BAD',
                                   'status': 'active', 'price': 24.4, 'fulfillment': 'fbm'})

    def _update(self, client):
        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        log = self.env['ke.amazon.sync.log'].create({'name': 'ZZT comm', 'sync_type': 'listing_fees'})
        with no_commit(self.env):
            return SyncEngine(self.env, None).update_listing_fees(log, self.mp, lambda mp: client)

    def test_fees_written_and_errors_kept(self):
        client = _FeesClient()
        done, skipped, errors = self._update(client)
        self.assertEqual((done, errors), (1, 1))
        self.assertAlmostEqual(self.fba.fee_referral, 4.58, places=2)
        self.assertAlmostEqual(self.fba.fee_fulfillment, 3.5, places=2)
        self.assertAlmostEqual(self.fba.fee_total, 8.08, places=2)
        self.assertEqual(self.fba.fee_price, 30.5)
        self.assertIn('SKU sconosciuto', self.bad.fee_error)
        self.assertEqual(client.calls[0][0]['currency'], self.env.company.currency_id.name)
        # Secondo giro: lo stimato allo stesso prezzo si salta, l'errore si riprova.
        client2 = _FeesClient()
        done, skipped, errors = self._update(client2)
        self.assertEqual(skipped, 1)
        self.assertEqual([it['sku'] for it in client2.calls[0]], ['ZZT-PA-BAD'])

    def test_analysis_view_numbers(self):
        self._update(_FeesClient())
        self.env.flush_all()
        row = self.env['ke.amazon.price.analysis'].search([('listing_id', '=', self.fba.id)])
        self.assertAlmostEqual(row.price_net, 25.0, places=2)        # 30,50 / 1,22
        self.assertAlmostEqual(row.cost, 10.0, places=2)
        self.assertAlmostEqual(row.markup_cost_pct, 150.0, places=1)  # 25 / 10 − 1
        self.assertAlmostEqual(row.markup_base_pct, 25.0, places=1)   # 25 / 20 − 1
        self.assertAlmostEqual(row.margin, 6.92, places=2)            # 25 − 8,08 − 10
        self.assertAlmostEqual(row.fee_pct, 26.5, places=1)           # 8,08 / 30,50
        self.assertTrue(row.has_cost and row.has_fees)
        bad = self.env['ke.amazon.price.analysis'].search([('listing_id', '=', self.bad.id)])
        self.assertFalse(bad.has_fees, 'stima in errore: niente commissioni né margine')

    def test_sync_price_and_variation(self):
        self.env.ref('ke_amazon_sync.pricelist_amazon').item_ids.write({'price_discount': -15.0})
        done, errors = self.env['ke.amazon.listing']._compute_sync_prices(self.fba | self.bad)
        self.assertEqual((done, errors), (2, 0))
        expected = self.variant.get_amazon_sale_price(self.mp)
        self.assertAlmostEqual(self.fba.price_sync, expected, places=2)
        self.env.flush_all()
        row = self.env['ke.amazon.price.analysis'].search([('listing_id', '=', self.fba.id)])
        self.assertAlmostEqual(row.price_sync_diff_pct, round(100 * (expected / 30.5 - 1), 1), places=1)
