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
# Fulfillment Inbound v2024-03-20 (piano spedizioni inbound FBA): rate
# ufficiale verificato il 2026-10-29, pagina per pagina, per OGNI operazione
# usata sotto — tutte 2 richieste/secondo (burst diverso, 2/6/30 secondo
# l'operazione, irrilevante qui: ogni chiamata è singola e sequenziale
# dentro un'azione utente, non un ciclo/pagina). Eccezione onesta: getLabels
# — la pagina di riferimento trovata documenta solo la v0 (non la
# v2024-03-20 realmente usata dalla libreria installata); stesso delay
# prudente per coerenza, da riverificare se si rivela insufficiente.
DELAY_INBOUND_S = 0.5
RETRY_MAX = 5
RETRY_BACKOFF_S = 60

_D_MARKETPLACE_ID = 'APJ6JRA9NG5V4'


RETRY_NET_BACKOFF_S = 15  # Attesa fissa per errori di rete/SSL (non throttling)

# La libreria python-amazon-sp-api (httpx sotto il cofano) non imposta NESSUN
# timeout di default: una connessione che si blocca (non un errore, una che
# non risponde né chiude) resterebbe appesa indefinitamente, senza che nulla
# la interrompa — verificato leggendo sp_api/base/_transport_httpx.py e
# client.py, non solo ipotizzato. Passato esplicitamente ad ogni client SP-API
# istanziato in questo file (vedi HTTP_TIMEOUT_S sotto).
HTTP_TIMEOUT_S = 30


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
            import httpx
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
                        socket.timeout, OSError, httpx.TransportError) as exc:
                    # Errori di rete/SSL/DNS transitori: retry con attesa fissa
                    # breve. httpx.TransportError (ConnectError/ReadTimeout/
                    # NetworkError/ecc.) aggiunto dopo un incidente reale: la
                    # libreria sp_api usa httpx sotto il cofano, non requests —
                    # le sue eccezioni non derivano da OSError/socket.timeout/
                    # ssl.SSLError (verificato sull'MRO), quindi un blip DNS
                    # transitorio (httpx.ConnectError, "Temporary failure in
                    # name resolution") non veniva mai catturato qui: usciva
                    # subito, facendo fallire l'intero thread invece di
                    # ritentare. Questo except esisteva già per requests, ma
                    # era morto per la libreria realmente in uso.
                    _log.warning('%s: errore rete/SSL/DNS "%s" (tentativo %d/%d), attendo %ds',
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

        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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

        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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
        api = CatalogItemsV20220401(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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

    # searchCatalogItems accetta fino a 20 identifiers per chiamata (stesso
    # rate limit ufficiale di getCatalogItem: 2 richieste/s, burst 2 — per
    # questo riusa DELAY_CATALOG_S) e con includedData=dimensions restituisce
    # lo stesso identico blocco 'dimensions'/'summaries'/'productTypes' di
    # getCatalogItem per ogni item trovato: permette di arricchire fino a 20
    # prodotti con una sola chiamata invece di una per ASIN (usato da
    # apply_category_mapping in sync_engine.py per il backfill categoria/
    # dimensioni, dove il collo di bottiglia erano le migliaia di chiamate
    # singole, non il rate limit in sé).
    @_with_retry(DELAY_CATALOG_S, retry_max=3, backoff_s=8)
    def _catalog_search_batch_call(self, asins):
        from sp_api.api.catalog_items.catalog_items_2022_04_01 import CatalogItemsV20220401
        api = CatalogItemsV20220401(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
        response = api.search_catalog_items(
            # Amazon vuole 'identifiers' come stringa comma-delimited (vedi
            # doc dell'operazione), NON una lista Python: la libreria
            # sp_api passa 'params' così com'è a httpx, che serializza una
            # lista come chiave ripetuta (identifiers=A&identifiers=B&...)
            # invece che come valore unico — Amazon con quella forma tiene
            # solo il primo e scarta silenziosamente tutti gli altri (nessun
            # errore, risposta 200 con un solo item: verificato dal vivo,
            # richiesti 5/10/20 ASIN, tornava sempre e solo il primo).
            # search_by_ean non ne aveva mai risentito perché passa sempre
            # un solo EAN alla volta.
            identifiers=','.join(asins),
            identifiersType='ASIN',
            marketplaceIds=[self.marketplace_id],
            includedData=['attributes', 'dimensions', 'identifiers', 'images',
                          'productTypes', 'summaries', 'classifications', 'relationships'],
            locale=self.locale,
        )
        return response.payload

    def get_catalog_items_batch(self, asins):
        """
        Come get_catalog_item, ma per un gruppo di ASIN in una sola richiesta
        ad Amazon. Il limite qui sotto (20) è quello della RICHIESTA
        (searchCatalogItems accetta fino a 20 identifiers, non dà errore con
        meno) — ma la RISPOSTA tronca silenziosamente 'items' a un massimo
        di 10, anche quando 'numberOfResults' conferma che tutti quelli
        richiesti esistono e senza 'pagination'/nextToken per recuperare il
        resto (verificato dal vivo: 12/15/20 richiesti -> sempre e solo 10
        tornati, non throttling, riproducibile). Chi chiama con più di 10
        ASIN per volta perderà silenziosamente dati reali, scambiabili per
        "Amazon non li ha" — usa gruppi da 10, non da 20, nonostante quello
        che dice la doc SP-API sulla richiesta (vedi _CAT_MAP_BATCH_SIZE in
        sync_engine.py, che usa 10 proprio per questo).
        Ritorna {asin: item_payload}; un ASIN passato ma assente dal dict
        risultato significa che Amazon non ha trovato/restituito dati per
        quell'ASIN (stesso significato del dict vuoto di get_catalog_item),
        non è un errore.

        SellingApiException NON viene catturata qui (a differenza di
        get_catalog_item): un fallimento della chiamata batch riguarda TUTTI
        gli ASIN richiesti insieme, non uno solo — se lo si inghiottisse e
        si ritornasse {} come per un "nessun dato", il chiamante non
        potrebbe distinguere "Amazon non ha questi dati" (vero, non serve
        ritentare) da "la chiamata è fallita" (throttling/errore transitorio,
        va ritentato) e finirebbe per segnare tutto il gruppo come "nessun
        dato disponibile" — visto succedere in produzione: stessi ASIN,
        richiesti di nuovo poco dopo, risposta completa. Il chiamante
        (apply_category_mapping) deve gestire l'eccezione batch per batch.
        """
        if not asins:
            return {}
        if len(asins) > 20:
            raise ValueError('get_catalog_items_batch supporta al massimo 20 ASIN per chiamata')
        payload = self._catalog_search_batch_call(asins)
        return {item.get('asin'): item for item in payload.get('items', []) if item.get('asin')}

    # ------------------------------------------------------------------
    # Ricerca per EAN
    # ------------------------------------------------------------------

    @_with_retry(DELAY_SEARCH_S)
    def _search_catalog_call(self, ean):
        from sp_api.api.catalog_items.catalog_items_2022_04_01 import CatalogItemsV20220401
        api = CatalogItemsV20220401(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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
        api = Inventories(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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
        Ritorna ([{'sku':..., 'asin':..., 'qty':...}, ...], next_token_o_None).
        Usata dalla sync a blocchi per evitare che un'unica invocazione debba
        scaricare tutte le pagine (potenzialmente centinaia) in un colpo solo,
        rischiando di superare il limite di tempo reale in caso di
        throttling/retry con backoff su una singola pagina. 'asin' è incluso
        perché la riconciliazione (sync_engine.sync_fba_quantities_page)
        aggrega per ASIN, non per SKU: durante una transizione di
        etichettatura Amazon lo stesso ASIN può comparire sotto due SKU
        diversi (vecchio e nuovo '+') nella stessa risposta.
        In caso di errore la chiamata viene rilanciata (non va confusa con la
        fine della paginazione): il chiamante deve trattarla come fallimento,
        altrimenti la sync verrebbe erroneamente segnata come completata a metà.
        """
        response = self._inventory_call(page_token)
        payload = response.payload
        items = [
            {
                'sku': s.get('sellerSku', ''),
                'asin': s.get('asin', ''),
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
        api = Sellers(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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

    # Nessuna di queste request_*_report/download_report_rows passava per
    # _with_retry — chiamate dirette, un blip di rete/DNS transitorio le
    # faceva fallire subito senza alcun ritentativo (a differenza delle
    # chiamate Catalog/Inventory/Listings più sotto, già tutte protette).
    # Delay minimo perché sono chiamate rare (una/due per marketplace per
    # sync, mai in un ciclo stretto) — qui serve resilienza, non un vero
    # rate limit.
    @_with_retry(1.0, retry_max=3, backoff_s=8)
    def request_listings_report(self):
        """Crea un report listing completo. Ritorna il reportId."""
        from sp_api.api import Reports
        api = Reports(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
        resp = api.create_report(
            reportType='GET_MERCHANT_LISTINGS_ALL_DATA',
            marketplaceIds=[self.marketplace_id],
        )
        return resp.payload['reportId']

    @_with_retry(1.0, retry_max=3, backoff_s=8)
    def request_returns_report(self, data_start_time):
        """Crea il report dei resi cliente FBA da data_start_time (ISO-8601) a oggi. Ritorna il reportId."""
        from sp_api.api import Reports
        api = Reports(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
        resp = api.create_report(
            reportType='GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA',
            marketplaceIds=[self.marketplace_id],
            dataStartTime=data_start_time,
        )
        return resp.payload['reportId']

    @_with_retry(1.0, retry_max=3, backoff_s=8)
    def request_orders_report(self, data_start_time, data_end_time, marketplace_ids=None):
        """
        Crea il report ordini completo (GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL,
        una riga per articolo ordine — SKU/ASIN/quantità/prezzo/stato già inclusi,
        niente chiamata separata getOrderItems per ordine). Richiede sia
        dataStartTime che dataEndTime (a differenza degli altri report qui, che
        coprono "da X a oggi"). Ritorna il reportId.

        'marketplace_ids', a differenza degli altri request_*_report di questo
        file, accetta una LISTA esplicita invece di usare solo self.marketplace_id:
        verificato con una chiamata reale (vedi progetto-sync-ordini-report.md
        §2-3) che un account con più marketplace EU attivi (endpoint regionale
        condiviso sellingpartnerapi-eu.amazon.com) restituisce ordini cross-
        marketplace anche passando un solo ID — la raccomandazione Amazon resta
        comunque di passare esplicitamente tutti i marketplace attivi in
        un'unica richiesta invece di farne una per marketplace (che darebbe
        dati sovrapposti/ridondanti, non complementari). self._marketplace()
        (che sceglie l'endpoint regionale) resta basato su self.marketplace_id
        del client instanziato — tutti i marketplace passati qui devono essere
        nella stessa regione di quell'endpoint (oggi solo EU è attivo).
        """
        from sp_api.api import Reports
        api = Reports(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
        resp = api.create_report(
            reportType='GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL',
            marketplaceIds=marketplace_ids or [self.marketplace_id],
            dataStartTime=data_start_time,
            dataEndTime=data_end_time,
        )
        return resp.payload['reportId']

    @_with_retry(1.0, retry_max=3, backoff_s=8)
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
        api = Reports(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
        resp = api.create_report(
            reportType='GET_FBA_MYI_ALL_INVENTORY_DATA',
            marketplaceIds=[self.marketplace_id],
        )
        return resp.payload['reportId']

    def check_report_ready(self, report_id):
        """
        Controlla lo stato del report.
        Ritorna reportDocumentId se DONE, None se ancora in elaborazione.
        Lancia RuntimeError se CANCELLED/FATAL, o se Amazon risponde con un
        errore permanente (4xx diverso dal throttling 429) — es. 403 quando
        l'app SP-API non è autorizzata per quel marketplace. Prima QUALSIASI
        SellingApiException veniva ingoiata e trattata come "ancora in
        elaborazione": un errore permanente faceva ritentare il cron
        all'infinito invece di segnalare il problema — trovato con un caso
        reale (marketplace non autorizzato, import bloccato per ore senza
        mai fallire visibilmente, nessun errore in log finché non fermato
        a mano). Solo gli errori davvero transitori (429, 5xx, e ora anche
        un blip di rete/DNS — httpx.TransportError, vedi sotto) vengono
        ancora ritentati silenziosamente: il chiamante gira già in un poll
        loop (ogni _REPORT_POLL_INTERVAL_S secondi, fino a un tetto di
        sicurezza), quindi basta ritornare None per farlo ritentare da solo
        al giro successivo — niente retry interno qui, lo fa già chi chiama.
        Trovato con un incidente reale: un blip DNS transitorio
        (httpx.ConnectError, "Temporary failure in name resolution") non
        era una SellingApiException (quella copre solo errori HTTP con
        risposta ricevuta da Amazon, non un fallimento di connessione prima
        ancora di arrivarci) — usciva subito, facendo fallire l'intero
        thread invece di essere ritentato al giro successivo.
        """
        import httpx
        from sp_api.api import Reports
        from sp_api.base import SellingApiException
        api = Reports(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
        try:
            resp = api.get_report(report_id)
        except httpx.TransportError as exc:
            _log.warning('check_report_ready: errore rete/DNS transitorio, ritento al prossimo giro: %s', exc)
            return None
        except SellingApiException as exc:
            code = getattr(exc, 'code', None)
            if code == 429 or (isinstance(code, int) and code >= 500):
                _log.warning('check_report_ready: errore transitorio (%s), ritento al prossimo giro: %s',
                              code, exc)
                return None
            raise RuntimeError(f'Errore permanente controllando il report {report_id}: {exc}') from exc
        status = resp.payload.get('processingStatus', '')
        if status == 'DONE':
            return resp.payload.get('reportDocumentId')
        if status in ('CANCELLED', 'FATAL'):
            raise RuntimeError(f'Report {report_id} terminato con stato: {status}')
        return None

    @_with_retry(1.0, retry_max=3, backoff_s=8)
    def download_report_raw(self, document_id):
        """
        Scarica il contenuto grezzo del report (TSV, BOM già rimosso), senza
        parsing. Separato da download_report_rows (sotto) per poter
        conservare il testo originale in un ir.attachment — vista "Report
        Amazon" (gestione report scaricati per il Full Import) — invece di
        scartarlo subito dopo il parsing, come succedeva prima.
        """
        from sp_api.api import Reports
        api = Reports(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
        resp = api.get_report_document(document_id, download=True)
        content = resp.payload.get('document', '') or ''
        return content.lstrip('﻿')  # strip UTF-8 BOM se presente

    def download_report_rows(self, document_id):
        """Scarica e parsa il report TSV. Ritorna lista di dict (una riga = un prodotto)."""
        import csv
        import io
        content = self.download_report_raw(document_id)
        if not content:
            return []
        reader = csv.DictReader(io.StringIO(content), delimiter='\t')
        return list(reader)

    @staticmethod
    def parse_report_text(content):
        """Parsa un testo TSV già scaricato (es. da un ir.attachment salvato
        in precedenza) in lista di dict — stessa logica di download_report_rows
        ma senza richiamare Amazon, per rielaborare un report già scaricato."""
        import csv
        import io
        if not content:
            return []
        content = content.lstrip('﻿')
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
        api = Orders(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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
        api = Orders(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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
        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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
        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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
        api = ProductTypeDefinitions(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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
        api = CatalogItemsV20220401(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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
        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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
        api = ListingsItems(marketplace=self._marketplace(), credentials=self.credentials, timeout=HTTP_TIMEOUT_S)
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

    # ------------------------------------------------------------------
    # Fulfillment Inbound v2024-03-20 — piano di spedizione inbound FBA
    # ------------------------------------------------------------------
    # Workflow multi-step, quasi tutto asincrono (operationId + polling via
    # poll_inbound_operation sotto). Verificato dalla documentazione
    # ufficiale il 2026-10-29 (vedi RICERCA_fulfillment_inbound_api_
    # sandbox_2026-10-29.md): confirm_placement_option è il punto di non
    # ritorno dichiarato da Amazon ("This option cannot be reversed after
    # it is selected.") — nessun metodo qui lo chiama da solo, solo
    # ke.amazon.inbound.plan.action_confirm_placement_option (con conferma
    # esplicita dell'utente) deve farlo.
    # Ogni metodo passa i campi del body COME kwarg diretti alla libreria
    # (non un singolo dict 'body='): verificato leggendo il codice installato
    # di FulfillmentInboundV20240320 (es. create_inbound_plan/
    # generate_transportation_options fanno `data=kwargs`, cioè i kwargs
    # passati qui diventano il body così com'è, senza un involucro 'body').

    def _inbound_api(self):
        from sp_api.api import FulfillmentInbound
        return FulfillmentInbound(
            marketplace=self._marketplace(), credentials=self.credentials,
            version='2024-03-20', timeout=HTTP_TIMEOUT_S,
        )

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def get_inbound_operation_status(self, operation_id):
        return self._inbound_api().get_inbound_operation_status(operation_id).payload

    def poll_inbound_operation(self, operation_id, poll_interval_s=10, poll_max_minutes=2):
        """
        Attende il completamento di un'operazione asincrona della Fulfillment
        Inbound API (create_inbound_plan, confirm_packing_option,
        confirm_placement_option, ... restituiscono tutte un operationId
        invece del risultato diretto) — stesso meccanismo già collaudato in
        AMZ_TESTS/_common.py::poll_inbound_operation, qui con un tetto più
        stretto (2 minuti, non 5) perché gira dentro una richiesta HTTP
        interattiva di Odoo (bottone sul form), non uno script a riga di
        comando. Ritorna il payload finale di get_inbound_operation_status.
        Solleva TimeoutError se non termina in tempo (il chiamante deve
        trattarlo come "riprova più tardi", non un errore permanente: il
        piano lato Amazon potrebbe comunque completarsi) e RuntimeError se
        lo stato finale è FAILED.
        """
        import time
        deadline = time.monotonic() + poll_max_minutes * 60
        while time.monotonic() < deadline:
            time.sleep(poll_interval_s)
            payload = self.get_inbound_operation_status(operation_id)
            status = payload.get('operationStatus', '')
            if status == 'SUCCESS':
                return payload
            if status == 'FAILED':
                raise RuntimeError(
                    f'Operazione {operation_id} fallita lato Amazon: '
                    f'{payload.get("operationProblems")}'
                )
        raise TimeoutError(
            f'Operazione {operation_id} non completata entro {poll_max_minutes} minuti — riprova.'
        )

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def create_inbound_plan(self, name, source_address, destination_marketplace_ids, items):
        """
        Crea un nuovo piano inbound. 'items': lista di
        {'msku', 'quantity', 'labelOwner', 'prepOwner'}. Asincrono: il
        payload contiene 'inboundPlanId' e 'operationId' — attendere
        poll_inbound_operation(operationId) prima di procedere.
        """
        return self._inbound_api().create_inbound_plan(
            name=name,
            sourceAddress=source_address,
            destinationMarketplaces=destination_marketplace_ids,
            items=items,
        ).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def generate_packing_options(self, inbound_plan_id):
        return self._inbound_api().generate_packing_options(inbound_plan_id).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def list_packing_options(self, inbound_plan_id):
        return self._inbound_api().list_packing_options(inbound_plan_id).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def confirm_packing_option(self, inbound_plan_id, packing_option_id):
        return self._inbound_api().confirm_packing_option(inbound_plan_id, packing_option_id).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def set_packing_information(self, inbound_plan_id, package_groupings):
        """'package_groupings': lista di {'packingGroupId', 'boxes': [...]}."""
        return self._inbound_api().set_packing_information(
            inbound_plan_id, packageGroupings=package_groupings,
        ).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def generate_placement_options(self, inbound_plan_id):
        return self._inbound_api().generate_placement_options(inbound_plan_id).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def list_placement_options(self, inbound_plan_id):
        return self._inbound_api().list_placement_options(inbound_plan_id).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def confirm_placement_option(self, inbound_plan_id, placement_option_id):
        """
        ATTENZIONE — punto di non ritorno dichiarato dalla documentazione
        ufficiale Amazon e confermato dal docstring della libreria stessa
        ("Once confirmed, it cannot be changed for the Inbound Plan."). Da
        chiamare SOLO dopo una conferma esplicita e consapevole dell'utente
        — vedi ke.amazon.inbound.plan.action_confirm_placement_option.
        """
        return self._inbound_api().confirm_placement_option(inbound_plan_id, placement_option_id).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def get_shipment(self, inbound_plan_id, shipment_id):
        return self._inbound_api().get_shipment(inbound_plan_id, shipment_id).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def generate_transportation_options(self, inbound_plan_id, placement_option_id,
                                         shipment_transportation_configurations):
        """
        'shipment_transportation_configurations': lista di
        {'shipmentId', 'readyToShipWindow': {'start': iso8601}, 'contactInformation'?,
        'freightInformation'?, 'pallets'?} — schema GenerateTransportationOptionsRequest
        verificato sulla pagina di riferimento ufficiale il 2026-10-29.
        """
        return self._inbound_api().generate_transportation_options(
            inbound_plan_id,
            placementOptionId=placement_option_id,
            shipmentTransportationConfigurations=shipment_transportation_configurations,
        ).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def list_transportation_options(self, inbound_plan_id, placement_option_id):
        return self._inbound_api().list_transportation_options(
            inbound_plan_id, placementOptionId=placement_option_id,
        ).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def confirm_transportation_options(self, inbound_plan_id, transportation_selections):
        """
        'transportation_selections': lista di {'shipmentId', 'transportationOptionId',
        'contactInformation'?} — schema ConfirmTransportationOptionsRequest
        verificato sulla pagina di riferimento ufficiale il 2026-10-29.
        Richiede che confirm_placement_option sia già stato chiamato con
        successo per questo piano (vincolo dichiarato dalla documentazione).
        """
        return self._inbound_api().confirm_transportation_options(
            inbound_plan_id, transportationSelections=transportation_selections,
        ).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def generate_delivery_window_options(self, inbound_plan_id, shipment_id):
        return self._inbound_api().generate_delivery_window_options(inbound_plan_id, shipment_id).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def list_delivery_window_options(self, inbound_plan_id, shipment_id):
        return self._inbound_api().list_delivery_window_options(inbound_plan_id, shipment_id).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def confirm_delivery_window_options(self, inbound_plan_id, shipment_id, delivery_window_option_id):
        return self._inbound_api().confirm_delivery_window_options(
            inbound_plan_id, shipment_id, delivery_window_option_id,
        ).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def get_labels(self, shipment_id, **kwargs):
        """
        Etichetta/e del collo per 'shipment_id'. Shape della risposta non
        verificata in questa sessione per la v2024-03-20 (la pagina di
        riferimento ufficiale trovata documenta solo la v0, con un
        'DownloadURL' valido 15 secondi — la libreria installata usa
        comunque un path v2024-03-20 per questo metodo). Verificare il
        payload reale restituito prima di collegare automaticamente il
        download/stampa — vedi ke.amazon.inbound.plan.action_get_labels.
        """
        return self._inbound_api().get_labels(shipment_id, **kwargs).payload

    @_with_retry(DELAY_INBOUND_S, retry_max=3, backoff_s=8)
    def cancel_inbound_plan(self, inbound_plan_id):
        """
        Annulla un piano inbound. La documentazione garantisce una finestra
        di annullamento senza costi solo DOPO la conferma del trasporto (24h
        pacco singolo, 1h LTL/FTL) — non dichiara esplicitamente cosa
        succede annullando PRIMA di quel punto (assenza di informazione, non
        una garanzia). Vedi RICERCA_fulfillment_inbound_api_sandbox_2026-10-29.md §2.
        """
        return self._inbound_api().cancel_inbound_plan(inbound_plan_id).payload
