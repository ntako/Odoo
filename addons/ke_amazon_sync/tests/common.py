"""
Doppi finti e helper per i test di sync_engine.py.

Mai `unittest.mock.patch` su metodi della classe `SpApiClient` reale — vedi
memoria persistente del progetto (testing_never_mock_patch_spapiclient.md):
il DB Docker usato per i test ha credenziali Amazon reali funzionanti, un
patch parziale rischia di lasciar passare una chiamata vera. Si usano solo
classi finte scritte a mano che sostituiscono l'intero client, oppure
`SyncEngine(env, None)` quando il test non ha bisogno di nessuna chiamata
Amazon (es. testare `_process_variant_listing` isolata). `no_commit` sotto
patcha invece `env.cr.commit` (un metodo locale del cursore DB, non l'API
Amazon) — categoria diversa, non in conflitto con quella regola.
"""
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def no_commit(env):
    """
    full_import_report_rows/full_import_chunk fanno `env.cr.commit()`
    internamente (per sopravvivere a un riavvio del server in produzione —
    vedi progetto-sync-thread-invece-di-cron.md). Il framework di test di
    Odoo sostituisce `commit()`/`rollback()` con un'eccezione esplicita
    ("Cannot commit or rollback a cursor from inside a test") per proteggere
    il rollback automatico di fine test, non con un vero COMMIT né un no-op
    silenzioso (verificato leggendo odoo/tests/common.py). Per poter
    chiamare queste funzioni nei test senza scrivere davvero sul DB, lo si
    rende un no-op solo per la durata della chiamata — il rollback standard
    di TransactionCase a fine test pulisce comunque tutto, come per
    qualsiasi altra modifica.
    """
    with patch.object(env.cr, 'commit', lambda: None):
        yield


class FakeSpApiClient:
    """
    Client SP-API finto per i test di matching del Full Import
    (full_import/full_import_report_rows/full_import_chunk,
    _process_variant_listing). Implementa solo `get_catalog_items_batch`,
    l'unica chiamata usata da quei percorsi durante l'arricchimento Catalog
    API a batch.
    """

    # Nessun marketplace reale censito con questo valore: _current_marketplace()
    # (usata da _upsert_listing, chiamata da _create_product/_add_variant_to_
    # template) cerca ke.amazon.marketplace per questo id, non lo trova, e si
    # comporta come "marketplace non censito" — no-op esplicito già previsto
    # dal codice, non un'eccezione. Evita di dover censire un marketplace
    # finto in ogni test solo per non far esplodere _upsert_listing.
    marketplace_id = None

    def __init__(self, parent_asin_by_asin=None):
        # {asin_figlio: parent_asin} — un ASIN non presente qui risulta
        # "nessuna relazione di variante" (prodotto standalone agli occhi
        # del Catalog API), stesso comportamento di un ASIN reale senza
        # 'relationships' nella risposta.
        self._parent_asin_by_asin = parent_asin_by_asin or {}

    def get_catalog_items_batch(self, asins):
        out = {}
        for asin in asins:
            parent = self._parent_asin_by_asin.get(asin)
            if parent:
                out[asin] = {
                    'relationships': [{'relationships': [
                        {'type': 'VARIATION', 'parentAsins': [parent], 'childAsins': []},
                    ]}],
                }
            else:
                out[asin] = {}
        return out


def make_report_row(sku, asin='', name='Test prodotto', price='9.99', qty='3',
                     fulfillment='DEFAULT', status='Active'):
    """
    Costruisce una riga del report listing (formato TSV Amazon) con le
    chiavi esatte attese da `SyncEngine._parse_listing_report`.
    """
    return {
        'seller-sku': sku,
        'asin1': asin,
        'item-name': name,
        'price': price,
        'quantity': qty,
        'fulfillment-channel': fulfillment,
        'product-id-type': '',
        'product-id': '',
        'status': status,
    }
