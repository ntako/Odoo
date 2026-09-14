"""
Client SP-API per ke_amazon_sync.
Legge le credenziali da ir.config_parameter (impostazioni Odoo).
Logica rate-limiting e retry basata sul progetto Amazon/SyncFromAmazon.
"""

import time
import logging
import functools

_log = logging.getLogger(__name__)

DELAY_CATALOG_S = 0.6
DELAY_LISTINGS_S = 0.3
DELAY_SEARCH_S = 0.5
DELAY_ORDERS_GET_S = 60.0   # getOrders (lista/pagina ordini): rate ufficiale sostenuto 0.0167 req/s (1 ogni 60s), burst 20
DELAY_ORDERS_ITEMS_S = 2.0  # getOrderItems (righe ordine): rate ufficiale 0.5 req/s, burst 30
DELAY_INVENTORY_S = 0.5   # getInventorySummaries: max 2 req/s (burst 2) → 0.5s tra ogni chiamata
DELAY_SELLERS_S = 60.0    # getMarketplaceParticipations: rate ufficiale 0.016 req/s (~1 ogni 60s), burst 15
RETRY_MAX = 5
RETRY_BACKOFF_S = 60

_D_MARKETPLACE_ID = 'APJ6JRA9NG5V4'


RETRY_NET_BACKOFF_S = 15  # Attesa fissa per errori di rete/SSL (non throttling)


def _with_retry(delay_s, retry_max=None, backoff_s=None):
    """
    retry_max/backoff_s permettono di stringere i tempi di attesa per le
    chiamate usate dentro cicli a budget di tempo (es. sync FBA a pagine):
    con i default (RETRY_MAX=5, RETRY_BACKOFF_S=60) un'unica chiamata può
    restare bloccata in time.sleep per diversi minuti in caso di throttling
    ripetuto, superando facilmente il limite di tempo reale del cron.
    """
    retry_max = retry_max or RETRY_MAX
    backoff_s = backoff_s or RETRY_BACKOFF_S

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            import socket
            import ssl
            import requests.exceptions
            from sp_api.base.exceptions import (
                SellingApiRequestThrottledException, SellingApiException,
            )
            for attempt in range(1, retry_max + 1):
                time.sleep(delay_s)
                try:
                    return fn(*args, **kwargs)
                except SellingApiRequestThrottledException:
                    wait = backoff_s * (2 ** (attempt - 1))
                    _log.warning('%s: 429 throttled (tentativo %d/%d), attendo %ds',
                                 fn.__name__, attempt, retry_max, wait)
                    if attempt == retry_max:
                        raise
                    time.sleep(wait)
                except SellingApiException as exc:
                    if attempt < retry_max:
                        wait = backoff_s * (2 ** (attempt - 1))
                        _log.warning('%s: errore API %s (tentativo %d/%d), attendo %ds',
                                     fn.__name__, exc, attempt, retry_max, wait)
                        time.sleep(wait)
                    else:
                        raise
                except (requests.exceptions.RequestException, ssl.SSLError,
                        socket.timeout, OSError) as exc:
                    # Errori di rete/SSL transitori: retry con attesa fissa breve
                    _log.warning('%s: errore rete/SSL "%s" (tentativo %d/%d), attendo %ds',
                                 fn.__name__, exc, attempt, retry_max, RETRY_NET_BACKOFF_S)
                    if attempt == retry_max:
                        raise
                    time.sleep(RETRY_NET_BACKOFF_S)
        return wrapper
    return decorator


