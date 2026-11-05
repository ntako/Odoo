"""
Migrazione 19.0.1.3.0: un annuncio Amazon è marketplace + SKU, non più
variante + marketplace (vedi amazon_listing.py, 2026-11-05).

Toglie il vecchio vincolo unique(product_id, marketplace_id): Odoo crea il
nuovo unique(marketplace_id, sku) ma non elimina da solo quello vecchio,
che impedirebbe il secondo annuncio della stessa variante. Prima elimina gli
eventuali doppioni (marketplace, SKU), tenendo il più recente: con il vecchio
vincolo potevano esistere solo se lo stesso SKU era finito su due varianti.
"""
import logging

_log = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("ALTER TABLE ke_amazon_listing DROP CONSTRAINT IF EXISTS ke_amazon_listing_product_marketplace_uniq")
    cr.execute("""
        DELETE FROM ke_amazon_listing l
         USING ke_amazon_listing newer
         WHERE l.sku IS NOT NULL AND l.sku <> ''
           AND newer.marketplace_id = l.marketplace_id AND newer.sku = l.sku
           AND newer.id > l.id
    """)
    if cr.rowcount:
        _log.warning('ke_amazon_listing: eliminati %d annunci doppi (stesso marketplace e SKU)', cr.rowcount)
