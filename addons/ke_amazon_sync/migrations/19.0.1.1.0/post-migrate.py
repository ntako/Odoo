"""
Migrazione: ke_amazon_listing.product_id da product.template a
product.product (vedi amazon_listing.py e TASK.md, refactor 2026-10-23).

Prima volta che questo modulo usa un vero script di migrazione Odoo (la
versione del manifest non era mai stata bumpata prima d'ora) — scelto invece
del solito pulsante "una tantum" in Impostazioni (pattern usato altrove in
questo modulo, es. action_migrate_to_amazon_listings) perché qui la colonna
esistente viene REINTERPRETATA (non solo arricchita): un cron o un thread di
sync già in esecuzione al momento dell'upgrade potrebbe leggere la tabella a
metà cammino se si aspettasse un click manuale. Girare dentro la stessa
transazione di `-u` chiude quella finestra.

Logica (nessun matching euristico SKU/ASIN sui dati storici — scelta
esplicita dell'utente: il punto di partenza reale, sia su Docker che in
produzione, è sempre il catalogo manuale PRIMA di un Full Import, quindi
questa tabella sarà quasi sempre vuota quando lo script gira per davvero):
  1) Righe di FAMIGLIA (template con più varianti): eliminate. Erano
     comunque ambigue (un'unica riga condivisa tra sorelle, scritta
     dall'ultima che capitava — il bug che questo refactor risolve) e
     verranno ricreate correttamente, una per variante, dal prossimo Full
     Import/riconciliazione col codice nuovo.
  2) Righe di prodotto SEMPLICE (template con 1 sola variante): nessuna
     ambiguità, repoint diretto sull'unica variante.

Ordine critico: PRIMA si eliminano le righe di famiglia (quando product_id
significa ancora univocamente "id template" per OGNI riga), POI si fa il
repoint dei semplici rimasti. Invertire l'ordine rischierebbe di
interpretare un id-variante già migrato come se fosse un id-template per
coincidenza numerica (le sequenze product_product/product_template sono
indipendenti in Postgres, possono collidere).
"""
import logging

_log = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        DELETE FROM ke_amazon_listing l
        USING product_template pt
        WHERE l.product_id = pt.id
          AND (SELECT count(*) FROM product_product pp
               WHERE pp.product_tmpl_id = pt.id) > 1
    """)
    deleted_family_rows = cr.rowcount

    cr.execute("""
        UPDATE ke_amazon_listing l
        SET product_id = pp.id
        FROM product_product pp
        WHERE pp.product_tmpl_id = l.product_id
    """)
    migrated_simple_rows = cr.rowcount

    _log.info(
        'Migrazione ke_amazon_listing a product.product: %d righe di famiglia '
        'eliminate (ambigue, verranno ricreate per variante dal prossimo Full '
        'Import), %d righe di prodotto semplice ripuntate alla loro variante.',
        deleted_family_rows, migrated_simple_rows,
    )
