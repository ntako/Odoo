"""
Modalità sicura (2026-11-11): con il parametro assente o spento nessuna
chiamata che modifica Amazon parte; le letture e la verifica senza creazione
restano libere.
"""
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.ke_amazon_sync.services.sp_api_client import (
    WRITES_ENABLED_PARAM, SpApiClient, _amazon_write, amazon_writes_enabled,
)


class _Client:
    def __init__(self, env):
        self._env = env
        self.calls = []

    @_amazon_write
    def patch_price(self, sku, price, validation_only=False):
        self.calls.append(sku)
        return 'ok'


@tagged('post_install', '-at_install')
class TestSafeMode(TransactionCase):

    def setUp(self):
        super().setUp()
        self.params = self.env['ir.config_parameter'].sudo()
        self.params.search([('key', '=', WRITES_ENABLED_PARAM)]).unlink()

    def test_missing_parameter_means_safe(self):
        self.assertFalse(amazon_writes_enabled(self.env))
        client = _Client(self.env)
        with self.assertRaises(UserError):
            client.patch_price('ZZT-1', 10)
        self.assertEqual(client.calls, [], 'nessuna chiamata partita')
        self.assertEqual(client.patch_price('ZZT-1', 10, validation_only=True), 'ok', 'la sola verifica è libera')

    def test_enabled_lets_writes_through(self):
        self.params.set_param(WRITES_ENABLED_PARAM, 'True')
        self.assertEqual(_Client(self.env).patch_price('ZZT-1', 10), 'ok')

    def test_real_client_write_methods_are_guarded(self):
        for name in ('patch_price', 'put_listing', 'patch_quantity', 'create_inbound_plan',
                     'confirm_placement_option', 'confirm_transportation_options', 'cancel_inbound_plan'):
            self.assertTrue(getattr(getattr(SpApiClient, name), '_amazon_write', False), name)
        for name in ('fetch_inventory_page', 'request_listings_report', 'get_fees_estimates', 'create_item_labels'):
            self.assertFalse(getattr(getattr(SpApiClient, name), '_amazon_write', False), name)

    def test_settings_round_trip_and_mixin(self):
        settings = self.env['res.config.settings'].create({})
        self.assertTrue(settings.amazon_safe_mode)
        settings.amazon_safe_mode = False
        settings.set_values()
        self.assertTrue(amazon_writes_enabled(self.env))
        self.assertFalse(self.env['product.template'].new({'name': 'x'}).ke_amazon_safe_mode)
        settings.amazon_safe_mode = True
        settings.set_values()
        self.assertFalse(amazon_writes_enabled(self.env))
        self.assertTrue(self.env['product.template'].new({'name': 'x'}).ke_amazon_safe_mode)

    def test_price_qty_cron_and_button_skip(self):
        with patch.object(SpApiClient, 'validate_credentials') as validate:
            self.env['ke.amazon.sync.log']._cron_sync_prices_quantities()
            validate.assert_not_called()
        with self.assertRaises(UserError):
            self.env['res.config.settings'].create({}).action_run_sync_prices_quantities()
