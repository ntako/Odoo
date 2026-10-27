from odoo import fields, models


class AmazonPrintStationShowKeyWizard(models.TransientModel):
    _name = 'ke.amazon.print.station.show.key'
    _description = 'Mostra una sola volta la chiave API generata per una postazione di stampa'

    station_id = fields.Many2one('ke.amazon.print.station', required=True)
    raw_key = fields.Char(
        'Chiave API', readonly=True,
        help='Copiala subito nella configurazione dell\'agente Windows: dopo aver chiuso '
             'questa finestra non sarà più recuperabile da Odoo (solo il suo hash è salvato).',
    )
