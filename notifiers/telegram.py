"""
Notifiche Telegram.

Un messaggio al secondo verso la stessa chat, tetto configurabile per run
(oltre il quale parte un solo riassunto), e nessun errore che propaga: un
invio fallito non deve impedire gli altri né far fallire il run.
"""

from __future__ import annotations

import html
import logging
import time
from typing import Any

import requests

from models import Annuncio, EsitoScraper
from utils.dates import formatta

log = logging.getLogger("monitor.telegram")

API = "https://api.telegram.org"

# Distanza minima fra due invii. 1.05s invece di 1.00s per assorbire la
# differenza fra l'orologio locale e quello dei server Telegram.
INTERVALLO_MINIMO = 1.05

# Limiti delle Bot API.
MAX_CARATTERI_MESSAGGIO = 4096
MAX_CARATTERI_DIDASCALIA = 1024

_EMOJI_PIATTAFORMA = {"ebay": "🛒", "vinted": "👕", "subito": "📦"}


def esc(testo: Any) -> str:
    """Escape HTML: i titoli degli annunci contengono spesso & < >."""
    return html.escape(str(testo or ""), quote=False)


class TelegramNotifier:
    """Client minimale delle Bot API, tollerante agli errori."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        tz_locale: str = "Europe/Rome",
        timeout: int = 20,
        abilitato: bool = True,
    ) -> None:
        self.token = token.strip()
        self.chat_id = str(chat_id).strip()
        self.tz_locale = tz_locale
        self.timeout = timeout
        # `abilitato=False` (modalità --no-notify) fa girare tutto il resto
        # normalmente ma non manda nulla: utile per provare i filtri.
        self.abilitato = abilitato and bool(self.token and self.chat_id)
        self.inviati = 0
        self.falliti = 0
        self._ultimo_invio = 0.0
        self._sessione = requests.Session()

        if not self.abilitato:
            log.warning(
                "Notifiche Telegram disattivate (token/chat_id assenti o modalità di prova)"
            )

    # -- trasporto ---------------------------------------------------------

    def _ritmo(self) -> None:
        trascorso = time.monotonic() - self._ultimo_invio
        if trascorso < INTERVALLO_MINIMO:
            time.sleep(INTERVALLO_MINIMO - trascorso)

    def _chiama(
        self, metodo: str, payload: dict[str, Any], *, tentativi: int = 2
    ) -> dict[str, Any] | None:
        """
        Invoca un metodo delle Bot API. Restituisce il risultato o None.
        Non solleva mai: ogni problema viene loggato e basta.
        """
        if not self.token:
            return None
        url = f"{API}/bot{self.token}/{metodo}"

        for tentativo in range(1, tentativi + 1):
            try:
                risposta = self._sessione.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("Telegram %s: errore di rete (%s)", metodo, exc)
                if tentativo < tentativi:
                    time.sleep(2)
                continue

            if risposta.status_code == 429:
                # Telegram indica esattamente quanti secondi attendere.
                try:
                    attesa = float(
                        (risposta.json().get("parameters") or {}).get("retry_after", 5)
                    )
                except ValueError:
                    attesa = 5.0
                attesa = min(max(attesa, 1.0), 60.0)
                log.warning("Telegram %s: rate limit, attendo %.0fs", metodo, attesa)
                time.sleep(attesa)
                continue

            try:
                dati = risposta.json()
            except ValueError:
                log.warning("Telegram %s: risposta non JSON (HTTP %s)", metodo, risposta.status_code)
                return None

            if dati.get("ok"):
                return dati.get("result")

            log.warning(
                "Telegram %s ha risposto ok=false: %s",
                metodo, str(dati.get("description"))[:200],
            )
            return None

        return None

    # -- invii -------------------------------------------------------------

    def invia_messaggio(self, testo_html: str, *, anteprima: bool = False) -> bool:
        """Invia un messaggio HTML. Restituisce True se è partito."""
        if not self.abilitato:
            log.info("[prova] messaggio non inviato:\n%s", testo_html[:400])
            return False

        self._ritmo()
        risultato = self._chiama(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": testo_html[:MAX_CARATTERI_MESSAGGIO],
                "parse_mode": "HTML",
                "disable_web_page_preview": not anteprima,
            },
        )
        self._ultimo_invio = time.monotonic()
        if risultato is None:
            self.falliti += 1
            return False
        self.inviati += 1
        return True

    def invia_annuncio(self, annuncio: Annuncio) -> bool:
        """
        Invia la scheda di un annuncio: miniatura + titolo + prezzo in
        evidenza + piattaforma, località, data e link diretto.
        """
        didascalia = self._componi_scheda(annuncio)

        if not self.abilitato:
            log.info("[prova] annuncio non inviato: %s — %s", annuncio.titolo, annuncio.url)
            return False

        # Con l'immagine si usa sendPhoto: la miniatura è più leggibile della
        # semplice anteprima del link.
        if annuncio.immagine:
            self._ritmo()
            risultato = self._chiama(
                "sendPhoto",
                {
                    "chat_id": self.chat_id,
                    "photo": annuncio.immagine,
                    "caption": didascalia[:MAX_CARATTERI_DIDASCALIA],
                    "parse_mode": "HTML",
                },
            )
            self._ultimo_invio = time.monotonic()
            if risultato is not None:
                self.inviati += 1
                return True
            # Immagine irraggiungibile o formato rifiutato: non si perde
            # l'annuncio, si ripiega sul messaggio di testo.
            log.info("sendPhoto fallito per %s: ripiego su messaggio di testo", annuncio.chiave)

        return self.invia_messaggio(didascalia, anteprima=not annuncio.immagine)

    def _componi_scheda(self, annuncio: Annuncio) -> str:
        """Costruisce il testo HTML di una notifica."""
        emoji = _EMOJI_PIATTAFORMA.get(annuncio.piattaforma, "🔎")

        if annuncio.prezzo is None:
            prezzo = "prezzo non indicato"
        else:
            simbolo = "€" if annuncio.valuta == "EUR" else esc(annuncio.valuta)
            prezzo = f"{annuncio.prezzo:.2f}".rstrip("0").rstrip(".") + f" {simbolo}"

        righe = [f"🆕 <b>{esc(annuncio.titolo)}</b>", f"💶 <b>{prezzo}</b>"]

        if annuncio.spedizione_inclusa is True:
            righe[-1] += " · spedizione inclusa"
        elif annuncio.spedizione_inclusa is False:
            righe[-1] += " · + spedizione"

        contesto = [f"{emoji} {esc(annuncio.piattaforma.capitalize())}"]
        if annuncio.localita:
            contesto.append(f"📍 {esc(annuncio.localita)}")
        if annuncio.condizione and annuncio.condizione != "qualsiasi":
            contesto.append(esc(annuncio.condizione))
        righe.append(" · ".join(contesto))

        quando = formatta(annuncio.data_effettiva, self.tz_locale)
        marcatore = " (data stimata)" if annuncio.data_incerta else ""
        riga_data = f"🕒 {quando}{marcatore}"
        if annuncio.ricerca:
            riga_data += f" · <i>{esc(annuncio.ricerca)}</i>"
        righe.append(riga_data)

        righe.append(f'\n<a href="{esc(annuncio.url)}">Apri l\'annuncio</a>')
        return "\n".join(righe)

    def invia_riassunto(self, rimanenti: list[Annuncio], mostrati: int = 5) -> bool:
        """
        Messaggio unico per gli annunci oltre il tetto per run.
        Include il conteggio e i link ai primi `mostrati`.
        """
        if not rimanenti:
            return False

        righe = [
            f"📬 <b>Altri {len(rimanenti)} annunci trovati</b>",
            "<i>oltre il tetto di notifiche di questo run</i>",
            "",
        ]
        for annuncio in rimanenti[:mostrati]:
            prezzo = f"{annuncio.prezzo:.0f} €" if annuncio.prezzo is not None else "—"
            righe.append(
                f'• <a href="{esc(annuncio.url)}">{esc(annuncio.titolo[:70])}</a> '
                f"— <b>{prezzo}</b> ({esc(annuncio.piattaforma)})"
            )
        if len(rimanenti) > mostrati:
            righe.append(f"\n…e altri {len(rimanenti) - mostrati}. Tutti sono nella dashboard.")

        return self.invia_messaggio("\n".join(righe))

    def invia_stato_controllo(
        self, testo_html: str, messaggio_id: int | None = None
    ) -> int | None:
        """
        Riscrive sempre lo stesso messaggio invece di mandarne uno nuovo: con
        un controllo ogni quarto d'ora sarebbero un centinaio di messaggi al
        giorno, e le notifiche degli annunci annegherebbero. Telegram non
        emette suoni per le modifiche.

        Restituisce l'id del messaggio, da riusare la volta successiva.
        """
        if not self.abilitato:
            log.info("[prova] stato del controllo non inviato")
            return messaggio_id

        if messaggio_id:
            self._ritmo()
            risultato = self._chiama(
                "editMessageText",
                {
                    "chat_id": self.chat_id,
                    "message_id": messaggio_id,
                    "text": testo_html[:MAX_CARATTERI_MESSAGGIO],
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            self._ultimo_invio = time.monotonic()
            if risultato is not None:
                return messaggio_id
            # Il messaggio può essere stato cancellato a mano, oppure il testo
            # è identico al precedente (Telegram rifiuta le modifiche a vuoto).
            # In entrambi i casi se ne manda uno nuovo.
            log.info("Modifica del messaggio di stato fallita: ne mando uno nuovo")

        self._ritmo()
        risultato = self._chiama(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": testo_html[:MAX_CARATTERI_MESSAGGIO],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": True,
            },
        )
        self._ultimo_invio = time.monotonic()
        if isinstance(risultato, dict) and risultato.get("message_id"):
            self.inviati += 1
            return int(risultato["message_id"])
        self.falliti += 1
        return None

    def invia_alert_scraper(self, piattaforma: str, run_a_vuoto: int, ultimo_errore: str | None) -> bool:
        """Alert dedicato quando uno scraper sembra rotto."""
        righe = [
            f"⚠️ <b>Lo scraper {esc(piattaforma)} sembra rotto</b>",
            f"Nessun risultato da {run_a_vuoto} run consecutivi.",
        ]
        if ultimo_errore:
            righe.append(f"\n<code>{esc(ultimo_errore[:300])}</code>")
        righe.append(
            "\nQuesto avviso non verrà ripetuto finché la piattaforma non torna a "
            "produrre risultati. Vedi la sezione Troubleshooting del README."
        )
        return self.invia_messaggio("\n".join(righe))

    def invia_errori(self, errori: list[str]) -> bool:
        """Riepilogo degli errori del run, se `notifica_errori` è attivo."""
        if not errori:
            return False
        righe = ["🔧 <b>Errori durante il run</b>", ""]
        for errore in errori[:8]:
            righe.append(f"• <code>{esc(errore[:200])}</code>")
        if len(errori) > 8:
            righe.append(f"\n…e altri {len(errori) - 8}.")
        return self.invia_messaggio("\n".join(righe))

    def invia_heartbeat(
        self,
        *,
        ricerche_attive: int,
        salute: dict[str, dict[str, Any]],
        ultimo_run: dict[str, Any],
        totale_storico: int,
    ) -> bool:
        """Messaggio giornaliero di vitalità: serve ad accorgersi se il cron muore."""
        righe = [
            "💚 <b>Monitor attivo</b>",
            f"Ricerche attive: <b>{ricerche_attive}</b>",
            f"Annunci in storico: <b>{totale_storico}</b>",
            "",
            "<b>Stato piattaforme</b>",
        ]
        icone = {
            EsitoScraper.OK.value: "✅",
            EsitoScraper.VUOTO.value: "➖",
            EsitoScraper.BLOCCATO.value: "⛔",
            EsitoScraper.ERRORE.value: "❌",
            EsitoScraper.QUARANTENA.value: "⏸",
        }
        for piattaforma, voce in sorted(salute.items()):
            icona = icone.get(str(voce.get("ultimo_esito")), "❔")
            righe.append(
                f"{icona} {esc(piattaforma)} — {esc(voce.get('ultimo_esito') or 'mai eseguito')}"
            )
        righe.append("")
        righe.append(
            f"Ultimo run: {esc(ultimo_run.get('terminato') or '?')} "
            f"({ultimo_run.get('notificati', 0)} notifiche)"
        )
        return self.invia_messaggio("\n".join(righe))

    def imposta_comandi(self, comandi: list[tuple[str, str]]) -> bool:
        """Registra il menu che compare digitando "/". Telegram lo memorizza
        lato server: non è un messaggio."""
        if not self.token:
            return False
        risultato = self._chiama(
            "setMyCommands",
            {
                "commands": [
                    {"command": nome.lstrip("/").lower(), "description": descrizione[:256]}
                    for nome, descrizione in comandi
                ],
                "scope": {"type": "default"},
                # NIENTE `language_code`: specificandolo, Telegram registra i
                # comandi solo per chi ha il client impostato su quella
                # lingua, e tutti gli altri ricadono sulla lista predefinita
                # (vuota), quindi non vedono alcun suggerimento. Le
                # descrizioni sono in italiano a prescindere.
            },
        )
        if risultato is not None:
            log.info("Menu dei comandi registrato su Telegram (%d voci)", len(comandi))
            return True
        return False

    def sincronizza_comandi(self, comandi: list[tuple[str, str]]) -> bool:
        """
        Chiede a Telegram cosa ha memorizzato e registra solo se differisce.
        Un'impronta salvata in locale non basterebbe: descriverebbe la lista
        dei comandi, non il modo in cui vengono registrati, e una correzione
        al secondo non farebbe scattare nulla.
        """
        if not self.token:
            return False

        desiderati = [
            {"command": n.lstrip("/").lower(), "description": d[:256]}
            for n, d in comandi
        ]
        attuali = self.leggi_comandi()

        def confrontabile(elenco: list[dict[str, Any]]) -> list[tuple[str, str]]:
            return [(str(c.get("command")), str(c.get("description"))) for c in elenco]

        if confrontabile(attuali) == confrontabile(desiderati):
            log.debug("Menu dei comandi già allineato (%d voci)", len(desiderati))
            return True

        log.info(
            "Menu dei comandi da aggiornare: %d registrati, %d desiderati",
            len(attuali), len(desiderati),
        )
        return self.imposta_comandi(comandi)

    def leggi_comandi(self) -> list[dict[str, Any]]:
        """Comandi attualmente registrati presso Telegram (per diagnostica)."""
        risultato = self._chiama("getMyCommands", {"scope": {"type": "default"}})
        return risultato if isinstance(risultato, list) else []

    # -- ricezione (usata dal bot) ----------------------------------------

    def leggi_aggiornamenti(self, offset: int) -> list[dict[str, Any]]:
        """
        Scarica i messaggi arrivati dopo `offset`.

        `timeout=0`: nessun long polling. Il bot vive dentro un run di Actions
        che dura pochi secondi, quindi si prende ciò che c'è e si prosegue.
        """
        if not self.token:
            return []
        risultato = self._chiama(
            "getUpdates",
            {
                "offset": offset + 1 if offset else None,
                "timeout": 0,
                "limit": 50,
                "allowed_updates": ["message"],
            },
        )
        if not isinstance(risultato, list):
            return []
        return risultato
