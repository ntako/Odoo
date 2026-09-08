from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError


def _amazon_pricelist_items_by_priority(pricelist, product):
    """
    Ritorna le regole del listino applicabili a 'product', dalla più specifica
    alla più generica — stessa priorità che userebbe Odoo: variante → template
    → categoria (risalendo la gerarchia) → globale. Possono esserci più regole
    allo stesso livello (es. una 'fixed' e una 'formula' entrambe sulla stessa
    variante): vengono ritornate tutte, nell'ordine.
    """
    variant = product if product._name == 'product.product' else product.product_variant_id
    template = variant.product_tmpl_id if variant else product
    items = pricelist.item_ids
    result = []
    if variant:
        result += list(items.filtered(
            lambda i: i.applied_on == '0_product_variant' and i.product_id == variant
        ))
    if template:
        result += list(items.filtered(
            lambda i: i.applied_on == '1_product' and i.product_tmpl_id == template
        ))
    categ = template.categ_id if template else False
    while categ:
        result += list(items.filtered(
            lambda i: i.applied_on == '2_product_category' and i.categ_id == categ
        ))
        categ = categ.parent_id
    result += list(items.filtered(lambda i: i.applied_on == '3_global'))
    return result


def _apply_amazon_pricelist_markup(product, base_price):
    """
    Applica al prezzo base le regole del listino 'Amazon' (Vendite/Inventario →
    Configurazione → Listini prezzi) applicabili a questo prodotto specifico,
    rispettando la priorità di Odoo (variante > template > categoria > globale)
    invece di usare sempre la prima regola del listino.
    - Una regola 'fixed' più specifica sostituisce il prezzo base netto di
      partenza (non è il prezzo finale: la maggiorazione si applica comunque).
    - La prima regola 'formula'/'percentage' trovata (alla priorità più alta
      disponibile) fornisce la percentuale di maggiorazione.
    Se il listino/le regole non esistono, ritorna il prezzo base invariato.
    """
    pricelist = product.env.ref('ke_amazon_sync.pricelist_amazon', raise_if_not_found=False)
    if not pricelist or not pricelist.item_ids:
        return base_price

    applicable = _amazon_pricelist_items_by_priority(pricelist, product)

    fixed_item = next((i for i in applicable if i.compute_price == 'fixed'), None)
    price = fixed_item.fixed_price if fixed_item else base_price

    markup_item = next((i for i in applicable if i.compute_price in ('formula', 'percentage')), None)
    if markup_item:
        # Convenzione Odoo per le regole 'formula'/'percentage': uno sconto
        # negativo è una maggiorazione.
        markup_pct = -(markup_item.price_discount or 0.0)
        price = price * (1 + markup_pct / 100.0)
        if markup_item.price_round:
            price = round(price / markup_item.price_round) * markup_item.price_round
    return price


def _get_amazon_vat_info(product):
    """
    Ritorna (aliquota, price_include) dalla prima tassa di vendita configurata
    sul prodotto (product.taxes_id) — così il calcolo si adatta da solo sia
    all'aliquota reale (22%/10%/4%...) sia al fatto che quel prodotto tratti
    list_price come netto o come lordo (price_include della tassa), senza dover
    verificare a mano le impostazioni fiscali generali di Odoo.
    Se il prodotto non ha tasse di vendita assegnate, usa l'aliquota di default
    configurata in Impostazioni → Amazon e assume list_price netto.
    """
    tax = product.taxes_id.filtered(
        lambda t: t.type_tax_use == 'sale' and t.amount_type == 'percent'
    )[:1]
    if tax:
        return tax.amount, tax.price_include
    rate = float(product.env['ir.config_parameter'].sudo().get_param(
        'ke_amazon_sync.default_vat_rate', '22'
    ))
    return rate, False


