from odoo import fields, models
from odoo.exceptions import UserError


class AmazonFbaStandaloneLabelWizard(models.TransientModel):
    _name = 'ke.amazon.fba.standalone.label.wizard'
    _description = 'Stampa etichette FNSKU per variante, senza spedizione/trasferimento'

    ean_scan = fields.Char('Scansiona EAN')
    product_id = fields.Many2one('product.product', string='Prodotto')
    quantity = fields.Integer('Copie da stampare', default=1)
    last_scan_message = fields.Char('Ultima identificazione', readonly=True)

    def _resolve_product(self):
        """
        Prodotto da etichettare: per EAN scansionato (tutto il catalogo, non
        solo una spedizione — qui non c'è nessuna spedizione) o per selezione
        diretta. Un EAN su più prodotti va segnalato, non risolto a caso
        (vedi progetto-odoo-amazon-fba-etichettatura.md §7, "EAN associato a
        più prodotti" tra i controlli bloccanti).
        """
        self.ensure_one()
        barcode = (self.ean_scan or '').strip()
        if barcode:
            products = self.env['product.product'].search([('barcode', '=', barcode)])
            if not products:
                raise UserError(f'EAN sconosciuto: nessun prodotto con il codice a barre "{barcode}".')
            if len(products) > 1:
                raise UserError(
                    f'EAN "{barcode}" associato a più prodotti '
                    f'({", ".join(products.mapped("display_name"))}) — '
                    f'serve una correzione del catalogo prima di stampare.'
                )
            return products
        if not self.product_id:
            raise UserError('Scansiona un EAN o seleziona un prodotto.')
        return self.product_id

    def action_scan(self):
        """
        Identifica il prodotto (da EAN o da selezione) e mostra SKU/FNSKU per
        un controllo visivo prima di stampare — stesso riscontro del flusso
        spedizione (amazon_fba_prep_wizard), separato dalla stampa vera e
        propria per lo stesso motivo: il rischio principale non è la velocità
        ma l'etichetta sbagliata su una referenza.
        """
        self.ensure_one()
        product = self._resolve_product()
        self.ean_scan = False
        self.product_id = product.id
        fnsku = product._get_amazon_fnsku()
        if fnsku and not product._amazon_fnsku_needs_label():
            fnsku_note = f'{fnsku} — marchio proprio, nessuna etichetta necessaria'
        else:
            fnsku_note = fnsku or 'MANCANTE'
        self.last_scan_message = (
            f'{product.display_name} — SKU {product.default_code or "-"} — FNSKU {fnsku_note}'
        )

    def action_print(self):
        self.ensure_one()
        product = self._resolve_product()
        fnsku = product._get_amazon_fnsku()
        if not fnsku:
            raise UserError(f'{product.display_name}: FNSKU non ancora importato. Esegui prima "Import FNSKU".')
        qty = self.quantity or 0
        if qty <= 0:
            raise UserError('Indica un numero di copie maggiore di zero.')

        if not product._amazon_fnsku_needs_label():
            # FNSKU con prefisso 'B0' (in realtà l'ASIN): marchio proprio,
            # traccia tramite il codice del produttore già sull'unità —
            # nessuna etichetta da stampare. Vedi product.product.
            # _amazon_fnsku_needs_label. Nessun log creato qui: a differenza
            # di stock.move._print_fnsku_labels non c'è nessuna quantità da
            # "completare" in questo flusso standalone.
            self.last_scan_message = (
                f'{product.display_name}: marchio proprio, nessuna etichetta FNSKU necessaria '
                f'(traccia tramite il codice del produttore).'
            )
            return True

        # Nessun move_id/picking_id: stampa indipendente da qualunque
        # spedizione, non tocca giacenza né richiede un trasferimento aperto
        # (vedi amazon_fba_label_log.py). Logica di creazione/dispatch
        # condivisa con stock.move._print_fnsku_labels e il bottone sulle
        # righe del wizard "Spedisci a FBA" — vedi _create_and_dispatch.
        result = self.env['ke.amazon.fba.label.log']._create_and_dispatch(product, fnsku, qty)
        if result['queued']:
            self.last_scan_message = (
                f'{qty} etichetta/e {product.display_name} in coda di stampa su "{result["station"]}".'
            )
            return True
        return result['report_action']
