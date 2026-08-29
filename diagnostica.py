#!/usr/bin/env python3
"""
Diagnostica dei segreti e delle connessioni.

Verifica una per una le credenziali configurate e dice **esattamente** cosa
non va, invece di lasciare interpretare un run verde che in realtà ha
ripiegato su un percorso alternativo.

Non stampa mai un segreto: solo la sua presenza, la lunghezza e un prefisso
di poche lettere, sufficienti a distinguere due token diversi senza rivelarli.

Uso:
    python diagnostica.py          # tutti i controlli
    python diagnostica.py --telegram
    python diagnostica.py --gist

Su GitHub Actions: Run workflow -> spunta "diagnostica".
"""

from __future__ import annotations

import argparse
import os

import requests

OK, KO, WARN, INFO = "✅", "❌", "⚠️ ", "ℹ️ "
_problemi: list[str] = []


def titolo(testo: str) -> None:
    print(f"\n{'=' * 68}\n{testo}\n{'=' * 68}")


def esito(ok: bool, messaggio: str, dettaglio: str = "") -> bool:
    print(f"  {OK if ok else KO} {messaggio}")
    if dettaglio:
        for riga in dettaglio.splitlines():
            print(f"       {riga}")
    if not ok:
        _problemi.append(messaggio)
    return ok


