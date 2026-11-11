"""
Ricalcolo del prezzo base (prezzo di vendita Odoo) dai prezzi attuali su
Amazon, all'indietro: prezzo dell'annuncio − IVA del paese − ricarico del
listino del marketplace (vedi SyncEngine._base_price_from_amazon).

Deciso con l'utente il 2026-11-09 per la produzione: lì tutti i prodotti
hanno prezzo di vendita 1,00 e il Full Import non cambia il prezzo dei
prodotti già esistenti, quindi accendendo la sync prezzi Amazon riceverebbe
~1,40 €. Di default tocca solo i prodotti con prezzo vuoto o 1,00, così un
prezzo base messo a mano non viene mai sovrascritto.

Sorgente per variante: l'annuncio con prezzo sul marketplace principale,
poi sugli altri in ordine di sequenza; un marketplace in valuta estera
senza tasso di cambio viene saltato (convertirebbe 1:1). Famiglie: il
prezzo più frequente (a parità il più basso) va sulla famiglia, le varianti
con un prezzo diverso ricevono il proprio "Prezzo di vendita variante"
(ke_list_price).
"""
from collections import Counter, defaultdict

# Prezzi di vendita considerati "non impostati": vuoto e il segnaposto 1,00
# della produzione al 2026-11-04.
PLACEHOLDER_PRICES = (0.0, 1.0)


