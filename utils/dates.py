"""
Parsing difensivo delle date: ISO, epoch, e testo relativo italiano
("Oggi alle 14:32", "3 min fa", "12 mar alle 09:10").

In caso di dubbio si restituisce `(None, True)` e il chiamante userà la data
di avvistamento. Non si inventa mai una data recente per un annuncio di età
ignota: significherebbe notificare annunci vecchi come nuovi.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from models import adesso_utc

# Mesi italiani, forma estesa e abbreviata (Subito usa entrambe).
_MESI: dict[str, int] = {
    "gennaio": 1, "gen": 1,
    "febbraio": 2, "feb": 2,
    "marzo": 3, "mar": 3,
    "aprile": 4, "apr": 4,
    "maggio": 5, "mag": 5,
    "giugno": 6, "giu": 6,
    "luglio": 7, "lug": 7,
    "agosto": 8, "ago": 8,
    "settembre": 9, "set": 9, "sett": 9,
    "ottobre": 10, "ott": 10,
    "novembre": 11, "nov": 11,
    "dicembre": 12, "dic": 12,
}

# "3 min fa", "2 ore fa", "un giorno fa", "meno di un minuto fa"
_RELATIVO = re.compile(
    r"(?:(?P<quantita>\d+)|(?P<uno>un[oa]?'?))\s*"
    r"(?P<unita>sec|secondi?|min|minuti?|or[ae]|giorni?|settiman[ae]|mes[ei])\b",
    re.IGNORECASE,
)

# "oggi alle 14:32" / "ieri alle 9:05"
_OGGI_IERI = re.compile(
    r"\b(?P<giorno>oggi|ieri|l'altro\s+ieri|altro\s+ieri)\b"
    r"(?:\s*(?:alle|,)?\s*(?P<ora>\d{1,2})[:.](?P<minuto>\d{2}))?",
    re.IGNORECASE,
)

# "12 mar alle 14:32" / "12 marzo 2026 09:10" / "28-ago 14:32" (forma eBay)
_DATA_TESTUALE = re.compile(
    r"\b(?P<giorno>\d{1,2})[\s-]+(?P<mese>[a-zà]+)\.?"
    r"(?:\s+(?P<anno>\d{4}))?"
    r"(?:\s*(?:alle|,)?\s*(?P<ora>\d{1,2})[:.](?P<minuto>\d{2}))?",
    re.IGNORECASE,
)

# "28/08/2026 14:32" oppure "28-08-2026"
_DATA_NUMERICA = re.compile(
    r"\b(?P<giorno>\d{1,2})[/-](?P<mese>\d{1,2})[/-](?P<anno>\d{2,4})"
    r"(?:\s+(?P<ora>\d{1,2})[:.](?P<minuto>\d{2}))?",
)

_UNITA_SECONDI: dict[str, int] = {
    "sec": 1, "secondo": 1, "secondi": 1,
    "min": 60, "minuto": 60, "minuti": 60,
    "ora": 3600, "ore": 3600,
    "giorno": 86400, "giorni": 86400,
    "settimana": 604800, "settimane": 604800,
    "mese": 2592000, "mesi": 2592000,
}


def fuso(nome: str) -> ZoneInfo:
    """ZoneInfo tollerante: se il nome è ignoto si ripiega su UTC."""
    try:
        return ZoneInfo(nome)
    except Exception:
        return ZoneInfo("UTC")


def _utc(dt: datetime) -> datetime:
    """Porta un datetime in UTC, assumendo UTC se è naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _da_epoch(valore: float) -> datetime | None:
    """Converte un epoch in secondi o millisecondi, scartando valori assurdi."""
    # Millisecondi: qualunque valore sopra ~1e11 non può essere in secondi.
    if valore > 1e11:
        valore = valore / 1000.0
    # Accetta solo l'intervallo 2000-2100, altrimenti è quasi certo un errore.
    if not (946_684_800 < valore < 4_102_444_800):
        return None
    return datetime.fromtimestamp(valore, tz=timezone.utc)


def _da_iso(testo: str) -> datetime | None:
    """Parsing ISO 8601, incluso il suffisso 'Z' che fromisoformat non ama."""
    candidato = testo.strip().replace("Z", "+00:00")
    try:
        return _utc(datetime.fromisoformat(candidato))
    except ValueError:
        return None


