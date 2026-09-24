from odoo import api, fields, models


class AmazonMarketplace(models.Model):
    _name = 'ke.amazon.marketplace'
    _description = 'Marketplace Amazon'
    _order = 'sequence, name'

    name = fields.Char('Nome', required=True, help="Es. 'Italia', 'Germania'.")
    code = fields.Char('Codice', required=True, help="Codice breve, es. 'IT', 'DE'.")
    marketplace_id = fields.Char(
        'Marketplace ID', required=True,
        help='ID marketplace Amazon usato nelle chiamate SP-API (es. APJ6JRA9NG5V4 per IT).',
    )
    active = fields.Boolean(
        'Attivo', default=False,
        help='Solo i marketplace attivi vengono inclusi nelle sync multi-marketplace '
             '(fase successiva). Il marketplace usato oggi da questo modulo (Impostazioni → '
             'Amazon → Marketplace ID) va attivato qui perché la migrazione dei dati esistenti '
             'lo trovi.',
    )
    currency_id = fields.Many2one('res.currency', string='Valuta')
    sequence = fields.Integer('Sequenza', default=10)
    fba_warehouse_id = fields.Many2one(
        'stock.warehouse', string='Magazzino FBA',
        help="Il magazzino Odoo dove questo marketplace tiene la sua giacenza FBA. "
             "Impostalo esplicitamente sullo STESSO magazzino per tutti i marketplace "
             "che condividono davvero lo stesso stock fisico (es. il pool Pan-Europeo "
             "IT/DE/ES/FR oggi) — l'appartenenza al pool si determina da questo, non da "
             "un caso speciale nel codice: vedi is_fba_pooled(). Se lasciato vuoto, "
             "eredita il Magazzino FBA configurato in Impostazioni → Amazon (comodo per "
             "non dover compilare tutti i marketplace uno per uno, ma il modo esplicito "
             "è preferibile per chiarezza). Imposta un magazzino diverso da quello degli "
             "altri solo per un marketplace con inventario FBA realmente separato, non "
             "pooled (es. UK o US).",
    )

    def get_fba_warehouse(self):
        """
        Magazzino FBA effettivo per questo marketplace: quello impostato qui, o —
        se vuoto — il Magazzino FBA aziendale configurato in Impostazioni → Amazon
        (comportamento di sempre, invariato, utile finché non tutti i marketplace
        pooled sono stati compilati esplicitamente).
        """
        self.ensure_one()
        return self.fba_warehouse_id or self.env.company.amazon_fba_warehouse_id

    def is_fba_pooled(self):
        """
        True se questo marketplace condivide il magazzino FBA effettivo (vedi
        get_fba_warehouse) con almeno un altro marketplace ATTIVO, o se è
        l'unico marketplace attivo (nessun altro con cui confrontarsi — resta
        "pooled" per non smettere di colpo di scrivere sui campi legacy
        x_amazon_qty/x_amazon_fnsku su cui si basa il resto del modulo).
        Questa è la definizione di "pool": non un caso speciale per l'EU, ma
        semplicemente "chi condivide lo stesso magazzino con altri". Un
        marketplace esce dal pool (torna 'dedicato', tracciato su
        ke.amazon.listing invece che sui campi condivisi) semplicemente
        assegnandogli un magazzino diverso da quello degli altri — e viceversa
        per farlo entrare — senza bisogno di toccare il codice.
        """
        self.ensure_one()
        my_warehouse = self.get_fba_warehouse()
        if not my_warehouse:
            return False
        active_marketplaces = self.env['ke.amazon.marketplace'].search([('active', '=', True)])
        warehouse_by_marketplace = {m.id: m.get_fba_warehouse() for m in active_marketplaces}
        distinct_warehouse_ids = {wh.id for wh in warehouse_by_marketplace.values() if wh}
        if len(distinct_warehouse_ids) <= 1:
            return True
        return any(
            mid != self.id and wh == my_warehouse
            for mid, wh in warehouse_by_marketplace.items()
        )

    # Sintassi Odoo 19: _sql_constraints (lista di tuple) non è più supportato,
    # sostituito da models.Constraint come attributo di classe — verificato
    # con un'installazione locale reale (Docker/odoo/), non solo per sentito
    # dire: il vecchio stile installava comunque ma con un warning esplicito
    # "no longer supported, please define models.Constraint".
    _marketplace_id_uniq = models.Constraint(
        'unique(marketplace_id)',
        'Esiste già un marketplace con questo Marketplace ID.',
    )

    @api.depends('name', 'code')
    def _compute_display_name(self):
        # Odoo 19: name_get() non esiste più (rimosso dall'ORM), il suo posto
        # è preso da questo compute — un altro caso verificato con
        # l'installazione locale, non solo dedotto dal changelog.
        for rec in self:
            rec.display_name = f'{rec.name} ({rec.code})' if rec.code else rec.name
