"""
Famiglie del fornitore (2026-11-20).

Le famiglie in Odoo sono nate dalle inserzioni Amazon, che spesso raggruppano
male: la stessa canna in 11 prodotti separati (Whiplash8), oppure un solo
prodotto con 8 esche diverse ("Berkley PowerBait"), o le linee Flex Carp,
Coarse, Pike… come varianti di un unico filo.

Il catalogo del fornitore dice qual è la famiglia di ogni articolo (Pure
Fishing: "Master code" + "Master Description"). L'arricchimento la salva
sulla VARIANTE (ke_supplier_family_ref / ke_supplier_family_name): sulla
variante e non sul prodotto perché oggi un prodotto Odoo può contenere
varianti di famiglie diverse.

Proposte (ke.product.family.proposal), una per famiglia del fornitore le cui
varianti in Odoo non stanno tutte in un prodotto solo, da sole:
- Unisci: le varianti sono sparse su più prodotti Odoo;
- Separa: stanno in un prodotto che contiene anche altre famiglie;
- Riorganizza: tutte e due le cose.
Prodotto di destinazione proposto: tra quelli che ne contengono le varianti,
quello dove questa famiglia è la principale (più varianti); un prodotto va a
una sola famiglia, le altre ne avranno uno nuovo.

"Applica" sposta le varianti (models/product_family_move.py), una proposta
alla volta e per intero o niente.
"""
from collections import defaultdict

from odoo import api, fields, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    ke_supplier_family_ref = fields.Char(
        'Codice famiglia fornitore', index=True, copy=False,
        help='Codice della famiglia a cui l\'articolo appartiene nel catalogo del fornitore '
             '(Pure Fishing: "Master code"). Scritto dall\'arricchimento.')
    ke_supplier_family_name = fields.Char(
        'Famiglia fornitore', copy=False,
        help='Nome della famiglia nel catalogo del fornitore (Pure Fishing: "Master Description").')
    # Codice dell'articolo presso il fornitore (2026-11-20), per variante: il codice Odoo è lo SKU Amazon
    # ripulito (es. TUB-20181) e non coincide col codice del file (20181XX). Dal secondo import l'abbinamento
    # passa da qui. "Fornitore" = etichetta del preset (il prefisso, es. "TUB-"): codici uguali di fornitori
    # diversi restano distinti.
    ke_supplier_code = fields.Char('Codice fornitore (variante)', index=True, copy=False)
    ke_supplier_tag = fields.Char('Fornitore (etichetta)', index=True, copy=False,
                                  help='Prefisso o nome del preset di import che ha scritto il codice fornitore.')


KINDS = [('merge', 'Unisci'), ('split', 'Separa'), ('both', 'Riorganizza')]


