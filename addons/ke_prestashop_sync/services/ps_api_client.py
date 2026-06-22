"""
Client per la WebService API di PrestaShop.

Autenticazione: HTTP Basic Auth con API key come username, password vuota.
Letture: output_format=JSON
Scritture: body XML (GET-then-PUT per aggiornamenti parziali).
"""

import logging
import re
import unicodedata
import xml.etree.ElementTree as ET

import requests

_log = logging.getLogger(__name__)

_PS_ORDER_STATES = {
    1: 'Awaiting check payment',
    2: 'Payment accepted',
    3: 'Processing in progress',
    4: 'Shipped',
    5: 'Delivered',
    6: 'Cancelled',
    7: 'Refunded',
}


class PsApiClient:
    def __init__(self, env):
        params = env['ir.config_parameter'].sudo()
        self._url = (params.get_param('ke_prestashop_sync.url') or '').rstrip('/')
        self._key = params.get_param('ke_prestashop_sync.api_key') or ''
        self._lang_id = int(params.get_param('ke_prestashop_sync.lang_id') or 1)

    # ------------------------------------------------------------------
    # Primitivi HTTP
    # ------------------------------------------------------------------

    def _ps_key_param(self):
        # L'API key PS viene passata come ws_key query param (più affidabile
        # della Basic Auth con proxy/web server che filtrano Authorization).
        return {'ws_key': self._key}

    def _get_json(self, resource, params=None):
        url = f'{self._url}/api/{resource}'
        p = {'output_format': 'JSON', 'display': 'full'}
        p.update(self._ps_key_param())
        if params:
            p.update(params)
        resp = requests.get(url, params=p, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _get_xml_str(self, resource, resource_id):
        """Recupera il body XML grezzo di una risorsa (per round-trip PUT)."""
        url = f'{self._url}/api/{resource}/{resource_id}'
        resp = requests.get(url,
                            params=self._ps_key_param() or None, timeout=30)
        resp.raise_for_status()
        return resp.text

    def _put_xml(self, resource, resource_id, xml_bytes):
        url = f'{self._url}/api/{resource}/{resource_id}'
        resp = requests.put(
            url,
            data=xml_bytes,
            headers={'Content-Type': 'text/xml; charset=UTF-8'},
            params=self._ps_key_param(),
            timeout=30,
        )
        resp.raise_for_status()

    def _post_xml(self, resource, xml_bytes):
        """Crea una nuova risorsa e restituisce l'ID creato."""
        url = f'{self._url}/api/{resource}'
        p = {'output_format': 'JSON'}
        p.update(self._ps_key_param())
        resp = requests.post(
            url,
            data=xml_bytes,
            headers={'Content-Type': 'text/xml; charset=UTF-8'},
            params=p,
            timeout=30,
        )
        if not resp.ok:
            _log.error('PS POST %s → HTTP %s: %s', resource, resp.status_code, resp.text[:1000])
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            _log.error('PS POST %s: risposta JSON inattesa (tipo %s): %s',
                       resource, type(data).__name__, str(data)[:500])
            return 0
        singular = resource.rstrip('s')
        item = data.get(singular) or data.get(resource)
        if isinstance(item, list):
            item = item[0] if item else {}
        if not isinstance(item, dict):
            item = {}
        return int(item.get('id', 0))

    # ------------------------------------------------------------------
    # Validazione
    # ------------------------------------------------------------------

    def validate_credentials(self):
        if not self._url or not self._key:
            raise ValueError('URL e API Key PrestaShop sono obbligatori.')
        # Usa XML (senza output_format=JSON): il root endpoint PS non supporta JSON
        p = self._ps_key_param()
        resp = requests.get(f'{self._url}/api/', params=p, timeout=30)
        resp.raise_for_status()
        return True

    # ------------------------------------------------------------------
    # Prodotti
    # ------------------------------------------------------------------

    def get_product_ids(self):
        """Lista tutti gli ID prodotto disponibili."""
        data = self._get_json('products', {'display': '[id]'})
        products = data.get('products') or []
        return [int(p['id']) for p in products]

    def get_product(self, ps_id):
        """Dettaglio prodotto come dict JSON."""
        data = self._get_json(f'products/{ps_id}')
        return data.get('product', {})

    def get_product_name(self, product_dict):
        """Estrae il nome nella lingua configurata (fallback: prima disponibile)."""
        names = product_dict.get('name', [])
        if isinstance(names, str):
            return names
        for entry in names:
            if int(entry.get('id', 0)) == self._lang_id:
                return entry.get('value', '')
        return names[0].get('value', '') if names else ''

    def get_product_description(self, product_dict, field='description_short'):
        """Estrae descrizione nella lingua configurata."""
        entries = product_dict.get(field, [])
        if isinstance(entries, str):
            return entries
        for entry in entries:
            if int(entry.get('id', 0)) == self._lang_id:
                return entry.get('value', '')
        return entries[0].get('value', '') if entries else ''

    def upload_product_image(self, ps_id, image_bytes):
        """Carica l'immagine principale di un prodotto su PS via multipart POST."""
        url = f'{self._url}/api/images/products/{ps_id}'
        mime, filename = self._detect_image_mime(image_bytes)
        resp = requests.post(
            url,
            files={'image': (filename, image_bytes, mime)},
            params={**self._ps_key_param(), 'output_format': 'JSON'},
            timeout=60,
        )
        if not resp.ok:
            _log.error('PS POST images/products/%s → HTTP %s: %s',
                       ps_id, resp.status_code, resp.text[:500])
        resp.raise_for_status()

    @staticmethod
    def _detect_image_mime(data):
        if len(data) >= 4 and data[:4] == b'\x89PNG':
            return 'image/png', 'product.png'
        if len(data) >= 2 and data[:2] == b'\xff\xd8':
            return 'image/jpeg', 'product.jpg'
        if len(data) >= 4 and data[:3] == b'GIF':
            return 'image/gif', 'product.gif'
        return 'image/jpeg', 'product.jpg'

    def update_product_price(self, ps_id, new_price):
        """Aggiorna il prezzo di un prodotto su PS (GET → modifica → PUT)."""
        xml_str = self._get_xml_str('products', ps_id)
        root = ET.fromstring(xml_str)
        price_el = root.find('.//product/price')
        if price_el is not None:
            price_el.text = f'{float(new_price):.6f}'
        xml_bytes = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        self._put_xml('products', ps_id, xml_bytes)

    @staticmethod
    def _make_slug(name):
        """Genera un link_rewrite ASCII valido per PrestaShop (obbligatorio alla creazione)."""
        # Normalizza Unicode → ASCII (è→e, à→a, ecc.) — PS richiede [a-zA-Z0-9_-]
        slug = unicodedata.normalize('NFKD', (name or '').lower())
        slug = slug.encode('ascii', 'ignore').decode('ascii')
        slug = re.sub(r'[^\w\s-]', '', slug)
        slug = re.sub(r'[\s_]+', '-', slug)
        slug = slug.strip('-')
        return (slug or 'product')[:128]

    def find_product_by_reference(self, reference):
        """Cerca un prodotto PS per codice riferimento; restituisce l'ID o None."""
        data = self._get_json('products', {
            'display': '[id]',
            'filter[reference]': f'[{reference}]',
        })
        if not isinstance(data, dict):
            _log.warning('find_product_by_reference: risposta PS inattesa (tipo %s): %s',
                         type(data).__name__, str(data)[:200])
            return None
        products = data.get('products') or []
        if not isinstance(products, list):
            return None
        return int(products[0]['id']) if products else None

    def create_product(self, name, reference='', ean13='', price=0.0,
                       description_short='', active=1):
        """Crea un prodotto su PS e restituisce il nuovo ID."""
        # PS accetta solo EAN-13 (13 cifre esatte); UPC a 12 cifre o codici non numerici → stringa vuota
        ean13 = ean13 if (ean13 and len(str(ean13)) == 13 and str(ean13).isdigit()) else ''
        # PS isCatalogName vieta ; < > = { } e in questa installazione anche #
        name = re.sub(r'[<>={}#]', '', (name or '')).replace(';', ',').strip()[:128]

        # description_short: tronca a 450 char (limite PS osservato: min ~596, con margine)
        description_short = (description_short or '')[:450]
        # name: XML entities (NO CDATA) — così il parser XML decodifica &amp;→& prima che
        # PS validi con isCatalogName; CDATA è testo letterale, &amp; conterrebbe ';' vietato
        name_xml = name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        # description_short: CDATA senza pre-escaping HTML (solo ]]> va protetto)
        desc_cdata = description_short.replace(']]>', ']]&gt;')
        # reference: CDATA senza pre-escaping
        ref_cdata = (reference or '').replace(']]>', ']]&gt;')
        slug = self._make_slug(name)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
  <product>
    <active>{active}</active>
    <state>1</state>
    <condition><![CDATA[new]]></condition>
    <visibility><![CDATA[both]]></visibility>
    <available_for_order>1</available_for_order>
    <show_price>1</show_price>
    <price>{float(price):.6f}</price>
    <id_tax_rules_group>1</id_tax_rules_group>
    <reference><![CDATA[{ref_cdata}]]></reference>
    <ean13>{ean13}</ean13>
    <name>
      <language id="{self._lang_id}">{name_xml}</language>
    </name>
    <link_rewrite>
      <language id="{self._lang_id}">{slug}</language>
    </link_rewrite>
    <description_short>
      <language id="{self._lang_id}"><![CDATA[{desc_cdata}]]></language>
    </description_short>
    <id_category_default>2</id_category_default>
    <associations>
      <categories>
        <category><id>2</id></category>
      </categories>
    </associations>
  </product>
</prestashop>""".encode('utf-8')
        return self._post_xml('products', xml)

    # ------------------------------------------------------------------
    # Opzioni / Combinazioni (prodotti configurabili)
    # ------------------------------------------------------------------

    def get_or_create_product_option(self, attribute_name):
        """Trova o crea un product_option (gruppo attributo, es. 'Taglia') in PS."""
        data = self._get_json('product_options')
        options = data.get('product_options') or [] if isinstance(data, dict) else []
        if not isinstance(options, list):
            options = [options]
        for opt in options:
            if not isinstance(opt, dict):
                continue
            for field in ('public_name', 'name'):
                names = opt.get(field, [])
                if isinstance(names, str):
                    if names.lower() == attribute_name.lower():
                        return int(opt['id'])
                elif isinstance(names, list):
                    for n in names:
                        if (int(n.get('id', 0)) == self._lang_id
                                and n.get('value', '').lower() == attribute_name.lower()):
                            return int(opt['id'])
        name_cdata = attribute_name.replace(']]>', ']]&gt;')
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
  <product_option>
    <is_color_group>0</is_color_group>
    <group_type>select</group_type>
    <name><language id="{self._lang_id}"><![CDATA[{name_cdata}]]></language></name>
    <public_name><language id="{self._lang_id}"><![CDATA[{name_cdata}]]></language></public_name>
  </product_option>
</prestashop>""".encode('utf-8')
        return self._post_xml('product_options', xml)

    def get_or_create_option_value(self, option_id, value_name):
        """Trova o crea un product_option_value (es. 'M') dentro un gruppo PS."""
        data = self._get_json('product_option_values', {
            'filter[id_attribute_group]': f'[{option_id}]',
        })
        values = data.get('product_option_values') or [] if isinstance(data, dict) else []
        if not isinstance(values, list):
            values = [values]
        for val in values:
            if not isinstance(val, dict):
                continue
            names = val.get('name', [])
            if isinstance(names, str):
                if names.lower() == value_name.lower():
                    return int(val['id'])
            elif isinstance(names, list):
                for n in names:
                    if (int(n.get('id', 0)) == self._lang_id
                            and n.get('value', '').lower() == value_name.lower()):
                        return int(val['id'])
        val_cdata = value_name.replace(']]>', ']]&gt;')
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
  <product_option_value>
    <id_attribute_group>{option_id}</id_attribute_group>
    <name><language id="{self._lang_id}"><![CDATA[{val_cdata}]]></language></name>
  </product_option_value>
</prestashop>""".encode('utf-8')
        return self._post_xml('product_option_values', xml)

    def create_combination(self, ps_product_id, option_value_ids,
                           reference='', ean13='', price_impact=0.0):
        """Crea una combinazione PS e restituisce il suo ID."""
        ean13 = ean13 if (ean13 and len(str(ean13)) == 13 and str(ean13).isdigit()) else ''
        ref_cdata = (reference or '').replace(']]>', ']]&gt;')
        ov_xml = ''.join(
            f'<product_option_value><id>{ov}</id></product_option_value>'
            for ov in option_value_ids
        )
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
  <combination>
    <id_product>{ps_product_id}</id_product>
    <reference><![CDATA[{ref_cdata}]]></reference>
    <ean13>{ean13}</ean13>
    <price>{float(price_impact):.6f}</price>
    <associations>
      <product_option_values>{ov_xml}</product_option_values>
    </associations>
  </combination>
</prestashop>""".encode('utf-8')
        return self._post_xml('combinations', xml)

    # ------------------------------------------------------------------
    # Stock (stock_availables)
    # ------------------------------------------------------------------

    def get_stock_availables(self, ps_product_id):
        """Restituisce lista di dict stock_available per un prodotto PS."""
        data = self._get_json('stock_availables', {
            'filter[id_product]': f'[{ps_product_id}]',
        })
        return data.get('stock_availables') or []

    def update_stock_available(self, stock_id, ps_product_id,
                                ps_attribute_id, quantity, id_shop=1):
        """Aggiorna la quantità disponibile su PS."""
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
  <stock_available>
    <id>{stock_id}</id>
    <id_product>{ps_product_id}</id_product>
    <id_product_attribute>{ps_attribute_id}</id_product_attribute>
    <id_shop>{id_shop}</id_shop>
    <id_shop_group>0</id_shop_group>
    <quantity>{int(quantity)}</quantity>
    <depends_on_stock>0</depends_on_stock>
    <out_of_stock>2</out_of_stock>
  </stock_available>
</prestashop>""".encode('utf-8')
        self._put_xml('stock_availables', stock_id, xml)

    # ------------------------------------------------------------------
    # Ordini
    # ------------------------------------------------------------------

    def get_order_ids(self, date_from=None, states=None):
        """
        Lista ID ordini. date_from: stringa 'YYYY-MM-DD HH:MM:SS'.
        states: lista di int (ID stato PS).
        """
        params = {'display': '[id]', 'sort': '[id_ASC]'}
        if date_from:
            params['filter[date_add]'] = f'>[{date_from}]'
        if states:
            state_str = '|'.join(str(s) for s in states)
            params['filter[current_state]'] = f'[{state_str}]'
        data = self._get_json('orders', params)
        orders = data.get('orders') or []
        return [int(o['id']) for o in orders]

    def get_order(self, order_id):
        """Dettaglio ordine."""
        data = self._get_json(f'orders/{order_id}')
        return data.get('order', {})

    def get_customer(self, customer_id):
        """Dati cliente PS."""
        data = self._get_json(f'customers/{customer_id}')
        return data.get('customer', {})

    def get_address(self, address_id):
        """Indirizzo PS."""
        data = self._get_json(f'addresses/{address_id}')
        return data.get('address', {})

    def get_country(self, country_id):
        """Paese PS (per mappare il country_id Odoo)."""
        data = self._get_json(f'countries/{country_id}')
        return data.get('country', {})

    @staticmethod
    def order_state_label(state_id):
        return _PS_ORDER_STATES.get(int(state_id), f'Stato {state_id}')
