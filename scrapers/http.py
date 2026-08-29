"""
Livello HTTP condiviso dagli scraper. Contromisure anti-blocco, tutte passive:

1. fingerprint TLS coerente via curl_cffi — dichiarare uno User-Agent di
   Chrome con un handshake OpenSSL è più sospetto di uno User-Agent onesto;
2. profilo browser completo, scelto una volta per run e non per richiesta;
3. ritmo di 3-8s fra richieste ALLO STESSO dominio: fra domini diversi non si
   attende, perché un sito non sa nulla delle richieste fatte a un altro;
4. rispetto dei blocchi: su 403 ci si ferma, su 429 si attende una volta sola.

Nessun captcha risolto, nessun proxy a rotazione, nessun tentativo su un 403.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Iterable
from urllib.parse import urlsplit

from scrapers.base import ScraperBloccato, ScraperError

log = logging.getLogger("monitor.http")

# --------------------------------------------------------------------------
# Backend HTTP
# --------------------------------------------------------------------------
try:
    from curl_cffi import requests as _backend  # type: ignore[import-not-found]

    BACKEND = "curl_cffi"
except ImportError:  # pragma: no cover - percorso di ripiego
    import requests as _backend  # type: ignore[no-redef]

    BACKEND = "requests"
    log.warning(
        "curl_cffi non disponibile: uso 'requests'. Il fingerprint TLS non "
        "somiglierà a quello di un browser e Vinted/Subito bloccheranno molto "
        "più spesso. Installa curl_cffi (vedi requirements-monitor.txt)."
    )


# --------------------------------------------------------------------------
# Profili browser
# --------------------------------------------------------------------------
# Ogni profilo è internamente coerente: la versione dichiarata nello
# User-Agent, quella in `sec-ch-ua` e la piattaforma coincidono, e
# `impersonate` seleziona il fingerprint TLS corrispondente in curl_cffi.
PROFILI: list[dict[str, str]] = [
    {
        "impersonate": "chrome131",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "piattaforma": '"Windows"',
    },
    {
        "impersonate": "chrome124",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Google Chrome";v="124", "Chromium";v="124", "Not_A Brand";v="99"',
        "piattaforma": '"macOS"',
    },
    {
        "impersonate": "chrome120",
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Google Chrome";v="120", "Chromium";v="120", "Not_A Brand";v="24"',
        "piattaforma": '"Linux"',
    },
]

# Frammenti che compaiono nelle pagine di challenge di Cloudflare e DataDome.
# Se li troviamo in un 200 la risposta non è la pagina dei risultati.
_SPIE_CHALLENGE: tuple[str, ...] = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "cf_chl_opt",
    "challenge-platform",
    "captcha-delivery.com",
    "geo.captcha-delivery",
    "datadome",
    "px-captcha",
    "enable javascript and cookies to continue",
    "access denied",
    "sei stato bloccato",
)


class ClientHTTP:
    """Sessione HTTP con ritmo umano, retry sensati e rilevamento dei blocchi."""

    def __init__(
        self,
        *,
        delay_min: float = 3.0,
        delay_max: float = 8.0,
        timeout: int = 20,
        max_tentativi: int = 3,
        profilo: dict[str, str] | None = None,
        seme: int | None = None,
    ) -> None:
        self._rng = random.Random(seme)
        self.profilo = profilo or self._rng.choice(PROFILI)
        self.delay_min = max(0.0, delay_min)
        self.delay_max = max(self.delay_min, delay_max)
        self.timeout = timeout
        self.max_tentativi = max(1, max_tentativi)
        self.richieste = 0
        # Istante dell'ultima richiesta, per dominio.
        self._ultima_per_host: dict[str, float] = {}

        self.sessione = self._crea_sessione()
        log.info(
            "Client HTTP pronto (backend=%s, profilo=%s)",
            BACKEND, self.profilo["impersonate"],
        )

    def _crea_sessione(self) -> Any:
        """Crea la sessione, con o senza impersonificazione TLS."""
        if BACKEND == "curl_cffi":
            try:
                return _backend.Session(impersonate=self.profilo["impersonate"])
            except Exception as exc:
                # Una versione di curl_cffi che non conosce quel profilo non
                # deve far fallire il run: si ripiega sul default.
                log.warning(
                    "Profilo '%s' non supportato da curl_cffi (%s): uso il default",
                    self.profilo["impersonate"], exc,
                )
                return _backend.Session()
        return _backend.Session()

    # -- intestazioni ------------------------------------------------------

    def intestazioni(
        self,
        *,
        json_atteso: bool = False,
        referer: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """
        Costruisce un set di header completo e coerente con il profilo.

        La distinzione fra navigazione (`document`) e chiamata XHR (`empty`)
        conta: una richiesta a un endpoint /api con i `Sec-Fetch-*` da
        navigazione è una combinazione che un browser non produce mai.
        """
        intestazioni = {
            "User-Agent": self.profilo["user_agent"],
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-ch-ua": self.profilo["sec_ch_ua"],
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self.profilo["piattaforma"],
            "Sec-Fetch-Site": "same-origin" if referer else "none",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if json_atteso:
            intestazioni.update({
                "Accept": "application/json, text/plain, */*",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "X-Requested-With": "XMLHttpRequest",
            })
        else:
            intestazioni.update({
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            })
        if referer:
            intestazioni["Referer"] = referer
        if extra:
            intestazioni.update(extra)
        return intestazioni

    # -- ritmo -------------------------------------------------------------

    def pausa(self, motivo: str = "") -> None:
        """
        Attesa esplicita, indipendente dal dominio.

        Non serve più fra una piattaforma e l'altra — se ne occupa il ritmo
        per sito — ma resta utile quando si vuole rallentare di proposito,
        per esempio dopo una risposta sospetta.
        """
        attesa = self._rng.uniform(self.delay_min, self.delay_max)
        if motivo:
            log.debug("Pausa di %.1fs (%s)", attesa, motivo)
        time.sleep(attesa)

    @staticmethod
    def _host(url: str) -> str:
        return urlsplit(url).netloc.lower()

    def _rispetta_ritmo(self, url: str) -> None:
        """Distanzia di 3-8 secondi le richieste allo stesso dominio. La prima
        verso un dominio parte subito: non c'è nulla da cui distanziarla."""
        host = self._host(url)
        ultima = self._ultima_per_host.get(host)
        if ultima is None:
            return
        trascorso = time.monotonic() - ultima
        attesa = self._rng.uniform(self.delay_min, self.delay_max) - trascorso
        if attesa > 0:
            log.debug("Attendo %.1fs prima della prossima richiesta a %s", attesa, host)
            time.sleep(attesa)

    # -- richieste ---------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_atteso: bool = False,
        referer: str | None = None,
        consenti_404: bool = False,
    ) -> Any:
        """
        GET con ritmo, retry sugli errori transitori e rilevamento dei blocchi.

        Solleva `ScraperBloccato` su 403/429/challenge e `ScraperError` sugli
        altri fallimenti definitivi.
        """
        intestazioni = headers or self.intestazioni(json_atteso=json_atteso, referer=referer)
        ultimo_errore: str = "errore sconosciuto"

        for tentativo in range(1, self.max_tentativi + 1):
            self._rispetta_ritmo(url)
            self.richieste += 1
            self._ultima_per_host[self._host(url)] = time.monotonic()

            try:
                risposta = self.sessione.get(
                    url, params=params, headers=intestazioni, timeout=self.timeout
                )
            except Exception as exc:  # rete: timeout, DNS, reset di connessione
                ultimo_errore = f"errore di rete: {type(exc).__name__}: {exc}"
                log.warning("GET %s tentativo %d/%d — %s", url, tentativo, self.max_tentativi, ultimo_errore)
                if tentativo < self.max_tentativi:
                    time.sleep(2 ** tentativo + self._rng.uniform(0, 1))
                continue

            stato = risposta.status_code

            if stato == 429:
                attesa = self._retry_after(risposta.headers)
                log.warning("HTTP 429 da %s: attendo %.0fs e riprovo una sola volta", url, attesa)
                time.sleep(attesa)
                if tentativo < self.max_tentativi:
                    continue
                raise ScraperBloccato(f"429 Too Many Requests su {url} (rate limit persistente)")

            if stato in (401, 403):
                raise ScraperBloccato(f"HTTP {stato} su {url}: richiesta rifiutata")

            if stato >= 500:
                ultimo_errore = f"HTTP {stato}"
                log.warning("GET %s tentativo %d/%d — %s", url, tentativo, self.max_tentativi, ultimo_errore)
                if tentativo < self.max_tentativi:
                    time.sleep(2 ** tentativo + self._rng.uniform(0, 1))
                continue

            if stato == 404 and not consenti_404:
                raise ScraperError(f"HTTP 404 su {url}: URL o parametri non più validi")

            if stato >= 400 and not (stato == 404 and consenti_404):
                raise ScraperError(f"HTTP {stato} su {url}")

            if self._sembra_challenge(risposta):
                raise ScraperBloccato(
                    f"Pagina di challenge anti-bot ricevuta da {url} "
                    "(risposta 200 ma contenuto non utilizzabile)"
                )

            return risposta

        raise ScraperError(f"GET {url} fallita dopo {self.max_tentativi} tentativi: {ultimo_errore}")

    def post(
        self,
        url: str,
        *,
        data: Any = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> Any:
        """POST senza retry: la usiamo solo per l'OAuth di eBay."""
        self._rispetta_ritmo(url)
        self.richieste += 1
        self._ultima_per_host[self._host(url)] = time.monotonic()
        try:
            return self.sessione.post(
                url,
                data=data,
                json=json_body,
                headers=headers or self.intestazioni(json_atteso=True),
                auth=auth,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise ScraperError(f"POST {url} fallita: {type(exc).__name__}: {exc}") from exc

    # -- rilevamento -------------------------------------------------------

    @staticmethod
    def _retry_after(intestazioni: Any) -> float:
        """Legge Retry-After, con un tetto per non bloccare il run per minuti."""
        try:
            valore = float(intestazioni.get("Retry-After", "") or 0)
        except (TypeError, ValueError):
            valore = 0.0
        return min(max(valore, 10.0), 45.0)

    @staticmethod
    def _sembra_challenge(risposta: Any) -> bool:
        """Riconosce le pagine di verifica servite con codice 200. Guarda solo
        i primi 4000 caratteri di risposte HTML, per non scambiare per
        challenge una pagina di risultati che cita "captcha" più in basso."""
        tipo = str(risposta.headers.get("Content-Type", "")).lower()
        if "html" not in tipo:
            return False
        try:
            inizio = risposta.text[:4000].lower()
        except Exception:
            return False
        return any(spia in inizio for spia in _SPIE_CHALLENGE)

    # -- ciclo di vita -----------------------------------------------------

    def chiudi(self) -> None:
        try:
            self.sessione.close()
        except Exception:
            pass

    def __enter__(self) -> "ClientHTTP":
        return self

    def __exit__(self, *_: object) -> None:
        self.chiudi()


def profili_disponibili() -> Iterable[str]:
    """Elenco dei profili, usato nei log diagnostici."""
    return (p["impersonate"] for p in PROFILI)