class ProductFamilyProposal(models.Model):
    _name = 'ke.product.family.proposal'
    _description = 'Proposta di famiglia'
    _order = 'certain, brand_id, name, id'

    name = fields.Char('Famiglia del fornitore', readonly=True)
    family_ref = fields.Char('Codice famiglia', readonly=True, index=True)
    brand_id = fields.Many2one('ke.product.brand', 'Marca', readonly=True)
    kind = fields.Selection(KINDS, string='Cosa fare', readonly=True)
    variant_ids = fields.Many2many(
        'product.product', 'ke_family_proposal_variant_rel', 'proposal_id', 'product_id',
        string='Varianti della famiglia', readonly=True)
    tmpl_ids = fields.Many2many(
        'product.template', 'ke_family_proposal_tmpl_rel', 'proposal_id', 'tmpl_id',
        string='Prodotti dove sono oggi', readonly=True)
    target_tmpl_id = fields.Many2one(
        'product.template', 'Prodotto di destinazione', readonly=True,
        help='Prodotto che diventerebbe la famiglia. Vuoto: un prodotto nuovo (tutti i prodotti attuali '
             'sono già la famiglia principale di un\'altra famiglia del fornitore).')
    variant_count = fields.Integer('Varianti', readonly=True)
    tmpl_count = fields.Integer('Prodotti', readonly=True)
    summary = fields.Char('In breve', readonly=True)
    warnings = fields.Text('Da sapere', readonly=True)
    certain = fields.Boolean('Senza avvisi', readonly=True)
    state = fields.Selection([('pending', 'Da valutare'), ('applied', 'Applicata'), ('discarded', 'Scartata')],
                             string='Stato', default='pending', required=True, index=True)
    apply_error = fields.Char('Errore', readonly=True, help='Motivo per cui "Applica" non è riuscito.')

    def action_apply(self):
        """
        Applica le proposte scelte, una per volta e ognuna per intero o niente (savepoint). Prima controlla che
        la famiglia sia ancora com'era quando è stata proposta: se no, chiede di ricalcolare.
        """
        todo = self.filtered(lambda p: p.state == 'pending')
        if len(todo) > self._BACKGROUND_FROM and not self.env.context.get('ke_family_foreground'):
            return todo._apply_in_background()
        from .product_family_move import QUIET
        self = self.with_context(**QUIET)
        todo = todo.with_context(**QUIET)
        done, errors = 0, []
        batch = self.env.context.get('ke_family_commit')
        for rec_id in todo.ids:
            # Una proposta per volta, senza prefetch delle altre (2026-11-20): con 2.000 proposte nello stesso
            # recordset e la cache mai svuotata ogni proposta rileggeva tutto il già caricato (da 1 a 12 s l'una).
            rec = self.browse(rec_id)
            Tmpl = self.env['product.template']
            label = rec.name or rec.family_ref
            current = self.env['product.product'].search([('ke_supplier_family_ref', '=', rec.family_ref),
                                                         ('product_tmpl_id.active', '=', True)])
            if current != rec.variant_ids or (rec.target_tmpl_id and not rec.target_tmpl_id.active):
                errors.append(f'{label}: le varianti sono cambiate dal calcolo, premi "Calcola proposte"')
                continue
            brand = rec.brand_id.name if rec.brand_id else ''
            name = rec.name if not brand or rec.name.lower().startswith(brand.lower()) else f'{brand} {rec.name}'
            try:
                with self.env.cr.savepoint():
                    target = Tmpl.ke_family_move(rec.variant_ids, rec.target_tmpl_id, name=name,
                                                 family_name=rec.name)
                    target.ke_apply_supplier_names()
                    rec.write({'state': 'applied', 'target_tmpl_id': target.id})
                done += 1
            except Exception as exc:   # noqa: BLE001 — una proposta che fallisce non ferma le altre
                errors.append(f'{label}: {exc}')
                rec.write({'apply_error': str(exc)[:500]})
            if batch:
                self.env.cr.commit()
                self.env.invalidate_all()
        message = f'{done} famiglie sistemate.' + (' Non applicate: ' + ' | '.join(errors[:10]) if errors else '')
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': 'Proposte di famiglia', 'type': 'warning' if errors else 'success',
                           'sticky': bool(errors), 'message': message,
                           'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}}}

    # Oltre questo numero di proposte "Applica" gira in background, con un salvataggio dopo ogni proposta
    # (2.189 proposte sulla copia di produzione: in una richiesta sola andrebbe in timeout).
    _BACKGROUND_FROM = 30

    def _apply_in_background(self):
        import threading
        import odoo
        from odoo.modules.registry import Registry
        ids, dbname, uid, context = self.ids, self.env.cr.dbname, self.env.uid, dict(self.env.context)

        def run():
            with Registry(dbname).cursor() as cr:
                env = odoo.api.Environment(cr, uid, dict(context, ke_family_foreground=True, ke_family_commit=True))
                try:
                    env['ke.product.family.proposal'].browse(ids).exists().action_apply()
                    cr.commit()
                except Exception:   # noqa: BLE001
                    cr.rollback()
                    import logging
                    logging.getLogger(__name__).exception('Applicazione proposte di famiglia in background fallita')

        threading.Thread(target=run, daemon=True, name='ke_family_apply').start()
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': 'Proposte di famiglia', 'type': 'info', 'sticky': True,
                           'message': f'{len(ids)} proposte in applicazione in background (circa un secondo l\'una). '
                                      'Ricarica la lista per vedere quelle già "Applicate"; eventuali errori nella '
                                      'colonna "Errore".'}}

    def action_discard(self):
        self.filtered(lambda p: p.state == 'pending').write({'state': 'discarded'})
        return True

    def action_restore(self):
        self.filtered(lambda p: p.state == 'discarded').write({'state': 'pending'})
        return True

    # ------------------------------------------------------------------
    # Generazione
    # ------------------------------------------------------------------

    def action_propose_all(self):
        # Niente @api.model: il bottone nell'intestazione della lista passa gli id selezionati.
        self = self.browse()
        created = self.propose()
        action = self.env['ir.actions.act_window']._for_xml_id('ke_product_catalog.action_ke_family_proposal')
        action['name'] = f'Proposte di famiglia ({created} da valutare)'
        return action

    @api.model
    def _load(self):
        """
        Varianti attive dei prodotti attivi: famiglia del fornitore per variante e, per ogni prodotto
        coinvolto, tutte le sue varianti (anche quelle senza famiglia del fornitore).
        """
        self.env.flush_all()   # la query legge il database: prima le modifiche ancora in memoria
        cr = self.env.cr
        cr.execute("""
            SELECT pp.id, pp.product_tmpl_id, pp.ke_supplier_family_ref, pp.ke_supplier_family_name
              FROM product_product pp
              JOIN product_template pt ON pt.id = pp.product_tmpl_id
             WHERE pp.active AND pt.active
               AND pp.product_tmpl_id IN (
                    SELECT p2.product_tmpl_id FROM product_product p2
                     WHERE p2.active AND COALESCE(p2.ke_supplier_family_ref, '') != '')
        """)
        return cr.fetchall()

    @api.model
    def propose(self):
        """Ricalcola le proposte da valutare (applicate e scartate restano). Ritorna quante ne crea."""
        rows = self._load()
        fam_variants = defaultdict(list)                       # famiglia → varianti
        fam_name = {}
        tmpl_fams = defaultdict(lambda: defaultdict(int))     # prodotto → famiglia → n varianti
        tmpl_loose = defaultdict(int)                          # prodotto → varianti senza famiglia
        for vid, tid, ref, name in rows:
            ref = (ref or '').strip()
            if not ref:
                tmpl_loose[tid] += 1
                continue
            fam_variants[ref].append((vid, tid))
            fam_name.setdefault(ref, name or ref)
            tmpl_fams[tid][ref] += 1

        # Ogni prodotto va alla famiglia che ne ha più varianti (a parità, codice più basso).
        owner = {tid: max(sorted(fams), key=lambda r: fams[r]) for tid, fams in tmpl_fams.items()}

        decided = {(p.family_ref, frozenset(p.variant_ids.ids)) for p in self.search(
            [('state', '=', 'discarded')])}
        self.search([('state', '=', 'pending')]).unlink()

        Tmpl = self.env['product.template'].with_context(active_test=False)
        vals = []
        for ref, items in fam_variants.items():
            tids = sorted({tid for _vid, tid in items})
            mixed = [tid for tid in tids if len(tmpl_fams[tid]) > 1]
            if len(tids) == 1 and not mixed:
                continue
            variant_ids = [vid for vid, _tid in items]
            if (ref, frozenset(variant_ids)) in decided:
                continue                        # scartata e niente è cambiato
            kind = 'merge' if not mixed else ('split' if len(tids) == 1 else 'both')
            mine = [tid for tid in tids if owner[tid] == ref]
            target = max(mine, key=lambda t: (tmpl_fams[t][ref], -t)) if mine else False
            templates = Tmpl.browse(tids)
            warnings = self._warnings(ref, templates, target, tmpl_fams, tmpl_loose)
            brand = templates.filtered(lambda t: 'ke_brand_id' in t._fields and t.ke_brand_id)[:1]
            vals.append({
                'name': fam_name[ref], 'family_ref': ref, 'kind': kind,
                'brand_id': brand.ke_brand_id.id if brand else False,
                'variant_ids': [(6, 0, variant_ids)], 'tmpl_ids': [(6, 0, tids)],
                'target_tmpl_id': target, 'variant_count': len(variant_ids), 'tmpl_count': len(tids),
                'summary': self._summary(kind, len(variant_ids), tids, mixed, tmpl_fams, ref),
                'warnings': '\n'.join(warnings) or False, 'certain': not warnings,
            })
        self.create(vals)
        return len(vals)

    @api.model
    def _summary(self, kind, n_variants, tids, mixed, tmpl_fams, ref):
        if kind == 'merge':
            return f'{n_variants} varianti sparse su {len(tids)} prodotti: unirle in uno'
        others = sum(len(tmpl_fams[t]) - 1 for t in mixed)
        if kind == 'split':
            return f'{n_variants} varianti dentro un prodotto con altre {others} famiglie: separarle'
        return (f'{n_variants} varianti su {len(tids)} prodotti, {len(mixed)} dei quali con altre famiglie: '
                f'riunirle in un prodotto solo')

    @api.model
    def _warnings(self, ref, templates, target, tmpl_fams, tmpl_loose):
        out = []
        loose = [t for t in templates if tmpl_loose.get(t.id)]
        if loose:
            out.append('Varianti che non sono nel file del fornitore (restano dove sono): ' + ', '.join(
                f'{tmpl_loose[t.id]} in "{t.name}"' for t in loose[:5]))
        if 'x_amazon_parent_asin' in templates._fields:
            parents = sorted({t.x_amazon_parent_asin for t in templates if t.x_amazon_parent_asin})
            if len(parents) > 1:
                out.append(f'Su Amazon sono {len(parents)} famiglie diverse ({", ".join(parents[:5])}): '
                           'le inserzioni restano come sono, cambia solo Odoo')
        if 'ps_product_id' in templates._fields:
            # Prodotti che si svuoterebbero (tutte le loro varianti sono di questa famiglia) già sul sito.
            gone = [t for t in templates if t.id != target and t.ps_product_id
                    and set(tmpl_fams[t.id]) == {ref} and not tmpl_loose.get(t.id)]
            if gone:
                out.append('Già su PrestaShop, verrebbero archiviati: ' + ', '.join(f'"{t.name}"' for t in gone[:5]))
        if not target:
            out.append('Nessun prodotto attuale è libero: la famiglia diventerebbe un prodotto nuovo')
        return out