class SpApiClient:
    """
    Wrapper intorno a python-amazon-sp-api che legge le credenziali da Odoo.
    Instanziare con l'env Odoo corrente; può anche essere usato con un dict
    credentials passato esplicitamente (per i wizard in thread separato).
    """

    def __init__(self, env=None, credentials=None, seller_id=None,
                 marketplace_id=None, locale='it_IT'):
        self._env = env
        self._credentials_override = credentials
        self._seller_id_override = seller_id
        self._marketplace_id_override = marketplace_id
        self._locale = locale

    # ------------------------------------------------------------------
    # Accesso parametri
    # ------------------------------------------------------------------

    def _get_param(self, key, default=''):
        if self._env:
            return self._env['ir.config_parameter'].sudo().get_param(key, default)
        return default

    @property
    def credentials(self):
        if self._credentials_override:
            return self._credentials_override
        return {
            'refresh_token': self._get_param('ke_amazon_sync.refresh_token'),
            'lwa_app_id': self._get_param('ke_amazon_sync.lwa_app_id'),
            'lwa_client_secret': self._get_param('ke_amazon_sync.lwa_client_secret'),
        }

    @property
    def seller_id(self):
        if self._seller_id_override:
            return self._seller_id_override
        return self._get_param('ke_amazon_sync.seller_id')

    @property
    def marketplace_id(self):
        if self._marketplace_id_override:
            return self._marketplace_id_override
        return self._get_param('ke_amazon_sync.marketplace_id') or _D_MARKETPLACE_ID

    @property
    def locale(self):
        if self._locale:
            return self._locale
        return self._get_param('ke_amazon_sync.locale', 'it_IT')

    def _marketplace(self):
        from sp_api.base import Marketplaces
        for m in Marketplaces:
            if m.marketplace_id == self.marketplace_id:
                return m
        raise ValueError(f'Marketplace ID non trovato: {self.marketplace_id}')

    def validate_credentials(self):
        """Lancia ValueError se le credenziali obbligatorie sono mancanti."""
        creds = self.credentials
        missing = [k for k in ('refresh_token', 'lwa_app_id', 'lwa_client_secret')
                   if not creds.get(k)]
        if missing:
            raise ValueError(
                f'Credenziali SP-API mancanti: {", ".join(missing)}. '
                'Configura in Impostazioni → Amazon Sync.'
            )
        if not self.seller_id:
            raise ValueError(
                'Seller ID mancante. Configuralo in Impostazioni → Amazon Sync.'
            )

    # ------------------------------------------------------------------
    # Listings: tutti gli SKU
    # ------------------------------------------------------------------

    def fetch_listings_page(self, page_token=None):
        """Scarica una singola pagina di listing. Ritorna (items, next_token)."""
        from sp_api.api import ListingsItems
        from sp_api.base import SellingApiException

        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials)
        kwargs = {
            'marketplaceIds': [self.marketplace_id],
            'includedData': ['summaries', 'attributes', 'offers', 'fulfillmentAvailability'],
            'pageSize': 20,
        }
        if page_token:
            kwargs['pageToken'] = page_token

        time.sleep(DELAY_LISTINGS_S)
        try:
            response = api.search_listings_items(self.seller_id, **kwargs)
        except SellingApiException as exc:
            _log.error('Errore listings API: %s', exc)
            raise  # Propaga: il cron gestisce l'errore e resetta lo stato

        items = response.payload.get('items', [])
        return items, response.next_token

    def iter_seller_skus(self, max_products=None):
        """Itera su tutti i listing con paginazione. Yield di dict raw."""
        from sp_api.api import ListingsItems
        from sp_api.base import SellingApiException

        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials)
        page_token = None
        count = 0

        while True:
            kwargs = {
                'marketplaceIds': [self.marketplace_id],
                'includedData': ['summaries', 'attributes', 'offers', 'fulfillmentAvailability'],
                'pageSize': 20,
            }
            if page_token:
                kwargs['pageToken'] = page_token

            time.sleep(DELAY_LISTINGS_S)
            try:
                response = api.search_listings_items(self.seller_id, **kwargs)
            except SellingApiException as exc:
                _log.error('Errore listings API: %s', exc)
                break

            items = response.payload.get('items', [])
            if not items:
                break

            for item in items:
                count += 1
                if max_products and count > max_products:
                    return
                yield item

            page_token = response.next_token
            if not page_token:
                break

    # ------------------------------------------------------------------
    # Catalog v2022
    # ------------------------------------------------------------------

    # retry_max/backoff_s ridotti (stesso motivo di _inventory_call): questa
    # chiamata è usata anche da cron a chunk (Aggiorna da Catalog API), dove
    # i default (5 tentativi, backoff fino a 480s) rischierebbero di superare
    # il limite di tempo reale del cron su un singolo prodotto throttled.
    @_with_retry(DELAY_CATALOG_S, retry_max=3, backoff_s=8)
    def _catalog_call(self, asin):
        from sp_api.api.catalog_items.catalog_items_2022_04_01 import CatalogItemsV20220401
        api = CatalogItemsV20220401(marketplace=self._marketplace(), credentials=self.credentials)
        response = api.get_catalog_item(
            asin,
            marketplaceIds=[self.marketplace_id],
            includedData=['attributes', 'dimensions', 'identifiers', 'images',
                          'productTypes', 'summaries', 'classifications', 'relationships'],
            locale=self.locale,
        )
        return response.payload

    def get_catalog_item(self, asin):
        from sp_api.base import SellingApiException
        try:
            return self._catalog_call(asin)
        except SellingApiException as exc:
            _log.warning('Impossibile recuperare catalogo ASIN %s: %s', asin, exc)
            return {}

    # ------------------------------------------------------------------
    # Ricerca per EAN
    # ------------------------------------------------------------------

    @_with_retry(DELAY_SEARCH_S)
    def _search_catalog_call(self, ean):
        from sp_api.api.catalog_items.catalog_items_2022_04_01 import CatalogItemsV20220401
        api = CatalogItemsV20220401(marketplace=self._marketplace(), credentials=self.credentials)
        response = api.search_catalog_items(
            identifiers=[ean],
            identifiersType='EAN',
            marketplaceIds=[self.marketplace_id],
            includedData=['summaries', 'identifiers', 'productTypes'],
        )
        return response.payload

    def search_by_ean(self, ean):
        """
        Cerca su Amazon Catalog per EAN.
        Ritorna lista di {'asin', 'name', 'brand', 'product_type'}.
        """
        from sp_api.base import SellingApiException
        try:
            payload = self._search_catalog_call(ean)
        except SellingApiException as exc:
            _log.warning('Ricerca EAN %s fallita: %s', ean, exc)
            return []

        results = []
        for item in payload.get('items', []):
            summaries = item.get('summaries', [{}])
            summary = summaries[0] if summaries else {}
            product_types = item.get('productTypes', [{}])
            pt = product_types[0].get('productType', '') if product_types else ''
            results.append({
                'asin': item.get('asin', ''),
                'name': summary.get('itemName', ''),
                'brand': summary.get('brand', ''),
                'product_type': pt,
            })
        return results

    # ------------------------------------------------------------------
    # Inventory FBA
    # ------------------------------------------------------------------

    # retry_max/backoff_s ridotti: un singolo blocco d'attesa deve restare
    # breve (worst case ~0.5+8+0.5+16 ≈ 25s) invece dei minuti che il backoff
    # esponenziale di default comporterebbe, per non tenere il thread di sync
    # bloccato a lungo su un throttling temporaneo.
    @_with_retry(DELAY_INVENTORY_S, retry_max=3, backoff_s=8)
    def _inventory_call(self, page_token):
        from sp_api.api import Inventories
        api = Inventories(marketplace=self._marketplace(), credentials=self.credentials)
        params = {'details': True}
        if page_token:
            params['nextToken'] = page_token
        return api.get_inventory_summary_marketplace(**params)

    def get_inventory_map(self):
        """Ritorna {sku: qty_disponibile} FBA."""
        from sp_api.base import SellingApiException
        inventory = {}
        page_token = None
        while True:
            try:
                response = self._inventory_call(page_token)
            except SellingApiException as exc:
                _log.warning('Errore inventory FBA: %s', exc)
                break
            payload = response.payload
            for s in payload.get('inventorySummaries', []):
                sku = s.get('sellerSku', '')
                qty = s.get('inventoryDetails', {}).get('fulfillableQuantity', 0)
                inventory[sku] = qty
            # response.next_token (non payload['pagination']['nextToken'], percorso
            # sbagliato per questo endpoint) — stesso meccanismo già usato e
            # verificato per la paginazione dei listing.
            page_token = response.next_token
            if not page_token:
                break
        return inventory

    def fetch_inventory_page(self, page_token=None):
        """
        Scarica UNA pagina di inventario FBA (senza scorrere tutte le pagine).
        Ritorna ([{'sku':..., 'qty':...}, ...], next_token_o_None). Usata dalla
        sync a blocchi per evitare che un'unica invocazione del cron debba
        scaricare tutte le pagine (potenzialmente centinaia) in un colpo solo,
        rischiando di superare il limite di tempo reale del cron in caso di
        throttling/retry con backoff su una singola pagina.
        In caso di errore la chiamata viene rilanciata (non va confusa con la
        fine della paginazione): il chiamante deve trattarla come fallimento,
        altrimenti la sync verrebbe erroneamente segnata come completata a metà.
        """
        response = self._inventory_call(page_token)
        payload = response.payload
        items = [
            {
                'sku': s.get('sellerSku', ''),
                'qty': s.get('inventoryDetails', {}).get('fulfillableQuantity', 0),
            }
            for s in payload.get('inventorySummaries', [])
        ]
        return items, response.next_token

    # ------------------------------------------------------------------
    # Sellers API — marketplace su cui il venditore è realmente attivo
    # ------------------------------------------------------------------

    # retry_max=1 (nessun retry interno): stesso motivo di _get_orders_call —
    # DELAY_SELLERS_S=60s è già una sleep dominante, un retry con backoff
    # aggiuntivo rischierebbe di far durare l'invocazione troppo a lungo.
    @_with_retry(DELAY_SELLERS_S, retry_max=1)
    def _marketplace_participations_call(self):
        from sp_api.api import Sellers
        api = Sellers(marketplace=self._marketplace(), credentials=self.credentials)
        return api.get_marketplace_participation()

    def get_marketplace_participations(self):
        """
        Ritorna la lista dei marketplace su cui il venditore risulta
        effettivamente attivo (getMarketplaceParticipations, Sellers API) —
        non è scoped a un marketplace (add_marketplace=False internamente),
        quindi ritorna sempre TUTTI i marketplace collegati all'account,
        indipendentemente da quale marketplace_id è configurato sul client.
        Ritorna [{'marketplace_id':..., 'code':..., 'name':...,
        'currency':..., 'is_participating': bool}, ...].
        """
        response = self._marketplace_participations_call()
        results = []
        for entry in response.payload:
            marketplace = entry.get('marketplace', {})
            participation = entry.get('participation', {})
            results.append({
                'marketplace_id': marketplace.get('id', ''),
                'code': marketplace.get('countryCode', ''),
                'name': marketplace.get('name', ''),
                'currency': marketplace.get('defaultCurrencyCode', ''),
                'is_participating': bool(participation.get('isParticipating')),
            })
        return results

    # ------------------------------------------------------------------
    # Reports API (GET_MERCHANT_LISTINGS_ALL_DATA) — nessun limite di 1000
    # ------------------------------------------------------------------

    def request_listings_report(self):
        """Crea un report listing completo. Ritorna il reportId."""
        from sp_api.api import Reports
        api = Reports(marketplace=self._marketplace(), credentials=self.credentials)
        resp = api.create_report(
            reportType='GET_MERCHANT_LISTINGS_ALL_DATA',
            marketplaceIds=[self.marketplace_id],
        )
        return resp.payload['reportId']

    def request_returns_report(self, data_start_time):
        """Crea il report dei resi cliente FBA da data_start_time (ISO-8601) a oggi. Ritorna il reportId."""
        from sp_api.api import Reports
        api = Reports(marketplace=self._marketplace(), credentials=self.credentials)
        resp = api.create_report(
            reportType='GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA',
            marketplaceIds=[self.marketplace_id],
            dataStartTime=data_start_time,
        )
        return resp.payload['reportId']

    def request_fnsku_report(self):
        """
        Crea il report inventario FBA (GET_FBA_MYI_ALL_INVENTORY_DATA). Ritorna il
        reportId. Oltre alla colonna 'fnsku' (identificatore giacenza FBA specifico
        di questo venditore, usato sulle etichette) contiene anche le quantità
        granulari per stato (riservato, in transito, non vendibile, ecc.) — non
        ancora usate qui, ma utili per una futura scomposizione della giacenza FBA
        per stato invece del solo totale disponibile.
        """
        from sp_api.api import Reports
        api = Reports(marketplace=self._marketplace(), credentials=self.credentials)
        resp = api.create_report(
            reportType='GET_FBA_MYI_ALL_INVENTORY_DATA',
            marketplaceIds=[self.marketplace_id],
        )
        return resp.payload['reportId']

    def check_report_ready(self, report_id):
        """
        Controlla lo stato del report.
        Ritorna reportDocumentId se DONE, None se ancora in elaborazione.
        Lancia RuntimeError se CANCELLED/FATAL.
        """
        from sp_api.api import Reports
        from sp_api.base import SellingApiException
        api = Reports(marketplace=self._marketplace(), credentials=self.credentials)
        try:
            resp = api.get_report(report_id)
        except SellingApiException as exc:
            _log.warning('check_report_ready: errore SP-API %s', exc)
            return None
        status = resp.payload.get('processingStatus', '')
        if status == 'DONE':
            return resp.payload.get('reportDocumentId')
        if status in ('CANCELLED', 'FATAL'):
            raise RuntimeError(f'Report {report_id} terminato con stato: {status}')
        return None

    def download_report_rows(self, document_id):
        """Scarica e parsa il report TSV. Ritorna lista di dict (una riga = un prodotto)."""
        import csv
        import io
        from sp_api.api import Reports
        api = Reports(marketplace=self._marketplace(), credentials=self.credentials)
        resp = api.get_report_document(document_id, download=True)
        content = resp.payload.get('document', '')
        if not content:
            return []
        content = content.lstrip('﻿')  # strip UTF-8 BOM se presente
        reader = csv.DictReader(io.StringIO(content), delimiter='\t')
        return list(reader)

    # ------------------------------------------------------------------
    # Orders API
    # ------------------------------------------------------------------

    # retry_max=1 (nessun retry interno): DELAY_ORDERS_GET_S=60s è già una
    # sleep dominante di per sé; un retry con backoff aggiuntivo rischierebbe
    # di far durare l'invocazione più del limite di tempo reale del cron
    # (osservato a 120s su questo deployment, vedi fix analogo su sync_fba).
    # In caso di errore l'invocazione fallisce e il prossimo tick del cron
    # (2 min dopo) riprova — stesso principio già adottato altrove.
    @_with_retry(DELAY_ORDERS_GET_S, retry_max=1)
    def _get_orders_call(self, **params):
        from sp_api.api import Orders
        api = Orders(marketplace=self._marketplace(), credentials=self.credentials)
        resp = api.get_orders(**params)
        return resp.payload

    def get_orders(self, created_after, next_token=None, order_statuses=None):
        """
        Recupera una pagina di ordini Amazon.
        Ritorna (orders_list, next_token_or_None).
        """
        params = {
            'MarketplaceIds': [self.marketplace_id],
            'MaxResultsPerPage': 100,
        }
        if next_token:
            params['NextToken'] = next_token
        else:
            params['CreatedAfter'] = created_after
            if order_statuses:
                params['OrderStatuses'] = order_statuses
        payload = self._get_orders_call(**params)
        return payload.get('Orders', []), payload.get('NextToken')

    # retry_max/backoff_s ridotti (stesso motivo di _inventory_call/_catalog_call/
    # _get_orders_call): i default (5 tentativi, backoff fino a 480s, ~900s
    # totali nel caso peggiore) rischiano di far superare il limite di tempo
    # reale del cron già su un solo ordine throttled dentro un chunk da 15.
    @_with_retry(DELAY_ORDERS_ITEMS_S, retry_max=3, backoff_s=8)
    def _get_order_items_call(self, order_id):
        from sp_api.api import Orders
        api = Orders(marketplace=self._marketplace(), credentials=self.credentials)
        resp = api.get_order_items(order_id)
        return resp.payload.get('OrderItems', [])

    def get_order_items(self, order_id):
        """Recupera gli item di un singolo ordine Amazon (con retry e rate limiting)."""
        from sp_api.base import SellingApiException
        try:
            return self._get_order_items_call(order_id)
        except SellingApiException as exc:
            _log.warning('Errore get_order_items %s: %s', order_id, exc)
            raise

    # ------------------------------------------------------------------
    # Listing singolo (per productType)
    # ------------------------------------------------------------------

    @_with_retry(DELAY_LISTINGS_S)
    def get_listing_item(self, sku):
        """Recupera un singolo listing (contiene productType e attributi attuali)."""
        from sp_api.api import ListingsItems
        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials)
        response = api.get_listings_item(
            self.seller_id, sku,
            marketplaceIds=[self.marketplace_id],
            includedData=['summaries', 'attributes', 'issues'],
        )
        return response.payload

    # ------------------------------------------------------------------
    # PATCH prezzo
    # ------------------------------------------------------------------

    @_with_retry(DELAY_LISTINGS_S)
    def patch_price(self, sku, product_type, price):
        """Aggiorna il prezzo di un listing via PATCH."""
        from sp_api.api import ListingsItems
        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials)
        body = {
            'productType': product_type,
            'patches': [{
                'op': 'replace',
                'path': '/attributes/purchasable_offer',
                'value': [{
                    'marketplace_id': self.marketplace_id,
                    'our_price': [{'schedule': [{'value_with_tax': round(float(price), 2)}]}],
                }],
            }],
        }
        return api.patch_listings_item(
            self.seller_id, sku,
            marketplaceIds=[self.marketplace_id],
            body=body,
        )

    # ------------------------------------------------------------------
    # Product Type Definitions
    # ------------------------------------------------------------------

    def search_product_types(self, keywords=None, item_name=None):
        """
        Cerca i tipi di prodotto Amazon disponibili nel marketplace.
        Ritorna lista di {'name': 'SHOE', 'display_name': 'Shoes'}.
        """
        from sp_api.api import ProductTypeDefinitions
        api = ProductTypeDefinitions(marketplace=self._marketplace(), credentials=self.credentials)
        kwargs = {'marketplaceIds': [self.marketplace_id], 'locale': self.locale}
        if keywords:
            kw_list = keywords if isinstance(keywords, list) else keywords.split()
            kwargs['keywords'] = kw_list
        if item_name:
            kwargs['itemName'] = item_name
        time.sleep(DELAY_LISTINGS_S)
        resp = api.search_definitions_product_types(**kwargs)
        return [
            {
                'name': pt.get('name', ''),
                'display_name': pt.get('displayName') or pt.get('name', ''),
            }
            for pt in resp.payload.get('productTypes', [])
            if pt.get('name')
        ]

    def search_browse_nodes(self, keyword):
        """
        Cerca browse node (categorie) Amazon rilevanti per una parola chiave.
        Usa searchCatalogItems e aggrega le browse classification uniche dai risultati.
        Ritorna lista di {'classification_id': '...', 'display_name': '...'}.
        """
        from sp_api.api.catalog_items.catalog_items_2022_04_01 import CatalogItemsV20220401
        api = CatalogItemsV20220401(marketplace=self._marketplace(), credentials=self.credentials)
        time.sleep(DELAY_SEARCH_S)
        resp = api.search_catalog_items(
            keywords=[keyword],
            marketplaceIds=[self.marketplace_id],
            includedData=['classifications'],
            pageSize=20,
        )
        seen = set()
        results = []
        for item in resp.payload.get('items', []):
            for cls_block in item.get('classifications', []):
                for cls in cls_block.get('classifications', []):
                    cid = cls.get('classificationId', '')
                    display = cls.get('displayName', '')
                    if cid and cid not in seen:
                        seen.add(cid)
                        results.append({'classification_id': cid, 'display_name': display})
        return results

    # ------------------------------------------------------------------
    # PUT listing (crea o sostituisce offerta/listing completo)
    # ------------------------------------------------------------------

    @_with_retry(DELAY_LISTINGS_S)
    def put_listing(self, sku, product_type, attributes, requirements='LISTING_OFFER_ONLY'):
        """
        Crea o sostituisce un listing su Amazon via PUT.
        requirements='LISTING_OFFER_ONLY': offerta su ASIN esistente (Scenario A).
        requirements='LISTING': listing completo, può creare nuovo ASIN (Scenario B).
        Ritorna il payload della risposta {'status', 'issues', 'submissionId'}.
        """
        from sp_api.api import ListingsItems
        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials)
        body = {
            'productType': product_type,
            'requirements': requirements,
            'attributes': attributes,
        }
        resp = api.put_listings_item(
            self.seller_id, sku,
            marketplaceIds=[self.marketplace_id],
            body=body,
        )
        return resp.payload

    # ------------------------------------------------------------------
    # PATCH quantità (FBM only)
    # ------------------------------------------------------------------

    @_with_retry(DELAY_LISTINGS_S)
    def patch_quantity(self, sku, product_type, qty):
        """Aggiorna la quantità FBM di un listing via PATCH."""
        from sp_api.api import ListingsItems
        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials)
        body = {
            'productType': product_type,
            'patches': [{
                'op': 'replace',
                'path': '/attributes/fulfillment_availability',
                'value': [{'fulfillment_channel_code': 'DEFAULT', 'quantity': int(qty)}],
            }],
        }
        return api.patch_listings_item(
            self.seller_id, sku,
            marketplaceIds=[self.marketplace_id],
            body=body,
        )
