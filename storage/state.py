"""
Stato persistente: annunci visti, storico, statistiche, salute delle
piattaforme, offset Telegram.

Salvataggio a due livelli. `main.py` scrive sempre su file locale dentro un
`finally`, poi prova a caricare sul Gist e lascia un marcatore. Lo step
`if: always()` del workflow esegue `python -m storage.state --upload`, che
carica solo se quel marcatore non c'è: se il processo principale muore, lo
stato si salva comunque.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from models import Annuncio, EsitoScraper, Impostazioni, Ricerca, adesso_utc
from utils.dates import fuso

log = logging.getLogger("monitor.stato")

VERSIONE_STATO = 1
CARTELLA_LOCALE = Path(".state")
PERCORSO_LOCALE = CARTELLA_LOCALE / "stato.json"
MARCATORE_CARICATO = CARTELLA_LOCALE / "caricato_sul_gist"
# Scritto da main.py in modalità --dry-run: dice allo step `if: always()`
# del workflow di NON caricare nulla. Senza questo, una prova a vuoto
# finirebbe comunque per scrivere sul Gist.
MARCATORE_NO_UPLOAD = CARTELLA_LOCALE / "no_upload"

# Le statistiche giornaliere sono minuscole: le teniamo più a lungo dello storico.
GIORNI_STATISTICHE = 120


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _da_iso(valore: Any) -> datetime | None:
    """Parsing tollerante: qualunque valore illeggibile diventa None."""
    if not valore:
        return None
    try:
        dt = datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Stato:
    """Wrapper tipizzato sul dizionario dello stato."""

    def __init__(self, dati: dict[str, Any] | None = None) -> None:
        self.dati: dict[str, Any] = dati if isinstance(dati, dict) else {}
        self._normalizza()

    # -- inizializzazione --------------------------------------------------

    def _normalizza(self) -> None:
        """Garantisce la presenza di ogni sezione, anche su stati vecchi o vuoti."""
        d = self.dati
        d.setdefault("versione", VERSIONE_STATO)
        d.setdefault("creato_il", adesso_utc().isoformat())
        d.setdefault("aggiornato_il", None)
        d.setdefault("ultimo_run", {})
        d.setdefault("ricerche", {})
        d.setdefault("visti", {})
        d.setdefault("fingerprint", {})
        d.setdefault("storico", [])
        d.setdefault("piattaforme", {})
        d.setdefault("statistiche", {"per_giorno": {}})
        d.setdefault("telegram", {"ultimo_update_id": 0})
        d.setdefault("heartbeat", {"ultimo_giorno": None})
        # Nomi delle ricerche sospese dall'ultimo /stop: permette a
        # /riprendi di riattivare solo quelle, lasciando in pausa ciò che
        # era stato sospeso singolarmente con /pause.
        d.setdefault("sospese_da_stop", [])
        # Impronta del menu comandi registrato su Telegram: serve a
        # ri-registrarlo solo quando cambia davvero, invece che a ogni run.
        d.setdefault("impronta_comandi", None)
        # Id del messaggio di stato su Telegram, riscritto a ogni controllo
        # invece di mandarne uno nuovo.
        d.setdefault("messaggio_stato_id", None)
        if not isinstance(d["statistiche"].get("per_giorno"), dict):
            d["statistiche"]["per_giorno"] = {}

    @classmethod
    def nuovo(cls) -> "Stato":
        return cls({})

    @property
    def vuoto(self) -> bool:
        """True al primissimo avvio in assoluto (nessun annuncio mai visto)."""
        return not self.dati["visti"] and not self.dati["ricerche"]

    # -- deduplicazione ----------------------------------------------------

    def ha_visto(self, annuncio: Annuncio) -> bool:
        return annuncio.chiave in self.dati["visti"]

    def fingerprint_noto(self, annuncio: Annuncio) -> bool:
        return annuncio.fingerprint in self.dati["fingerprint"]

    def marca_visto(self, annuncio: Annuncio) -> None:
        """Registra l'annuncio come già incontrato, con l'istante corrente."""
        adesso = adesso_utc().isoformat()
        self.dati["visti"][annuncio.chiave] = adesso
        self.dati["fingerprint"][annuncio.fingerprint] = adesso

    # -- storico e statistiche --------------------------------------------

    def aggiungi_storico(self, annuncio: Annuncio) -> None:
        self.dati["storico"].append(annuncio.to_dict())

    def storico(self) -> list[Annuncio]:
        """Storico ricostruito in oggetti, scartando le voci illeggibili."""
        risultato: list[Annuncio] = []
        for voce in self.dati.get("storico") or []:
            try:
                risultato.append(Annuncio.from_dict(voce))
            except Exception:
                continue
        return risultato

    def registra_statistica(self, annuncio: Annuncio, tz_locale: str) -> None:
        """Incrementa i contatori del giorno locale in cui l'annuncio è stato notificato."""
        giorno = adesso_utc().astimezone(fuso(tz_locale)).strftime("%Y-%m-%d")
        per_giorno = self.dati["statistiche"]["per_giorno"]
        voce = per_giorno.setdefault(
            giorno, {"totale": 0, "per_piattaforma": {}, "per_ricerca": {}}
        )
        voce["totale"] = int(voce.get("totale", 0)) + 1
        voce["per_piattaforma"][annuncio.piattaforma] = (
            int(voce["per_piattaforma"].get(annuncio.piattaforma, 0)) + 1
        )
        if annuncio.ricerca:
            voce["per_ricerca"][annuncio.ricerca] = (
                int(voce["per_ricerca"].get(annuncio.ricerca, 0)) + 1
            )

    # -- pianificazione delle ricerche ------------------------------------

    def _voce_ricerca(self, nome: str) -> dict[str, Any]:
        return self.dati["ricerche"].setdefault(
            nome, {"ultima_esecuzione": None, "ultimo_nuovo": None, "totale_notificati": 0}
        )

    def primo_avvio(self, nome: str) -> bool:
        """True se questa ricerca non è mai stata eseguita prima."""
        return _da_iso(self.dati["ricerche"].get(nome, {}).get("ultima_esecuzione")) is None

    def da_eseguire(self, ricerca: Ricerca, adesso: datetime | None = None) -> bool:
        """
        Applica `intervallo_minuti` SOPRA il cron del workflow: la ricerca gira
        solo se è passato abbastanza tempo dalla sua ultima esecuzione.
        """
        ultima = _da_iso(self.dati["ricerche"].get(ricerca.nome, {}).get("ultima_esecuzione"))
        if ultima is None:
            return True
        trascorsi = (adesso or adesso_utc()) - ultima
        # Tolleranza di 30s: il cron non è mai puntuale al secondo e senza
        # margine una ricerca a 15' verrebbe saltata un run sì e uno no.
        return trascorsi >= timedelta(minutes=ricerca.intervallo_minuti) - timedelta(seconds=30)

    def registra_esecuzione(self, nome: str, notificati: int = 0) -> None:
        voce = self._voce_ricerca(nome)
        adesso = adesso_utc().isoformat()
        voce["ultima_esecuzione"] = adesso
        if notificati:
            voce["ultimo_nuovo"] = adesso
            voce["totale_notificati"] = int(voce.get("totale_notificati", 0)) + notificati

    def ultima_esecuzione(self, nome: str) -> datetime | None:
        return _da_iso(self.dati["ricerche"].get(nome, {}).get("ultima_esecuzione"))

    # -- salute delle piattaforme -----------------------------------------

    def _voce_piattaforma(self, piattaforma: str) -> dict[str, Any]:
        return self.dati["piattaforme"].setdefault(
            piattaforma,
            {
                "run_zero_consecutivi": 0,
                "alert_inviato": False,
                "quarantena_run": 0,
                "ultimo_esito": None,
                "ultimo_errore": None,
                "ultimo_ok": None,
            },
        )

    def in_quarantena(self, piattaforma: str) -> bool:
        """True se la piattaforma deve essere saltata in questo run."""
        return int(self._voce_piattaforma(piattaforma).get("quarantena_run", 0)) > 0

    def consuma_quarantena(self, piattaforma: str) -> None:
        """Scala di uno il contatore di quarantena. Da chiamare una volta per run."""
        voce = self._voce_piattaforma(piattaforma)
        rimanenti = int(voce.get("quarantena_run", 0))
        if rimanenti > 0:
            voce["quarantena_run"] = rimanenti - 1

    def registra_esito(
        self,
        piattaforma: str,
        esito: EsitoScraper,
        *,
        risultati: int = 0,
        errore: str | None = None,
        impostazioni: Impostazioni | None = None,
    ) -> None:
        """Aggiorna i contatori di salute di una piattaforma dopo una chiamata."""
        voce = self._voce_piattaforma(piattaforma)
        voce["ultimo_esito"] = esito.value
        voce["ultimo_errore"] = errore

        if esito is EsitoScraper.OK and risultati > 0:
            voce["run_zero_consecutivi"] = 0
            voce["alert_inviato"] = False
            voce["ultimo_ok"] = adesso_utc().isoformat()
        elif esito in (EsitoScraper.VUOTO, EsitoScraper.ERRORE, EsitoScraper.BLOCCATO):
            voce["run_zero_consecutivi"] = int(voce.get("run_zero_consecutivi", 0)) + 1

        if esito is EsitoScraper.BLOCCATO and impostazioni is not None:
            voce["quarantena_run"] = max(
                int(voce.get("quarantena_run", 0)), impostazioni.run_pausa_dopo_blocco
            )

    def alert_da_inviare(self, piattaforma: str, soglia: int) -> bool:
        """
        True quando lo scraper è a zero risultati da `soglia` run consecutivi e
        l'alert non è ancora stato mandato. Volutamente una sola volta.
        """
        voce = self._voce_piattaforma(piattaforma)
        return (
            int(voce.get("run_zero_consecutivi", 0)) >= soglia
            and not voce.get("alert_inviato", False)
        )

    def marca_alert_inviato(self, piattaforma: str) -> None:
        self._voce_piattaforma(piattaforma)["alert_inviato"] = True

    def salute_piattaforme(self) -> dict[str, dict[str, Any]]:
        return dict(self.dati["piattaforme"])

    # -- Telegram e heartbeat ---------------------------------------------

    @property
    def ultimo_update_id(self) -> int:
        try:
            return int(self.dati["telegram"].get("ultimo_update_id") or 0)
        except (TypeError, ValueError):
            return 0

    @ultimo_update_id.setter
    def ultimo_update_id(self, valore: int) -> None:
        self.dati["telegram"]["ultimo_update_id"] = int(valore)

    def heartbeat_dovuto(self, impostazioni: Impostazioni, adesso: datetime | None = None) -> bool:
        """True una sola volta al giorno, a partire dall'ora configurata."""
        if not impostazioni.heartbeat_giornaliero:
            return False
        locale = (adesso or adesso_utc()).astimezone(fuso(impostazioni.timezone))
        if locale.hour < impostazioni.heartbeat_ora:
            return False
        return self.dati["heartbeat"].get("ultimo_giorno") != locale.strftime("%Y-%m-%d")

    def marca_heartbeat(self, impostazioni: Impostazioni, adesso: datetime | None = None) -> None:
        locale = (adesso or adesso_utc()).astimezone(fuso(impostazioni.timezone))
        self.dati["heartbeat"]["ultimo_giorno"] = locale.strftime("%Y-%m-%d")

    # -- pausa globale (/stop e /riprendi) --------------------------------

    @property
    def sospese_da_stop(self) -> list[str]:
        valore = self.dati.get("sospese_da_stop")
        return [str(n) for n in valore] if isinstance(valore, list) else []

    @sospese_da_stop.setter
    def sospese_da_stop(self, nomi: list[str]) -> None:
        self.dati["sospese_da_stop"] = list(nomi)

    # -- messaggio di stato riscritto in place ----------------------------

    @property
    def messaggio_stato_id(self) -> int | None:
        valore = self.dati.get("messaggio_stato_id")
        try:
            return int(valore) if valore else None
        except (TypeError, ValueError):
            return None

    @messaggio_stato_id.setter
    def messaggio_stato_id(self, valore: int | None) -> None:
        self.dati["messaggio_stato_id"] = valore

    # -- menu dei comandi Telegram ----------------------------------------

    def comandi_da_registrare(self, impronta: str) -> bool:
        return self.dati.get("impronta_comandi") != impronta

    def marca_comandi_registrati(self, impronta: str) -> None:
        self.dati["impronta_comandi"] = impronta

    # -- riepilogo del run -------------------------------------------------

    def registra_run(
        self,
        *,
        iniziato: datetime,
        terminato: datetime,
        nuovi: int,
        notificati: int,
        richieste: int,
        errori: Iterable[str],
    ) -> None:
        elenco_errori = list(errori)
        self.dati["ultimo_run"] = {
            "iniziato": _iso(iniziato),
            "terminato": _iso(terminato),
            "durata_s": round((terminato - iniziato).total_seconds(), 1),
            "nuovi": nuovi,
            "notificati": notificati,
            "richieste": richieste,
            "errori": elenco_errori[:20],
            "esito": "ok" if not elenco_errori else "parziale",
        }
        self.dati["aggiornato_il"] = terminato.isoformat()

    @property
    def ultimo_run(self) -> dict[str, Any]:
        return dict(self.dati.get("ultimo_run") or {})

    # -- potatura ----------------------------------------------------------

    def pota_piattaforme(self, in_uso: set[str]) -> list[str]:
        """
        Dimentica le piattaforme che nessuna ricerca usa più.

        Senza questo, una piattaforma tolta dalla configurazione resta nello
        stato per sempre con il suo ultimo esito — tipicamente "bloccato" o
        "in quarantena" — e continua a comparire in /status, nell'heartbeat,
        nel messaggio di controllo e nella dashboard, facendo credere a un
        guasto che non esiste più.

        Restituisce i nomi rimossi, per il log.
        """
        if not in_uso:
            return []
        presenti = set(self.dati.get("piattaforme") or {})
        obsolete = sorted(presenti - in_uso)
        for nome in obsolete:
            self.dati["piattaforme"].pop(nome, None)
        return obsolete

    def pota_ricerche(self, in_uso: set[str]) -> dict[str, int]:
        """
        Dimentica le ricerche che non esistono più nella configurazione.

        Serve perché eliminare una ricerca abbia un effetto visibile: senza
        questo, i suoi annunci resterebbero nell'archivio per trenta giorni e
        continuerebbero a comparire nella dashboard, con una scheda dedicata a
        qualcosa che non si cerca più.

        Tocca storico, statistiche e stato di esecuzione. Le chiavi degli
        annunci già visti NON vengono rimosse: se un giorno ricrei la stessa
        ricerca, ricordarli evita di rinotificare mezzo marketplace.
        """
        if not in_uso:
            return {}

        rimossi = {"storico": 0, "ricerche": 0, "statistiche": 0}

        storico = self.dati.get("storico") or []
        tenuti = [v for v in storico if (v.get("ricerca") or "") in in_uso or not v.get("ricerca")]
        rimossi["storico"] = len(storico) - len(tenuti)
        self.dati["storico"] = tenuti

        note = set(self.dati.get("ricerche") or {})
        for nome in note - in_uso:
            self.dati["ricerche"].pop(nome, None)
            rimossi["ricerche"] += 1

        for giorno in (self.dati["statistiche"].get("per_giorno") or {}).values():
            per_ricerca = giorno.get("per_ricerca") or {}
            for nome in set(per_ricerca) - in_uso:
                per_ricerca.pop(nome, None)
                rimossi["statistiche"] += 1

        return {k: v for k, v in rimossi.items() if v}

    def pota(self, impostazioni: Impostazioni) -> dict[str, int]:
        """
        Rimuove i dati troppo vecchi per stare nei limiti del Gist.
        Applica ENTRAMBI i vincoli: finestra temporale e numero massimo.
        Restituisce quanti elementi sono stati rimossi, per il log.
        """
        adesso = adesso_utc()
        limite = adesso - timedelta(days=impostazioni.storico_giorni)
        rimossi = {"storico": 0, "visti": 0, "fingerprint": 0, "statistiche": 0}

        # 1) Storico: prima per data, poi per numero massimo (tengo i più recenti).
        storico = self.dati.get("storico") or []
        recenti = []
        for voce in storico:
            data = _da_iso(voce.get("data_avvistamento")) or _da_iso(voce.get("data_pubblicazione"))
            if data is None or data >= limite:
                recenti.append(voce)
        rimossi["storico"] += len(storico) - len(recenti)

        if len(recenti) > impostazioni.storico_max_annunci:
            recenti.sort(
                key=lambda v: str(v.get("data_avvistamento") or ""), reverse=True
            )
            rimossi["storico"] += len(recenti) - impostazioni.storico_max_annunci
            recenti = recenti[: impostazioni.storico_max_annunci]
        self.dati["storico"] = recenti

        # 2) Chiavi "viste": stessa finestra temporale. Un annuncio più vecchio
        #    della retention non è più in giro sulle prime pagine, quindi
        #    dimenticarlo non provoca rinotifiche.
        for sezione in ("visti", "fingerprint"):
            mappa: dict[str, Any] = self.dati.get(sezione) or {}
            tenuti = {
                chiave: valore
                for chiave, valore in mappa.items()
                if (_da_iso(valore) or adesso) >= limite
            }
            rimossi[sezione] = len(mappa) - len(tenuti)
            self.dati[sezione] = tenuti

        # 3) Statistiche giornaliere.
        soglia_stat = (adesso - timedelta(days=GIORNI_STATISTICHE)).strftime("%Y-%m-%d")
        per_giorno: dict[str, Any] = self.dati["statistiche"]["per_giorno"]
        tenute = {g: v for g, v in per_giorno.items() if g >= soglia_stat}
        rimossi["statistiche"] = len(per_giorno) - len(tenute)
        self.dati["statistiche"]["per_giorno"] = tenute

        if any(rimossi.values()):
            log.info(
                "Potatura stato: storico -%d, visti -%d, fingerprint -%d, statistiche -%d",
                rimossi["storico"], rimossi["visti"],
                rimossi["fingerprint"], rimossi["statistiche"],
            )
        return rimossi

    # -- serializzazione ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return self.dati