class ProductFamilyNamesWizard(models.TransientModel):
    _name = 'ke.product.family.names.wizard'
    _description = 'Nomi dal catalogo fornitore'

    scope = fields.Selection([('selected', 'Prodotti selezionati'), ('all', 'Tutti i prodotti con famiglia del fornitore')],
                             string='Prodotti', default='all', required=True)
    product_count = fields.Integer('Prodotti selezionati', readonly=True)
    rename_product = fields.Boolean('Nome del prodotto', default=True,
                                    help='In ogni lingua: marca + famiglia del fornitore + tipo (come sul sito).')
    rename_variants = fields.Boolean('Nomi delle varianti', default=True,
                                     help='Le specifiche che distinguono le varianti (es. "0,30 mm").')
    dry_run = fields.Boolean('Solo verifica', default=True)
    result = fields.Text('Resoconto', readonly=True)

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        if self.env.context.get('active_model') == 'product.template' and self.env.context.get('active_ids'):
            vals.update(scope='selected', product_count=len(self.env.context['active_ids']))
        return vals

    def action_run(self):
        self.ensure_one()
        Tmpl = self.env['product.template']
        if self.scope == 'selected':
            templates = Tmpl.browse(self.env.context.get('active_ids') or [])
        else:
            self.env.flush_all()
            self.env.cr.execute("""SELECT DISTINCT product_tmpl_id FROM product_product
                                    WHERE active AND COALESCE(ke_supplier_family_ref, '') != ''""")
            templates = Tmpl.browse([r[0] for r in self.env.cr.fetchall()])
        if len(templates) > self._BACKGROUND_FROM and not self.dry_run \
                and not self.env.context.get('ke_family_foreground'):
            return self._run_in_background(templates.ids)
        lines = self._apply_names(templates.ids)
        head =('SOLO VERIFICA — nessuna modifica. ' if self.dry_run else '') + \
            f'{len(lines)} prodotti {"da cambiare" if self.dry_run else "cambiati"} su {len(templates)}.'
        self.result = '\n'.join([head, ''] + lines)
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new'}

    # Oltre questo numero di prodotti i nomi si applicano in background (a blocchi, un salvataggio per blocco).
    _BACKGROUND_FROM = 300
    _CHUNK = 100

    def _apply_names(self, tmpl_ids):
        """A blocchi, svuotando la cache tra un blocco e l'altro (con migliaia di prodotti rallentava tutto)."""
        from .product_family_move import QUIET
        Tmpl = self.env['product.template'].with_context(**QUIET)
        commit = self.env.context.get('ke_family_commit')
        lines = []
        for start in range(0, len(tmpl_ids), self._CHUNK):
            lines += Tmpl.browse(tmpl_ids[start:start + self._CHUNK]).ke_apply_supplier_names(
                self.rename_product, self.rename_variants, self.dry_run)
            self.env.flush_all()
            if commit:
                self.env.cr.commit()
            self.env.invalidate_all()
        return lines

    def _run_in_background(self, tmpl_ids):
        import logging
        import threading
        import odoo
        from odoo.modules.registry import Registry
        dbname, uid, context = self.env.cr.dbname, self.env.uid, dict(self.env.context)
        # La procedura guidata non è ancora salvata quando parte il thread: gli passo le scelte, non l'id.
        flags = {'scope': self.scope, 'rename_product': self.rename_product,
                 'rename_variants': self.rename_variants, 'dry_run': False}

        def run():
            with Registry(dbname).cursor() as cr:
                env = odoo.api.Environment(cr, uid, dict(context, ke_family_foreground=True, ke_family_commit=True))
                try:
                    lines = env[self._name].new(flags)._apply_names(tmpl_ids)
                    logging.getLogger(__name__).info('Nomi dal catalogo fornitore: %s prodotti cambiati su %s',
                                                     len(lines), len(tmpl_ids))
                    cr.commit()
                except Exception:   # noqa: BLE001
                    cr.rollback()
                    logging.getLogger(__name__).exception('Nomi dal catalogo fornitore in background falliti')

        threading.Thread(target=run, daemon=True, name='ke_family_names').start()
        self.result = (f'{len(tmpl_ids)} prodotti in lavorazione in background (pochi minuti). Chiudi pure: '
                       'i nomi compaiono sui prodotti man mano che i blocchi sono salvati.')
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new'}
