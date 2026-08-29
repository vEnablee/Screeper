"""
Scraper Subito.it: API JSON interna `hades.subito.it/v1/search/items`, molto
più stabile del markup. Ripiego su `__NEXT_DATA__` della pagina risultati.

Il filtro per zona è applicato lato client: quello server richiede id
numerici interni non documentati e instabili. Chi conosce i propri può
metterli in `subito.regione_id` / `subito.citta_id`.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from models import Annuncio, Condizione, Ricerca
from scrapers.base import (
    BaseScraper,
    ScraperError,
    estrai_prezzo,
    normalizza_condizione,
    testo_pulito,
)
from utils.dates import parse_data

BASE = "https://www.subito.it"
API = "https://hades.subito.it/v1/search/items"
PER_PAGINA = 30

# Preset di ritaglio delle immagini di Subito: senza questo parametro
# l'URL base restituisce 404. Verificato sulle pagine del sito.
REGOLA_IMMAGINE = "gallery-desktop-2x-auto"

_ID_DA_URN = re.compile(r"(\d{5,})")


class SubitoScraper(BaseScraper):
    """API interna con ripiego su `__NEXT_DATA__`."""

    nome = "subito"

    # -- ingresso ----------------------------------------------------------

    def cerca(self, ricerca: Ricerca, pagine: int) -> list[Annuncio]:
        try:
            annunci = self._cerca_api(ricerca, pagine)
            self.via = "api"
        except ScraperError as exc:
            self.log.warning("API Subito non utilizzabile (%s), provo l'HTML", exc)
            annunci = self._cerca_html(ricerca, pagine)
            self.via = "html"
        return annunci

    # -- via API -----------------------------------------------------------

    def _cerca_api(self, ricerca: Ricerca, pagine: int) -> list[Annuncio]:
        annunci: list[Annuncio] = []
        referer = self._url_html(ricerca, 1)

        for pagina in range(pagine):
            parametri: dict[str, Any] = {
                "q": ricerca.parole_chiave,
                "t": "s",              # solo annunci di vendita
                "qso": "true",         # ricerca anche nel corpo dell'annuncio
                "sort": "datedesc",    # più recenti per primi
                "lim": PER_PAGINA,
                "start": pagina * PER_PAGINA,
            }
            if ricerca.prezzo_min is not None:
                parametri["ps"] = int(ricerca.prezzo_min)
            if ricerca.prezzo_max is not None:
                parametri["pe"] = int(ricerca.prezzo_max)
            if ricerca.spedizione_inclusa_richiesta:
                parametri["shp"] = "true"
            # Parametri geografici opzionali (vedi nota in testa al modulo).
            if ricerca.subito.regione_id:
                parametri["r"] = ricerca.subito.regione_id
            if ricerca.subito.citta_id:
                parametri["ci"] = ricerca.subito.citta_id
                if ricerca.subito.raggio_km:
                    parametri["rs"] = ricerca.subito.raggio_km

            risposta = self.http.get(API, params=parametri, json_atteso=True, referer=referer)
            try:
                dati = risposta.json()
            except ValueError as exc:
                raise ScraperError(f"Risposta Subito non JSON: {exc}") from exc

            elementi = dati.get("ads")
            if elementi is None:
                raise ScraperError("Risposta Subito senza chiave 'ads': API cambiata")

            elementi = self._filtra_zona(elementi, ricerca)
            for elemento in elementi:
                annuncio = self._da_ad(elemento)
                if annuncio:
                    annunci.append(annuncio)

            self.log.debug("API pagina %d: %d risultati", pagina + 1, len(elementi))
            if len(elementi) < PER_PAGINA:
                break

        return annunci

    def _da_ad(self, ad: Any) -> Annuncio | None:
        """Traduce un annuncio Subito nel modello normalizzato."""
        if not isinstance(ad, dict):
            return None
        # In `__NEXT_DATA__` l'annuncio può essere annidato sotto "item".
        if "urn" not in ad and isinstance(ad.get("item"), dict):
            ad = ad["item"]

        # L'urn ha la forma "id:ad:<uuid>:list:658632842": l'id numerico è
        # l'ultimo segmento. Si estrae da lì e non con una ricerca di cifre
        # sull'intera stringa, che potrebbe agganciarsi a pezzi dell'uuid.
        urn = str(ad.get("urn") or ad.get("id") or "")
        id_annuncio = ""
        ultimo = urn.rsplit(":", 1)[-1]
        if ultimo.isdigit():
            id_annuncio = ultimo
        else:
            trovati = _ID_DA_URN.findall(urn)
            id_annuncio = trovati[-1] if trovati else ""

        titolo = testo_pulito(ad.get("subject"))
        url = str((ad.get("urls") or {}).get("default") or ad.get("url") or "").strip()
        if not id_annuncio or not titolo or not url:
            return None

        caratteristiche = ad.get("features") or {}
        prezzo = estrai_prezzo(self._valore_caratteristica(caratteristiche, "/price"))
        condizione_grezza = self._valore_caratteristica(
            caratteristiche, "/item_condition", "/condition"
        )
        # Su Subito "spedizione disponibile" non vuol dire "inclusa nel
        # prezzo": il costo è a parte. Spedibile => esplicitamente non inclusa.
        spedibile = self._valore_caratteristica(
            caratteristiche, "/item_shippable", "/item_shipping_allowed"
        )
        spedizione_inclusa: bool | None = None
        if spedibile:
            spedizione_inclusa = False

        geo = ad.get("geo") or {}
        localita = " ".join(
            parte for parte in (
                testo_pulito((geo.get("town") or {}).get("value")),
                self._sigla_provincia(geo),
            ) if parte
        ).strip() or testo_pulito((geo.get("region") or {}).get("value")) or None

        # La data: nel JSON dell'API c'è il campo "date" in ISO; nella forma
        # incorporata nell'HTML spesso resta solo "Oggi alle 14:32".
        # Solo `display_iso8601` porta l'offset di fuso. Leggere `display`
        # come UTC sposterebbe ogni annuncio due ore indietro.
        date = ad.get("dates") or {}
        data, incerta = parse_data(
            date.get("display_iso8601") or ad.get("date") or date.get("display"),
            tz_locale=self.impostazioni.timezone,
        )

        return self._annuncio(
            id_annuncio=id_annuncio,
            titolo=titolo,
            url=url,
            prezzo=prezzo,
            spedizione_inclusa=spedizione_inclusa,
            immagine=self._prima_immagine(ad),
            localita=localita,
            condizione=normalizza_condizione(condizione_grezza) if condizione_grezza
            else Condizione.QUALSIASI.value,
            venditore=testo_pulito((ad.get("advertiser") or {}).get("name")) or None,
            descrizione=testo_pulito(ad.get("body")) or None,
            data_pubblicazione=data,
            data_incerta=incerta,
        )

    @staticmethod
    def _valore_caratteristica(caratteristiche: Any, *chiavi: str) -> str | None:
        """Estrae il valore di una feature. Arrivano come lista di
        `{"uri": ..., "values": [...]}`, ma in alcune risposte sono una mappa
        indicizzata per uri: il formato è già cambiato una volta."""
        if isinstance(caratteristiche, dict):
            voci = [
                dict(v, uri=v.get("uri", k)) if isinstance(v, dict) else {}
                for k, v in caratteristiche.items()
            ]
        elif isinstance(caratteristiche, list):
            voci = [v for v in caratteristiche if isinstance(v, dict)]
        else:
            return None

        per_uri = {str(v.get("uri")): v for v in voci if v.get("uri")}
        for chiave in chiavi:
            voce = per_uri.get(chiave)
            if not voce:
                continue
            valori = voce.get("values")
            if isinstance(valori, list) and valori:
                primo = valori[0]
                if isinstance(primo, dict):
                    risultato = primo.get("value") or primo.get("key")
                    if risultato:
                        return str(risultato)
                elif primo:
                    return str(primo)
            if voce.get("value"):
                return str(voce["value"])
        return None

    @staticmethod
    def _sigla_provincia(geo: dict[str, Any]) -> str:
        """Restituisce '(MI)' se la sigla della provincia è disponibile."""
        citta = geo.get("city") or {}
        sigla = testo_pulito(citta.get("short_name") or citta.get("shortName"))
        return f"({sigla})" if sigla and len(sigla) <= 3 else ""

    @staticmethod
    def _prima_immagine(ad: dict[str, Any]) -> str | None:
        """URL della prima immagine. Il JSON dà solo la base, che da sola
        restituisce 404: serve il parametro `rule`. Le risposte più vecchie
        usano invece una lista `scale` con gli URL già pronti."""
        immagini = ad.get("images") or []
        if not isinstance(immagini, list) or not immagini:
            return None
        prima = immagini[0]
        if isinstance(prima, str):
            return prima
        if not isinstance(prima, dict):
            return None

        scale = prima.get("scale")
        if isinstance(scale, list) and scale:
            preferita = scale[1] if len(scale) > 1 else scale[0]
            if isinstance(preferita, dict):
                uri = (
                    preferita.get("secureuri")
                    or preferita.get("secure_uri")
                    or preferita.get("uri")
                )
                if uri and str(uri).startswith("http"):
                    return str(uri)

        base = prima.get("cdn_base_url") or prima.get("base_url")
        if base and str(base).startswith("http"):
            return f"{base}?rule={REGOLA_IMMAGINE}"
        return None

    # -- via HTML ----------------------------------------------------------

    def _url_html(self, ricerca: Ricerca, pagina: int) -> str:
        """
        URL della pagina risultati. La zona entra nel percorso
        (`/annunci-lombardia/`), che è la forma che Subito usa davvero.
        """
        zona = (ricerca.subito.zona or "italia").strip().lower().replace(" ", "-")
        parametri: dict[str, Any] = {"q": ricerca.parole_chiave, "order": "datedesc"}
        if pagina > 1:
            parametri["o"] = pagina
        if ricerca.prezzo_min is not None:
            parametri["ps"] = int(ricerca.prezzo_min)
        if ricerca.prezzo_max is not None:
            parametri["pe"] = int(ricerca.prezzo_max)
        return f"{BASE}/annunci-{zona}/vendita/usato/?{urlencode(parametri)}"

    def _cerca_html(self, ricerca: Ricerca, pagine: int) -> list[Annuncio]:
        annunci: list[Annuncio] = []
        for pagina in range(1, pagine + 1):
            url = self._url_html(ricerca, pagina)
            risposta = self.http.get(url, referer=BASE + "/")
            elementi = self._estrai_next_data(risposta.text)
            if not elementi:
                self.log.warning(
                    "Nessun annuncio estraibile dall'HTML Subito (pagina %d)", pagina
                )
                break
            for elemento in self._filtra_zona(elementi, ricerca):
                annuncio = self._da_ad(elemento)
                if annuncio:
                    annunci.append(annuncio)
        return annunci

    @staticmethod
    def _estrai_next_data(html: str) -> list[dict[str, Any]]:
        """Recupera la lista annunci da `__NEXT_DATA__`."""
        zuppa = BeautifulSoup(html, "lxml")
        tag = zuppa.find("script", id="__NEXT_DATA__")
        candidati: list[str] = []
        if tag is not None and tag.string:
            candidati.append(tag.string)
        else:
            for script in zuppa.find_all("script"):
                contenuto = script.string or ""
                if '"urn"' in contenuto and '"subject"' in contenuto:
                    candidati.append(contenuto)

        for grezzo in candidati:
            testo = grezzo.strip()
            if not testo.startswith("{"):
                corrispondenza = re.search(r"\{.*\}", testo, re.DOTALL)
                if not corrispondenza:
                    continue
                testo = corrispondenza.group(0)
            try:
                dati = json.loads(testo)
            except (ValueError, RecursionError):
                continue
            elementi = _cerca_annunci(dati)
            if elementi:
                return elementi
        return []

    # -- filtro geografico lato client -------------------------------------

    def _filtra_zona(self, elementi: list[Any], ricerca: Ricerca) -> list[Any]:
        """
        Confronta la zona con i valori del blocco `geo`, non con la località
        mostrata: quella contiene comune e provincia, quindi una zona espressa
        come regione non vi comparirebbe mai e scarterebbe tutto.

        Un annuncio senza dati geografici non viene scartato.
        """
        zona = (ricerca.subito.zona or "italia").strip().lower()
        if zona in ("", "italia") or ricerca.subito.regione_id or ricerca.subito.citta_id:
            return elementi

        atteso = zona.replace("-", " ")
        tenuti = [e for e in elementi if self._in_zona(e, atteso)]
        scartati = len(elementi) - len(tenuti)
        if scartati:
            self.log.debug("Filtro zona '%s': scartati %d annunci fuori area", zona, scartati)
        return tenuti

    @staticmethod
    def _in_zona(elemento: Any, atteso: str) -> bool:
        """True se uno dei nomi geografici dell'annuncio contiene la zona."""
        if not isinstance(elemento, dict):
            return True
        if "urn" not in elemento and isinstance(elemento.get("item"), dict):
            elemento = elemento["item"]

        geo = elemento.get("geo") or {}
        if not isinstance(geo, dict):
            return True

        nomi: list[str] = []
        for livello in ("region", "city", "town"):
            voce = geo.get(livello) or {}
            if isinstance(voce, dict):
                nomi += [
                    str(voce.get(campo) or "")
                    for campo in ("value", "friendly_name", "short_name")
                ]
        nomi = [n.lower().replace("-", " ") for n in nomi if n]
        if not nomi:
            return True   # nessun dato geografico: non si scarta
        return any(atteso in nome for nome in nomi)


def _cerca_annunci(nodo: Any, profondita: int = 0) -> list[dict[str, Any]]:
    """Cerca ricorsivamente una lista di annunci Subito dentro un JSON."""
    if profondita > 8:
        return []
    if isinstance(nodo, list):
        validi = [
            e for e in nodo
            if isinstance(e, dict)
            and (e.get("urn") or (isinstance(e.get("item"), dict) and e["item"].get("urn")))
        ]
        if len(validi) >= 2:
            return validi
        for elemento in nodo:
            trovati = _cerca_annunci(elemento, profondita + 1)
            if trovati:
                return trovati
    elif isinstance(nodo, dict):
        for chiave in ("ads", "list", "items", "results"):
            if chiave in nodo:
                trovati = _cerca_annunci(nodo[chiave], profondita + 1)
                if trovati:
                    return trovati
        for valore in nodo.values():
            trovati = _cerca_annunci(valore, profondita + 1)
            if trovati:
                return trovati
    return []
