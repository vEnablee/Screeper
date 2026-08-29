"""
Scraper eBay.

Via preferenziale: **Browse API ufficiale**. È l'unica delle tre piattaforme
che offre un'API pubblica e documentata, non viene mai bloccata e restituisce
dati puliti (data di pubblicazione, spedizione, venditore, condizione).
Richiede EBAY_CLIENT_ID / EBAY_CLIENT_SECRET nei secrets.

Ripiego: parsing HTML della pagina di ricerca `ebay.it/sch/i.html`, usato
quando le credenziali non ci sono o quando l'API risponde male. Il markup di
eBay cambia spesso, quindi l'estrazione prova più selettori in cascata.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from models import Annuncio, Condizione, Ricerca
from scrapers.base import (
    BaseScraper,
    ScraperBloccato,
    ScraperError,
    estrai_prezzo,
    normalizza_condizione,
    testo_pulito,
)
from utils.dates import parse_data

API_TOKEN = "https://api.ebay.com/identity/v1/oauth2/token"
API_RICERCA = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE = "EBAY_IT"
HTML_RICERCA = "https://www.ebay.it/sch/i.html"

# Risultati per pagina: 50 è il massimo comodo per l'API, l'HTML ne dà 60.
PER_PAGINA_API = 50

# conditionIds della Browse API. Il ricondizionato ha più codici (venditore,
# produttore, eccellente/molto buono) e vanno messi in OR con la pipe.
_CONDIZIONI_API: dict[str, str] = {
    Condizione.NUOVO.value: "{1000|1500}",
    Condizione.USATO.value: "{3000}",
    Condizione.RICONDIZIONATO.value: "{2000|2010|2020|2030}",
}

_ID_DA_URL = re.compile(r"/itm/(?:[^/]*/)?(\d{9,15})")


class EbayScraper(BaseScraper):
    """Browse API con ripiego sull'HTML."""

    nome = "ebay"

    def __init__(self, http: Any, impostazioni: Any) -> None:
        super().__init__(http, impostazioni)
        self._client_id = os.environ.get("EBAY_CLIENT_ID", "").strip()
        self._client_secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
        # Il token dura circa due ore; lo teniamo solo in memoria per la durata
        # del run. Non finisce nello stato: un token nel Gist è un segreto in
        # più da custodire, in cambio di una sola richiesta risparmiata.
        self._token: str | None = None

    @property
    def api_disponibile(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # -- ingresso ----------------------------------------------------------

    def cerca(self, ricerca: Ricerca, pagine: int) -> list[Annuncio]:
        if self.api_disponibile:
            try:
                annunci = self._cerca_api(ricerca, pagine)
                self.via = "api"
                return annunci
            except ScraperBloccato:
                # Un blocco sull'API ufficiale significa quota esaurita o
                # credenziali revocate: ha senso provare l'HTML.
                self.log.warning("Browse API ha rifiutato la richiesta, provo l'HTML")
            except ScraperError as exc:
                self.log.warning("Browse API non utilizzabile (%s), provo l'HTML", exc)
        else:
            self.log.info(
                "EBAY_CLIENT_ID/SECRET non configurati: uso il parsing HTML "
                "(meno affidabile, nessuna data di pubblicazione)"
            )

        annunci = self._cerca_html(ricerca, pagine)
        self.via = "html"
        return annunci

    # -- via API -----------------------------------------------------------

    def _ottieni_token(self) -> str:
        """Token applicativo OAuth (client credentials), valido ~2 ore."""
        if self._token:
            return self._token

        risposta = self.http.post(
            API_TOKEN,
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": self.http.profilo["user_agent"],
            },
            auth=(self._client_id, self._client_secret),
        )
        if risposta.status_code != 200:
            raise ScraperError(
                f"OAuth eBay fallito: HTTP {risposta.status_code} {risposta.text[:200]}"
            )
        try:
            token = risposta.json()["access_token"]
        except (ValueError, KeyError) as exc:
            raise ScraperError(f"Risposta OAuth eBay inattesa: {exc}") from exc

        self._token = str(token)
        self.log.debug("Token Browse API ottenuto")
        return self._token

    def _filtro_api(self, ricerca: Ricerca) -> str:
        """Costruisce il parametro `filter` della Browse API."""
        parti: list[str] = []
        if ricerca.prezzo_min is not None or ricerca.prezzo_max is not None:
            minimo = "" if ricerca.prezzo_min is None else f"{ricerca.prezzo_min:.2f}"
            massimo = "" if ricerca.prezzo_max is None else f"{ricerca.prezzo_max:.2f}"
            parti.append(f"price:[{minimo}..{massimo}]")
            # Obbligatorio quando si filtra sul prezzo.
            parti.append("priceCurrency:EUR")
        condizioni = _CONDIZIONI_API.get(ricerca.condizione)
        if condizioni:
            parti.append(f"conditionIds:{condizioni}")
        return ",".join(parti)

    def _cerca_api(self, ricerca: Ricerca, pagine: int) -> list[Annuncio]:
        token = self._ottieni_token()
        intestazioni = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
            "Accept": "application/json",
            "Accept-Language": "it-IT",
            "User-Agent": self.http.profilo["user_agent"],
        }

        annunci: list[Annuncio] = []
        for pagina in range(pagine):
            parametri: dict[str, Any] = {
                "q": ricerca.parole_chiave,
                "limit": PER_PAGINA_API,
                "offset": pagina * PER_PAGINA_API,
                # Ordinamento per data di inserimento decrescente: è il
                # requisito "più recenti" della configurazione.
                "sort": "newlyListed",
            }
            filtro = self._filtro_api(ricerca)
            if filtro:
                parametri["filter"] = filtro

            risposta = self.http.get(
                API_RICERCA, params=parametri, headers=intestazioni, json_atteso=True
            )
            try:
                dati = risposta.json()
            except ValueError as exc:
                raise ScraperError(f"Risposta Browse API non JSON: {exc}") from exc

            if "errors" in dati and not dati.get("itemSummaries"):
                messaggio = "; ".join(
                    str(e.get("message", "")) for e in dati.get("errors", [])
                )
                raise ScraperError(f"Browse API ha risposto con errori: {messaggio[:200]}")

            elementi = dati.get("itemSummaries") or []
            for elemento in elementi:
                annuncio = self._da_json_api(elemento)
                if annuncio:
                    annunci.append(annuncio)

            self.log.debug("API pagina %d: %d risultati", pagina + 1, len(elementi))
            # Meno risultati del limite: le pagine successive sarebbero vuote.
            if len(elementi) < PER_PAGINA_API:
                break

        return annunci

    def _da_json_api(self, elemento: dict[str, Any]) -> Annuncio | None:
        """Traduce un `itemSummary` nel modello normalizzato."""
        id_annuncio = str(elemento.get("itemId") or "").strip()
        titolo = testo_pulito(elemento.get("title"))
        url = str(elemento.get("itemWebUrl") or "").strip()
        if not id_annuncio or not titolo or not url:
            return None

        prezzo_grezzo = elemento.get("price") or {}
        prezzo = estrai_prezzo(prezzo_grezzo.get("value"))
        valuta = str(prezzo_grezzo.get("currency") or "EUR")

        # Spedizione: la prima opzione con costo 0 (o di tipo "gratuita").
        spedizione_inclusa: bool | None = None
        opzioni = elemento.get("shippingOptions") or []
        if opzioni:
            costo = (opzioni[0].get("shippingCost") or {}).get("value")
            costo_num = estrai_prezzo(costo)
            if costo_num is not None:
                spedizione_inclusa = costo_num == 0.0

        immagine = (elemento.get("image") or {}).get("imageUrl")
        if not immagine:
            miniature = elemento.get("thumbnailImages") or []
            immagine = miniature[0].get("imageUrl") if miniature else None

        posizione = elemento.get("itemLocation") or {}
        localita = ", ".join(
            str(p) for p in (posizione.get("city"), posizione.get("stateOrProvince")) if p
        ) or posizione.get("country")

        # `itemCreationDate` non è presente in tutte le risposte: se manca,
        # marchiamo la data come incerta invece di inventarla.
        data, incerta = parse_data(
            elemento.get("itemCreationDate"), tz_locale=self.impostazioni.timezone
        )

        return self._annuncio(
            id_annuncio=id_annuncio,
            titolo=titolo,
            url=url,
            prezzo=prezzo,
            valuta=valuta,
            spedizione_inclusa=spedizione_inclusa,
            immagine=immagine,
            localita=testo_pulito(localita) or None,
            condizione=normalizza_condizione(elemento.get("condition")),
            venditore=str((elemento.get("seller") or {}).get("username") or "") or None,
            descrizione=testo_pulito(elemento.get("shortDescription")) or None,
            data_pubblicazione=data,
            data_incerta=incerta,
        )

    # -- via HTML ----------------------------------------------------------

    def _url_html(self, ricerca: Ricerca, pagina: int) -> str:
        parametri: dict[str, Any] = {
            "_nkw": ricerca.parole_chiave,
            "_sop": 10,      # ordinamento: inserzioni più recenti
            "_ipg": 60,      # risultati per pagina
            "_pgn": pagina,
            "rt": "nc",
        }
        if ricerca.prezzo_min is not None:
            parametri["_udlo"] = int(ricerca.prezzo_min)
        if ricerca.prezzo_max is not None:
            parametri["_udhi"] = int(ricerca.prezzo_max)
        # LH_ItemCondition: 1000 = nuovo, 3000 = usato (eBay non offre un
        # codice unico per il ricondizionato su tutte le categorie).
        if ricerca.condizione == Condizione.NUOVO.value:
            parametri["LH_ItemCondition"] = 1000
        elif ricerca.condizione == Condizione.USATO.value:
            parametri["LH_ItemCondition"] = 3000
        return f"{HTML_RICERCA}?{urlencode(parametri)}"

    def _cerca_html(self, ricerca: Ricerca, pagine: int) -> list[Annuncio]:
        annunci: list[Annuncio] = []
        for pagina in range(1, pagine + 1):
            url = self._url_html(ricerca, pagina)
            risposta = self.http.get(url, referer="https://www.ebay.it/")
            zuppa = BeautifulSoup(risposta.text, "lxml")

            schede = (
                zuppa.select("li.s-item")
                or zuppa.select("li.s-card")
                or zuppa.select("[data-testid='item-card']")
                or zuppa.select("div.s-item__wrapper")
            )
            if not schede:
                self.log.warning(
                    "Nessuna scheda riconosciuta nell'HTML eBay: il markup è "
                    "probabilmente cambiato (pagina %d)", pagina
                )
                break

            trovati = 0
            for scheda in schede:
                annuncio = self._da_html(scheda)
                if annuncio:
                    annunci.append(annuncio)
                    trovati += 1
            self.log.debug("HTML pagina %d: %d schede, %d valide", pagina, len(schede), trovati)

        return annunci

    def _da_html(self, scheda: Any) -> Annuncio | None:
        """Estrae un annuncio da una scheda HTML, tollerando markup diversi."""
        # eBay inserisce nei link testo destinato ai soli lettori di schermo
        # ("Si apre in una nuova finestra o scheda"), nascosto via CSS ma ben
        # presente nel DOM: senza rimuoverlo finisce dentro ogni titolo.
        for nascosto in scheda.select(".clipped, .s-item__caption--signal, .su-sr-only"):
            nascosto.decompose()

        collegamento = scheda.select_one("a.s-item__link, a.su-link, a[href*='/itm/']")
        if collegamento is None:
            return None
        url = str(collegamento.get("href") or "").split("?")[0]
        corrispondenza = _ID_DA_URL.search(url)
        if not corrispondenza:
            # La prima "scheda" della lista è un segnaposto senza id: si scarta.
            return None
        id_annuncio = corrispondenza.group(1)

        titolo = testo_pulito(
            self._testo(scheda, ".s-item__title", ".s-card__title", "[role='heading']")
        )
        # eBay antepone "Nuova inserzione" o "Sponsorizzato" al titolo, e in
        # coda può restare il testo di accessibilità se il markup cambia
        # ancora: entrambi vanno via.
        titolo = re.sub(
            r"^\s*(nuova inserzione|new listing|sponsorizzato|sponsored)\s*",
            "", titolo, flags=re.IGNORECASE,
        )
        titolo = re.sub(
            r"\s*(si apre|viene aperta)\s+(in\s+)?una nuova (finestra|scheda).*$",
            "", titolo, flags=re.IGNORECASE,
        )
        titolo = re.sub(
            r"\s*opens in a new (window|tab).*$", "", titolo, flags=re.IGNORECASE
        ).strip()
        if not titolo or titolo.lower() == "shop on ebay":
            return None

        prezzo = estrai_prezzo(self._testo(scheda, ".s-item__price", ".s-card__price"))
        spedizione_testo = self._testo(
            scheda, ".s-item__shipping", ".s-item__logisticsCost", ".s-card__shipping"
        )
        spedizione_inclusa: bool | None = None
        if spedizione_testo:
            spedizione_inclusa = bool(
                re.search(r"gratis|gratuit|free", spedizione_testo, re.IGNORECASE)
            )

        immagine = None
        tag_img = scheda.select_one(".s-item__image-wrapper img, .s-card__image img, img")
        if tag_img is not None:
            immagine = (
                tag_img.get("src")
                or tag_img.get("data-src")
                or tag_img.get("data-defer-load")
            )

        localita = testo_pulito(self._testo(scheda, ".s-item__location", ".s-item__itemLocation"))
        localita = re.sub(r"^\s*da\s+", "", localita, flags=re.IGNORECASE) or None

        data, incerta = parse_data(
            self._testo(scheda, ".s-item__listingDate", ".s-item__time-left", ".s-item__time"),
            tz_locale=self.impostazioni.timezone,
        )

        return self._annuncio(
            id_annuncio=id_annuncio,
            titolo=titolo,
            url=url,
            prezzo=prezzo,
            spedizione_inclusa=spedizione_inclusa,
            immagine=str(immagine) if immagine else None,
            localita=localita,
            condizione=normalizza_condizione(
                self._testo(scheda, ".SECONDARY_INFO", ".s-item__subtitle")
            ),
            venditore=None,   # non esposto in modo affidabile nella lista HTML
            data_pubblicazione=data,
            data_incerta=incerta,
        )

    @staticmethod
    def _testo(scheda: Any, *selettori: str) -> str:
        """Primo selettore che produce del testo, altrimenti stringa vuota."""
        for selettore in selettori:
            elemento = scheda.select_one(selettore)
            if elemento is not None:
                testo = elemento.get_text(" ", strip=True)
                if testo:
                    return testo
        return ""
