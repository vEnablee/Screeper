"""
Modelli dati condivisi.

Ogni datetime è aware e in UTC; la conversione al fuso locale avviene solo
quando si formatta un testo. `Annuncio` è la forma normalizzata a cui ogni
scraper converge.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enumerazioni
# ---------------------------------------------------------------------------

class Piattaforma(str, Enum):
    """Piattaforme supportate. Il valore è la chiave usata in config.yaml."""

    EBAY = "ebay"
    VINTED = "vinted"
    SUBITO = "subito"

    @classmethod
    def valide(cls) -> set[str]:
        return {p.value for p in cls}


class Condizione(str, Enum):
    """Condizione dell'oggetto, normalizzata fra le piattaforme."""

    NUOVO = "nuovo"
    USATO = "usato"
    RICONDIZIONATO = "ricondizionato"
    QUALSIASI = "qualsiasi"

    @classmethod
    def valide(cls) -> set[str]:
        return {c.value for c in cls}


class EsitoScraper(str, Enum):
    """Esito di una singola coppia (ricerca, piattaforma) in un run."""

    OK = "ok"                    # richiesta riuscita, almeno un risultato
    VUOTO = "vuoto"              # richiesta riuscita, zero risultati
    BLOCCATO = "bloccato"        # 403/429/challenge anti-bot
    ERRORE = "errore"            # eccezione di rete o di parsing
    QUARANTENA = "quarantena"    # saltato perché la piattaforma era in pausa
    SALTATO = "saltato"          # saltato per intervallo non ancora scaduto


def adesso_utc() -> datetime:
    """Istante corrente, aware, in UTC. Punto unico di verità sull'orologio."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Annuncio normalizzato
# ---------------------------------------------------------------------------

_NON_ALFANUMERICI = re.compile(r"[^a-z0-9]+")


def _normalizza_testo(testo: str) -> str:
    """Riduce un testo alla sua forma canonica per confronti e fingerprint."""
    return _NON_ALFANUMERICI.sub(" ", testo.lower()).strip()


@dataclass(slots=True)
class Annuncio:
    """Un annuncio, nella forma normalizzata comune a tutte le piattaforme."""

    piattaforma: str
    id_annuncio: str
    titolo: str
    url: str

    prezzo: float | None = None
    valuta: str = "EUR"
    spedizione_inclusa: bool | None = None
    immagine: str | None = None
    localita: str | None = None
    condizione: str = Condizione.QUALSIASI.value
    venditore: str | None = None
    descrizione: str | None = None

    # Data dichiarata dalla piattaforma. Può essere None: in quel caso il
    # resto dell'applicazione usa `data_avvistamento`.
    data_pubblicazione: datetime | None = None
    # Istante in cui NOI abbiamo visto l'annuncio per la prima volta.
    data_avvistamento: datetime = field(default_factory=adesso_utc)
    # True quando la data di pubblicazione è stata dedotta o è assente: serve a
    # non trattare mai come "appena pubblicato" un annuncio di data ignota.
    data_incerta: bool = False

    # Nome della ricerca che lo ha prodotto (valorizzato da main.py).
    ricerca: str = ""

    # -- identità ----------------------------------------------------------

    @property
    def chiave(self) -> str:
        """Chiave di deduplicazione primaria: piattaforma + id nativo."""
        return f"{self.piattaforma}:{self.id_annuncio}"

    @property
    def fingerprint(self) -> str:
        """Riconosce un annuncio ripubblicato: stessi titolo, prezzo e
        venditore ma id diverso."""
        prezzo = f"{self.prezzo:.2f}" if self.prezzo is not None else "?"
        grezzo = "|".join((
            self.piattaforma,
            _normalizza_testo(self.titolo),
            prezzo,
            _normalizza_testo(self.venditore or ""),
        ))
        return hashlib.sha1(grezzo.encode("utf-8")).hexdigest()[:16]

    @property
    def data_effettiva(self) -> datetime:
        """
        Data per ordinamenti e finestra di primo avvio. Se quella dichiarata
        manca o è incerta vale l'avvistamento: un annuncio vecchio con data
        illeggibile non deve mai passare per nuovo.
        """
        if self.data_pubblicazione is not None and not self.data_incerta:
            return self.data_pubblicazione
        return self.data_avvistamento

    # -- serializzazione ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Forma JSON-serializzabile, usata nello stato salvato sul Gist."""
        return {
            "piattaforma": self.piattaforma,
            "id_annuncio": self.id_annuncio,
            "titolo": self.titolo,
            "url": self.url,
            "prezzo": self.prezzo,
            "valuta": self.valuta,
            "spedizione_inclusa": self.spedizione_inclusa,
            "immagine": self.immagine,
            "localita": self.localita,
            "condizione": self.condizione,
            "venditore": self.venditore,
            "data_pubblicazione": (
                self.data_pubblicazione.isoformat() if self.data_pubblicazione else None
            ),
            "data_avvistamento": self.data_avvistamento.isoformat(),
            "data_incerta": self.data_incerta,
            "ricerca": self.ricerca,
        }

    @classmethod
    def from_dict(cls, dati: dict[str, Any]) -> "Annuncio":
        """Inverso di `to_dict`, tollerante a campi mancanti o malformati."""

        def _data(valore: Any) -> datetime | None:
            if not valore:
                return None
            try:
                dt = datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
            except ValueError:
                return None
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        return cls(
            piattaforma=str(dati.get("piattaforma", "")),
            id_annuncio=str(dati.get("id_annuncio", "")),
            titolo=str(dati.get("titolo", "")),
            url=str(dati.get("url", "")),
            prezzo=dati.get("prezzo"),
            valuta=str(dati.get("valuta") or "EUR"),
            spedizione_inclusa=dati.get("spedizione_inclusa"),
            immagine=dati.get("immagine"),
            localita=dati.get("localita"),
            condizione=str(dati.get("condizione") or Condizione.QUALSIASI.value),
            venditore=dati.get("venditore"),
            data_pubblicazione=_data(dati.get("data_pubblicazione")),
            data_avvistamento=_data(dati.get("data_avvistamento")) or adesso_utc(),
            data_incerta=bool(dati.get("data_incerta", False)),
            ricerca=str(dati.get("ricerca") or ""),
        )