def impronta(valore: str) -> str:
    """Descrive un segreto senza rivelarlo."""
    if not valore:
        return "assente"
    return f"presente, {len(valore)} caratteri, inizia con '{valore[:4]}…'"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def controlla_telegram() -> None:
    titolo("TELEGRAM")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    print(f"  {INFO}TELEGRAM_BOT_TOKEN: {impronta(token)}")
    print(f"  {INFO}TELEGRAM_CHAT_ID:   {'assente' if not chat_id else chat_id}")

    if not esito(bool(token), "TELEGRAM_BOT_TOKEN presente"):
        return
    if not esito(bool(chat_id), "TELEGRAM_CHAT_ID presente"):
        return

    # Il formato del token è <numero>:<stringa>. Un errore qui è quasi sempre
    # un copia-incolla parziale o uno spazio finale.
    if ":" not in token:
        esito(False, "Formato del token non valido",
              "Un token Telegram ha la forma 123456789:AAH... — "
              "controlla di averlo copiato per intero.")
        return

    base = f"https://api.telegram.org/bot{token}"

    # 1) Il token è valido?
    try:
        r = requests.get(f"{base}/getMe", timeout=20)
        dati = r.json()
    except Exception as exc:
        esito(False, "Chiamata a getMe fallita", str(exc))
        return

    if not dati.get("ok"):
        esito(False, "Token rifiutato da Telegram",
              f"Risposta: {dati.get('description')}\n"
              "Se hai usato /revoke su @BotFather, il token vecchio è morto: "
              "serve quello nuovo nei secrets.")
        return

    bot = dati["result"]
    esito(True, f"Token valido — bot @{bot.get('username')} (id {bot.get('id')})")

    # 2) Cosa c'è in coda, e da quali chat?
    try:
        r = requests.get(f"{base}/getUpdates", params={"timeout": 0, "limit": 50}, timeout=25)
        dati = r.json()
    except Exception as exc:
        esito(False, "Chiamata a getUpdates fallita", str(exc))
        dati = {}

    aggiornamenti = dati.get("result") or []
    print(f"  {INFO}Messaggi in coda: {len(aggiornamenti)}")

    chat_viste: dict[str, int] = {}
    for agg in aggiornamenti:
        msg = agg.get("message") or {}
        cid = str((msg.get("chat") or {}).get("id") or "")
        if cid:
            chat_viste[cid] = chat_viste.get(cid, 0) + 1
            testo = str(msg.get("text") or "")[:40]
            print(f"       update {agg.get('update_id')} da chat {cid}: {testo!r}")

    if chat_viste and chat_id not in chat_viste:
        esito(False, "Il TELEGRAM_CHAT_ID configurato NON corrisponde",
              f"Configurato: {chat_id}\n"
              f"Chat che hanno scritto al bot: {', '.join(chat_viste)}\n"
              "I comandi da chat non autorizzate vengono ignorati: è questo "
              "il motivo per cui il bot non risponde.")
    elif chat_viste:
        esito(True, f"Il chat_id configurato compare fra i mittenti ({chat_id})")
    elif aggiornamenti:
        print(f"  {WARN}Aggiornamenti presenti ma senza messaggi di testo")
    else:
        print(f"  {WARN}Nessun messaggio in coda.")
        print("       Le cause possibili sono due:")
        print("       a) un run precedente li ha già consumati (normale);")
        print("       b) non hai ancora scritto al bot da quando hai cambiato token.")

    # 3) Il menu dei comandi è registrato? È ciò che fa comparire l'elenco
    #    quando si digita "/" nella chat.
    try:
        r = requests.post(f"{base}/getMyCommands",
                          json={"scope": {"type": "default"}}, timeout=20)
        comandi = (r.json() or {}).get("result") or []
    except Exception:
        comandi = []

    if comandi:
        esito(True, f"Menu dei comandi registrato ({len(comandi)} voci)",
              ", ".join("/" + str(c.get("command")) for c in comandi[:12]))
    else:
        # La diagnostica non si limita a segnalare: registra il menu subito.
        # È un'operazione idempotente e senza effetti collaterali, e risolve
        # il problema in questo istante invece di rimandarlo al prossimo run.
        print(f"  {WARN}Nessun comando registrato: lo registro adesso.")
        from bot.commands import COMANDI_BOT
        payload = {
            "commands": [
                {"command": n.lstrip("/").lower(), "description": d[:256]}
                for n, d in COMANDI_BOT
            ],
            "scope": {"type": "default"},
        }
        try:
            r = requests.post(f"{base}/setMyCommands", json=payload, timeout=20)
            ok = bool((r.json() or {}).get("ok"))
        except Exception as exc:
            ok = False
            print(f"       {exc}")
        esito(ok, f"Menu dei comandi registrato adesso ({len(payload['commands'])} voci)"
              if ok else "Registrazione del menu fallita",
              "Riapri la chat con il bot: il client tiene in cache la lista."
              if ok else "")

    # 4) La prova decisiva: il bot riesce a scriverti?
    try:
        r = requests.post(
            f"{base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "🔧 <b>Diagnostica</b>\nSe leggi questo messaggio, "
                        "token e chat_id sono corretti.",
                "parse_mode": "HTML",
            },
            timeout=20,
        )
        dati = r.json()
    except Exception as exc:
        esito(False, "Invio del messaggio di prova fallito", str(exc))
        return

    if dati.get("ok"):
        esito(True, "Messaggio di prova inviato — controlla Telegram")
        return

    descrizione = str(dati.get("description", ""))
    suggerimento = ""
    if "chat not found" in descrizione.lower():
        suggerimento = (
            "Il chat_id non esiste per questo bot. Apri una chat con il bot e "
            "manda /start, poi rileggi l'id da getUpdates: ogni bot vede un "
            "proprio elenco di chat."
        )
    elif "bot was blocked" in descrizione.lower():
        suggerimento = "Hai bloccato il bot: sbloccalo dalla chat su Telegram."
    elif "bot can't initiate conversation" in descrizione.lower():
        suggerimento = "Devi mandare tu /start al bot: non può scrivere per primo."
    esito(False, f"Telegram ha rifiutato l'invio: {descrizione}", suggerimento)


# ---------------------------------------------------------------------------
# Gist
# ---------------------------------------------------------------------------

