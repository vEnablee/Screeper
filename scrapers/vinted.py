"""
Scraper Vinted.

Via preferenziale: l'endpoint JSON interno usato dal sito stesso,
`/api/v2/catalog/items`. Due dettagli non negoziabili:

  * **serve una sessione**: chiamare l'endpoint "a freddo" restituisce 401.
    Bisogna prima fare una GET della homepage per farsi assegnare i cookie
    anonimi, e poi riusare la stessa sessione;
  * **serve un Referer coerente**: la chiamata deve sembrare la XHR che parte
    dalla pagina del catalogo, non una richiesta isolata.

Ripiego: la pagina HTML del catalogo incorpora lo stato iniziale di React in
un tag <script>. Da lì si recuperano gli stessi oggetti, quando l'API cambia.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from models import Annuncio, Ricerca
from scrapers.base import (
    BaseScraper,
    ScraperBloccato,
    ScraperError,
    estrai_prezzo,
    normalizza_condizione,
    testo_pulito,
)
from utils.dates import parse_data

BASE = "https://www.vinted.it"
API_CATALOGO = f"{BASE}/api/v2/catalog/items"
PER_PAGINA = 48


class VintedScraper(BaseScraper):
    """API interna con ripiego sull'HTML del catalogo."""

    nome = "vinted"

    def __init__(self, http: Any, impostazioni: Any) -> None:
        super().__init__(http, impostazioni)
        self._sessione_pronta = False

    # -- sessione ----------------------------------------------------------

    def _prepara_sessione(self, forza: bool = False) -> None:
        """
        Visita la homepage per ottenere i cookie anonimi.

        Va fatto una volta per run: i cookie durano molto più della durata di
        un run, e ripetere la visita a ogni ricerca sarebbe solo traffico in
        più (e un pattern più visibile).
        """
        if self._sessione_pronta and not forza:
            return
        self.log.debug("Inizializzo la sessione Vinted (cookie anonimi)")
        self.http.get(BASE + "/", referer=None)
        self._sessione_pronta = True

    # -- ingresso ----------------------------------------------------------

    def cerca(self, ricerca: Ricerca, pagine: int) -> list[Annuncio]:
        try:
            self._prepara_sessione()
            annunci = self._cerca_api(ricerca, pagine)
            self.via = "api"
            return annunci
        except ScraperBloccato:
            # Un blocco vero non si aggira: lo lasciamo salire perché main.py
            # metta Vinted in quarantena. Il ripiego HTML si tenta solo per gli
            # errori di formato, che indicano un cambio di API.
            raise
        except ScraperError as exc:
            self.log.warning("API Vinted non utilizzabile (%s), provo l'HTML", exc)

        annunci = self._cerca_html(ricerca, pagine)
        self.via = "html"
        return annunci

    # -- via API -----------------------------------------------------------

    def _url_catalogo(self, ricerca: Ricerca, pagina: int) -> str:
        """URL della pagina di catalogo, usato come Referer della XHR."""
        parametri: dict[str, Any] = {
            "search_text": ricerca.parole_chiave,
            "order": "newest_first",
        }
        if pagina > 1:
            parametri["page"] = pagina
        return f"{BASE}/catalog?{urlencode(parametri)}"

    def _cerca_api(self, ricerca: Ricerca, pagine: int) -> list[Annuncio]:
        annunci: list[Annuncio] = []

        for pagina in range(1, pagine + 1):
            parametri: dict[str, Any] = {
                "search_text": ricerca.parole_chiave,
                "order": "newest_first",
                "per_page": PER_PAGINA,
                "page": pagina,
                "currency": "EUR",
            }
            if ricerca.prezzo_min is not None:
                parametri["price_from"] = int(ricerca.prezzo_min)
            if ricerca.prezzo_max is not None:
                parametri["price_to"] = int(ricerca.prezzo_max)

            referer = self._url_catalogo(ricerca, pagina)
            try:
                risposta = self.http.get(
                    API_CATALOGO, params=parametri, json_atteso=True, referer=referer
                )
            except ScraperBloccato as exc:
                # 401 al primo colpo significa quasi sempre cookie scaduti:
                # si rigenera la sessione e si riprova UNA volta sola.
                if pagina == 1 and "401" in str(exc):
                    self.log.info("Sessione Vinted scaduta: la rigenero e riprovo")
                    self._prepara_sessione(forza=True)
                    risposta = self.http.get(
                        API_CATALOGO, params=parametri, json_atteso=True, referer=referer
                    )
                else:
                    raise

            try:
                dati = risposta.json()
            except ValueError as exc:
                raise ScraperError(f"Risposta Vinted non JSON: {exc}") from exc

            elementi = dati.get("items")
            if elementi is None:
                raise ScraperError("Risposta Vinted senza chiave 'items': API cambiata")

            for elemento in elementi:
                annuncio = self._da_json(elemento)
                if annuncio:
                    annunci.append(annuncio)

            self.log.debug("API pagina %d: %d risultati", pagina, len(elementi))
            if len(elementi) < PER_PAGINA:
                break

        return annunci

    def _da_json(self, elemento: dict[str, Any]) -> Annuncio | None:
        """Traduce un elemento del catalogo nel modello normalizzato."""
        if not isinstance(elemento, dict):
            return None

        id_annuncio = str(elemento.get("id") or "").strip()
        titolo = testo_pulito(elemento.get("title"))
        url = str(elemento.get("url") or "").strip()
        if url and not url.startswith("http"):
            url = BASE + url
        if not url and id_annuncio:
            url = f"{BASE}/items/{id_annuncio}"
        if not id_annuncio or not titolo or not url:
            return None

        # Il prezzo è un dizionario nelle versioni recenti, una stringa in
        # quelle vecchie: gestiamo entrambe le forme.
        grezzo = elemento.get("price")
        if isinstance(grezzo, dict):
            prezzo = estrai_prezzo(grezzo.get("amount"))
            valuta = str(grezzo.get("currency_code") or "EUR")
        else:
            prezzo = estrai_prezzo(grezzo)
            valuta = str(elemento.get("currency") or "EUR")

        foto = elemento.get("photo") or {}
        immagine = foto.get("url") or (foto.get("thumbnails") or [{}])[0].get("url")

        # Data: l'API non espone un campo di pubblicazione stabile. Il
        # timestamp della foto in alta risoluzione è il proxy migliore
        # disponibile, ma resta un proxy: se manca, data incerta.
        grezza_data = (
            elemento.get("created_at_ts")
            or (foto.get("high_resolution") or {}).get("timestamp")
            or foto.get("high_resolution_timestamp")
        )
        data, incerta = parse_data(grezza_data, tz_locale=self.impostazioni.timezone)

        utente = elemento.get("user") or {}
        descrizione_parti = [
            testo_pulito(elemento.get("brand_title")),
            testo_pulito(elemento.get("size_title")),
        ]

        return self._annuncio(
            id_annuncio=id_annuncio,
            titolo=titolo,
            url=url,
            prezzo=prezzo,
            valuta=valuta,
            # Su Vinted il prezzo mostrato non comprende mai la spedizione.
            spedizione_inclusa=False,
            immagine=str(immagine) if immagine else None,
            localita=testo_pulito(utente.get("city") or utente.get("country_title")) or None,
            condizione=normalizza_condizione(elemento.get("status")),
            venditore=testo_pulito(utente.get("login")) or None,
            descrizione=" · ".join(p for p in descrizione_parti if p) or None,
            data_pubblicazione=data,
            data_incerta=incerta,
        )

    # -- via HTML ----------------------------------------------------------

    def _cerca_html(self, ricerca: Ricerca, pagine: int) -> list[Annuncio]:
        annunci: list[Annuncio] = []
        for pagina in range(1, pagine + 1):
            url = self._url_catalogo(ricerca, pagina)
            risposta = self.http.get(url, referer=BASE + "/")
            elementi = self._estrai_json_incorporato(risposta.text)
            if not elementi:
                self.log.warning(
                    "Nessun annuncio estraibile dall'HTML Vinted (pagina %d)", pagina
                )
                break
            for elemento in elementi:
                annuncio = self._da_json(elemento)
                if annuncio:
                    annunci.append(annuncio)
        return annunci

    @staticmethod
    def _estrai_json_incorporato(html: str) -> list[dict[str, Any]]:
        """
        Recupera la lista di articoli dallo stato React incorporato nella pagina.

        Vinted usa react-on-rails: lo stato sta in uno <script> con attributo
        `data-js-react-on-rails-store`. Se anche questo cambia, si ripiega su
        una scansione ricorsiva di qualunque JSON presente nella pagina alla
        ricerca di una lista di oggetti che somigliano ad articoli.
        """
        zuppa = BeautifulSoup(html, "lxml")
        candidati: list[str] = []

        for tag in zuppa.find_all("script"):
            attributi = tag.attrs or {}
            if "data-js-react-on-rails-store" in attributi or attributi.get("id") == "__NEXT_DATA__":
                if tag.string:
                    candidati.append(tag.string)

        if not candidati:
            # Ultima spiaggia: qualunque script che contenga una lista "items".
            for tag in zuppa.find_all("script"):
                contenuto = tag.string or ""
                if '"items"' in contenuto and "{" in contenuto:
                    candidati.append(contenuto)

        for grezzo in candidati:
            testo = grezzo.strip()
            # Alcuni script sono assegnazioni: si isola il primo oggetto JSON.
            if not testo.startswith("{"):
                corrispondenza = re.search(r"\{.*\}", testo, re.DOTALL)
                if not corrispondenza:
                    continue
                testo = corrispondenza.group(0)
            try:
                dati = json.loads(testo)
            except (ValueError, RecursionError):
                continue
            elementi = _cerca_lista_articoli(dati)
            if elementi:
                return elementi
        return []


def _cerca_lista_articoli(nodo: Any, profondita: int = 0) -> list[dict[str, Any]]:
    """
    Cerca ricorsivamente una lista di articoli dentro una struttura JSON.

    Riconosce una lista come "articoli" se contiene dizionari con almeno un id,
    un titolo e un prezzo o un URL. La profondità è limitata per non degenerare
    su documenti molto annidati.
    """
    if profondita > 8:
        return []
    if isinstance(nodo, list):
        validi = [
            e for e in nodo
            if isinstance(e, dict)
            and e.get("id") is not None
            and e.get("title")
            and (e.get("price") is not None or e.get("url"))
        ]
        if len(validi) >= 3:
            return validi
        for elemento in nodo:
            trovati = _cerca_lista_articoli(elemento, profondita + 1)
            if trovati:
                return trovati
    elif isinstance(nodo, dict):
        # "items" per primo: è il nome usato dall'API e dallo store React.
        for chiave in ("items", "catalogItems", "results"):
            if chiave in nodo:
                trovati = _cerca_lista_articoli(nodo[chiave], profondita + 1)
                if trovati:
                    return trovati
        for valore in nodo.values():
            trovati = _cerca_lista_articoli(valore, profondita + 1)
            if trovati:
                return trovati
    return []
