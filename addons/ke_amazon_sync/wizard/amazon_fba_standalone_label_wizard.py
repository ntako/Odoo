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
        self.last_scan_message = (
            f'{product.display_name} — SKU {product.default_code or "-"} — '
            f'FNSKU {fnsku or "MANCANTE"}'
        )

    def action_print(self):
        self.ensure_one()
        product = self._resolve_product()
        fnsku = product._get_amazon_fnsku()
        if not fnsku:
            raise UserError(f'{product.display_name}: FNSKU mancante. Esegui prima "Import FNSKU".')
        qty = self.quantity or 0
        if qty <= 0:
            raise UserError('Indica un numero di copie maggiore di zero.')

        # Nessun move_id/picking_id: stampa indipendente da qualunque
        # spedizione, non tocca giacenza né richiede un trasferimento aperto
        # (vedi amazon_fba_label_log.py). Un record per copia, così il report
        # (un foglio per 'doc') stampa esattamente 'qty' etichette.
        logs = self.env['ke.amazon.fba.label.log'].create([{
            'product_id': product.id,
            'fnsku': fnsku,
        } for _ in range(qty)])

        # "Usa stampa di rete (ZPL)" (Impostazioni → Amazon): se attiva e con
        # una postazione configurata, accoda invece di generare il PDF — vedi
        # ke.amazon.print.station._get_configured_station(). Record vuoto
        # (opzione disattivata, comportamento di sempre) non è un errore.
        station = self.env['ke.amazon.print.station']._get_configured_station()
        if station:
            for log in logs:
                self.env['ke.amazon.print.job']._enqueue_from_label_log(station, log)
            self.last_scan_message = (
                f'{qty} etichetta/e {product.display_name} in coda di stampa su "{station.name}".'
            )
            return True

        return self.env.ref('ke_amazon_sync.action_report_amazon_fba_label').report_action(
            logs, config=False,
        )
