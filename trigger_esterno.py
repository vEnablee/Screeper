"""
Controllo del trigger esterno (cron-job.org), che è ciò che fa partire il
monitor: lo scheduler di GitHub su questo repository non ha mai funzionato.

L'operazione è asimmetrica. Spegnere il cronjob significa nessun run, e il
bot vive dentro il run: da Telegram non si potrà più riaccendere. La
riaccensione è nella dashboard, che è un'applicazione a sé.

Serve una chiave API: cron-job.org -> Settings -> API.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger("monitor.trigger")

API = "https://api.cron-job.org"
TIMEOUT = 20


class TriggerNonConfigurato(Exception):
    """Chiave API o id del cronjob assenti."""


def _intestazioni(chiave: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {chiave}",
        "Content-Type": "application/json",
        "User-Agent": "screeper",
    }


def stato_job(chiave: str, job_id: str) -> dict[str, Any]:
    """
    Legge lo stato del cronjob.

    Restituisce un dizionario con `attivo`, `titolo`, `prossima_esecuzione`
    (epoch o None) e `ultimo_esito`. Solleva TriggerNonConfigurato se le
    credenziali mancano, RuntimeError se l'API risponde male.
    """
    if not chiave or not job_id:
        raise TriggerNonConfigurato(
            "Servono i secret CRONJOB_API_KEY e CRONJOB_JOB_ID."
        )

    risposta = requests.get(
        f"{API}/jobs/{job_id}", headers=_intestazioni(chiave), timeout=TIMEOUT
    )
    if risposta.status_code == 401:
        raise RuntimeError("Chiave API di cron-job.org rifiutata (401).")
    if risposta.status_code == 404:
        raise RuntimeError(f"Cronjob {job_id} non trovato su cron-job.org.")
    if risposta.status_code != 200:
        raise RuntimeError(f"cron-job.org ha risposto {risposta.status_code}.")

    dettagli = (risposta.json() or {}).get("jobDetails") or {}
    return {
        "attivo": bool(dettagli.get("enabled")),
        "titolo": dettagli.get("title") or "(senza titolo)",
        "prossima_esecuzione": dettagli.get("nextExecution"),
        "ultimo_esito": dettagli.get("lastStatus"),
    }


def imposta_attivo(chiave: str, job_id: str, attivo: bool) -> tuple[bool, str]:
    """
    Accende o spegne il cronjob. Restituisce `(riuscito, messaggio)`.

    Non solleva: è invocata da un comando Telegram, dove un'eccezione
    significherebbe nessuna risposta all'utente.
    """
    if not chiave or not job_id:
        return False, "Servono i secret CRONJOB_API_KEY e CRONJOB_JOB_ID."

    try:
        risposta = requests.patch(
            f"{API}/jobs/{job_id}",
            headers=_intestazioni(chiave),
            json={"job": {"enabled": bool(attivo)}},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        return False, f"cron-job.org non raggiungibile: {exc}"

    if risposta.status_code == 200:
        log.info("Trigger esterno %s", "attivato" if attivo else "disattivato")
        return True, "attivato" if attivo else "disattivato"
    if risposta.status_code == 401:
        return False, "Chiave API rifiutata (401): controlla CRONJOB_API_KEY."
    if risposta.status_code == 404:
        return False, f"Cronjob {job_id} non trovato: controlla CRONJOB_JOB_ID."
    return False, f"cron-job.org ha risposto {risposta.status_code}: {risposta.text[:120]}"
