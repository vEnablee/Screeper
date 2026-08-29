"""
Gist privato usato come archivio di stato: GitHub Actions è stateless, fra un
run e il successivo non sopravvive nulla.

Formato: JSON -> gzip -> base64. La compressione serve, il rapporto è 8-12:1
su questi dati.

Il GITHUB_TOKEN automatico di Actions NON ha lo scope `gist`: serve un
Personal Access Token *classico* con il solo scope `gist`.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import random
import time
from typing import Any

import requests

log = logging.getLogger("monitor.gist")

NOME_FILE_STATO = "stato_monitor.json.gz.b64"
API = "https://api.github.com"

# Soglia oltre la quale avvisiamo: il contenuto restituito dall'API viene
# troncato a 1 MB, quindi conviene restare comodamente sotto.
LIMITE_AVVISO_BYTES = 900_000


class GistError(Exception):
    """Errore nell'accesso al Gist di stato."""


class GistNonTrovato(GistError):
    """Il GIST_ID configurato non esiste o non è accessibile dal token."""


def comprimi(stato: dict[str, Any]) -> str:
    """JSON -> gzip -> base64. `mtime=0` rende l'output riproducibile."""
    grezzo = json.dumps(stato, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compresso = gzip.compress(grezzo, compresslevel=9, mtime=0)
    return base64.b64encode(compresso).decode("ascii")


def decomprimi(contenuto: str) -> dict[str, Any]:
    """Inverso di `comprimi`, con supporto al JSON in chiaro come ripiego."""
    testo = (contenuto or "").strip()
    if not testo:
        return {}
    # Un file salvato a mano potrebbe essere JSON non compresso: lo accettiamo.
    if testo.startswith("{"):
        return json.loads(testo)
    try:
        compresso = base64.b64decode(testo, validate=False)
        grezzo = gzip.decompress(compresso)
    except Exception as exc:
        raise GistError(f"Contenuto del Gist non decodificabile: {exc}") from exc
    return json.loads(grezzo.decode("utf-8"))


class GistClient:
    """Accesso in lettura/scrittura al Gist di stato, con retry."""

    def __init__(
        self,
        token: str,
        gist_id: str | None = None,
        *,
        nome_file: str = NOME_FILE_STATO,
        timeout: int = 30,
        max_tentativi: int = 4,
    ) -> None:
        if not token:
            raise GistError("GIST_TOKEN mancante: impossibile accedere al Gist di stato")
        self._token = token
        self.gist_id = (gist_id or "").strip() or None
        self._nome_file = nome_file
        self._timeout = timeout
        self._max_tentativi = max_tentativi
        self._sessione = requests.Session()
        self._sessione.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "screeper",
        })

    # -- livello di trasporto ---------------------------------------------

    def _richiesta(self, metodo: str, url: str, **kwargs: Any) -> requests.Response:
        """Esegue la richiesta con backoff esponenziale su errori transitori."""
        ultimo_errore: Exception | None = None
        for tentativo in range(1, self._max_tentativi + 1):
            try:
                risposta = self._sessione.request(
                    metodo, url, timeout=self._timeout, **kwargs
                )
            except requests.RequestException as exc:
                ultimo_errore = exc
                risposta = None  # type: ignore[assignment]
            else:
                if risposta.status_code == 404:
                    raise GistNonTrovato(
                        f"Gist non trovato o non accessibile: {url} "
                        "(verifica GIST_ID e che GIST_TOKEN abbia lo scope 'gist')"
                    )
                if risposta.status_code == 401:
                    raise GistError(
                        "GIST_TOKEN rifiutato (401): il token è scaduto o revocato"
                    )
                if risposta.status_code == 403 and "rate limit" not in risposta.text.lower():
                    raise GistError(
                        "Accesso al Gist negato (403): il token non ha lo scope 'gist'. "
                        "Serve un PAT *classico*; i token fine-grained non gestiscono i Gist."
                    )
                if risposta.status_code < 400:
                    return risposta
                ultimo_errore = GistError(
                    f"HTTP {risposta.status_code}: {risposta.text[:300]}"
                )

            if tentativo < self._max_tentativi:
                attesa = (2 ** tentativo) + random.uniform(0, 1.5)
                log.warning(
                    "Gist: tentativo %d/%d fallito (%s); riprovo fra %.1fs",
                    tentativo, self._max_tentativi, ultimo_errore, attesa,
                )
                time.sleep(attesa)

        raise GistError(f"Accesso al Gist fallito dopo {self._max_tentativi} tentativi: {ultimo_errore}")

    # -- operazioni --------------------------------------------------------

    def leggi(self) -> dict[str, Any]:
        """
        Scarica e decodifica lo stato. Restituisce {} se il Gist esiste ma non
        contiene ancora il file di stato (situazione normale al primo run).
        """
        if not self.gist_id:
            return {}

        risposta = self._richiesta("GET", f"{API}/gists/{self.gist_id}")
        dati = risposta.json()
        file_stato = (dati.get("files") or {}).get(self._nome_file)
        if not file_stato:
            log.info("Gist %s presente ma senza file di stato: parto da zero", self.gist_id)
            return {}

        contenuto = file_stato.get("content") or ""
        # Oltre 1 MB l'API tronca `content`: il testo integrale va preso da raw_url.
        if file_stato.get("truncated") and file_stato.get("raw_url"):
            log.warning("Contenuto del Gist troncato dall'API: scarico da raw_url")
            grezzo = self._richiesta("GET", file_stato["raw_url"])
            contenuto = grezzo.text

        stato = decomprimi(contenuto)
        log.info(
            "Stato letto dal Gist: %d annunci visti, %d in storico",
            len(stato.get("visti") or {}),
            len(stato.get("storico") or []),
        )
        return stato

    def scrivi(self, stato: dict[str, Any], descrizione: str | None = None) -> None:
        """Aggiorna il file di stato nel Gist esistente."""
        if not self.gist_id:
            raise GistError("scrivi() richiede un gist_id: usare crea() o assicura()")

        contenuto = comprimi(stato)
        self._avvisa_se_grande(contenuto)

        corpo: dict[str, Any] = {"files": {self._nome_file: {"content": contenuto}}}
        if descrizione:
            corpo["description"] = descrizione[:250]

        self._richiesta("PATCH", f"{API}/gists/{self.gist_id}", json=corpo)
        log.info("Stato salvato sul Gist %s (%d KB compressi)", self.gist_id, len(contenuto) // 1024)

    def crea(self, stato: dict[str, Any], descrizione: str = "Stato SCreeper") -> str:
        """Crea un Gist PRIVATO nuovo e restituisce il suo id."""
        contenuto = comprimi(stato)
        self._avvisa_se_grande(contenuto)
        risposta = self._richiesta(
            "POST",
            f"{API}/gists",
            json={
                "description": descrizione,
                "public": False,
                "files": {self._nome_file: {"content": contenuto}},
            },
        )
        self.gist_id = risposta.json()["id"]
        log.warning(
            "Creato un nuovo Gist privato con id %s — "
            "salvalo subito nel secret GIST_ID (GitHub e Streamlit), "
            "altrimenti al prossimo run lo stato ripartirà da zero.",
            self.gist_id,
        )
        return str(self.gist_id)

    def assicura(self, stato_iniziale: dict[str, Any]) -> str:
        """Restituisce l'id del Gist, creandolo se GIST_ID non è configurato."""
        if self.gist_id:
            return self.gist_id
        return self.crea(stato_iniziale)

    @staticmethod
    def _avvisa_se_grande(contenuto: str) -> None:
        dimensione = len(contenuto.encode("ascii"))
        if dimensione > LIMITE_AVVISO_BYTES:
            log.warning(
                "Lo stato compresso pesa %d KB, vicino al limite gestibile del Gist. "
                "Riduci 'storico_giorni' o 'storico_max_annunci' in config.yaml.",
                dimensione // 1024,
            )
