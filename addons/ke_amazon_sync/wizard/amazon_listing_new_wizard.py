from odoo import api, fields, models
from odoo.exceptions import UserError

_CONDITIONS = [
    ('new_new', 'Nuovo'),
    ('used_like_new', 'Usato — Come nuovo'),
    ('used_very_good', 'Usato — Molto buono'),
    ('used_good', 'Usato — Buono'),
    ('used_acceptable', 'Usato — Accettabile'),
]


class AmazonListingNewWizard(models.TransientModel):
    """
    Nuova inserzione Amazon: offerta su un ASIN già presente nel catalogo
    Amazon, per una variante (anche di famiglia), su un marketplace a scelta,
    con uno SKU a scelta (proposto lo SKU principale; un secondo SKU dello
    stesso prodotto è ammesso). Prima "Verifica con Amazon"
    (mode=VALIDATION_PREVIEW: Amazon controlla senza creare nulla), poi
    "Pubblica", solo dopo una verifica senza errori e senza modifiche nel
    frattempo. La pubblicazione crea subito l'annuncio in Odoo
    (ke.amazon.listing) "In elaborazione"; lo stato vero lo aggiorna il
    prossimo Full Import o riconciliazione.

    Gli attributi inviati (merchant_suggested_asin, condition_type,
    purchasable_offer, fulfillment_availability) seguono lo schema del tipo
    di prodotto PRODUCT per le offerte; la documentazione letta il
    2026-11-05 conferma PRODUCT, purchasable_offer e VALIDATION_PREVIEW ma
    non gli altri nomi: per questo la pubblicazione passa sempre dalla
    verifica di Amazon.
    """
    _name = 'ke.amazon.listing.new.wizard'
    _inherit = ['ke.amazon.safe.mode.mixin']
    _description = 'Nuova inserzione Amazon'

    product_id = fields.Many2one('product.product', string='Variante', required=True)
    marketplace_id = fields.Many2one(
        'ke.amazon.marketplace', string='Marketplace', required=True, domain=[('active', '=', True)],
    )
    sku = fields.Char('SKU', required=True)
    asin = fields.Char('ASIN', required=True)
    fulfillment = fields.Selection(
        [('fba', 'FBA (Amazon Logistics)'), ('fbm', 'FBM (Merchant)')],
        string='Fulfillment', required=True, default='fbm',
    )
    condition = fields.Selection(_CONDITIONS, string='Condizione', required=True, default='new_new')
    currency_id = fields.Many2one('res.currency', string='Valuta', compute='_compute_currency_id')
    price = fields.Float('Prezzo IVA inclusa', required=True)
    quantity = fields.Integer('Quantità (FBM)', help='Solo FBM: per FBA la quantità la gestisce Amazon.')
    sync_price = fields.Boolean('Sync prezzo', default=False)
    sync_qty = fields.Boolean('Sync quantità', default=False)
    existing_listing_ids = fields.Many2many(
        'ke.amazon.listing', string='Annunci già presenti', compute='_compute_existing_listing_ids',
    )
    checked_signature = fields.Char(readonly=True)
    check_ok = fields.Boolean('Verifica superata', readonly=True)
    result = fields.Text('Esito', readonly=True)

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        ctx = self.env.context
        product = self.env['product.product']
        if ctx.get('active_model') == 'product.product' and ctx.get('active_id'):
            product = product.browse(ctx['active_id'])
        elif ctx.get('active_model') == 'product.template' and ctx.get('active_id'):
            tmpl = self.env['product.template'].browse(ctx['active_id'])
            product = tmpl.product_variant_id if len(tmpl.product_variant_ids) == 1 else product
        if product:
            vals['product_id'] = product.id
        return vals

    @api.depends('marketplace_id')
    def _compute_currency_id(self):
        for wiz in self:
            wiz.currency_id = wiz.marketplace_id.currency_id or self.env.company.currency_id

    @api.depends('product_id')
    def _compute_existing_listing_ids(self):
        for wiz in self:
            wiz.existing_listing_ids = wiz.product_id.x_amazon_listing_ids

    @api.onchange('product_id', 'marketplace_id')
    def _onchange_product_marketplace(self):
        product = self.product_id
        if not product:
            return
        tmpl = product.product_tmpl_id
        listings = product.x_amazon_listing_ids
        self.sku = product._amazon_main_sku()
        self.asin = (product.x_amazon_asin or (len(tmpl.product_variant_ids) == 1 and tmpl.x_amazon_asin)
                     or listings.filtered('asin')[:1].asin or '')
        main = listings.filtered(lambda l: l.fulfillment)[:1]
        self.fulfillment = main.fulfillment or tmpl.x_amazon_fulfillment or 'fbm'
        self.price = self._default_price()
        self.quantity = self._default_quantity()

    def _default_price(self):
        # Listino e IVA del marketplace; se manca (es. tasso GBP) il prezzo si scrive a mano.
        try:
            return self.product_id.get_amazon_sale_price(self.marketplace_id)
        except UserError:
            return 0.0

    def _default_quantity(self):
        warehouse = self.env.company.amazon_fbm_warehouse_id
        if not warehouse:
            return int(self.product_id.qty_available)
        quants = self.env['stock.quant'].search([
            ('product_id', '=', self.product_id.id),
            ('location_id', 'child_of', warehouse.view_location_id.id),
            ('location_id.usage', '=', 'internal'),
        ])
        return int(sum(quants.mapped('quantity')) - sum(quants.mapped('reserved_quantity')))

    def _signature(self):
        return '|'.join(str(v) for v in (
            self.product_id.id, self.marketplace_id.id, self.sku, self.asin, self.fulfillment,
            self.condition, round(self.price, 2), self.quantity if self.fulfillment == 'fbm' else '',
        ))

    def _attributes(self):
        mp = self.marketplace_id.marketplace_id
        currency = (self.currency_id.name or 'EUR')
        if self.fulfillment == 'fba':
            availability = [{'fulfillment_channel_code': 'AMAZON_EU'}]
        else:
            availability = [{'fulfillment_channel_code': 'DEFAULT', 'quantity': max(int(self.quantity), 0)}]
        return {
            'merchant_suggested_asin': [{'value': self.asin.strip(), 'marketplace_id': mp}],
            'condition_type': [{'value': self.condition, 'marketplace_id': mp}],
            'purchasable_offer': [{
                'marketplace_id': mp, 'currency': currency, 'audience': 'ALL',
                'our_price': [{'schedule': [{'value_with_tax': round(float(self.price), 2)}]}],
            }],
            'fulfillment_availability': availability,
        }

    def _check_inputs(self):
        self.ensure_one()
        self.sku = (self.sku or '').strip()
        self.asin = (self.asin or '').strip().upper()
        if not self.sku or not self.asin:
            raise UserError('SKU e ASIN sono obbligatori.')
        if self.price <= 0:
            raise UserError('Il prezzo deve essere maggiore di zero.')
        existing = self.env['ke.amazon.listing'].search([
            ('marketplace_id', '=', self.marketplace_id.id), ('sku', '=', self.sku),
        ], limit=1)
        if existing:
            raise UserError(
                f'Lo SKU "{self.sku}" ha già un annuncio su {self.marketplace_id.name} '
                f'(prodotto {existing.product_id.display_name}). Scegli un altro SKU.')

    def _client(self):
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        return SpApiClient(self.env, marketplace_id=self.marketplace_id.marketplace_id)

    @staticmethod
    def _issues_text(result):
        issues = result.get('issues') or []
        lines = [f"[{i.get('severity', '?')}] {i.get('code', '')} {i.get('attributeName') or ''}: "
                 f"{i.get('message', '')}".replace('  ', ' ') for i in issues]
        return '\n'.join(lines)

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
            'view_mode': 'form', 'target': 'new',
        }

    def action_check(self):
        """Verifica con Amazon (mode=VALIDATION_PREVIEW): nessun annuncio creato o modificato."""
        self._check_inputs()
        client = self._client()
        try:
            if client.listing_exists(self.sku):
                raise UserError(
                    f'Su Amazon ({self.marketplace_id.name}) esiste già un annuncio con lo SKU "{self.sku}", '
                    f'che Odoo non conosce ancora (lo registrerà il prossimo Full Import). Pubblicare lo '
                    f'sostituirebbe: scegli un altro SKU.')
            result = client.put_listing(self.sku, 'PRODUCT', self._attributes(), 'LISTING_OFFER_ONLY',
                                        validation_only=True)
        except UserError:
            raise
        except Exception as exc:
            raise UserError(f'Errore nella verifica con Amazon: {exc}') from exc
        issues = self._issues_text(result)
        errors = [i for i in (result.get('issues') or []) if i.get('severity') == 'ERROR']
        ok = result.get('status') != 'INVALID' and not errors
        self.write({
            'check_ok': ok,
            'checked_signature': self._signature() if ok else False,
            'result': ('Verifica superata: puoi pubblicare.' if ok else 'Amazon ha trovato degli errori:')
                      + (f'\n{issues}' if issues else ''),
        })
        return self._reopen()

    def action_publish(self):
        """Pubblica l'offerta su Amazon e crea l'annuncio in Odoo."""
        self._check_inputs()
        if not self.check_ok or self.checked_signature != self._signature():
            raise UserError('Prima "Verifica con Amazon" (di nuovo, se hai cambiato qualcosa).')
        try:
            result = self._client().put_listing(self.sku, 'PRODUCT', self._attributes(), 'LISTING_OFFER_ONLY')
        except Exception as exc:
            raise UserError(f'Errore nella pubblicazione su Amazon: {exc}') from exc
        errors = [i for i in (result.get('issues') or []) if i.get('severity') == 'ERROR']
        if result.get('status') == 'INVALID' or errors:
            self.write({'check_ok': False, 'result': 'Amazon ha rifiutato l\'annuncio:\n' + self._issues_text(result)})
            return self._reopen()
        listing = self.env['ke.amazon.listing'].sudo().create({
            'product_id': self.product_id.id,
            'marketplace_id': self.marketplace_id.id,
            'sku': self.sku,
            'asin': self.asin,
            'product_type': 'PRODUCT',
            'fulfillment': self.fulfillment,
            'condition': self.condition,
            'status': 'pending',
            'publish_state': 'pending',
            'price': self.price,
            'qty': self.quantity if self.fulfillment == 'fbm' else 0,
            'sync_price': self.sync_price,
            'sync_qty': self.sync_qty,
            'last_sync': fields.Datetime.now(),
        })
        product = self.product_id
        if not product._amazon_main_sku():
            product.x_amazon_sku = self.sku
        warnings = self._issues_text(result)
        product.product_tmpl_id.message_post(body=(
            f'Nuova inserzione Amazon {self.marketplace_id.name}: SKU {self.sku}, ASIN {self.asin}, '
            f'{dict(self._fields["fulfillment"].selection)[self.fulfillment]}, prezzo {self.price:.2f} '
            f'{self.currency_id.name or ""} (invio {result.get("submissionId", "")}).'
            + (f' Avvisi di Amazon: {warnings}' if warnings else '')
        ))
        return {
            'type': 'ir.actions.act_window', 'res_model': 'ke.amazon.listing', 'res_id': listing.id,
            'view_mode': 'form', 'target': 'current',
        }
