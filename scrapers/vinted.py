"""
Scraper Vinted.

Vinted ha dismesso l'API REST interna `/api/v2/catalog/items`, che ora
risponde 404, passando a React Server Components: i dati non arrivano più da
una chiamata del browser, sono già dentro la pagina del catalogo, in un
payload di flight dove il JSON è annidato ed escapato.

Si richiede quindi la pagina con l'header `RSC: 1`, che fa restituire il solo
payload senza l'involucro HTML — 5.8 MB invece di 7.2 e circa metà del tempo —
e se ne estraggono gli oggetti `productItem`.

LIMITE NOTO: il nuovo payload non contiene alcuna data di pubblicazione, né
il timestamp della foto che l'API vecchia esponeva. Gli annunci Vinted sono
quindi sempre marcati con data incerta, e la novità viene stabilita
dall'assenza dell'id fra quelli già visti — che è comunque il criterio più
affidabile.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode

from models import Annuncio, Ricerca
from scrapers.base import (
    BaseScraper,
    ScraperError,
    estrai_prezzo,
    normalizza_condizione,
    testo_pulito,
)

BASE = "https://www.vinted.it"
CATALOGO = f"{BASE}/catalog"

# Osservati 96 per pagina, il doppio di quanti ne dava l'API dismessa.
PER_PAGINA = 96

_INIZIO_ARTICOLO = re.compile(r'"productItem":\s*\{')


class VintedScraper(BaseScraper):
    """Legge il catalogo dal payload React Server Components."""

    nome = "vinted"

    # -- ingresso ----------------------------------------------------------

    def cerca(self, ricerca: Ricerca, pagine: int) -> list[Annuncio]:
        annunci: list[Annuncio] = []
        for pagina in range(1, pagine + 1):
            elementi = self._scarica(ricerca, pagina)
            if not elementi:
                if pagina == 1:
                    raise ScraperError(
                        "Nessun articolo estraibile dal catalogo Vinted: "
                        "il formato del payload è probabilmente cambiato"
                    )
                break
            for elemento in elementi:
                annuncio = self._da_json(elemento)
                if annuncio:
                    annunci.append(annuncio)
            self.log.debug("pagina %d: %d articoli", pagina, len(elementi))
            if len(elementi) < PER_PAGINA:
                break
        return annunci

    def _url(self, ricerca: Ricerca, pagina: int) -> str:
        parametri: dict[str, Any] = {
            "search_text": ricerca.parole_chiave,
            "order": "newest_first",
            "currency": "EUR",
        }
        # Verificati funzionanti lato server: restringono davvero i risultati.
        if ricerca.prezzo_min is not None:
            parametri["price_from"] = int(ricerca.prezzo_min)
        if ricerca.prezzo_max is not None:
            parametri["price_to"] = int(ricerca.prezzo_max)
        if pagina > 1:
            parametri["page"] = pagina
        return f"{CATALOGO}?{urlencode(parametri)}"

    def _scarica(self, ricerca: Ricerca, pagina: int) -> list[dict[str, Any]]:
        url = self._url(ricerca, pagina)

        # Con `RSC: 1` Next.js restituisce il solo payload di flight. Se un
        # domani smettesse di funzionare, la pagina completa contiene gli
        # stessi dati e il parser è lo stesso.
        intestazioni = self.http.intestazioni(referer=BASE + "/")
        risposta = self.http.get(url, headers={**intestazioni, "RSC": "1"})
        elementi = estrai_articoli(risposta.text)
        if elementi:
            self.via = "rsc"
            return elementi

        self.log.info("Payload RSC vuoto: riprovo con la pagina completa")
        risposta = self.http.get(url, referer=BASE + "/")
        elementi = estrai_articoli(risposta.text)
        self.via = "html"
        return elementi

    # -- conversione -------------------------------------------------------

    def _da_json(self, elemento: dict[str, Any]) -> Annuncio | None:
        if not isinstance(elemento, dict):
            return None

        id_annuncio = str(elemento.get("id") or "").strip()
        titolo = testo_pulito(elemento.get("title"))
        percorso = str(elemento.get("url") or "").strip()
        if not id_annuncio or not titolo:
            return None

        url = percorso if percorso.startswith("http") else BASE + percorso
        if not percorso:
            url = f"{BASE}/items/{id_annuncio}"

        prezzo_grezzo = elemento.get("price") or {}
        prezzo = estrai_prezzo(prezzo_grezzo.get("amount"))
        valuta = str(prezzo_grezzo.get("currencyCode") or "EUR")

        immagine = elemento.get("thumbnailUrl")
        if not immagine:
            foto = elemento.get("photos") or []
            if foto and isinstance(foto[0], dict):
                immagine = foto[0].get("url")

        scheda = elemento.get("itemBox") or {}
        marca = testo_pulito(scheda.get("firstLine"))
        condizione = normalizza_condizione(scheda.get("secondLine"))

        # Non c'è un nome utente nel nuovo payload, solo l'identificativo
        # numerico: basta a riconoscere le ripubblicazioni dello stesso
        # venditore, che è l'unico uso che ne facciamo.
        utente = elemento.get("user") or {}
        venditore = str(utente.get("id")) if utente.get("id") else None

        return self._annuncio(
            id_annuncio=id_annuncio,
            titolo=titolo,
            url=url,
            prezzo=prezzo,
            valuta=valuta,
            # Su Vinted il prezzo mostrato non comprende mai la spedizione.
            spedizione_inclusa=False,
            immagine=str(immagine) if immagine else None,
            localita=None,
            condizione=condizione,
            venditore=venditore,
            descrizione=marca or None,
            # Nessuna data nel payload: si dichiara l'incertezza invece di
            # dedurne una, così l'annuncio non passa mai per recente.
            data_pubblicazione=None,
            data_incerta=True,
        )


# ---------------------------------------------------------------------------
# Estrazione dal payload
# ---------------------------------------------------------------------------

def _oggetto_bilanciato(testo: str, inizio: int) -> str | None:
    """
    Ritaglia l'oggetto JSON che comincia a `inizio` contando le graffe.

    Serve perché gli articoli sono annidati dentro un payload molto più
    grande e non c'è un delimitatore su cui spezzare: una regex si fermerebbe
    alla prima graffa chiusa, che appartiene a un oggetto interno.
    """
    livello = 0
    dentro_stringa = False
    fuga = False
    for i in range(inizio, len(testo)):
        c = testo[i]
        if fuga:
            fuga = False
        elif c == "\\":
            fuga = True
        elif c == '"':
            dentro_stringa = not dentro_stringa
        elif not dentro_stringa:
            if c == "{":
                livello += 1
            elif c == "}":
                livello -= 1
                if livello == 0:
                    return testo[inizio:i + 1]
    return None


def estrai_articoli(grezzo: str) -> list[dict[str, Any]]:
    """
    Recupera gli oggetti `productItem` dal payload di flight.

    Il JSON è annidato dentro stringhe JavaScript, quindi va prima
    de-escapato. Un oggetto illeggibile viene saltato senza far cadere gli
    altri: un cambio di formato parziale deve degradare, non azzerare.
    """
    if not grezzo:
        return []
    testo = grezzo.replace('\\"', '"').replace("\\\\", "\\")

    articoli: list[dict[str, Any]] = []
    visti: set[str] = set()
    for corrispondenza in _INIZIO_ARTICOLO.finditer(testo):
        apertura = testo.find("{", corrispondenza.end() - 1)
        if apertura == -1:
            continue
        frammento = _oggetto_bilanciato(testo, apertura)
        if not frammento:
            continue
        try:
            articolo = json.loads(frammento)
        except ValueError:
            continue
        if not isinstance(articolo, dict):
            continue
        # Lo stesso articolo può comparire più volte nel payload.
        chiave = str(articolo.get("id") or "")
        if chiave and chiave in visti:
            continue
        visti.add(chiave)
        articoli.append(articolo)
    return articoli
