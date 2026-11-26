"""
Classificazione dei prodotti in Odoo (2026-11-17, chiesto dall'utente al
posto del file da rivedere in Excel).

- Regole (ke.product.category.rule), modificabili: categoria del fornitore,
  tecnica (specifiche), categoria Amazon o parole nel nome → categoria Odoo;
  "Si applica a" limita la regola ai prodotti di certe categorie (es. la
  tecnica Surfcasting porta in Mulinelli / Surfcasting solo i mulinelli).
- Proposte (ke.product.category.proposal): per i prodotti scelti, la
  categoria proposta (modificabile), il motivo e se è sicura o da
  verificare; "Applica" cambia solo la categoria Odoo del prodotto.

Scelta della categoria: le regole che si applicano, in ordine; vince la
prima non debole (una regola debole cede a qualsiasi altra che trova una
categoria diversa). Due regole non deboli in disaccordo → da verificare.
Se la categoria trovata ha regole sue ("si applica a"), si provano subito
(es. amo → Ami → Ami / Mare). Nessuna proposta verso una categoria più
generica di quella attuale (un amo già in "Ami / Mare" resta lì).
"""
import re

from odoo import api, fields, models
from odoo.exceptions import UserError

KINDS = [
    ('supplier', 'Categoria del fornitore'),
    ('technique', 'Tecnica (specifiche)'),
    ('amazon', 'Categoria Amazon'),
    ('name', 'Parole nel nome'),
]


