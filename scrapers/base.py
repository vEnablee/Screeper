"""
Interfaccia comune degli scraper ed eccezioni condivise.

Ogni piattaforma implementa `BaseScraper.cerca()` e restituisce una lista di
`Annuncio` già normalizzati. Tutta la logica di filtro, deduplicazione e
notifica vive fuori dagli scraper: uno scraper sa solo interrogare il sito e
tradurre la risposta nel modello comune.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from models import Annuncio, Condizione, Impostazioni, Ricerca

if TYPE_CHECKING:  # solo per i type hint: evita un import circolare a runtime
    from scrapers.http import ClientHTTP


class ScraperError(Exception):
    """Errore generico e non fatale di uno scraper."""


class ScraperBloccato(ScraperError):
    """
    La piattaforma ci ha bloccati: 403, 429, o pagina di challenge anti-bot.

    È volutamente distinta da `ScraperError` perché richiede una reazione
    diversa: non si riprova, si mette la piattaforma in quarantena per qualche
    run. Insistere su un blocco è il modo migliore per trasformarlo in
    permanente.
    """


# Prezzo in formato italiano: "1.234,50 €", "€ 45", "45,00"
_PREZZO = re.compile(r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)")


def estrai_prezzo(grezzo: object) -> float | None:
    """
    Estrae un prezzo da testo o numero, gestendo la notazione italiana.

    Restituisce None per "Gratis", "Trattabile", stringhe vuote o valori non
    interpretabili: un prezzo sconosciuto non viene mai forzato a 0, perché
    finirebbe per passare i filtri di prezzo minimo.
    """
    if grezzo is None:
        return None
    if isinstance(grezzo, (int, float)) and not isinstance(grezzo, bool):
        valore = float(grezzo)
        return valore if valore >= 0 else None

    testo = str(grezzo).strip()
    if not testo:
        return None
    if re.search(r"\b(gratis|regalo|free)\b", testo, re.IGNORECASE):
        return 0.0

    corrispondenza = _PREZZO.search(testo.replace("\xa0", " "))
    if not corrispondenza:
        return None

    numero = corrispondenza.group(1).replace(" ", "")
    if "," in numero:
        # Formato italiano: il punto è separatore di migliaia, la virgola di decimali.
        numero = numero.replace(".", "").replace(",", ".")
    elif numero.count(".") == 1 and len(numero.split(".")[1]) == 3:
        # "1.234" senza decimali: il punto è separatore di migliaia.
        numero = numero.replace(".", "")
    try:
        valore = float(numero)
    except ValueError:
        return None
    return valore if valore >= 0 else None


def normalizza_condizione(grezzo: object) -> str:
    """Mappa le diciture delle varie piattaforme sui nostri quattro valori."""
    testo = str(grezzo or "").strip().lower()
    if not testo:
        return Condizione.QUALSIASI.value
    # "Come nuovo" va valutato PRIMA: Subito usa la dicitura "Come nuovo -
    # perfetto o ricondizionato", che resta un oggetto usato. Classificarlo
    # come ricondizionato lo farebbe sparire da una ricerca `condizione: usato`.
    if "come nuovo" in testo:
        return Condizione.USATO.value
    if any(p in testo for p in ("ricondizionat", "refurbish", "rigenerat")):
        return Condizione.RICONDIZIONATO.value
    if any(p in testo for p in ("nuovo", "new", "mai usato", "con cartellino", "con etichetta")):
        return Condizione.NUOVO.value
    # Vinted abbrevia: il campo `status` vale "Buone", "Ottime", "Discrete",
    # non "Buone condizioni". Vanno riconosciute entrambe le forme.
    if any(p in testo for p in ("usato", "used", "pre-owned", "buone", "ottime",
                                "discrete", "soddisfacente", "accettabil")):
        return Condizione.USATO.value
    return Condizione.QUALSIASI.value


def testo_pulito(grezzo: object) -> str:
    """Collassa spazi e a capo: i titoli scrapati dall'HTML ne sono pieni."""
    return re.sub(r"\s+", " ", str(grezzo or "")).strip()


class BaseScraper(ABC):
    """Classe base di ogni scraper di piattaforma."""

    #: chiave usata in config.yaml e nei log (deve stare in `Piattaforma`)
    nome: str = ""

    def __init__(self, http: "ClientHTTP", impostazioni: Impostazioni) -> None:
        self.http = http
        self.impostazioni = impostazioni
        self.log = logging.getLogger(f"monitor.{self.nome}")
        #: valorizzato dallo scraper per far sapere a main.py quale via ha usato
        self.via: str = ""

    @abstractmethod
    def cerca(self, ricerca: Ricerca, pagine: int) -> list[Annuncio]:
        """
        Esegue la ricerca e restituisce gli annunci normalizzati.

        Deve sollevare `ScraperBloccato` in caso di blocco e `ScraperError` per
        ogni altro problema; non deve mai restituire None.
        """

    # -- utilità comuni ----------------------------------------------------

    def _annuncio(self, **campi: object) -> Annuncio:
        """Costruisce un Annuncio già etichettato con la piattaforma corrente."""
        campi.setdefault("piattaforma", self.nome)
        return Annuncio(**campi)  # type: ignore[arg-type]
