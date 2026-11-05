"""
Test per "Nuova inserzione Amazon" (ke.amazon.listing.new.wizard, 2026-11-05):
verifica con Amazon senza creare nulla, pubblicazione solo dopo una verifica
superata sugli stessi dati, annuncio creato in Odoo, SKU già usato bloccato.
Nessuna chiamata reale: il client Amazon è finto.
"""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


class _FakeClient:
    def __init__(self, status='ACCEPTED', issues=None, exists=False):
        self.calls = []
        self.status, self.issues, self.exists = status, issues or [], exists

    def listing_exists(self, sku):
        return self.exists

    def put_listing(self, sku, product_type, attributes, requirements, validation_only=False):
        self.calls.append({'sku': sku, 'product_type': product_type, 'attributes': attributes,
                           'requirements': requirements, 'validation_only': validation_only})
        return {'sku': sku, 'status': self.status, 'issues': self.issues, 'submissionId': 'ZZT-SUB'}


@tagged('post_install', '-at_install')
class TestListingNewWizard(TransactionCase):

    def setUp(self):
        super().setUp()
        self.mp = self.env['ke.amazon.marketplace'].create({
            'name': 'ZZT Nuova', 'code': 'ZZNW', 'marketplace_id': 'ZZ_NEW_MP', 'active': True,
        })
        self.tmpl = self.env['product.template'].create({
            'name': 'ZZT nuova inserzione', 'type': 'consu', 'is_storable': True, 'list_price': 20.0,
            'default_code': 'ZZT-NEW-1', 'x_amazon_sku': 'ZZT-NEW-1', 'x_amazon_asin': 'B0ZZNEW001',
        })
        self.variant = self.tmpl.product_variant_id

    def _wizard(self, **vals):
        wiz = self.env['ke.amazon.listing.new.wizard'].with_context(
            active_model='product.product', active_id=self.variant.id,
        ).new({'marketplace_id': self.mp.id})
        wiz._onchange_product_marketplace()
        values = {f: wiz[f] for f in ('product_id', 'marketplace_id', 'sku', 'asin', 'fulfillment', 'price', 'quantity')}
        values = {k: (v.id if hasattr(v, 'id') else v) for k, v in values.items()}
        values.update(vals)
        return self.env['ke.amazon.listing.new.wizard'].create(values)

    def test_defaults_from_variant(self):
        wiz = self._wizard()
        self.assertEqual((wiz.sku, wiz.asin), ('ZZT-NEW-1', 'B0ZZNEW001'))
        self.assertGreater(wiz.price, 0)

    def test_check_then_publish_creates_listing(self):
        wiz = self._wizard(sku='ZZT-NEW-1-B', fulfillment='fbm', quantity=4, price=21.5)
        client = _FakeClient()
        with patch.object(type(wiz), '_client', return_value=client):
            with self.assertRaises(UserError):
                wiz.action_publish()  # senza verifica
            wiz.action_check()
            self.assertTrue(wiz.check_ok)
            self.assertTrue(client.calls[0]['validation_only'])
            attrs = client.calls[0]['attributes']
            self.assertEqual(attrs['merchant_suggested_asin'][0]['value'], 'B0ZZNEW001')
            self.assertEqual(attrs['fulfillment_availability'], [{'fulfillment_channel_code': 'DEFAULT', 'quantity': 4}])
            self.assertEqual(attrs['purchasable_offer'][0]['our_price'][0]['schedule'][0]['value_with_tax'], 21.5)
            self.assertFalse(self.env['ke.amazon.listing'].search([('sku', '=', 'ZZT-NEW-1-B')]),
                             'la verifica non crea annunci')
            wiz.action_publish()
        self.assertFalse(client.calls[1]['validation_only'])
        listing = self.env['ke.amazon.listing'].search([('sku', '=', 'ZZT-NEW-1-B'), ('marketplace_id', '=', self.mp.id)])
        self.assertEqual((listing.product_id, listing.status, listing.asin), (self.variant, 'pending', 'B0ZZNEW001'))
        self.assertEqual(self.tmpl.x_amazon_sku, 'ZZT-NEW-1', 'lo SKU principale del prodotto non cambia')

    def test_change_after_check_requires_new_check(self):
        wiz = self._wizard(sku='ZZT-NEW-1-C')
        with patch.object(type(wiz), '_client', return_value=_FakeClient()):
            wiz.action_check()
            wiz.price = wiz.price + 1
            with self.assertRaises(UserError):
                wiz.action_publish()

    def test_fba_sends_eu_channel_without_quantity(self):
        wiz = self._wizard(sku='ZZT-NEW-1-F', fulfillment='fba', quantity=9)
        self.assertEqual(wiz._attributes()['fulfillment_availability'], [{'fulfillment_channel_code': 'AMAZON_EU'}])

    def test_amazon_errors_block_publish(self):
        wiz = self._wizard(sku='ZZT-NEW-1-E')
        client = _FakeClient(status='INVALID', issues=[{'severity': 'ERROR', 'code': '90220', 'message': 'manca'}])
        with patch.object(type(wiz), '_client', return_value=client):
            wiz.action_check()
        self.assertFalse(wiz.check_ok)
        self.assertIn('90220', wiz.result)

    def test_sku_already_used_is_blocked(self):
        self.env['ke.amazon.listing'].create({
            'product_id': self.variant.id, 'marketplace_id': self.mp.id, 'sku': 'ZZT-NEW-1',
        })
        wiz = self._wizard()
        with patch.object(type(wiz), '_client', return_value=_FakeClient()):
            with self.assertRaises(UserError):
                wiz.action_check()
        wiz2 = self._wizard(sku='ZZT-NEW-1-X')
        with patch.object(type(wiz2), '_client', return_value=_FakeClient(exists=True)):
            with self.assertRaises(UserError):
                wiz2.action_check()
