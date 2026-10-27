from odoo import api, fields, models


class AmazonFbaLabelLog(models.Model):
    _name = 'ke.amazon.fba.label.log'
    _description = 'Storico etichette FNSKU stampate (spedizione FBA)'
    _order = 'create_date desc'

    picking_id = fields.Many2one(
        'stock.picking', string='Spedizione', ondelete='cascade', index=True,
        help='Vuoto per una stampa autonoma (ke.amazon.fba.standalone.label.wizard), '
             'non legata a una spedizione/trasferimento — vedi move_id.',
    )
    move_id = fields.Many2one(
        'stock.move', string='Movimento', ondelete='cascade',
        help='Riga della spedizione (un prodotto) a cui questa etichetta si riferisce, '
             'se stampata dal flusso di etichettatura spedizione. Vuoto per una stampa '
             'autonoma per variante — non conta per la quantità etichettata di nessun '
             'movimento e non richiede giacenza, trasferimento o spedizione aperta.',
    )
    product_id = fields.Many2one(
        'product.product', string='Prodotto', required=True, index=True,
    )
    fnsku = fields.Char(
        'FNSKU', required=True,
        help="Valore FNSKU stampato su questa etichetta, congelato al momento della "
             "stampa (se cambiasse più tardi su un nuovo import, lo storico resta corretto).",
    )
    user_id = fields.Many2one(
        'res.users', string='Operatore', required=True, default=lambda self: self.env.user,
    )
    is_reprint = fields.Boolean(
        'Ristampa', default=False,
        help="True se al momento della stampa la riga aveva già raggiunto la quantità "
             "prevista (unità danneggiata/etichetta da rifare) — non ha incrementato la "
             "quantità fatta sul movimento, serve solo come traccia storica.",
    )

    @api.depends('product_id', 'fnsku')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f'{rec.product_id.display_name} — {rec.fnsku}'