def parse_data(
    grezzo: Any,
    *,
    tz_locale: str = "Europe/Rome",
    adesso: datetime | None = None,
) -> tuple[datetime | None, bool]:
    """
    Prova a interpretare una data in qualunque dei formati incontrati.

    Ritorna `(data_utc, incerta)`:
      * `(datetime, False)` -> data affidabile;
      * `(datetime, True)`  -> data plausibile ma dedotta (es. testo relativo
        con granularità grossolana come "2 mesi fa", o data senza anno);
      * `(None, True)`      -> non interpretabile, usare l'avvistamento.
    """
    if grezzo is None:
        return None, True

    ora = adesso or adesso_utc()
    zona = fuso(tz_locale)

    # 1) datetime già pronto
    if isinstance(grezzo, datetime):
        return _utc(grezzo), False

    # 2) epoch numerico
    if isinstance(grezzo, (int, float)) and not isinstance(grezzo, bool):
        risultato = _da_epoch(float(grezzo))
        return (risultato, False) if risultato else (None, True)

    if not isinstance(grezzo, str):
        return None, True

    testo = grezzo.strip()
    if not testo:
        return None, True

    # 3) stringa che contiene solo un epoch
    if re.fullmatch(r"\d{9,14}", testo):
        risultato = _da_epoch(float(testo))
        if risultato:
            return risultato, False

    # 4) ISO 8601
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", testo):
        risultato = _da_iso(testo)
        if risultato:
            return risultato, False

    minuscolo = testo.lower()

    # 5) "adesso" / "poco fa"
    if re.search(r"\b(adesso|ora|poco\s+fa|pochi\s+secondi)\b", minuscolo):
        return ora, False

    # 6) oggi / ieri / l'altro ieri (valutati nel fuso LOCALE, non in UTC)
    corrispondenza = _OGGI_IERI.search(minuscolo)
    if corrispondenza:
        oggi_locale = ora.astimezone(zona)
        etichetta = corrispondenza.group("giorno")
        scarto = 0 if etichetta == "oggi" else (1 if etichetta == "ieri" else 2)
        giorno = oggi_locale - timedelta(days=scarto)
        if corrispondenza.group("ora"):
            ore = int(corrispondenza.group("ora"))
            minuti = int(corrispondenza.group("minuto"))
            if 0 <= ore <= 23 and 0 <= minuti <= 59:
                locale = giorno.replace(
                    hour=ore, minute=minuti, second=0, microsecond=0
                )
                return _utc(locale), False
        # Senza orario sappiamo solo il giorno: data incerta.
        locale = giorno.replace(hour=0, minute=0, second=0, microsecond=0)
        return _utc(locale), True

    # 7) forma relativa ("3 min fa", "un'ora fa")
    corrispondenza = _RELATIVO.search(minuscolo)
    if corrispondenza and re.search(r"\bfa\b", minuscolo):
        unita = corrispondenza.group("unita").lower()
        secondi_unita = None
        for chiave, secondi in _UNITA_SECONDI.items():
            if unita.startswith(chiave[:3]):
                secondi_unita = secondi
                break
        if secondi_unita is not None:
            quantita = int(corrispondenza.group("quantita") or 1)
            delta = timedelta(seconds=quantita * secondi_unita)
            # Oltre il giorno la granularità è troppo grossa per fidarsi.
            return ora - delta, secondi_unita >= 86400

    # 8) data numerica gg/mm/aaaa
    corrispondenza = _DATA_NUMERICA.search(minuscolo)
    if corrispondenza:
        risultato = _componi(
            giorno=corrispondenza.group("giorno"),
            mese=corrispondenza.group("mese"),
            anno=corrispondenza.group("anno"),
            ora=corrispondenza.group("ora"),
            minuto=corrispondenza.group("minuto"),
            zona=zona,
            adesso=ora,
        )
        if risultato:
            return risultato

    # 9) data testuale "12 mar alle 14:32"
    corrispondenza = _DATA_TESTUALE.search(minuscolo)
    if corrispondenza:
        mese = _MESI.get(corrispondenza.group("mese").rstrip("."))
        if mese:
            risultato = _componi(
                giorno=corrispondenza.group("giorno"),
                mese=str(mese),
                anno=corrispondenza.group("anno"),
                ora=corrispondenza.group("ora"),
                minuto=corrispondenza.group("minuto"),
                zona=zona,
                adesso=ora,
            )
            if risultato:
                return risultato

    # 10) ultimo tentativo: ISO in mezzo ad altro testo
    risultato = _da_iso(testo)
    if risultato:
        return risultato, False

    return None, True


def _componi(
    *,
    giorno: str | None,
    mese: str | None,
    anno: str | None,
    ora: str | None,
    minuto: str | None,
    zona: ZoneInfo,
    adesso: datetime,
) -> tuple[datetime, bool] | None:
    """
    Costruisce un datetime dai componenti estratti, nel fuso locale.

    Se l'anno manca si assume l'anno corrente; se la data risultante è nel
    futuro (tipico a cavallo di capodanno) si retrocede di un anno. In assenza
    di anno o di orario il risultato è marcato come incerto.
    """
    try:
        g = int(giorno or 0)
        m = int(mese or 0)
    except ValueError:
        return None
    if not (1 <= g <= 31 and 1 <= m <= 12):
        return None

    incerta = False
    if anno:
        a = int(anno)
        if a < 100:
            a += 2000
    else:
        a = adesso.astimezone(zona).year
        incerta = True

    if ora and minuto:
        h, mi = int(ora), int(minuto)
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None
    else:
        h, mi = 0, 0
        incerta = True

    try:
        locale = datetime(a, m, g, h, mi, tzinfo=zona)
    except ValueError:
        return None

    utc = _utc(locale)
    # Data nel futuro con anno dedotto: era l'anno scorso.
    if utc > adesso + timedelta(days=1) and not anno:
        try:
            utc = _utc(datetime(a - 1, m, g, h, mi, tzinfo=zona))
        except ValueError:
            return None
    return utc, incerta


def formatta(dt: datetime | None, tz_locale: str = "Europe/Rome") -> str:
    """Formato compatto per notifiche e log: '28/08 14:32'."""
    if dt is None:
        return "data ignota"
    return dt.astimezone(fuso(tz_locale)).strftime("%d/%m %H:%M")


def formatta_completo(dt: datetime | None, tz_locale: str = "Europe/Rome") -> str:
    """Formato esteso per /status e dashboard: '28/08/2026 14:32:07'."""
    if dt is None:
        return "mai"
    return dt.astimezone(fuso(tz_locale)).strftime("%d/%m/%Y %H:%M:%S")
