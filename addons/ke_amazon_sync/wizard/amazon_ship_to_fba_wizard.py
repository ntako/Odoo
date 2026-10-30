from odoo import fields, models
from odoo.exceptions import UserError


class AmazonShipToFbaWizard(models.TransientModel):
    _name = 'ke.amazon.ship.wizard'
    _description = 'Registra spedizione di rifornimento verso Amazon FBA'

    source_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Magazzino di partenza', required=True,
        default=lambda self: self.env.company.amazon_default_warehouse_id,
    )
    marketplace_id = fields.Many2one(
        'ke.amazon.marketplace', string='Marketplace di destinazione',
        domain="[('active', '=', True)]",
        help="Lascia vuoto per il pool FBA Pan-EU condiviso (IT/DE/ES/FR oggi). "
             "Seleziona un marketplace solo se ha un magazzino FBA dedicato "
             "impostato (inventario non pooled, es. UK/US).",
    )
    line_ids = fields.One2many(
        'ke.amazon.ship.wizard.line', 'wizard_id', string='Prodotti',
    )

    def action_confirm(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError('Aggiungi almeno un prodotto da spedire.')

        from odoo.addons.ke_amazon_sync.services.sync_engine import SyncEngine
        engine = SyncEngine(self.env, None)
        lines = [(line.product_id, line.quantity) for line in self.line_ids]
        picking = engine.ship_to_fba(self.source_warehouse_id, lines, marketplace=self.marketplace_id)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_print_all_fnsku_labels(self):
        """
        Stampa in UN SOLO click le etichette FNSKU di TUTTE le righe (ognuna
        per la propria quantità) PRIMA ancora di confermare la spedizione —
        il trasferimento/stock.move non esiste ancora a questo punto.
        Bottone a livello di wizard (non per riga): un bottone dentro una
        riga della lista editabile non salva sempre la riga prima di
        chiamare il metodo (trovato dal vivo il 2026-10-29: "non funziona se
        non salvo prima"), mentre un bottone sul form principale salva
        sempre l'intero form, righe comprese, prima di eseguire l'azione.
        Nessun move/picking collegato (come il wizard standalone): questa
        stampa non traccia nessuna quantità "etichettata" contro un
        movimento, va fatta di nuovo dal wizard di etichettatura/scansione
        se si vuole un conteggio legato al trasferimento reale.
        Le righe marchio proprio (FNSKU con prefisso 'B0', in realtà l'ASIN
        — vedi product.product._amazon_fnsku_needs_label) vengono escluse
        dalla stampa senza errore: tracciano tramite il codice del
        produttore già sull'unità, nessuna etichetta serve.
        """
        self.ensure_one()
        if not self.line_ids:
            raise UserError('Aggiungi almeno un prodotto prima di stampare.')

        specs = []
        for line in self.line_ids:
            product = line.product_id
            if not product:
                raise UserError('Una riga non ha un prodotto selezionato.')
            fnsku = product._get_amazon_fnsku()
            if not fnsku:
                raise UserError(f'{product.display_name}: FNSKU non ancora importato. Esegui prima "Import FNSKU".')
            qty = int(line.quantity or 0)
            if qty <= 0:
                raise UserError(f'{product.display_name}: indica una quantità maggiore di zero.')
            if product._amazon_fnsku_needs_label():
                specs.append((product, fnsku, qty, False))

        if not specs:
            raise UserError(
                'Nessuna etichetta da stampare: tutti i prodotti in elenco sono marchio proprio '
                '(tracciano tramite il codice del produttore, nessun FNSKU da applicare).'
            )
        result = self.env['ke.amazon.fba.label.log']._create_and_dispatch_many(specs)
        if result['queued']:
            return True
        return result['report_action']


class AmazonShipToFbaWizardLine(models.TransientModel):
    _name = 'ke.amazon.ship.wizard.line'
    _description = 'Riga spedizione verso Amazon FBA'

    wizard_id = fields.Many2one('ke.amazon.ship.wizard', required=True, ondelete='cascade')
    product_id = fields.Many2one(
        'product.product', string='Prodotto', required=True,
        domain="[('product_tmpl_id.x_amazon_fulfillment', '=', 'fba')]",
    )
    quantity = fields.Float('Quantità', required=True, default=1.0)