# ---------------------------------------------------------------------------
# Persistenza locale (ponte fra main.py e lo step `if: always()` del workflow)
# ---------------------------------------------------------------------------

def salva_locale(stato: Stato, percorso: Path = PERCORSO_LOCALE) -> None:
    """Scrive lo stato su disco. Non solleva: è chiamato dentro un `finally`."""
    try:
        percorso.parent.mkdir(parents=True, exist_ok=True)
        percorso.write_text(
            json.dumps(stato.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        log.error("Impossibile scrivere lo stato locale in %s: %s", percorso, exc)


def carica_locale(percorso: Path = PERCORSO_LOCALE) -> Stato | None:
    """Rilegge lo stato dal file locale, o None se assente/illeggibile."""
    if not percorso.is_file():
        return None
    try:
        return Stato(json.loads(percorso.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Stato locale illeggibile (%s): lo ignoro", exc)
        return None


def marca_caricato() -> None:
    """Segnala che lo stato è già finito sul Gist: lo step finale non deve rifarlo."""
    try:
        MARCATORE_CARICATO.parent.mkdir(parents=True, exist_ok=True)
        MARCATORE_CARICATO.write_text(adesso_utc().isoformat(), encoding="utf-8")
    except OSError:
        pass


def gia_caricato() -> bool:
    return MARCATORE_CARICATO.is_file()


def marca_da_non_caricare() -> None:
    """Vieta allo step di recupero di caricare lo stato (usato da --dry-run)."""
    try:
        MARCATORE_NO_UPLOAD.parent.mkdir(parents=True, exist_ok=True)
        MARCATORE_NO_UPLOAD.write_text(adesso_utc().isoformat(), encoding="utf-8")
    except OSError:
        pass


def upload_vietato() -> bool:
    return MARCATORE_NO_UPLOAD.is_file()


# ---------------------------------------------------------------------------
# CLI di rete di sicurezza:  python -m storage.state --upload
# ---------------------------------------------------------------------------

def scrivi_riepilogo_actions(stato: Stato) -> None:
    """
    Scrive il riepilogo del run nella pagina di GitHub Actions.

    `GITHUB_STEP_SUMMARY` punta a un file markdown che GitHub mostra sopra i
    log del job: è il posto giusto per l'esito, molto più comodo che scorrere
    l'output. Fuori da Actions la variabile non esiste e la funzione non fa nulla.
    """
    percorso = os.environ.get("GITHUB_STEP_SUMMARY")
    if not percorso:
        return

    run = stato.ultimo_run
    righe = [
        "### SCreeper",
        "",
        f"- Esito: **{run.get('esito', 'sconosciuto')}**",
        f"- Durata: {run.get('durata_s', 0)} s",
        f"- Richieste HTTP: {run.get('richieste', 0)}",
        f"- Nuovi annunci: {run.get('nuovi', 0)}",
        f"- Notifiche inviate: {run.get('notificati', 0)}",
        f"- Annunci in storico: {len(stato.dati.get('storico') or [])}",
    ]

    salute = stato.salute_piattaforme()
    if salute:
        righe += ["", "| Piattaforma | Esito | Run a vuoto | Quarantena |", "|---|---|---|---|"]
        for piattaforma, voce in sorted(salute.items()):
            righe.append(
                f"| {piattaforma} | {voce.get('ultimo_esito') or 'mai'} "
                f"| {voce.get('run_zero_consecutivi', 0)} "
                f"| {voce.get('quarantena_run', 0)} |"
            )

    errori = run.get("errori") or []
    if errori:
        righe += ["", "<details><summary>Errori</summary>", ""]
        righe += [f"- `{errore}`" for errore in errori]
        righe += ["", "</details>"]

    try:
        with open(percorso, "a", encoding="utf-8") as f:
            f.write("\n".join(righe) + "\n")
    except OSError as exc:
        log.warning("Riepilogo di Actions non scritto: %s", exc)


def _upload() -> int:
    """Carica sul Gist lo stato locale, se non è già stato caricato."""
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-7s %(message)s", stream=sys.stdout
    )

    stato_per_riepilogo = carica_locale()
    if stato_per_riepilogo is not None:
        scrivi_riepilogo_actions(stato_per_riepilogo)

    if upload_vietato():
        log.info("Esecuzione in modalità di prova (--dry-run): non carico nulla sul Gist.")
        return 0

    if gia_caricato():
        log.info("Stato già caricato sul Gist dal processo principale: niente da fare.")
        return 0

    stato = stato_per_riepilogo
    if stato is None:
        log.info("Nessuno stato locale da caricare (il run è forse fallito prima di partire).")
        return 0

    token = os.environ.get("GIST_TOKEN", "")
    gist_id = os.environ.get("GIST_ID", "")
    if not token:
        log.error("GIST_TOKEN assente: impossibile salvare lo stato. Il prossimo run ripartirà da qui.")
        return 1

    # Import ritardato: evita una dipendenza circolare a livello di modulo.
    from gist_client import GistClient, GistError

    try:
        client = GistClient(token, gist_id)
        client.assicura(stato.to_dict())
        client.scrivi(stato.to_dict(), descrizione=descrizione_gist(stato))
        marca_caricato()
        log.info("Stato di recupero caricato sul Gist.")
        return 0
    except GistError as exc:
        log.error("Salvataggio di recupero fallito: %s", exc)
        return 1


def descrizione_gist(stato: Stato) -> str:
    """Descrizione del Gist: si legge a colpo d'occhio dalla UI di GitHub."""
    run = stato.ultimo_run
    return (
        f"SCreeper — ultimo run {run.get('terminato') or '?'} · "
        f"{run.get('notificati', 0)} notificati · "
        f"{len(stato.dati.get('storico') or [])} in storico"
    )


def main(argomenti: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Utilità sullo stato del monitor")
    parser.add_argument(
        "--upload",
        action="store_true",
        help="carica su Gist lo stato locale se non è già stato salvato",
    )
    opzioni = parser.parse_args(argomenti)
    if opzioni.upload:
        return _upload()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