# ---------------------------------------------------------------------------
# Configurazione: ricerche e impostazioni globali
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ConfigSubito:
    """Parametri specifici di Subito per una ricerca."""

    # Nome di regione o città come compare negli URL di Subito
    # (es. "lombardia", "milano", "italia" per nessun filtro).
    zona: str = "italia"
    # Usato solo insieme a `citta_id`; su Subito il raggio è server-side.
    raggio_km: int = 0
    # Parametri avanzati e opzionali: se valorizzati vengono passati
    # direttamente all'API, altrimenti il filtro geografico è applicato
    # lato client confrontando `zona` con la località dell'annuncio.
    regione_id: int | None = None
    citta_id: int | None = None


@dataclass(slots=True)
class Ricerca:
    """Una ricerca configurata dall'utente."""

    nome: str
    parole_chiave: str
    piattaforme: list[str] = field(default_factory=list)
    attiva: bool = True
    in_pausa: bool = False
    intervallo_minuti: int = 15
    parole_escluse: list[str] = field(default_factory=list)
    prezzo_min: float | None = None
    prezzo_max: float | None = None
    condizione: str = Condizione.QUALSIASI.value
    solo_titolo: bool = True
    spedizione_inclusa_richiesta: bool = False
    subito: ConfigSubito = field(default_factory=ConfigSubito)

    @property
    def eseguibile(self) -> bool:
        """Una ricerca gira solo se è attiva e non sospesa."""
        return self.attiva and not self.in_pausa

    # -- filtri ------------------------------------------------------------

    def testo_filtrato(self, annuncio: Annuncio) -> str:
        """Testo su cui applicare le parole escluse, secondo `solo_titolo`."""
        if self.solo_titolo or not annuncio.descrizione:
            return annuncio.titolo
        return f"{annuncio.titolo}\n{annuncio.descrizione}"

    def parola_esclusa(self, testo: str) -> str | None:
        """
        Restituisce la prima parola esclusa trovata nel testo, o None.

        Due passaggi:

        1. confronto su PAROLE INTERE, senza distinzione fra maiuscole e
           minuscole, che gestisce anche le espressioni composte come
           "non funzionante";
        2. confronto sul testo con TUTTI GLI SPAZI RIMOSSI, per intercettare
           chi scrive "s c a m b i o" apposta per aggirare i filtri dei
           marketplace. Su Subito è una pratica diffusa.

        Il secondo passaggio si applica solo ai termini di almeno sei
        lettere: senza confini di parola, uno corto produrrebbe falsi
        positivi a raffica ("fat" dentro "fatto", "usato" dentro "inusuale").
        """
        normalizzato = _normalizza_testo(testo)
        compattato = re.sub(r"\s+", "", normalizzato)

        for parola in self.parole_escluse:
            termine = _normalizza_testo(str(parola))
            if not termine:
                continue
            schema = r"\b" + r"\s+".join(re.escape(p) for p in termine.split()) + r"\b"
            if re.search(schema, normalizzato):
                return str(parola)
            compatto = termine.replace(" ", "")
            if len(compatto) >= 6 and compatto in compattato:
                return str(parola)
        return None

    def prezzo_ok(self, prezzo: float | None) -> bool:
        """
        Un prezzo sconosciuto NON viene scartato: meglio una notifica in più
        che perdere un annuncio perché la piattaforma non espone il prezzo.
        """
        if prezzo is None:
            return True
        if self.prezzo_min is not None and prezzo < self.prezzo_min:
            return False
        if self.prezzo_max is not None and prezzo > self.prezzo_max:
            return False
        return True

    def condizione_ok(self, condizione: str) -> bool:
        """Filtro sulla condizione; 'qualsiasi' su entrambi i lati passa."""
        if self.condizione == Condizione.QUALSIASI.value:
            return True
        if condizione == Condizione.QUALSIASI.value:
            return True
        return condizione == self.condizione

    def spedizione_ok(self, spedizione_inclusa: bool | None) -> bool:
        """Se il dato non è disponibile l'annuncio passa comunque."""
        if not self.spedizione_inclusa_richiesta:
            return True
        return spedizione_inclusa is not False


