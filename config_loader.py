"""
Lettura, validazione e modifica di config.yaml.

Si usa `ruamel.yaml` in round-trip invece di PyYAML perché le modifiche fatte
dal bot devono preservare i commenti del file.

Due livelli: `carica_configurazione()` per gli oggetti tipizzati,
`carica_documento()` più le funzioni `modifica_*` per l'albero YAML che il
bot ricommitta con `commit_config()`.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from pathlib import Path
from typing import Any

import requests
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from models import (
    Condizione,
    ConfigSubito,
    Configurazione,
    Impostazioni,
    Piattaforma,
    Ricerca,
)

log = logging.getLogger("monitor.config")

PERCORSO_DEFAULT = "config.yaml"
_NOME_VALIDO = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


class ConfigError(Exception):
    """Configurazione assente, malformata o incoerente."""


def _yaml() -> YAML:
    """Istanza YAML round-trip con formattazione stabile."""
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096          # evita che ruamel spezzi le righe lunghe
    y.indent(mapping=2, sequence=4, offset=2)
    return y


# ---------------------------------------------------------------------------
# Lettura tipizzata
# ---------------------------------------------------------------------------

def carica_documento(percorso: str | Path = PERCORSO_DEFAULT) -> CommentedMap:
    """Carica il YAML preservando commenti e formattazione."""
    p = Path(percorso)
    if not p.is_file():
        raise ConfigError(f"File di configurazione non trovato: {p}")
    try:
        with p.open("r", encoding="utf-8") as f:
            documento = _yaml().load(f)
    except Exception as exc:
        raise ConfigError(f"YAML non valido in {p}: {exc}") from exc
    if not isinstance(documento, dict):
        raise ConfigError(f"{p} deve contenere una mappa YAML al primo livello")
    return documento


def serializza_documento(documento: CommentedMap) -> str:
    """Riporta il documento a testo YAML, commenti inclusi."""
    buffer = io.StringIO()
    _yaml().dump(documento, buffer)
    return buffer.getvalue()


def salva_documento(documento: CommentedMap, percorso: str | Path = PERCORSO_DEFAULT) -> None:
    """Scrive il documento su disco (usato in locale e prima del commit)."""
    Path(percorso).write_text(serializza_documento(documento), encoding="utf-8")


def _intero(valore: Any, default: int, minimo: int | None = None) -> int:
    try:
        n = int(valore)
    except (TypeError, ValueError):
        return default
    if minimo is not None and n < minimo:
        return default
    return n


def _decimale(valore: Any, default: float | None) -> float | None:
    if valore is None or valore == "":
        return default
    try:
        return float(valore)
    except (TypeError, ValueError):
        return default


def _booleano(valore: Any, default: bool) -> bool:
    if isinstance(valore, bool):
        return valore
    if isinstance(valore, str):
        if valore.strip().lower() in {"true", "si", "sì", "yes", "1", "on"}:
            return True
        if valore.strip().lower() in {"false", "no", "0", "off"}:
            return False
    return default


def _impostazioni_da_dict(dati: Any) -> Impostazioni:
    """Costruisce le impostazioni globali, con default per ogni campo assente."""
    d: dict[str, Any] = dict(dati) if isinstance(dati, dict) else {}
    base = Impostazioni()
    impostazioni = Impostazioni(
        timezone=str(d.get("timezone") or base.timezone),
        finestra_primo_avvio_minuti=_intero(
            d.get("finestra_primo_avvio_minuti"), base.finestra_primo_avvio_minuti, 0
        ),
        max_notifiche_per_run=_intero(
            d.get("max_notifiche_per_run"), base.max_notifiche_per_run, 1
        ),
        pagine_per_ricerca=_intero(d.get("pagine_per_ricerca"), base.pagine_per_ricerca, 1),
        pagine_primo_avvio=_intero(d.get("pagine_primo_avvio"), base.pagine_primo_avvio, 1),
        delay_min_secondi=float(_decimale(d.get("delay_min_secondi"), base.delay_min_secondi) or 0.0),
        delay_max_secondi=float(_decimale(d.get("delay_max_secondi"), base.delay_max_secondi) or 0.0),
        timeout_secondi=_intero(d.get("timeout_secondi"), base.timeout_secondi, 5),
        max_tentativi=_intero(d.get("max_tentativi"), base.max_tentativi, 1),
        storico_giorni=_intero(d.get("storico_giorni"), base.storico_giorni, 1),
        storico_max_annunci=_intero(d.get("storico_max_annunci"), base.storico_max_annunci, 50),
        rileva_ripubblicati=_booleano(d.get("rileva_ripubblicati"), base.rileva_ripubblicati),
        run_zero_per_alert=_intero(d.get("run_zero_per_alert"), base.run_zero_per_alert, 1),
        run_pausa_dopo_blocco=_intero(
            d.get("run_pausa_dopo_blocco"), base.run_pausa_dopo_blocco, 0
        ),
        heartbeat_giornaliero=_booleano(
            d.get("heartbeat_giornaliero"), base.heartbeat_giornaliero
        ),
        heartbeat_ora=_intero(d.get("heartbeat_ora"), base.heartbeat_ora, 0),
        notifica_errori=_booleano(d.get("notifica_errori"), base.notifica_errori),
        notifica_ogni_controllo=str(
            d.get("notifica_ogni_controllo") or base.notifica_ogni_controllo
        ).strip().lower(),
    )
    # Coerenza dei delay: se invertiti o negativi si riportano a valori sani.
    if impostazioni.delay_min_secondi < 0:
        impostazioni.delay_min_secondi = 0.0
    if impostazioni.delay_max_secondi < impostazioni.delay_min_secondi:
        impostazioni.delay_max_secondi = impostazioni.delay_min_secondi
    if not 0 <= impostazioni.heartbeat_ora <= 23:
        impostazioni.heartbeat_ora = 9
    if impostazioni.notifica_ogni_controllo not in ("mai", "sempre", "aggiorna"):
        log.warning(
            "notifica_ogni_controllo='%s' non valido: uso 'aggiorna'",
            impostazioni.notifica_ogni_controllo,
        )
        impostazioni.notifica_ogni_controllo = "aggiorna"
    return impostazioni


def _ricerca_da_dict(dati: Any, indice: int) -> Ricerca:
    """Costruisce e valida una singola ricerca."""
    if not isinstance(dati, dict):
        raise ConfigError(f"ricerche[{indice}] deve essere una mappa YAML")

    nome = str(dati.get("nome") or "").strip()
    if not nome:
        raise ConfigError(f"ricerche[{indice}]: campo 'nome' obbligatorio")
    if not set(nome.lower()) <= _NOME_VALIDO:
        raise ConfigError(
            f"ricerca '{nome}': il nome può contenere solo lettere minuscole, "
            "cifre, trattini e underscore (serve come identificatore nei comandi Telegram)"
        )

    parole_chiave = str(dati.get("parole_chiave") or "").strip()
    if not parole_chiave:
        raise ConfigError(f"ricerca '{nome}': campo 'parole_chiave' obbligatorio")

    grezze = dati.get("piattaforme") or []
    if isinstance(grezze, str):
        grezze = [p.strip() for p in grezze.split(",")]
    piattaforme = [str(p).strip().lower() for p in grezze if str(p).strip()]
    if not piattaforme:
        raise ConfigError(f"ricerca '{nome}': indicare almeno una piattaforma")
    ignote = set(piattaforme) - Piattaforma.valide()
    if ignote:
        raise ConfigError(
            f"ricerca '{nome}': piattaforme sconosciute {sorted(ignote)}; "
            f"valide: {sorted(Piattaforma.valide())}"
        )

    condizione = str(dati.get("condizione") or Condizione.QUALSIASI.value).strip().lower()
    if condizione not in Condizione.valide():
        raise ConfigError(
            f"ricerca '{nome}': condizione '{condizione}' non valida; "
            f"valide: {sorted(Condizione.valide())}"
        )

    escluse_grezze = dati.get("parole_escluse") or []
    if isinstance(escluse_grezze, str):
        escluse_grezze = [p.strip() for p in escluse_grezze.split(",")]
    parole_escluse = [str(p).strip() for p in escluse_grezze if str(p).strip()]

    prezzo_min = _decimale(dati.get("prezzo_min"), None)
    prezzo_max = _decimale(dati.get("prezzo_max"), None)
    if prezzo_min is not None and prezzo_max is not None and prezzo_min > prezzo_max:
        raise ConfigError(
            f"ricerca '{nome}': prezzo_min ({prezzo_min}) maggiore di prezzo_max ({prezzo_max})"
        )

    sub = dati.get("subito") or {}
    if not isinstance(sub, dict):
        sub = {}
    config_subito = ConfigSubito(
        zona=str(sub.get("zona") or "italia").strip().lower() or "italia",
        raggio_km=_intero(sub.get("raggio_km"), 0, 0),
        regione_id=_intero(sub["regione_id"], 0, 0) or None if sub.get("regione_id") else None,
        citta_id=_intero(sub["citta_id"], 0, 0) or None if sub.get("citta_id") else None,
    )

    return Ricerca(
        nome=nome,
        parole_chiave=parole_chiave,
        piattaforme=piattaforme,
        attiva=_booleano(dati.get("attiva"), True),
        in_pausa=_booleano(dati.get("in_pausa"), False),
        intervallo_minuti=_intero(dati.get("intervallo_minuti"), 15, 1),
        parole_escluse=parole_escluse,
        prezzo_min=prezzo_min,
        prezzo_max=prezzo_max,
        condizione=condizione,
        solo_titolo=_booleano(dati.get("solo_titolo"), True),
        spedizione_inclusa_richiesta=_booleano(dati.get("spedizione_inclusa_richiesta"), False),
        eta_massima_giorni=(
            _intero(dati["eta_massima_giorni"], 0, 1) or None
            if dati.get("eta_massima_giorni") else None
        ),
        subito=config_subito,
    )


def configurazione_da_documento(documento: CommentedMap) -> Configurazione:
    """Trasforma il documento YAML negli oggetti tipizzati usati dal monitor."""
    impostazioni = _impostazioni_da_dict(documento.get("impostazioni"))

    grezze = documento.get("ricerche")
    if grezze is None:
        grezze = []
    if not isinstance(grezze, (list, CommentedSeq)):
        raise ConfigError("La chiave 'ricerche' deve essere una lista")

    ricerche = [_ricerca_da_dict(r, i) for i, r in enumerate(grezze)]

    nomi = [r.nome for r in ricerche]
    duplicati = {n for n in nomi if nomi.count(n) > 1}
    if duplicati:
        raise ConfigError(f"Nomi di ricerca duplicati: {sorted(duplicati)}")

    return Configurazione(impostazioni=impostazioni, ricerche=ricerche)


def carica_configurazione(percorso: str | Path = PERCORSO_DEFAULT) -> Configurazione:
    """Scorciatoia: carica il file e restituisce la configurazione tipizzata."""
    return configurazione_da_documento(carica_documento(percorso))


# ---------------------------------------------------------------------------
# Modifiche (usate dai comandi del bot Telegram)
# ---------------------------------------------------------------------------

def _lista_ricerche(documento: CommentedMap) -> CommentedSeq:
    """Restituisce la lista delle ricerche, creandola se assente."""
    ricerche = documento.get("ricerche")
    if ricerche is None:
        ricerche = CommentedSeq()
        documento["ricerche"] = ricerche
    return ricerche


def trova_ricerca(documento: CommentedMap, nome: str) -> CommentedMap | None:
    """Cerca una ricerca per nome (confronto case-insensitive)."""
    bersaglio = nome.strip().lower()
    for voce in _lista_ricerche(documento):
        if isinstance(voce, dict) and str(voce.get("nome", "")).strip().lower() == bersaglio:
            return voce
    return None


def modifica_aggiungi_ricerca(documento: CommentedMap, campi: dict[str, Any]) -> str:
    """
    Aggiunge una ricerca al documento. `campi` arriva già normalizzato dal
    parser dei comandi. Solleva ConfigError se il risultato non è valido:
    così una richiesta sbagliata non può corrompere il file.
    """
    nome = str(campi.get("nome", "")).strip().lower()
    if not nome:
        raise ConfigError("manca il campo obbligatorio 'nome'")
    if trova_ricerca(documento, nome) is not None:
        raise ConfigError(f"esiste già una ricerca chiamata '{nome}'")

    voce = CommentedMap()
    voce["nome"] = nome
    voce["attiva"] = True
    voce["in_pausa"] = False
    voce["intervallo_minuti"] = int(campi.get("intervallo_minuti") or 15)
    voce["piattaforme"] = CommentedSeq(campi.get("piattaforme") or ["ebay"])
    voce["piattaforme"].fa.set_flow_style()
    voce["parole_chiave"] = str(campi.get("parole_chiave") or "")
    voce["parole_escluse"] = CommentedSeq(campi.get("parole_escluse") or [])
    voce["prezzo_min"] = campi.get("prezzo_min")
    voce["prezzo_max"] = campi.get("prezzo_max")
    voce["condizione"] = str(campi.get("condizione") or Condizione.QUALSIASI.value)
    voce["solo_titolo"] = bool(campi.get("solo_titolo", True))
    voce["eta_massima_giorni"] = campi.get("eta_massima_giorni")
    voce["spedizione_inclusa_richiesta"] = bool(campi.get("spedizione_inclusa_richiesta", False))

    sotto = CommentedMap()
    sotto["zona"] = str(campi.get("zona") or "italia")
    sotto["raggio_km"] = int(campi.get("raggio_km") or 0)
    voce["subito"] = sotto

    # Validazione a monte della scrittura: se questa fallisce, non tocchiamo nulla.
    _ricerca_da_dict(voce, len(_lista_ricerche(documento)))

    _lista_ricerche(documento).append(voce)
    return nome


def modifica_rimuovi_ricerca(documento: CommentedMap, nome: str) -> bool:
    """Rimuove una ricerca. Restituisce False se non esisteva."""
    ricerche = _lista_ricerche(documento)
    bersaglio = nome.strip().lower()
    for indice, voce in enumerate(ricerche):
        if isinstance(voce, dict) and str(voce.get("nome", "")).strip().lower() == bersaglio:
            del ricerche[indice]
            return True
    return False


def modifica_aggiungi_esclusa(documento: CommentedMap, nome: str, parola: str) -> bool:
    """Aggiunge una parola esclusa. False se la ricerca non esiste."""
    voce = trova_ricerca(documento, nome)
    if voce is None:
        return False
    escluse = voce.get("parole_escluse")
    if not isinstance(escluse, (list, CommentedSeq)):
        escluse = CommentedSeq()
        voce["parole_escluse"] = escluse
    testo = parola.strip()
    esistenti = {str(p).strip().lower() for p in escluse}
    if testo.lower() not in esistenti:
        escluse.append(testo)
    return True


def modifica_pausa_tutte(
    documento: CommentedMap,
    in_pausa: bool,
    solo: set[str] | None = None,
) -> list[str]:
    """
    Sospende o riattiva più ricerche in un colpo solo.

    Tocca soltanto le ricerche che cambiano davvero stato, e restituisce i
    loro nomi: serve a `/stop` per ricordare quali ha sospeso, così che
    `/riprendi` non riattivi anche quelle che erano già in pausa per scelta.

    Le ricerche con `attiva: false` non vengono mai riattivate: quel flag
    indica una disattivazione voluta e a lungo termine.
    """
    cambiate: list[str] = []
    for voce in _lista_ricerche(documento):
        if not isinstance(voce, dict):
            continue
        nome = str(voce.get("nome", "")).strip().lower()
        if solo is not None and nome not in solo:
            continue
        if not _booleano(voce.get("attiva"), True):
            continue
        if _booleano(voce.get("in_pausa"), False) == in_pausa:
            continue
        voce["in_pausa"] = in_pausa
        cambiate.append(nome)
    return cambiate


def modifica_pausa(documento: CommentedMap, nome: str, in_pausa: bool) -> bool:
    """Sospende o riattiva una ricerca. False se non esiste."""
    voce = trova_ricerca(documento, nome)
    if voce is None:
        return False
    voce["in_pausa"] = in_pausa
    return True


# ---------------------------------------------------------------------------
# Commit del config sul repo, via API GitHub
# ---------------------------------------------------------------------------

def leggi_config_da_github(
    *,
    token: str,
    repository: str,
    percorso_repo: str = PERCORSO_DEFAULT,
    branch: str = "main",
    timeout: int = 30,
) -> tuple[str, str]:
    """
    Scarica config.yaml e restituisce `(contenuto, sha)`. Serve alla
    dashboard, che non ha il repo sottomano. Lo `sha` va ripassato a
    `commit_config()`: impedisce di sovrascrivere una modifica del bot.
    """
    if not token or not repository:
        raise ConfigError("Token GitHub o repository non configurati")

    risposta = requests.get(
        f"https://api.github.com/repos/{repository}/contents/{percorso_repo}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "screeper",
        },
        params={"ref": branch},
        timeout=timeout,
    )
    if risposta.status_code == 401:
        raise ConfigError("Token GitHub rifiutato (401): scaduto o revocato")
    if risposta.status_code == 404:
        raise ConfigError(
            f"{percorso_repo} non trovato in {repository}@{branch}. "
            "Su un repository privato il token deve avere lo scope `repo`."
        )
    if risposta.status_code != 200:
        raise ConfigError(f"Lettura fallita: HTTP {risposta.status_code} {risposta.text[:200]}")

    dati = risposta.json()
    try:
        contenuto = base64.b64decode(dati["content"]).decode("utf-8")
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"Contenuto non decodificabile: {exc}") from exc
    return contenuto, str(dati.get("sha", ""))


def documento_da_testo(testo: str) -> CommentedMap:
    """Carica un documento YAML da una stringa, preservando i commenti."""
    try:
        documento = _yaml().load(testo)
    except Exception as exc:
        raise ConfigError(f"YAML non valido: {exc}") from exc
    if not isinstance(documento, dict):
        raise ConfigError("Il documento deve essere una mappa YAML")
    return documento


def commit_config(
    contenuto: str,
    *,
    token: str,
    repository: str,
    messaggio: str,
    percorso_repo: str = PERCORSO_DEFAULT,
    branch: str = "main",
    timeout: int = 30,
    sha_atteso: str | None = None,
) -> bool:
    """
    Scrive `contenuto` in `percorso_repo` sul branch indicato.

    Usa il GITHUB_TOKEN del workflow (serve `permissions: contents: write`).
    Nota utile: i commit fatti con il GITHUB_TOKEN di default NON innescano
    altri workflow, quindi non si crea un ciclo infinito con il trigger `push`.
    """
    if not token or not repository:
        log.warning("Commit del config saltato: GITHUB_TOKEN o GITHUB_REPOSITORY assenti")
        return False

    base = f"https://api.github.com/repos/{repository}/contents/{percorso_repo}"
    intestazioni = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "screeper",
    }

    try:
        # 1) SHA della versione corrente: obbligatorio per un aggiornamento.
        risposta = requests.get(
            base, headers=intestazioni, params={"ref": branch}, timeout=timeout
        )
        sha: str | None = None
        if risposta.status_code == 200:
            sha = risposta.json().get("sha")
            # Se il chiamante sa da quale versione è partito e nel frattempo
            # il file è cambiato, si rifiuta di scrivere: altrimenti una
            # modifica fatta dal bot verrebbe cancellata senza accorgersene.
            if sha_atteso and sha != sha_atteso:
                log.error(
                    "config.yaml è cambiato mentre lo modificavi "
                    "(atteso %s, trovato %s): scrittura annullata",
                    sha_atteso[:8], str(sha)[:8],
                )
                return False
        elif risposta.status_code != 404:
            log.error(
                "Lettura di %s fallita: HTTP %s %s",
                percorso_repo, risposta.status_code, risposta.text[:200],
            )
            return False

        # 2) Scrittura.
        corpo: dict[str, Any] = {
            "message": messaggio,
            "content": base64.b64encode(contenuto.encode("utf-8")).decode("ascii"),
            "branch": branch,
            "committer": {
                "name": "screeper[bot]",
                "email": "screeper@users.noreply.github.com",
            },
        }
        if sha:
            corpo["sha"] = sha

        risposta = requests.put(base, headers=intestazioni, json=corpo, timeout=timeout)
        if risposta.status_code in (200, 201):
            log.info("config.yaml aggiornato su %s@%s", repository, branch)
            return True
        log.error(
            "Commit di %s fallito: HTTP %s %s",
            percorso_repo, risposta.status_code, risposta.text[:300],
        )
        return False
    except requests.RequestException as exc:
        log.error("Commit di %s fallito per errore di rete: %s", percorso_repo, exc)
        return False


def avvia_workflow(
    *,
    token: str,
    repository: str,
    workflow: str = "monitor.yml",
    branch: str = "main",
    inputs: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[bool, str]:
    """
    Avvia il workflow su richiesta, senza aspettare il trigger periodico.
    Richiede un token con permessi sulle Actions.
    """
    if not token or not repository:
        return False, "Token GitHub o repository non configurati"

    try:
        risposta = requests.post(
            f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/dispatches",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "screeper",
            },
            json={"ref": branch, "inputs": inputs or {}},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return False, f"Errore di rete: {exc}"

    if risposta.status_code == 204:
        return True, "Controllo avviato"
    if risposta.status_code == 403:
        return False, (
            "Permesso negato: il token deve avere lo scope `repo` "
            "(un token con il solo scope `gist` non basta)."
        )
    if risposta.status_code == 404:
        return False, (
            f"Workflow '{workflow}' non trovato su {repository}@{branch}, "
            "oppure il token non vede il repository."
        )
    if risposta.status_code == 422:
        return False, (
            "GitHub ha rifiutato la richiesta: il workflow deve avere il "
            "trigger `workflow_dispatch` sul branch di default."
        )
    return False, f"HTTP {risposta.status_code}: {risposta.text[:160]}"


def repository_corrente() -> str:
    """Repository 'owner/nome', da variabile d'ambiente di GitHub Actions."""
    return os.environ.get("GITHUB_REPOSITORY", "").strip()


def branch_corrente() -> str:
    """Branch di default su cui committare (sovrascrivibile con MONITOR_BRANCH)."""
    return os.environ.get("MONITOR_BRANCH", "main").strip() or "main"