def controlla_gist() -> None:
    titolo("GIST")

    token = os.environ.get("GIST_TOKEN", "").strip()
    gist_id = os.environ.get("GIST_ID", "").strip()

    print(f"  {INFO}GIST_TOKEN: {impronta(token)}")
    print(f"  {INFO}GIST_ID:    {'assente' if not gist_id else gist_id[:8] + '…'}")

    if not esito(bool(token), "GIST_TOKEN presente"):
        return
    if not esito(bool(gist_id), "GIST_ID presente"):
        return

    intestazioni = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "screeper-diagnostica",
    }

    # 1) Il token ha davvero lo scope `gist`?
    try:
        r = requests.get("https://api.github.com/user", headers=intestazioni, timeout=20)
    except Exception as exc:
        esito(False, "Chiamata all'API GitHub fallita", str(exc))
        return

    if r.status_code == 401:
        esito(False, "Token GitHub rifiutato (401)", "Scaduto, revocato o incollato male.")
        return

    scope = r.headers.get("X-OAuth-Scopes", "")
    utente = r.json().get("login", "?") if r.status_code == 200 else "?"
    esito(True, f"Token valido — utente {utente}")

    if scope:
        if "gist" in [s.strip() for s in scope.split(",")]:
            esito(True, f"Scope corretti: {scope}")
        else:
            esito(False, f"Manca lo scope 'gist' (presenti: {scope or 'nessuno'})",
                  "Serve un Personal Access Token *classic* con lo scope `gist`. "
                  "I token fine-grained non gestiscono i Gist.")
            return
    else:
        print(f"  {WARN}GitHub non riporta gli scope: probabilmente è un token "
              "fine-grained, che con i Gist non funziona.")

    # 2) Lettura
    try:
        r = requests.get(f"https://api.github.com/gists/{gist_id}",
                         headers=intestazioni, timeout=25)
    except Exception as exc:
        esito(False, "Lettura del Gist fallita", str(exc))
        return

    if r.status_code == 404:
        esito(False, "Gist non trovato (404)",
              "L'ID è sbagliato, oppure il Gist appartiene a un altro account.")
        return
    if r.status_code != 200:
        esito(False, f"Lettura del Gist: HTTP {r.status_code}", r.text[:200])
        return

    gist = r.json()
    file_presenti = list((gist.get("files") or {}).keys())
    esito(True, f"Gist leggibile — {'privato' if not gist.get('public') else 'PUBBLICO'}")

    if gist.get("public"):
        print(f"  {WARN}Questo Gist è pubblico: lo stato è visibile a chiunque. "
              "Meglio ricrearlo come 'secret gist'.")

    print(f"  {INFO}File presenti: {', '.join(file_presenti) or 'nessuno'}")
    atteso = "stato_monitor.json.gz.b64"
    if atteso in file_presenti:
        dimensione = (gist["files"][atteso].get("size") or 0)
        esito(True, f"File di stato presente ({dimensione} byte)")
    else:
        print(f"  {WARN}Il file '{atteso}' non c'è ancora: verrà creato al primo "
              "salvataggio. Non è un errore.")

    # 3) Scrittura (su un file di prova, senza toccare lo stato)
    try:
        r = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers=intestazioni,
            json={"files": {"_diagnostica.txt": {"content": "prova di scrittura"}}},
            timeout=25,
        )
    except Exception as exc:
        esito(False, "Scrittura sul Gist fallita", str(exc))
        return

    if r.status_code != 200:
        esito(False, f"Scrittura sul Gist: HTTP {r.status_code}", r.text[:200])
        return
    esito(True, "Scrittura sul Gist riuscita")

    # Pulizia: si rimuove il file di prova.
    requests.patch(
        f"https://api.github.com/gists/{gist_id}",
        headers=intestazioni,
        json={"files": {"_diagnostica.txt": None}},
        timeout=25,
    )


# ---------------------------------------------------------------------------

def main(argomenti: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnostica dei segreti del monitor")
    parser.add_argument("--telegram", action="store_true", help="solo i controlli Telegram")
    parser.add_argument("--gist", action="store_true", help="solo i controlli sul Gist")
    opzioni = parser.parse_args(argomenti)

    tutti = not (opzioni.telegram or opzioni.gist)
    if tutti or opzioni.telegram:
        controlla_telegram()
    if tutti or opzioni.gist:
        controlla_gist()

    titolo("RIEPILOGO")
    if _problemi:
        print(f"  {KO} {len(_problemi)} problemi da risolvere:")
        for p in _problemi:
            print(f"     • {p}")
        return 1
    print(f"  {OK} Tutti i controlli superati.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
