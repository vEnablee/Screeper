"""
Configurazione del logging.

Due requisiti specifici di questo progetto:
  1. i log finiscono su stdout, perché è quello che GitHub Actions cattura e
     mostra nella UI del run;
  2. i log di Actions sono leggibili da chiunque abbia accesso al repo (e sono
     pubblici se il repo è pubblico), quindi ogni segreto noto viene sostituito
     con '***' prima di essere scritto.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Iterable

from utils.dates import fuso


class FiltroSegreti(logging.Filter):
    """Sostituisce nel testo dei log qualunque segreto noto."""

    def __init__(self, segreti: Iterable[str]) -> None:
        super().__init__()
        # Solo stringhe abbastanza lunghe: filtrare "1" o "ok" rovinerebbe i log.
        self._segreti = sorted(
            {s for s in segreti if s and len(s) >= 8},
            key=len,
            reverse=True,
        )

    def _oscura(self, testo: str) -> str:
        for segreto in self._segreti:
            if segreto in testo:
                testo = testo.replace(segreto, "***")
        return testo

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._oscura(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._oscura(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    self._oscura(a) if isinstance(a, str) else a for a in record.args
                )
        return True


class FormatterLocale(logging.Formatter):
    """Formatter che stampa l'orario nel fuso locale configurato."""

    def __init__(self, fmt: str, tz_locale: str) -> None:
        super().__init__(fmt)
        self._zona = fuso(tz_locale)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        istante = datetime.fromtimestamp(record.created, tz=self._zona)
        return istante.strftime(datefmt or "%H:%M:%S")


def configura_logging(
    *,
    verboso: bool = False,
    segreti: Iterable[str] = (),
    tz_locale: str = "Europe/Rome",
) -> logging.Logger:
    """Inizializza il logger radice e restituisce quello dell'applicazione."""
    livello = logging.DEBUG if verboso else logging.INFO

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        FormatterLocale("%(asctime)s %(levelname)-7s %(name)-18s %(message)s", tz_locale)
    )
    handler.addFilter(FiltroSegreti(segreti))

    radice = logging.getLogger()
    radice.handlers.clear()
    radice.setLevel(livello)
    radice.addHandler(handler)

    # Le librerie HTTP sono rumorose in DEBUG e non aggiungono informazione.
    for rumorosa in ("urllib3", "requests", "charset_normalizer", "curl_cffi"):
        logging.getLogger(rumorosa).setLevel(logging.WARNING)

    return logging.getLogger("monitor")
