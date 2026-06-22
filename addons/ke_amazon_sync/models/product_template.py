from datetime import datetime

from odoo import fields, models
from odoo.exceptions import UserError


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # --- Famiglia varianti Amazon ---
    x_amazon_parent_asin = fields.Char(
        'Amazon Parent ASIN', index=True, copy=False,
        help='Parent ASIN della famiglia varianti. Uguale per tutti i template della stessa famiglia.',
    )

    # --- Identificatori Amazon ---
    x_amazon_asin = fields.Char(
        'Amazon ASIN', index=True, copy=False,
        help='ASIN univoco su Amazon. Compilato automaticamente durante import o ricerca.',
    )
    x_amazon_sku = fields.Char(
        'Amazon SKU', index=True, copy=False,
        help='SKU interno Amazon (può differire dal default_code Odoo).',
    )
    x_amazon_product_type = fields.Char(
        'Amazon Product Type', copy=False,
        help='Tipo prodotto Amazon (es. FISHING_PRODUCT). Necessario per le PATCH API.',
    )

    # --- Stato su Amazon ---
    x_amazon_status = fields.Selection([
        ('active', 'Attivo'),
        ('inactive', 'Inattivo'),
        ('not_listed', 'Non listato'),
        ('found_manufacturer', 'Trovato (produttore)'),
        ('pending', 'In attesa'),
        ('error', 'Errore'),
    ], string='Stato Amazon', copy=False, index=True)

    x_amazon_fulfillment = fields.Selection([
        ('fba', 'FBA (Amazon Logistics)'),
        ('fbm', 'FBM (Merchant)'),
    ], string='Fulfillment', copy=False,
       help='FBA: Amazon gestisce le spedizioni. FBM: spedisci tu.')

    x_amazon_found_by_manufacturer = fields.Boolean(
        'Presente su Amazon (produttore)',
        copy=False,
        help='True se il prodotto è già listato su Amazon da un altro venditore/produttore.',
    )

    # --- Sincronizzazione ---
    x_amazon_sync_price = fields.Boolean(
        'Sync prezzo', default=False,
        help='Se attivo, il prezzo viene aggiornato automaticamente su Amazon.',
    )
    x_amazon_sync_qty = fields.Boolean(
        'Sync quantità', default=False,
        help='Attivo solo per FBM. Aggiorna la quantità disponibile su Amazon.',
    )
    x_amazon_last_sync = fields.Datetime('Ultima sync', copy=False, readonly=True)
    x_amazon_sync_error = fields.Text('Errore sync', copy=False, readonly=True)

    # --- Prezzi Amazon (lettura) ---
    x_amazon_price = fields.Float(
        'Prezzo Amazon (attuale)', copy=False, readonly=True,
        help='Prezzo letto da Amazon durante l\'ultimo import/sync.',
    )
    x_amazon_qty = fields.Integer(
        'Quantità Amazon (FBA)', copy=False, readonly=True,
        help='Quantità disponibile FBA letta da Amazon.',
    )

    # ------------------------------------------------------------------
    # Azioni manuali sul singolo prodotto
    # ------------------------------------------------------------------

    def _get_sp_client(self):
        from odoo.addons.ke_amazon_sync.services.sp_api_client import SpApiClient
        client = SpApiClient(self.env)
        try:
            client.validate_credentials()
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        return client

    def action_amazon_sync_price_qty(self):
        """Sincronizza prezzo e/o quantità di questo prodotto su Amazon."""
        self.ensure_one()
        if not self.x_amazon_sku:
            raise UserError('Questo prodotto non ha un Amazon SKU.')
        if not self.x_amazon_asin:
            raise UserError('Questo prodotto non ha un Amazon ASIN.')

        client = self._get_sp_client()
        sku = self.x_amazon_sku
        product_type = self.x_amazon_product_type or 'PRODUCT'
        errors = []

        if self.x_amazon_sync_price:
            try:
                client.patch_price(sku, product_type, self.list_price)
            except Exception as exc:
                errors.append(f'prezzo: {exc}')

        if self.x_amazon_sync_qty and self.x_amazon_fulfillment == 'fbm':
            try:
                qty = int(self.with_context(warehouse=False).qty_available)
                client.patch_quantity(sku, product_type, qty)
            except Exception as exc:
                errors.append(f'quantità: {exc}')

        now = datetime.now()
        if errors:
            self.write({'x_amazon_sync_error': '; '.join(errors), 'x_amazon_last_sync': now})
            raise UserError('Sync completata con errori:\n' + '\n'.join(errors))
        self.write({'x_amazon_sync_error': False, 'x_amazon_last_sync': now})

    def action_amazon_download_image(self):
        """Scarica l'immagine da Amazon Catalog per questo prodotto."""
        self.ensure_one()
        if not self.x_amazon_asin:
            raise UserError('Questo prodotto non ha un Amazon ASIN.')

        from odoo.addons.ke_amazon_sync.services.sync_engine import _fetch_image_b64
        client = self._get_sp_client()
        catalog = client.get_catalog_item(self.x_amazon_asin)
        images_data = catalog.get('images', [{}])
        images_block = images_data[0] if images_data else {}
        all_images = images_block.get('images', [])
        main_images = [i.get('link', '') for i in all_images
                       if i.get('variant') == 'MAIN' and i.get('link')]
        url = main_images[0] if main_images else ''
        img = _fetch_image_b64(url) if url else None
        if not img:
            raise UserError('Nessuna immagine disponibile su Amazon per questo prodotto.')
        self.write({'image_1920': img})

    def action_amazon_refresh_from_catalog(self):
        """Aggiorna nome, descrizione, EAN e immagine dal Catalog Amazon."""
        self.ensure_one()
        if not self.x_amazon_asin:
            raise UserError('Questo prodotto non ha un Amazon ASIN.')

        from odoo.addons.ke_amazon_sync.services.sync_engine import (
            _fetch_image_b64, _merge_catalog,
        )
        client = self._get_sp_client()
        catalog = client.get_catalog_item(self.x_amazon_asin)
        if not catalog:
            raise UserError('Nessun dato trovato su Amazon per ASIN: ' + self.x_amazon_asin)

        product = {
            'sku': self.x_amazon_sku or '',
            'asin': self.x_amazon_asin,
            'name': self.name,
            'status': '',
            'product_type': self.x_amazon_product_type or '',
            'price': self.list_price,
            'fulfillment': self.x_amazon_fulfillment or 'fbm',
            'attributes': {},
        }
        _merge_catalog(product, catalog)

        vals = {'x_amazon_last_sync': datetime.now()}
        if product.get('name'):
            vals['name'] = product['name']
        if product.get('ean') and not self.barcode:
            vals['barcode'] = product['ean']
        if product.get('image_url'):
            img = _fetch_image_b64(product['image_url'])
            if img:
                vals['image_1920'] = img
        if product.get('product_type'):
            vals['x_amazon_product_type'] = product['product_type']
        self.write(vals)