class ProductCategoryRule(models.Model):
    _name = 'ke.product.category.rule'
    _description = 'Regola di classificazione prodotti'
    _order = 'sequence, id'

    sequence = fields.Integer('Priorità', default=10, help='Le regole si provano in quest\'ordine.')
    active = fields.Boolean(default=True)
    kind = fields.Selection(KINDS, string='Guarda', required=True, default='name')
    pattern = fields.Char(
        'Testo da trovare', required=True,
        help='Espressione regolare, senza distinzione di maiuscole. Più alternative con | '
             '(es. "surf|beach"). Per la categoria del fornitore si confronta "Categoria / Sottocategoria".')
    exclude_pattern = fields.Char(
        'Tranne se il nome contiene',
        help='Espressione regolare sul nome: se la trova, la regola non vale '
             '(es. una regola "filo" che non deve toccare i mulinelli con il filo incluso).')
    source_categ_ids = fields.Many2many(
        'product.category', 'ke_category_rule_source_rel', 'rule_id', 'categ_id', string='Si applica a',
        help='Solo ai prodotti che sono in queste categorie (o in loro sottocategorie). Vuoto: a tutti.')
    target_categ_id = fields.Many2one('product.category', 'Categoria proposta', required=True, ondelete='cascade')
    weak = fields.Boolean(
        'Debole', help='Cede a qualsiasi altra regola che trova una categoria diversa '
                       '(es. la tecnica "Spinning" di Amazon, data anche ai mulinelli da surf).')
    note = fields.Char('Nota')

    @api.constrains('pattern', 'exclude_pattern')
    def _check_patterns(self):
        for rule in self:
            for value in (rule.pattern, rule.exclude_pattern):
                if value:
                    try:
                        re.compile(value)
                    except re.error as exc:
                        raise UserError(f'Espressione non valida "{value}": {exc}')

    def _applies_to(self, tmpl, categ=None):
        """categ: la categoria da considerare al posto di quella del prodotto (seconda passata)."""
        if not self.source_categ_ids:
            return True
        path = (categ or tmpl.categ_id).parent_path or ''
        return any(path.startswith(c.parent_path) for c in self.source_categ_ids if c.parent_path)

    # ------------------------------------------------------------------
    # Regole predefinite (quelle messe a punto il 2026-11-17)
    # ------------------------------------------------------------------

    @api.model
    def _category_by_path(self, path, create=False):
        Categ = self.env['product.category']
        parent = Categ.browse()
        for name in [p.strip() for p in path.split('/') if p.strip()]:
            child = Categ.search([('parent_id', '=', parent.id or False)]).filtered(
                lambda c: (c.name or '').strip().lower() == name.lower())[:1]
            if not child:
                if not create:
                    return Categ.browse()
                child = Categ.create({'name': name, 'parent_id': parent.id or False})
            parent = child
        return parent

    def action_load_default_rules(self):
        # Niente @api.model: il bottone nell'intestazione della lista passa gli id selezionati
        # (anche nessuno) e con @api.model la chiamata fallisce ("takes 1 positional argument").
        self = self.browse()
        """Crea le regole predefinite che mancano (e le sottocategorie che servono); solo se c'è "Pesca"."""
        from . import category_rules_default as defaults
        if not self._category_by_path('Pesca'):
            raise UserError('Le regole predefinite sono per l\'albero "Pesca / …": qui non c\'è.')
        renamed, merged, moved = self._apply_tree_changes(defaults.TREE_CHANGES)
        retired = self._retire_rules(defaults.RETIRED, defaults.rules())
        created = 0
        for seq, kind, pattern, sources, target, weak, exclude in defaults.rules():
            target_categ = self._category_by_path(target, create=True)
            source_categs = self.env['product.category'].browse(
                [c.id for c in (self._category_by_path(s) for s in sources) if c])
            if sources and not source_categs:
                continue
            exists = self.with_context(active_test=False).search([
                ('kind', '=', kind), ('pattern', '=', pattern), ('target_categ_id', '=', target_categ.id),
                ('weak', '=', bool(weak)), ('exclude_pattern', '=', exclude or False), ('sequence', '=', seq)],
                limit=1)
            if exists:
                # "Si applica a" comparse dopo la prima creazione (es. "Nautica" creata in seguito).
                missing = source_categs - exists.source_categ_ids
                if missing:
                    exists.source_categ_ids = [(4, c.id) for c in missing]
                continue
            self.create({'sequence': seq, 'kind': kind, 'pattern': pattern, 'exclude_pattern': exclude or False,
                         'source_categ_ids': [(6, 0, source_categs.ids)], 'target_categ_id': target_categ.id,
                         'weak': weak})
            created += 1
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': 'Regole di classificazione', 'type': 'success',
                           'message': self._load_message(created, retired, renamed, merged, moved),
                           'next': {'type': 'ir.actions.act_window_close'}}}

    @api.model
    def _load_message(self, created, retired, renamed, merged, moved):
        parts = [f'{created} regole create']
        if retired:
            parts.append(f'{retired} regole di prima disattivate')
        if renamed:
            parts.append(f'{renamed} categorie rinominate')
        if merged:
            parts.append(f'{merged} categorie unite ({moved} prodotti spostati)')
        return '; '.join(parts) + '.'

    @api.model
    def _apply_tree_changes(self, changes):
        """Albero delle categorie (2026-11-26, come Nencini Sport): vecchia → nuova. Se la nuova non c'è
        si rinomina o si sposta la vecchia (tiene prodotti, regole, corrispondenze); altrimenti si unisce: ogni campo
        che punta alla vecchia (prodotti, regole, corrispondenze Amazon, sottocategorie…) passa alla nuova
        e la vecchia si cancella. Ritorna (rinominate, unite, prodotti spostati)."""
        renamed = merged = moved = 0
        for old_path, new_path in changes:
            old = self._category_by_path(old_path)
            if not old:
                continue
            new = self._category_by_path(new_path)
            if not new:
                # Rinomina o sposta (es. Pesca / Minuteria / Ami → Pesca / Ami): stesso record, con le
                # sue sottocategorie, i prodotti e ogni riferimento.
                parent = self._category_by_path(new_path.rsplit('/', 1)[0], create=True)
                old.write({'name': new_path.rsplit('/', 1)[1].strip(), 'parent_id': parent.id})
                renamed += 1
                continue
            new = new or self._category_by_path(new_path, create=True)
            moved += self.env['product.template'].with_context(active_test=False).search_count(
                [('categ_id', '=', old.id)])
            self._repoint_category(old, new)
            old.unlink()
            merged += 1
        return renamed, merged, moved

    @api.model
    def _repoint_category(self, old, new):
        """Ogni riferimento a old (many2one e many2many salvati, di qualsiasi modulo) passa a new."""
        fields_ = self.env['ir.model.fields'].sudo().search([
            ('relation', '=', 'product.category'), ('ttype', 'in', ('many2one', 'many2many')), ('store', '=', True)])
        for f in fields_:
            if f.model not in self.env or f.name not in self.env[f.model]._fields:
                continue
            Model = self.env[f.model].sudo().with_context(active_test=False)
            if Model._abstract or not Model._auto:
                continue
            records = Model.search([(f.name, 'in', old.ids)])
            if not records:
                continue
            if f.ttype == 'many2one':
                records.write({f.name: new.id})
            else:
                records.write({f.name: [(3, old.id), (4, new.id)]})

    @api.model
    def _retire_rules(self, retired, current):
        """Disattiva le regole predefinite sostituite: (tipo, testo, categoria sotto cui stanno). Resta
        attiva quella uguale a una regola di adesso (stessa priorità, tipo, testo, categoria e "debole")."""
        keep = {(seq, kind, pattern, target, bool(weak)) for seq, kind, pattern, _src, target, weak, _e in current}
        count = 0
        for kind, pattern, root_path in retired:
            root = self._category_by_path(root_path)
            if not root:
                continue
            rules = self.search([('kind', '=', kind), ('pattern', '=', pattern),
                                 ('target_categ_id', 'child_of', root.id)]).filtered(
                lambda r: (r.sequence, r.kind, r.pattern, r.target_categ_id.complete_name, r.weak) not in keep)
            rules.write({'active': False})
            count += len(rules)
        return count