def compute_amazon_sale_price(product, base_price):
    """
    Prezzo finale IVA inclusa da comunicare ad Amazon, a partire da base_price
    (list_price o lst_price del prodotto, nella convenzione — netta o lorda —
    che risulta dalla sua configurazione fiscale): scorpora l'IVA se necessario
    → applica il markup del listino 'Amazon' sul netto → reintegra l'IVA.
    """
    rate, price_include = _get_amazon_vat_info(product)
    net_price = base_price / (1 + rate / 100.0) if price_include else base_price
    marked_up = _apply_amazon_pricelist_markup(product, net_price)
    return round(marked_up * (1 + rate / 100.0), 2)


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

    x_amazon_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Magazzino Amazon',
        compute='_compute_x_amazon_warehouse_id', store=True,
        help='Magazzino Odoo configurato (Impostazioni → Amazon) corrispondente al '
             'fulfillment di questo prodotto — solo indicativo, non riflette per forza '
             "dove si trova fisicamente la giacenza. Utile per filtrare/raggruppare "
             'il catalogo per magazzino Amazon.',
    )

    @api.depends('x_amazon_fulfillment')
    def _compute_x_amazon_warehouse_id(self):
        params = self.env['ir.config_parameter'].sudo()
        fba_id = int(params.get_param('ke_amazon_sync.fba_warehouse_id', 0) or 0)
        fbm_id = int(params.get_param('ke_amazon_sync.fbm_warehouse_id', 0) or 0)
        for product in self:
            if product.x_amazon_fulfillment == 'fba':
                product.x_amazon_warehouse_id = fba_id
            elif product.x_amazon_fulfillment == 'fbm':
                product.x_amazon_warehouse_id = fbm_id
            else:
                product.x_amazon_warehouse_id = False

    x_amazon_odoo_fbm_qty = fields.Float(
        'Giacenza Odoo (magazzino FBM)',
        compute='_compute_x_amazon_odoo_fbm_qty', store=True,
        help='Giacenza attuale in Odoo nel magazzino FBM configurato, calcolata alla '
             "stessa maniera di quanto verrebbe inviato ad Amazon. Si ricalcola a "
             "ogni sync prodotti — confrontala con 'Quantità Amazon (dichiarata)' "
             'per individuare disallineamenti tra i due sistemi.',
    )
    x_amazon_qty_diff = fields.Float(
        'Differenza giacenza (Amazon - Odoo)',
        compute='_compute_x_amazon_odoo_fbm_qty', store=True,
        help='x_amazon_qty meno la giacenza reale Odoo nel magazzino FBM. Zero = allineati. '
             'Utile per ordinare/filtrare il catalogo e trovare le divergenze più grandi.',
    )

    @api.depends('x_amazon_qty', 'x_amazon_fulfillment')
    def _compute_x_amazon_odoo_fbm_qty(self):
        params = self.env['ir.config_parameter'].sudo()
        fbm_id = int(params.get_param('ke_amazon_sync.fbm_warehouse_id', 0) or 0)
        fbm_warehouse = self.env['stock.warehouse'].browse(fbm_id) if fbm_id else None
        ctx = {'warehouse': fbm_warehouse.id} if fbm_warehouse else {}
        for product in self:
            if product.x_amazon_fulfillment != 'fbm' or not fbm_warehouse:
                product.x_amazon_odoo_fbm_qty = 0.0
                product.x_amazon_qty_diff = 0.0
                continue
            odoo_qty = product.with_context(**ctx).qty_available
            product.x_amazon_odoo_fbm_qty = odoo_qty
            product.x_amazon_qty_diff = (product.x_amazon_qty or 0) - odoo_qty

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

    # --- Categoria Amazon ---
    x_amazon_category = fields.Char(
        'Categoria Amazon', copy=False,
        help='Nome categoria browse Amazon (browseClassification).',
    )
    x_amazon_browse_node_id = fields.Char(
        'Amazon Browse Node ID', copy=False,
        help='ID numerico del browse node Amazon. Usato per la pubblicazione del listing.',
    )

    # --- Pubblicazione su Amazon ---
    x_amazon_brand = fields.Char(
        'Brand Amazon', copy=False,
        help='Brand/marchio come registrato su Amazon. Obbligatorio per creare un nuovo listing.',
    )
    x_amazon_condition = fields.Selection([
        ('new_new', 'Nuovo'),
        ('used_like_new', 'Usato — Come nuovo'),
        ('used_very_good', 'Usato — Molto buono'),
        ('used_good', 'Usato — Buono'),
        ('used_acceptable', 'Usato — Accettabile'),
    ], string='Condizione Amazon', default='new_new', copy=False)
    x_amazon_publish_state = fields.Selection([
        ('not_published', 'Non pubblicato'),
        ('pending', 'In elaborazione'),
        ('published', 'Pubblicato'),
        ('error', 'Errore'),
    ], string='Stato pubblicazione', default='not_published', readonly=True, copy=False)
    x_amazon_publish_error = fields.Text(
        'Errore pubblicazione', readonly=True, copy=False,
    )

    # Le dimensioni fisiche (lunghezza/larghezza/altezza) e il volume sono
    # campi neutri forniti dal modulo condiviso ke_product_dimensions (non
    # specifici di Amazon, riutilizzabili da altre integrazioni): vedi
    # product_length/product_width/product_height, scritti durante la sync.

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

    def get_amazon_sale_price(self):
        """
        Prezzo IVA inclusa da comunicare ad Amazon: list_price con il markup del
        listino 'Amazon' applicato (Vendite/Inventario → Configurazione →
        Listini prezzi) e l'IVA reintegrata — aliquota e convenzione
        netto/lordo lette dalle tasse di vendita configurate sul prodotto.
        """
        self.ensure_one()
        return compute_amazon_sale_price(self, self.list_price)

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
                client.patch_price(sku, product_type, self.get_amazon_sale_price())
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

    def action_amazon_search_browse_node(self):
        """Apre il wizard di ricerca categoria (browse node) Amazon."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ke.amazon.browse.node.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_product_id': self.id,
                'default_keyword': self.name or '',
            },
        }

    def action_amazon_search_product_type(self):
        """Apre il wizard di ricerca tipo prodotto Amazon."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ke.amazon.product.type.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_product_id': self.id,
                'default_keyword': self.name or '',
            },
        }

    def action_amazon_publish(self):
        """
        Pubblica questo prodotto su Amazon.
        - Se ha già un ASIN: crea/aggiorna l'offerta (prezzo, quantità, condizione).
        - Se non ha ASIN: invia un listing completo, Amazon assegnerà l'ASIN.
        """
        self.ensure_one()

        sku = self.x_amazon_sku or self.default_code
        if not sku:
            raise UserError(
                'Configura un Codice Prodotto (SKU) prima di pubblicare su Amazon.'
            )

        client = self._get_sp_client()
        marketplace_id = client.marketplace_id
        product_type = self.x_amazon_product_type or 'PRODUCT'
        condition = self.x_amazon_condition or 'new_new'
        price = self.get_amazon_sale_price()
        is_fba = self.x_amazon_fulfillment == 'fba'

        if self.x_amazon_asin:
            # --- Scenario A: ASIN noto → offerta su listing esistente ---
            qty = 0 if is_fba else int(self.with_context(warehouse=False).qty_available)
            fulfillment_code = 'AMAZON_NA' if is_fba else 'DEFAULT'
            attributes = {
                'condition_type': [{'value': condition, 'marketplace_id': marketplace_id}],
                'list_price': [{'value': price, 'currency': 'EUR', 'marketplace_id': marketplace_id}],
                'fulfillment_availability': [{
                    'fulfillment_channel_code': fulfillment_code,
                    'quantity': qty,
                    'marketplace_id': marketplace_id,
                }],
            }
            requirements = 'LISTING_OFFER_ONLY'
        else:
            # --- Scenario B: nessun ASIN → listing completo ---
            if not self.x_amazon_brand:
                raise UserError(
                    'Inserisci il Brand Amazon (campo "Brand Amazon") prima di pubblicare un nuovo listing.'
                )
            qty = int(self.with_context(warehouse=False).qty_available)
            base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
            image_url = f'{base_url}/web/image/product.template/{self.id}/image_1920' if base_url else ''

            attributes = {
                'item_name': [{'value': self.name, 'language_tag': client.locale, 'marketplace_id': marketplace_id}],
                'brand': [{'value': self.x_amazon_brand, 'marketplace_id': marketplace_id}],
                'condition_type': [{'value': condition, 'marketplace_id': marketplace_id}],
                'list_price': [{'value': price, 'currency': 'EUR', 'marketplace_id': marketplace_id}],
                'fulfillment_availability': [{
                    'fulfillment_channel_code': 'DEFAULT',
                    'quantity': qty,
                    'marketplace_id': marketplace_id,
                }],
            }
            if image_url:
                attributes['main_product_image_locator'] = [{'media_location': image_url, 'marketplace_id': marketplace_id}]
            description = self.description_sale or self.description or ''
            if description:
                attributes['product_description'] = [{'value': str(description), 'language_tag': client.locale, 'marketplace_id': marketplace_id}]
            if self.barcode:
                attributes['externally_assigned_product_identifier'] = [{'type': 'ean', 'value': self.barcode, 'marketplace_id': marketplace_id}]
            requirements = 'LISTING'

        try:
            result = client.put_listing(sku, product_type, attributes, requirements)
        except Exception as exc:
            self.write({'x_amazon_publish_state': 'error', 'x_amazon_publish_error': str(exc)})
            raise UserError(f'Errore pubblicazione Amazon: {exc}') from exc

        status = result.get('status', '')
        issues = result.get('issues', [])
        errors = [i for i in issues if i.get('severity') == 'ERROR']
        warnings = [i for i in issues if i.get('severity') == 'WARNING']

        if errors or status == 'INVALID':
            msgs = '\n'.join(f"[{i.get('code', '?')}] {i.get('message', '')}" for i in errors) or 'Risposta non valida da Amazon.'
            self.write({'x_amazon_publish_state': 'error', 'x_amazon_publish_error': msgs})
            raise UserError(f'Amazon ha rifiutato il listing:\n{msgs}')

        vals = {
            'x_amazon_publish_error': False,
            'x_amazon_last_sync': datetime.now(),
        }
        if not self.x_amazon_sku:
            vals['x_amazon_sku'] = sku

        if self.x_amazon_asin:
            vals['x_amazon_publish_state'] = 'published'
            vals['x_amazon_status'] = 'active'
        else:
            vals['x_amazon_publish_state'] = 'pending'
        self.write(vals)

        warning_text = '\n'.join(f"[{i.get('code', '?')}] {i.get('message', '')}" for i in warnings)
        if not self.x_amazon_asin:
            msg = 'Listing inviato ad Amazon. Amazon assegnerà l\'ASIN entro qualche minuto — usa "Verifica ASIN" per recuperarlo.'
            if warning_text:
                msg += f'\n\nAvvisi:\n{warning_text}'
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'title': 'In elaborazione', 'message': msg, 'type': 'warning', 'sticky': True},
            }
        if warning_text:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'title': 'Pubblicato con avvisi', 'message': warning_text, 'type': 'warning', 'sticky': False},
            }

    def action_amazon_get_asin(self):
        """Recupera l'ASIN assegnato da Amazon dopo l'invio del listing (Scenario B)."""
        self.ensure_one()
        sku = self.x_amazon_sku or self.default_code
        if not sku:
            raise UserError('Nessun SKU disponibile per questa ricerca.')

        client = self._get_sp_client()
        try:
            payload = client.get_listing_item(sku)
        except Exception as exc:
            raise UserError(f'Impossibile recuperare il listing da Amazon: {exc}') from exc

        summaries = payload.get('summaries', [{}])
        s0 = summaries[0] if summaries else {}
        asin = s0.get('asin', '')
        if not asin:
            raise UserError('Amazon non ha ancora assegnato un ASIN. Attendi qualche minuto e riprova.')

        self.write({
            'x_amazon_asin': asin,
            'x_amazon_status': 'active',
            'x_amazon_publish_state': 'published',
            'x_amazon_publish_error': False,
            'x_amazon_last_sync': datetime.now(),
        })

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
