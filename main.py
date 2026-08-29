#!/usr/bin/env python3
"""
SCreeper — punto di ingresso.

Esegue un solo ciclo di controllo e termina: la periodicità è di chi lo
invoca. Un processo che dorme dentro un job di Actions consumerebbe minuti
senza fare nulla.

    python main.py --dry-run    nessuna notifica, nessuna scrittura
    python main.py --seed       marca tutto come visto senza notificare
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any

from bot.commands import COMANDI_BOT, ProcessoreComandi
from config_loader import ConfigError, carica_configurazione
from gist_client import GistClient, GistError
from notifiers.telegram import TelegramNotifier
from models import (
    Annuncio,
    Configurazione,
    EsitoScraper,
    Impostazioni,
    Piattaforma,
    Ricerca,
    adesso_utc,
)
from scrapers.base import BaseScraper, ScraperBloccato, ScraperError
from scrapers.ebay import EbayScraper
from scrapers.http import ClientHTTP
from scrapers.subito import SubitoScraper
from scrapers.vinted import VintedScraper
from storage import state as archivio
from storage.state import Stato
from notifiers.telegram import esc as esc_html
from utils.dates import formatta, formatta_completo
from utils.logging_setup import configura_logging

SCRAPER: dict[str, type[BaseScraper]] = {
    Piattaforma.EBAY.value: EbayScraper,
    Piattaforma.VINTED.value: VintedScraper,
    Piattaforma.SUBITO.value: SubitoScraper,
}


# ---------------------------------------------------------------------------
# Argomenti
# ---------------------------------------------------------------------------

def analizza_argomenti(argomenti: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SCreeper — esegue un singolo ciclo di controllo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config.yaml", help="percorso di config.yaml")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="non invia notifiche, non scrive sul Gist, non committa il config",
    )
    parser.add_argument(
        "--no-notify", action="store_true",
        help="esegue tutto ma senza inviare messaggi Telegram",
    )
    parser.add_argument(
        "--seed", action="store_true",
        help="marca tutti gli annunci trovati come già visti, senza notificare",
    )
    parser.add_argument(
        "--no-bot", action="store_true",
        help="salta la lettura dei comandi Telegram",
    )
    parser.add_argument("--solo-ricerca", metavar="NOME", help="esegue solo la ricerca indicata")
    parser.add_argument(
        "--solo-piattaforma", metavar="NOME",
        choices=sorted(Piattaforma.valide()), help="interroga solo la piattaforma indicata",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log di dettaglio")
    return parser.parse_args(argomenti)


# ---------------------------------------------------------------------------
# Filtri
# ---------------------------------------------------------------------------

def _parole_chiave_nel_titolo(ricerca: Ricerca, annuncio: Annuncio) -> bool:
    """
    Con `solo_titolo: true` si pretende che ogni parola chiave compaia nel
    titolo. Serve perché tutte e tre le piattaforme cercano anche nel corpo
    dell'annuncio, e senza questo controllo arriverebbero risultati in cui le
    parole chiave stanno solo nella descrizione.
    """
    if not ricerca.solo_titolo:
        return True
    titolo = annuncio.titolo.lower()
    for parola in ricerca.parole_chiave.lower().split():
        # I termini di una lettera (misure, taglie) non sono discriminanti.
        if len(parola) >= 2 and parola not in titolo:
            return False
    return True


def filtra(
    annunci: list[Annuncio], ricerca: Ricerca, log: logging.Logger
) -> list[Annuncio]:
    """Applica i filtri della ricerca, registrando i motivi degli scarti."""
    tenuti: list[Annuncio] = []
    motivi: dict[str, int] = {}

    def scarta(motivo: str) -> None:
        motivi[motivo] = motivi.get(motivo, 0) + 1

    for annuncio in annunci:
        if not _parole_chiave_nel_titolo(ricerca, annuncio):
            scarta("parole chiave non nel titolo")
            continue
        parola = ricerca.parola_esclusa(ricerca.testo_filtrato(annuncio))
        if parola:
            scarta(f"parola esclusa '{parola}'")
            continue
        if not ricerca.prezzo_ok(annuncio.prezzo):
            scarta("fuori fascia di prezzo")
            continue
        if not ricerca.condizione_ok(annuncio.condizione):
            scarta("condizione diversa")
            continue
        if not ricerca.spedizione_ok(annuncio.spedizione_inclusa):
            scarta("spedizione non inclusa")
            continue
        tenuti.append(annuncio)

    if motivi:
        dettaglio = ", ".join(f"{m}: {n}" for m, n in sorted(motivi.items()))
        log.debug("Filtri [%s]: scartati %d (%s)", ricerca.nome, len(annunci) - len(tenuti), dettaglio)
    return tenuti


def seleziona_nuovi(
    annunci: list[Annuncio],
    ricerca: Ricerca,
    stato: Stato,
    impostazioni: Impostazioni,
    *,
    primo_avvio: bool,
    solo_semina: bool,
    log: logging.Logger,
) -> list[Annuncio]:
    """
    Decide quali annunci vanno notificati, aggiornando lo stato di ciò che è
    stato visto. Ogni annuncio incontrato viene marcato come visto, anche
    quando non viene notificato: è ciò che impedisce di rinotificarlo dopo.
    """
    da_notificare: list[Annuncio] = []
    limite_primo_avvio = adesso_utc() - timedelta(
        minutes=impostazioni.finestra_primo_avvio_minuti
    )
    saltati_vecchi = 0
    saltati_data_ignota = 0
    ripubblicati = 0

    for annuncio in annunci:
        annuncio.ricerca = ricerca.nome

        if stato.ha_visto(annuncio):
            continue

        # Ripubblicazione: stesso titolo, prezzo e venditore ma id nuovo.
        if impostazioni.rileva_ripubblicati and stato.fingerprint_noto(annuncio):
            stato.marca_visto(annuncio)
            ripubblicati += 1
            continue

        stato.marca_visto(annuncio)

        if solo_semina:
            continue

        # Primo avvio della ricerca: si notificano solo gli annunci davvero
        # recenti, altrimenti la prima esecuzione manderebbe centinaia di
        # messaggi con lo storico intero della piattaforma.
        if primo_avvio:
            # Data ignota al primo avvio: non sappiamo se l'annuncio è di un
            # minuto o di un anno fa, e l'avvistamento è sempre "adesso".
            # Dal run successivo "mai visto" basta come prova di novità.
            if annuncio.data_incerta:
                saltati_data_ignota += 1
                continue
            if annuncio.data_effettiva < limite_primo_avvio:
                saltati_vecchi += 1
                continue

        da_notificare.append(annuncio)
        stato.aggiungi_storico(annuncio)

    if saltati_vecchi:
        log.info(
            "[%s] primo avvio: %d annunci più vecchi di %d minuti marcati come visti senza notifica",
            ricerca.nome, saltati_vecchi, impostazioni.finestra_primo_avvio_minuti,
        )
    if saltati_data_ignota:
        log.info(
            "[%s] primo avvio: %d annunci di data ignota marcati come visti senza notifica "
            "(dal prossimo run verranno notificati se davvero nuovi)",
            ricerca.nome, saltati_data_ignota,
        )
    if ripubblicati:
        log.info("[%s] %d ripubblicazioni riconosciute e ignorate", ricerca.nome, ripubblicati)

    return da_notificare


# ---------------------------------------------------------------------------
# Esecuzione di una ricerca
# ---------------------------------------------------------------------------

def esegui_ricerca(
    ricerca: Ricerca,
    *,
    scrapers: dict[str, BaseScraper],
    stato: Stato,
    impostazioni: Impostazioni,
    http: ClientHTTP,
    quarantena: set[str],
    solo_piattaforma: str | None,
    log: logging.Logger,
) -> tuple[list[Annuncio], list[str]]:
    """Interroga le piattaforme di una ricerca. Restituisce (annunci, errori)."""
    primo_avvio = stato.primo_avvio(ricerca.nome)

    # Dopo una pausa lunga si leggono due pagine invece di una: la prima
    # può essersi riempita e gli annunci più vecchi essere scivolati oltre,
    # dove non guardiamo mai. Su Vinted la prima pagina copre ~23 ore e una
    # notte la riempie per tre quarti.
    ultima = stato.ultima_esecuzione(ricerca.nome)
    assenza = (adesso_utc() - ultima) if ultima else None
    soglia_recupero = timedelta(minutes=ricerca.intervallo_minuti * 3)
    recupero = assenza is not None and assenza > soglia_recupero

    if primo_avvio or recupero:
        pagine = impostazioni.pagine_primo_avvio
        if recupero and not primo_avvio:
            log.info(
                "[%s] ferma da %.1f ore: leggo %d pagine per recuperare gli "
                "annunci scivolati oltre la prima",
                ricerca.nome, assenza.total_seconds() / 3600, pagine,
            )
    else:
        pagine = impostazioni.pagine_per_ricerca

    raccolti: list[Annuncio] = []
    errori: list[str] = []

    for piattaforma in ricerca.piattaforme:
        if solo_piattaforma and piattaforma != solo_piattaforma:
            continue

        scraper = scrapers.get(piattaforma)
        if scraper is None:
            log.warning("[%s] piattaforma '%s' non implementata: la salto", ricerca.nome, piattaforma)
            continue

        if piattaforma in quarantena:
            log.info(
                "[%s] %s in quarantena dopo un blocco: salto questo run",
                ricerca.nome, piattaforma,
            )
            stato.registra_esito(piattaforma, EsitoScraper.QUARANTENA)
            continue

        try:
            annunci = scraper.cerca(ricerca, pagine)
        except ScraperBloccato as exc:
            # Un blocco è un evento previsto, non un bug: si registra, si mette
            # la piattaforma in quarantena e si passa oltre. Le altre
            # piattaforme di questa ricerca continuano normalmente.
            messaggio = f"{piattaforma}/{ricerca.nome}: BLOCCATO — {exc}"
            log.warning(messaggio)
            errori.append(messaggio)
            stato.registra_esito(
                piattaforma, EsitoScraper.BLOCCATO, errore=str(exc), impostazioni=impostazioni
            )
            quarantena.add(piattaforma)
            continue
        except ScraperError as exc:
            messaggio = f"{piattaforma}/{ricerca.nome}: errore — {exc}"
            log.warning(messaggio)
            errori.append(messaggio)
            stato.registra_esito(piattaforma, EsitoScraper.ERRORE, errore=str(exc))
            continue
        except Exception as exc:  # bug imprevisto in uno scraper
            messaggio = f"{piattaforma}/{ricerca.nome}: eccezione imprevista — {type(exc).__name__}: {exc}"
            log.exception(messaggio)
            errori.append(messaggio)
            stato.registra_esito(piattaforma, EsitoScraper.ERRORE, errore=str(exc))
            continue

        esito = EsitoScraper.OK if annunci else EsitoScraper.VUOTO
        stato.registra_esito(piattaforma, esito, risultati=len(annunci))
        log.info(
            "[%s] %s (%s): %d risultati grezzi",
            ricerca.nome, piattaforma, scraper.via or "?", len(annunci),
        )
        raccolti.extend(annunci)
        # Nessuna pausa esplicita qui: il client HTTP distanzia già le
        # richieste allo stesso dominio. Aspettare anche fra domini diversi
        # allungherebbe il run senza proteggere nulla.

    return raccolti, errori


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def carica_stato(log: logging.Logger, dry_run: bool) -> tuple[Stato, GistClient | None]:
    """
    Carica lo stato dal Gist. Senza GIST_TOKEN si ripiega sul file locale,
    così l'esecuzione in locale funziona anche senza segreti configurati.
    """
    token = os.environ.get("GIST_TOKEN", "").strip()
    gist_id = os.environ.get("GIST_ID", "").strip()

    if not token:
        log.warning(
            "GIST_TOKEN assente: uso solo lo stato locale in %s. "
            "Su GitHub Actions questo significa che ogni run ripartirebbe da zero.",
            archivio.PERCORSO_LOCALE,
        )
        return archivio.carica_locale() or Stato.nuovo(), None

    client = GistClient(token, gist_id)
    try:
        dati = client.leggi()
        return Stato(dati), client
    except GistError as exc:
        # Non si prosegue con uno stato vuoto quando il Gist esiste ma è
        # illeggibile: si rischierebbe di rinotificare tutto lo storico.
        if gist_id and not dry_run:
            raise
        log.warning("Gist non leggibile (%s): parto dallo stato locale", exc)
        return archivio.carica_locale() or Stato.nuovo(), client


def salva_stato(
    stato: Stato, client: GistClient | None, impostazioni: Impostazioni,
    log: logging.Logger, dry_run: bool, piattaforme_in_uso: set[str] | None = None,
    ricerche_in_uso: set[str] | None = None,
) -> None:
    """Pota, scrive il file locale e prova a caricare sul Gist."""
    if ricerche_in_uso:
        rimossi = stato.pota_ricerche(ricerche_in_uso)
        if rimossi:
            log.info(
                "Ricerche non più configurate, rimosse dall'archivio: %s",
                ", ".join(f"{k} -{v}" for k, v in rimossi.items()),
            )
    if piattaforme_in_uso:
        obsolete = stato.pota_piattaforme(piattaforme_in_uso)
        if obsolete:
            log.info(
                "Piattaforme non più usate da alcuna ricerca, rimosse dallo "
                "stato: %s", ", ".join(obsolete),
            )
    stato.pota(impostazioni)
    archivio.salva_locale(stato)

    if dry_run:
        # Il divieto va scritto su disco: lo step `if: always()` del workflow è
        # un altro processo e non ha modo di sapere che eravamo in prova.
        archivio.marca_da_non_caricare()
        log.info("[prova] salvataggio sul Gist saltato (--dry-run)")
        return
    if client is None:
        log.info("Stato salvato solo in locale (nessun GIST_TOKEN)")
        return

    try:
        client.assicura(stato.to_dict())
        client.scrivi(stato.to_dict(), descrizione=archivio.descrizione_gist(stato))
        archivio.marca_caricato()
    except GistError as exc:
        # Non è fatale: lo step `if: always()` del workflow riproverà a
        # caricare il file locale.
        log.error("Salvataggio sul Gist fallito (%s): ci riproverà lo step finale", exc)


def componi_stato_controllo(
    *,
    stato: Stato,
    impostazioni: Impostazioni,
    dettaglio: list[dict[str, Any]],
    saltate: list[str],
    nuovi: int,
    notificati: int,
    richieste: int,
    errori: list[str],
    terminato: datetime,
) -> str:
    """Riepilogo di un controllo: cosa ha girato e con quale esito."""
    icone = {
        EsitoScraper.OK.value: "✅", EsitoScraper.VUOTO.value: "➖",
        EsitoScraper.BLOCCATO.value: "⛔", EsitoScraper.ERRORE.value: "❌",
        EsitoScraper.QUARANTENA.value: "⏸",
    }
    piattaforme = " ".join(
        f"{icone.get(str(v.get('ultimo_esito')), '❔')}{nome}"
        for nome, v in sorted(stato.salute_piattaforme().items())
    ) or "nessuna piattaforma interrogata"

    intestazione = "✅ Controllo eseguito" if not errori else "⚠️ Controllo con errori"
    righe = [
        f"<b>{intestazione}</b> — {formatta(terminato, impostazioni.timezone)}",
        "",
    ]

    if dettaglio:
        larghezza = max(len(d["nome"]) for d in dettaglio)
        corpo = []
        for d in dettaglio:
            esito = f"{d['nuovi']} nuovi" if d["nuovi"] else "—"
            corpo.append(f"{d['nome']:<{larghezza}}  {d['filtrati']:>3} rilevanti  {esito}")
        righe.append("<pre>" + esc_html("\n".join(corpo)) + "</pre>")
    else:
        righe.append("<i>nessuna ricerca da eseguire in questo giro</i>")

    if saltate:
        righe.append(f"⏭ in attesa del proprio turno: {esc_html(', '.join(saltate))}")

    righe.append("")
    righe.append(f"{richieste} richieste · {piattaforme}")
    if notificati:
        righe.append(f"📨 {notificati} " + ("notifica inviata" if notificati == 1
                                            else "notifiche inviate"))
    if errori:
        righe.append("")
        righe.append(f"<code>{esc_html(errori[0][:160])}</code>")
    return "\n".join(righe)


def notifica(
    nuovi: list[Annuncio],
    notifier: TelegramNotifier,
    stato: Stato,
    impostazioni: Impostazioni,
    log: logging.Logger,
) -> int:
    """Invia le notifiche rispettando il tetto per run. Torna quante ne ha inviate."""
    if not nuovi:
        return 0

    # Più recenti per primi: se il tetto taglia, si perdono i meno interessanti.
    nuovi.sort(key=lambda a: a.data_effettiva, reverse=True)
    tetto = impostazioni.max_notifiche_per_run
    da_inviare, eccedenza = nuovi[:tetto], nuovi[tetto:]

    inviate = 0
    for annuncio in da_inviare:
        if notifier.invia_annuncio(annuncio):
            inviate += 1
        # La statistica conta l'annuncio nuovo trovato, non il messaggio
        # riuscito: un errore di rete su Telegram non deve falsare i grafici.
        stato.registra_statistica(annuncio, impostazioni.timezone)

    if eccedenza:
        log.info("Tetto di %d notifiche raggiunto: riassumo altri %d annunci", tetto, len(eccedenza))
        notifier.invia_riassunto(eccedenza)
        for annuncio in eccedenza:
            stato.registra_statistica(annuncio, impostazioni.timezone)

    return inviate


def esegui(opzioni: argparse.Namespace) -> int:
    """Corpo del run. Restituisce il codice di uscita del processo."""
    iniziato = adesso_utc()

    # 1) Configurazione (prima del logging: serve il fuso orario).
    try:
        configurazione: Configurazione = carica_configurazione(opzioni.config)
    except ConfigError as exc:
        print(f"ERRORE di configurazione: {exc}", file=sys.stderr)
        return 2

    impostazioni = configurazione.impostazioni
    segreti = [
        os.environ.get(nome, "")
        for nome in (
            "TELEGRAM_BOT_TOKEN", "GIST_TOKEN", "GITHUB_TOKEN",
            "EBAY_CLIENT_SECRET", "EBAY_CLIENT_ID", "TELEGRAM_CHAT_ID",
        )
    ]
    log = configura_logging(
        verboso=opzioni.verbose, segreti=segreti, tz_locale=impostazioni.timezone
    )

    log.info("=" * 72)
    log.info(
        "Avvio run — %s | %d ricerche configurate%s",
        formatta_completo(iniziato, impostazioni.timezone),
        len(configurazione.ricerche),
        " | MODALITÀ DI PROVA" if opzioni.dry_run else "",
    )

    # 2) Stato.
    try:
        stato, gist = carica_stato(log, opzioni.dry_run)
    except GistError as exc:
        log.error("Impossibile leggere lo stato: %s", exc)
        return 3

    # 3) Notificatore.
    notifier = TelegramNotifier(
        os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        os.environ.get("TELEGRAM_CHAT_ID", ""),
        tz_locale=impostazioni.timezone,
        abilitato=not (opzioni.dry_run or opzioni.no_notify),
    )

    errori: list[str] = []
    nuovi_totali: list[Annuncio] = []
    inviate = 0
    # Piattaforme citate da almeno una ricerca, anche sospesa: una pausa non
    # deve cancellare lo storico di salute di quella piattaforma.
    piattaforme_in_uso: set[str] = {
        p for r in configurazione.ricerche for p in r.piattaforme
    }
    ricerche_in_uso: set[str] = {r.nome for r in configurazione.ricerche}
    dettaglio_ricerche: list[dict[str, Any]] = []

    try:
        # 4) Menu dei comandi (i suggerimenti su "/"): confrontato con quanto
        #    Telegram ha memorizzato, così un disallineamento si ripara da sé.
        if not opzioni.no_bot and notifier.abilitato:
            notifier.sincronizza_comandi(COMANDI_BOT)

        # 5) Comandi Telegram: prima delle ricerche, così una ricerca aggiunta
        #    ora parte già in questo run.
        if not opzioni.no_bot:
            try:
                processore = ProcessoreComandi(
                    notifier, stato,
                    percorso_config=opzioni.config,
                    tz_locale=impostazioni.timezone,
                    token_github=os.environ.get("GITHUB_TOKEN", ""),
                    dry_run=opzioni.dry_run,
                )
                if processore.processa():
                    log.info("Configurazione modificata via Telegram: la ricarico")
                    configurazione = carica_configurazione(opzioni.config)
                    impostazioni = configurazione.impostazioni
                    piattaforme_in_uso = {
                        p for r in configurazione.ricerche for p in r.piattaforme
                    }
                    ricerche_in_uso = {r.nome for r in configurazione.ricerche}
            except Exception as exc:  # il bot non deve mai fermare il monitor
                log.exception("Errore nel processare i comandi Telegram: %s", exc)
                errori.append(f"bot Telegram: {exc}")

        # 6) Scraping.
        http = ClientHTTP(
            delay_min=impostazioni.delay_min_secondi,
            delay_max=impostazioni.delay_max_secondi,
            timeout=impostazioni.timeout_secondi,
            max_tentativi=impostazioni.max_tentativi,
        )
        scrapers: dict[str, BaseScraper] = {
            nome: classe(http, impostazioni) for nome, classe in SCRAPER.items()
        }

        # Quarantene decise nei run precedenti: si consumano una volta sola.
        quarantena = {p for p in piattaforme_in_uso if stato.in_quarantena(p)}
        for piattaforma in quarantena:
            stato.consuma_quarantena(piattaforma)
        if quarantena:
            log.warning("Piattaforme in quarantena in questo run: %s", ", ".join(sorted(quarantena)))

        da_eseguire = [
            r for r in configurazione.ricerche
            if r.eseguibile
            and (not opzioni.solo_ricerca or r.nome == opzioni.solo_ricerca)
            and stato.da_eseguire(r)
        ]
        nomi_da_eseguire = {r.nome for r in da_eseguire}
        nomi_saltate = [
            r.nome for r in configurazione.ricerche
            if r.eseguibile and r.nome not in nomi_da_eseguire
        ]
        log.info(
            "Ricerche da eseguire ora: %d (%d non ancora dovute)",
            len(da_eseguire), len(nomi_saltate),
        )

        for indice, ricerca in enumerate(da_eseguire, start=1):
            log.info("-" * 72)
            log.info(
                "[%d/%d] %s — '%s' su %s",
                indice, len(da_eseguire), ricerca.nome,
                ricerca.parole_chiave, ", ".join(ricerca.piattaforme),
            )
            primo_avvio = stato.primo_avvio(ricerca.nome)

            grezzi, errori_ricerca = esegui_ricerca(
                ricerca,
                scrapers=scrapers, stato=stato, impostazioni=impostazioni,
                http=http, quarantena=quarantena,
                solo_piattaforma=opzioni.solo_piattaforma, log=log,
            )
            errori.extend(errori_ricerca)

            filtrati = filtra(grezzi, ricerca, log)
            nuovi = seleziona_nuovi(
                filtrati, ricerca, stato, impostazioni,
                primo_avvio=primo_avvio, solo_semina=opzioni.seed, log=log,
            )
            log.info(
                "[%s] %d grezzi -> %d dopo i filtri -> %d nuovi da notificare",
                ricerca.nome, len(grezzi), len(filtrati), len(nuovi),
            )

            stato.registra_esecuzione(ricerca.nome, notificati=len(nuovi))
            nuovi_totali.extend(nuovi)
            dettaglio_ricerche.append({
                "nome": ricerca.nome, "trovati": len(grezzi),
                "filtrati": len(filtrati), "nuovi": len(nuovi),
            })

        http.chiudi()

        # 7) Notifiche.
        log.info("=" * 72)
        if opzioni.seed:
            log.info("Modalità --seed: %d annunci marcati come visti, nessuna notifica", len(nuovi_totali))
        else:
            inviate = notifica(nuovi_totali, notifier, stato, impostazioni, log)

        # 8) Alert per scraper probabilmente rotti (una sola volta ciascuno).
        for piattaforma in sorted(piattaforme_in_uso):
            if stato.alert_da_inviare(piattaforma, impostazioni.run_zero_per_alert):
                voce = stato.salute_piattaforme().get(piattaforma, {})
                log.warning(
                    "Scraper %s a vuoto da %s run: invio l'alert",
                    piattaforma, voce.get("run_zero_consecutivi"),
                )
                if notifier.invia_alert_scraper(
                    piattaforma,
                    int(voce.get("run_zero_consecutivi") or 0),
                    voce.get("ultimo_errore"),
                ):
                    stato.marca_alert_inviato(piattaforma)

        # 9) Riepilogo del run. Va registrato PRIMA dell'heartbeat, altrimenti
        #    il messaggio giornaliero riporterebbe i dati del run precedente.
        terminato = adesso_utc()
        stato.registra_run(
            iniziato=iniziato, terminato=terminato,
            nuovi=len(nuovi_totali), notificati=inviate,
            richieste=http.richieste, errori=errori,
        )

        # 10) Avviso di avvenuto controllo.
        modalita = impostazioni.notifica_ogni_controllo
        if modalita != "mai" and not opzioni.seed:
            testo = componi_stato_controllo(
                stato=stato, impostazioni=impostazioni,
                dettaglio=dettaglio_ricerche, saltate=nomi_saltate,
                nuovi=len(nuovi_totali), notificati=inviate,
                richieste=http.richieste, errori=errori, terminato=terminato,
            )
            if modalita == "sempre":
                notifier.invia_messaggio(testo)
            else:
                # "aggiorna": si riscrive sempre lo stesso messaggio, così la
                # chat non si riempie e lo si può fissare in cima.
                stato.messaggio_stato_id = notifier.invia_stato_controllo(
                    testo, stato.messaggio_stato_id
                )

        # 11) Errori e heartbeat.
        if errori and impostazioni.notifica_errori and not opzioni.seed:
            notifier.invia_errori(errori)

        if stato.heartbeat_dovuto(impostazioni):
            if notifier.invia_heartbeat(
                ricerche_attive=sum(1 for r in configurazione.ricerche if r.eseguibile),
                salute=stato.salute_piattaforme(),
                ultimo_run=stato.ultimo_run,
                totale_storico=len(stato.dati.get("storico") or []),
            ):
                stato.marca_heartbeat(impostazioni)

        log.info(
            "Run completato in %.1fs — %d richieste HTTP, %d nuovi, %d notificati, %d errori",
            (terminato - iniziato).total_seconds(), http.richieste,
            len(nuovi_totali), inviate, len(errori),
        )
        return 0

    finally:
        # Lo stato si salva SEMPRE: anche se qualcosa è esploso a metà run,
        # ciò che è già stato visto non deve essere rinotificato al prossimo giro.
        try:
            salva_stato(stato, gist, impostazioni, log, opzioni.dry_run,
                        piattaforme_in_uso, ricerche_in_uso)
        except Exception as exc:
            log.error("Salvataggio dello stato fallito: %s", exc)


def main(argomenti: list[str] | None = None) -> int:
    opzioni = analizza_argomenti(argomenti)
    if opzioni.dry_run:
        opzioni.no_notify = True
        # Anche la coda dei comandi è un effetto collaterale: verrebbero
        # eseguiti e le risposte scartate, senza che nessuno lo sappia.
        opzioni.no_bot = True
    try:
        return esegui(opzioni)
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
