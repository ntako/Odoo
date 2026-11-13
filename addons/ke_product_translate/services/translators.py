"""
Traduttori semplici (fase 1, 2026-11-13): LibreTranslate e DeepL via HTTP.

Interfaccia comune: translate(testi, lingua di partenza, lingua di arrivo,
html) → testi tradotti nello stesso ordine. Le lingue sono codici Odoo
(it_IT, fr_FR, en_GB…); ogni traduttore li converte nei suoi.
"""
import logging
import time

import requests

_log = logging.getLogger(__name__)


class TranslatorError(Exception):
    """Errore del servizio di traduzione, con un messaggio da mostrare all'utente."""


class BaseTranslator:
    timeout = 90

    def translate(self, texts, source, target, html=False):
        raise NotImplementedError

    def check(self):
        """Breve descrizione dello stato del servizio (per il pulsante "Prova")."""
        raise NotImplementedError

    @staticmethod
    def iso(lang):
        return (lang or '').split('_')[0].lower()


class LibreTranslateTranslator(BaseTranslator):
    """LibreTranslate (open source, self-hosted): gratuito e senza limiti di caratteri."""

    def __init__(self, url, api_key=''):
        self.url = (url or '').rstrip('/')
        self.api_key = api_key or ''
        if not self.url:
            raise TranslatorError('LibreTranslate: manca l\'indirizzo del servizio (es. http://libretranslate:5000).')

    def _request(self, method, path, payload=None):
        try:
            resp = requests.request(method, f'{self.url}{path}', json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise TranslatorError(f'LibreTranslate non raggiungibile ({self.url}): {exc}') from exc
        if resp.status_code != 200:
            try:
                detail = resp.json().get('error') or resp.text
            except ValueError:
                detail = resp.text
            raise TranslatorError(f'LibreTranslate: HTTP {resp.status_code} – {str(detail)[:300]}')
        return resp.json()

    def languages(self):
        return sorted({row.get('code') for row in self._request('GET', '/languages') or []})

    def translate(self, texts, source, target, html=False):
        texts = list(texts)
        if not texts:
            return []
        payload = {'q': texts, 'source': self.iso(source), 'target': self.iso(target),
                   'format': 'html' if html else 'text'}
        if self.api_key:
            payload['api_key'] = self.api_key
        out = self._request('POST', '/translate', payload).get('translatedText')
        if isinstance(out, str):
            out = [out]
        if not isinstance(out, list) or len(out) != len(texts):
            raise TranslatorError('LibreTranslate: risposta inattesa (numero di testi diverso).')
        return out

    def check(self):
        return 'Lingue disponibili: ' + ', '.join(self.languages())


class DeepLTranslator(BaseTranslator):
    """
    DeepL API. Chiave "Free" (finisce con ":fx"): 500.000 caratteri al mese
    gratis, indirizzo api-free.deepl.com; chiave Pro: api.deepl.com.
    """

    # Lingue di arrivo che DeepL vuole con la variante.
    _TARGET = {'en_GB': 'EN-GB', 'en_US': 'EN-US', 'pt_BR': 'PT-BR', 'pt_PT': 'PT-PT'}
    _RETRY_STATUS = (429, 500, 502, 503, 504, 529)

    def __init__(self, api_key, url=''):
        self.api_key = (api_key or '').strip()
        if not self.api_key:
            raise TranslatorError('DeepL: manca la chiave API.')
        default = 'https://api-free.deepl.com' if self.api_key.endswith(':fx') else 'https://api.deepl.com'
        self.url = (url or default).rstrip('/')

    def _target(self, lang):
        if lang in self._TARGET:
            return self._TARGET[lang]
        iso = self.iso(lang)
        return {'en': 'EN-GB', 'pt': 'PT-PT'}.get(iso, iso.upper())

    def _request(self, method, path, payload=None):
        headers = {'Authorization': f'DeepL-Auth-Key {self.api_key}'}
        for attempt in range(4):
            try:
                resp = requests.request(method, f'{self.url}{path}', json=payload, headers=headers,
                                        timeout=self.timeout)
            except requests.RequestException as exc:
                raise TranslatorError(f'DeepL non raggiungibile: {exc}') from exc
            if resp.status_code in self._RETRY_STATUS and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            break
        if resp.status_code == 403:
            raise TranslatorError('DeepL: chiave API non valida (HTTP 403).')
        if resp.status_code == 456:
            raise TranslatorError('DeepL: quota di caratteri del mese esaurita (HTTP 456).')
        if resp.status_code != 200:
            try:
                detail = resp.json().get('message') or resp.text
            except ValueError:
                detail = resp.text
            raise TranslatorError(f'DeepL: HTTP {resp.status_code} – {str(detail)[:300]}')
        return resp.json()

    def translate(self, texts, source, target, html=False):
        texts = list(texts)
        if not texts:
            return []
        payload = {'text': texts, 'source_lang': self.iso(source).upper(), 'target_lang': self._target(target)}
        if html:
            payload['tag_handling'] = 'html'
        rows = self._request('POST', '/v2/translate', payload).get('translations') or []
        if len(rows) != len(texts):
            raise TranslatorError('DeepL: risposta inattesa (numero di testi diverso).')
        return [row.get('text', '') for row in rows]

    def usage(self):
        data = self._request('GET', '/v2/usage')
        return int(data.get('character_count') or 0), int(data.get('character_limit') or 0)

    def check(self):
        used, limit = self.usage()
        return f'Caratteri usati questo mese: {used:,} su {limit:,}'.replace(',', '.')
