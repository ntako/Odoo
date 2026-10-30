"""
Generazione del comando ZPL per l'etichetta FNSKU — vedi
progetto-odoo-amazon-fba-etichettatura.md §9 per il contenuto di riferimento
(barcode Code128 + FNSKU leggibile + SKU + descrizione breve).

Coordinate/dimensioni sono in dot, non mm: a 203dpi 1mm ≈ 8 dot. I valori di
default qui sotto assumono un'etichetta 50x25mm a 203dpi (ipotesi del
documento di progetto, §4) — non ancora calibrati su un rotolo reale montato
sulla ZD621 (dato mancante, vedi §13). Vanno verificati e corretti con un
lotto di prova prima della produzione; tenerli qui, isolati dalla logica di
coda (ke.amazon.print.job), rende quella calibrazione un cambio di una
funzione sola, non una caccia nel codice.
"""
_LABEL_WIDTH_DOT = 400   # 50mm a 203dpi
_LABEL_HEIGHT_DOT = 200  # 25mm a 203dpi
_MAX_NAME_LEN = 40


def build_fnsku_zpl(fnsku, sku, name, condition=''):
    """
    ZPL per un'etichetta FNSKU: barcode Code128, testo FNSKU, SKU interno,
    descrizione breve troncata, condizione ('Nuovo', come sull'etichetta di
    Amazon — aggiunta il 2026-10-30). 'fnsku' è obbligatorio (verificato dal
    chiamante, vedi ke.amazon.print.job._enqueue_from_label_log) — qui non
    si riverifica, solo si genera.
    """
    sku = (sku or '').strip()
    name = (name or '').strip()[:_MAX_NAME_LEN]
    condition = (condition or '').strip()
    return (
        f'^XA\n'
        f'^PW{_LABEL_WIDTH_DOT}\n'
        f'^LL{_LABEL_HEIGHT_DOT}\n'
        f'^FO20,15^BY2\n'
        f'^BCN,70,N,N,N\n'
        f'^FD{fnsku}^FS\n'
        f'^FO20,95^A0N,24,24^FD{fnsku}^FS\n'
        f'^FO20,125^A0N,18,18^FD{sku}^FS\n'
        f'^FO20,150^A0N,16,16^FD{name}^FS\n'
        + (f'^FO20,172^A0N,16,16^FD{condition}^FS\n' if condition else '')
        + f'^XZ\n'
    )
