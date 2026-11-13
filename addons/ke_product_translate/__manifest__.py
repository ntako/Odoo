{
    'name': 'KE Product Translate',
    'version': '19.0.1.0.0',
    'category': 'Sales/Sales',
    'summary': 'Traduzione dei testi del sito dei prodotti (traduttori gratuiti, in seguito AI)',
    'description': """
Traduce i testi della scheda "Sito web" dei prodotti (ke_product_catalog:
nome sul sito, meta title, meta description, URL, descrizione breve e
completa) nelle altre lingue attive, con il fornitore scelto.

Fase 1 (2026-11-13): traduttori semplici gratuiti LibreTranslate
(self-hosted, senza limiti) e DeepL API (quota gratuita 500.000 caratteri
al mese). Fasi successive: fornitori AI (Anthropic, OpenAI e compatibili,
Gemini, modelli locali) per tradurre, migliorare e controllare i testi.

- Fornitori: configurati in Odoo; le chiavi le vede solo l'amministratore.
- Azione "Traduci testi sito" sui prodotti: lingua di partenza, lingue di
  arrivo, solo campi vuoti (o copia della lingua di partenza) oppure tutti,
  scrittura diretta oppure proposte da approvare.
- Più di qualche prodotto: in coda, a turni in background, con registro.

Non tocca nome e descrizione di vendita del prodotto (quelli di Amazon e
dei fornitori): solo i testi del sito.
""",
    'author': 'KE',
    'depends': ['ke_product_catalog'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/translate_provider_views.xml',
        'views/translate_proposal_views.xml',
        'views/translate_job_views.xml',
        'wizard/translate_wizard_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