@dataclass(slots=True)
class Impostazioni:
    """Impostazioni globali (blocco `impostazioni` di config.yaml)."""

    timezone: str = "Europe/Rome"
    finestra_primo_avvio_minuti: int = 60
    max_notifiche_per_run: int = 15
    pagine_per_ricerca: int = 1
    pagine_primo_avvio: int = 2
    delay_min_secondi: float = 3.0
    delay_max_secondi: float = 8.0
    timeout_secondi: int = 20
    max_tentativi: int = 3
    storico_giorni: int = 30
    storico_max_annunci: int = 4000
    rileva_ripubblicati: bool = True
    run_zero_per_alert: int = 3
    run_pausa_dopo_blocco: int = 3
    heartbeat_giornaliero: bool = True
    heartbeat_ora: int = 9
    notifica_errori: bool = True
    # Avviso a ogni controllo: "mai" | "sempre" | "aggiorna".
    #   mai      -> nessun messaggio, si notificano solo gli annunci
    #   sempre   -> un messaggio nuovo a ogni controllo (con squillo)
    #   aggiorna -> un solo messaggio, riscritto ogni volta (senza squillo)
    notifica_ogni_controllo: str = "aggiorna"


@dataclass(slots=True)
class Configurazione:
    """Configurazione completa: impostazioni globali + elenco ricerche."""

    impostazioni: Impostazioni
    ricerche: list[Ricerca]

    def ricerca_per_nome(self, nome: str) -> Ricerca | None:
        for r in self.ricerche:
            if r.nome == nome:
                return r
        return None


# ---------------------------------------------------------------------------
# Risultato di uno scraper
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RisultatoScraper:
    """Esito di una chiamata a uno scraper per una singola ricerca."""

    piattaforma: str
    esito: EsitoScraper
    annunci: list[Annuncio] = field(default_factory=list)
    richieste: int = 0
    errore: str | None = None
    via: str = ""   # "api", "html", ... — utile nei log per capire il fallback