class ProductCategoryProposal(models.Model):
    _name = 'ke.product.category.proposal'
    _description = 'Proposta di categoria'
    _order = 'certain, current_categ_id, product_tmpl_id'
    _rec_name = 'product_tmpl_id'

    product_tmpl_id = fields.Many2one('product.template', 'Prodotto', required=True, ondelete='cascade', index=True)
    current_categ_id = fields.Many2one('product.category', 'Categoria attuale', readonly=True)
    proposed_categ_id = fields.Many2one('product.category', 'Categoria proposta', required=True)
    rule_id = fields.Many2one('ke.product.category.rule', 'Regola', ondelete='set null', readonly=True)
    reason = fields.Char('Motivo', readonly=True)
    certain = fields.Boolean('Sicura', readonly=True)
    on_site = fields.Boolean('Sul sito', compute='_compute_on_site', store=True)
    amazon_clash = fields.Boolean(
        'Nome come categoria Amazon', readonly=True,
        help='La categoria proposta si chiama come una categoria Amazon e non è usata dalle corrispondenze: '
             'la sync Amazon potrebbe spostare di nuovo il prodotto.')
    state = fields.Selection([('pending', 'Da valutare'), ('applied', 'Applicata'), ('discarded', 'Scartata')],
                             string='Stato', default='pending', required=True, index=True)

    @api.depends('product_tmpl_id')
    def _compute_on_site(self):
        for rec in self:
            tmpl = rec.product_tmpl_id
            rec.on_site = bool('ps_product_id' in tmpl._fields and tmpl.ps_product_id)

    def action_apply(self):
        for rec in self.filtered(lambda p: p.state == 'pending'):
            rec.product_tmpl_id.categ_id = rec.proposed_categ_id
            rec.state = 'applied'
        return True

    def action_discard(self):
        self.filtered(lambda p: p.state == 'pending').write({'state': 'discarded'})
        return True

    # ------------------------------------------------------------------
    # Generazione
    # ------------------------------------------------------------------

    @api.model
    def _texts(self, tmpl):
        names = ' | '.join({tmpl.with_context(lang=l).name or '' for l in ('it_IT', 'en_US')})
        technique = ''
        if 'ke_spec_ids' in tmpl._fields:
            technique = ', '.join(tmpl.with_context(lang='it_IT').ke_spec_ids.filtered(
                lambda s: s.key_id.code == 'technique').mapped('value'))
        return {
            'name': names,
            'supplier': tmpl.ke_supplier_category or '' if 'ke_supplier_category' in tmpl._fields else '',
            'technique': technique,
            'amazon': tmpl.x_amazon_category or '' if 'x_amazon_category' in tmpl._fields else '',
        }

    @api.model
    def _decide(self, tmpl, rules, categ=None):
        """(categoria, regola, motivo, sicura) o None. categ: come se il prodotto fosse in quella categoria."""
        texts = self._texts(tmpl)
        hits = []
        for rule in rules:
            if not rule._applies_to(tmpl, categ):
                continue
            m = re.search(rule.pattern, texts[rule.kind] or '', re.I)
            if not m:
                continue
            if rule.exclude_pattern and re.search(rule.exclude_pattern, texts['name'], re.I):
                continue
            hits.append((rule, m.group(0)))
        if not hits:
            return None
        # Una fonte che indica 3 o più categorie diverse (es. Amazon: "Spinning, Surfcasting, Feeder,
        # Bolognese, Trota" per un mulinello tuttofare) non dice niente: le sue regole valgono deboli.
        by_kind = {}
        for rule, _found in hits:
            by_kind.setdefault(rule.kind, set()).add(rule.target_categ_id)
        noisy = {k for k, targets in by_kind.items() if len(targets) >= 3}
        weak = lambda h: h[0].weak or h[0].kind in noisy
        strong = [h for h in hits if not weak(h)]
        cands = strong or hits
        under = lambda child, parent: child != parent and (child.parent_path or '').startswith(parent.parent_path or '~')
        # Contano solo le regole della priorità migliore; "Fili" e "Fili / Trecciati" non sono in
        # disaccordo (vale la più specifica).
        best = min(h[0].sequence for h in cands)
        top = [h for h in cands if h[0].sequence == best]
        targets = {h[0].target_categ_id for h in top}
        leaves = {t for t in targets if not any(under(o, t) for o in targets)}
        rule, found = next(h for h in top if h[0].target_categ_id in leaves)
        label = dict(KINDS)[rule.kind].lower()
        reason = f'{label}: "{found}"'
        certain = len(leaves) == 1
        differ = lambda h: h[0].target_categ_id != rule.target_categ_id and not under(rule.target_categ_id,
                                                                                      h[0].target_categ_id)
        # Nel motivo prima il disaccordo che conta (stessa priorità), poi gli altri.
        # Una regola debole che cede a una non debole non si cita (es. il ripiego "Accessori vari").
        chosen_weak = weak((rule, found))
        others = sorted((h for h in hits if differ(h) and (chosen_weak or not weak(h))),
                        key=lambda h: (h not in top, weak(h), h[0].sequence))
        if others:
            o_rule, o_found = others[0]
            reason += f' (ma {dict(KINDS)[o_rule.kind].lower()} "{o_found}" → {o_rule.target_categ_id.name})'
        return rule.target_categ_id, rule, reason, certain

    @api.model
    def _amazon_keys(self):
        if 'ke.amazon.category.map' not in self.env:
            return set(), set()
        maps = self.env['ke.amazon.category.map'].sudo().search([])
        return set(maps.mapped('key')), set(maps.mapped('categ_id').ids)

    @staticmethod
    def _under(child, parent):
        """child è una sottocategoria (anche indiretta) di parent."""
        return bool(child and parent) and child != parent and (child.parent_path or '').startswith(
            parent.parent_path or '~')

    @api.model
    def _refine(self, tmpl, rules, decision):
        """Seconda passata (2026-11-24): se il prodotto va in una categoria che ha regole sue ("si applica
        a" Ami), le prova come se ci fosse già; vale se non è più generica della prima. Così un amo
        appena importato va subito in "Ami / Mare" e un Penn Surfblaster ("Spinning Reels" per il
        fornitore) in "Mulinelli / Surfcasting" invece di "Frizione anteriore"."""
        if not decision or decision[0] == tmpl.categ_id:
            return decision
        second = self._decide(tmpl, rules, categ=decision[0])
        if second and second[0] != decision[0] and not self._under(decision[0], second[0]):
            return second
        return decision

    @api.model
    def propose_for(self, templates):
        """Proposte per i prodotti dati (sostituisce quelle ancora da valutare). Ritorna il numero creato."""
        rules = self.env['ke.product.category.rule'].search([])
        if not rules:
            raise UserError('Nessuna regola di classificazione: creane qualcuna o carica quelle predefinite.')
        self.search([('product_tmpl_id', 'in', templates.ids), ('state', '=', 'pending')]).unlink()
        keys, mapped = self._amazon_keys()
        key_of = lambda name: re.sub(r'[^a-z0-9]', '', (name or '').lower())
        vals = []
        for tmpl in templates:
            decision = self._refine(tmpl, rules, self._decide(tmpl, rules))
            if not decision or decision[0] == tmpl.categ_id or self._under(tmpl.categ_id, decision[0]):
                continue
            categ, rule, reason, certain = decision
            # Dal 2026-11-26 la sync Amazon non tocca le destinazioni delle regole (né i loro rami): una
            # categoria proposta da una regola non rischia più di essere svuotata anche se si chiama come
            # una categoria Amazon (es. "Pesca / Abbigliamento / Guanti").
            protected = categ in rules.mapped('target_categ_id')
            clash = (bool(keys) and not protected and categ.id not in mapped
                     and any(key_of(categ.name) == key_of(k) for k in keys))
            vals.append({'product_tmpl_id': tmpl.id, 'current_categ_id': tmpl.categ_id.id,
                         'proposed_categ_id': categ.id, 'rule_id': rule.id, 'reason': reason[:250],
                         'certain': certain and not clash, 'amazon_clash': clash})
        self.create(vals)
        return len(vals)


class ProductCategoryProposeWizard(models.TransientModel):
    _name = 'ke.product.category.propose.wizard'
    _description = 'Proponi categorie'

    categ_ids = fields.Many2many('product.category', string='Prodotti delle categorie',
                                 help='Con le loro sottocategorie. Vuoto: i prodotti selezionati.')
    product_count = fields.Integer('Prodotti selezionati', readonly=True)

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        if self.env.context.get('active_model') == 'product.template':
            vals['product_count'] = len(self.env.context.get('active_ids') or [])
        return vals

    def action_propose(self):
        Tmpl = self.env['product.template']
        if self.categ_ids:
            templates = Tmpl.search([('categ_id', 'child_of', self.categ_ids.ids)])
        else:
            templates = Tmpl.browse(self.env.context.get('active_ids') or [])
        if not templates:
            raise UserError('Scegli delle categorie o seleziona dei prodotti.')
        created = self.env['ke.product.category.proposal'].propose_for(templates)
        action = self.env['ir.actions.act_window']._for_xml_id('ke_product_catalog.action_ke_category_proposal')
        action['context'] = {'search_default_pending': 1}
        action['name'] = f'Proposte di categoria ({created} nuove su {len(templates)} prodotti)'
        return action