class BasePriceRecalc:

    SAMPLE_ROWS = 30

    def __init__(self, env):
        from .sync_engine import SyncEngine
        self.env = env
        self.engine = SyncEngine(env, None)

    # ------------------------------------------------------------------
    # Marketplace sorgente
    # ------------------------------------------------------------------

    def _marketplaces(self):
        """Marketplace attivi in ordine di preferenza, e quelli saltati (valuta senza tasso)."""
        Marketplace = self.env['ke.amazon.marketplace']
        company = self.env.company
        primary = Marketplace._for_api_id(
            self.env['ir.config_parameter'].sudo().get_param('ke_amazon_sync.marketplace_id'))
        usable, skipped = [], []
        for mp in Marketplace.search([('active', '=', True)]).sorted(lambda m: (m != primary, m.sequence, m.id)):
            currency = mp.currency_id
            if currency and currency != company.currency_id and not currency.sudo().rate_ids:
                skipped.append(mp)
            else:
                usable.append(mp)
        return usable, skipped

    def _listing_prices(self, marketplaces):
        """{variant_id: [(marketplace, prezzo Amazon)] nell'ordine dei marketplace}."""
        order = {mp.id: i for i, mp in enumerate(marketplaces)}
        listings = self.env['ke.amazon.listing'].search([
            ('marketplace_id', 'in', list(order)), ('price', '>', 0), ('product_id', '!=', False),
        ])
        grouped = defaultdict(lambda: defaultdict(list))
        for listing in listings:
            grouped[listing.product_id.id][listing.marketplace_id.id].append(listing)
        result = {}
        for variant_id, by_mp in grouped.items():
            rows = []
            for mp_id in sorted(by_mp, key=order.get):
                candidates = by_mp[mp_id]
                if len(candidates) > 1:
                    main_sku = candidates[0].product_id._amazon_main_sku()
                    candidates = sorted(candidates, key=lambda l: (l.status != 'active', l.sku != main_sku, -l.id))
                rows.append((candidates[0].marketplace_id, candidates[0].price))
            result[variant_id] = rows
        return result

    # ------------------------------------------------------------------
    # Piano e applicazione
    # ------------------------------------------------------------------

    @staticmethod
    def _is_placeholder(price):
        return any(abs((price or 0.0) - p) < 0.005 for p in PLACEHOLDER_PRICES)

    @staticmethod
    def _family_price(prices):
        counts = Counter(prices)
        return min(counts, key=lambda p: (-counts[p], p))

    def plan(self, include_existing=False):
        """
        Calcola senza scrivere. Ritorna un dizionario JSON-serializzabile:
        templates {tmpl_id: prezzo famiglia}, variants {variant_id: prezzo
        variante, 0 = come la famiglia}, righe di esempio e conteggi.
        """
        marketplaces, skipped = self._marketplaces()
        prices_by_variant = self._listing_prices(marketplaces)
        Variant = self.env['product.product']
        variants = Variant.browse(list(prices_by_variant)).filtered('active')
        by_template = defaultdict(list)
        for variant in variants:
            by_template[variant.product_tmpl_id].append(variant)

        plan = {'templates': {}, 'variants': {}, 'rows': [], 'by_marketplace': Counter(),
                'kept_existing': 0, 'variant_own_kept': 0, 'changed': 0, 'variants_total': 0,
                'families_from_variants': 0}
        for template, tmpl_variants in by_template.items():
            if not template.active:
                continue
            if not include_existing and not self._is_placeholder(template.list_price):
                plan['kept_existing'] += 1
                continue
            computed = {}
            for variant in tmpl_variants:
                if variant.ke_list_price and not include_existing:
                    plan['variant_own_kept'] += 1
                    continue
                mp, gross = prices_by_variant[variant.id][0]
                base = self.engine._base_price_from_amazon(gross, variant, mp)
                if base > 0:
                    computed[variant] = (base, mp, gross)
            # Famiglia a 0/1 € con prezzi già sulle varianti (49 su odoo_prod,
            # 2026-11-11): la famiglia prende il prezzo più frequente, le
            # varianti con quel prezzo tornano a seguirla (stesso prezzo).
            own_prices = {} if include_existing else {
                v: round(v.ke_list_price, 2) for v in template.product_variant_ids if v.ke_list_price}
            if not computed and not own_prices:
                continue
            family = self._family_price([v[0] for v in computed.values()] + list(own_prices.values()))
            plan['templates'][template.id] = family
            for variant, price in own_prices.items():
                if abs(price - family) <= 0.005:
                    plan['variants'][variant.id] = 0.0
            if not computed:
                plan['families_from_variants'] += 1
            for variant, (base, mp, gross) in computed.items():
                own = base if abs(base - family) > 0.005 and len(template.product_variant_ids) > 1 else 0.0
                if own or variant.ke_list_price:
                    plan['variants'][variant.id] = own
                plan['by_marketplace'][mp.code] += 1
                plan['variants_total'] += 1
                if abs(base - variant.lst_price) <= 0.005:
                    continue
                plan['changed'] += 1
                if len(plan['rows']) >= self.SAMPLE_ROWS:
                    continue
                plan['rows'].append({
                    'name': variant.display_name, 'marketplace': mp.code, 'amazon': gross,
                    'currency': (mp.currency_id or self.env.company.currency_id).name,
                    'old': variant.lst_price, 'new': base, 'variant_price': bool(own),
                })
        plan['by_marketplace'] = dict(plan['by_marketplace'])
        plan['skipped_marketplaces'] = [mp.code for mp in skipped]
        plan['marketplaces'] = [self._describe_marketplace(mp) for mp in marketplaces]
        domain = [] if include_existing else ['|', ('list_price', '=', 0), ('list_price', '=', 1.0)]
        plan['without_amazon_price'] = self.env['product.template'].search_count(
            domain + [('id', 'not in', list(plan['templates']))])
        return plan

    def _describe_marketplace(self, mp):
        from odoo.addons.ke_amazon_sync.models.product_template import _amazon_pricelist_items_by_priority
        pricelist = mp.pricelist_id or self.env.ref('ke_amazon_sync.pricelist_amazon', raise_if_not_found=False)
        markup = ''
        if pricelist:
            rule = next((i for i in _amazon_pricelist_items_by_priority(pricelist, self.env['product.product'])
                         if i.compute_price in ('formula', 'percentage')), None)
            if rule:
                discount = rule.price_discount if rule.compute_price == 'formula' else rule.percent_price
                markup = f' ({-discount:+g}%)'
        vat = f'IVA {mp.vat_rate:g}%' if mp.vat_rate else 'IVA del prodotto'
        return f'{mp.code}: {vat}, listino "{pricelist.name if pricelist else "nessuno"}"{markup}'

    def apply(self, plan):
        """Scrive il piano; prezzi uguali in un'unica write. Ritorna (famiglie, varianti)."""
        Template = self.env['product.template']
        Variant = self.env['product.product']
        by_price = defaultdict(list)
        for tmpl_id, price in plan['templates'].items():
            by_price[price].append(int(tmpl_id))
        for price, ids in by_price.items():
            Template.browse(ids).write({'list_price': price})
        by_price = defaultdict(list)
        for variant_id, price in plan['variants'].items():
            by_price[price].append(int(variant_id))
        for price, ids in by_price.items():
            Variant.browse(ids).write({'ke_list_price': price})
        return len(plan['templates']), sum(1 for p in plan['variants'].values() if p)
