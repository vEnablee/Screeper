"""
Comandi del bot Telegram.

Il bot vive dentro il run: a ogni esecuzione scarica i messaggi arrivati e li
processa, quindi la risposta arriva al controllo successivo. È anche il
motivo per cui `/add` è un comando a colpo singolo e non una procedura
guidata: un dialogo richiederebbe un ciclo di attesa fra una domanda e l'altra.

Solo il TELEGRAM_CHAT_ID configurato può impartire comandi.
"""

from __future__ import annotations

import logging
import shlex
from typing import Any, Callable

from config_loader import (
    ConfigError,
    branch_corrente,
    carica_documento,
    commit_config,
    configurazione_da_documento,
    modifica_aggiungi_esclusa,
    modifica_aggiungi_ricerca,
    modifica_pausa,
    modifica_pausa_tutte,
    modifica_rimuovi_ricerca,
    repository_corrente,
    salva_documento,
    serializza_documento,
)
from models import Condizione, Piattaforma
from notifiers.telegram import TelegramNotifier, esc
from storage.state import Stato
from utils.dates import formatta_completo

log = logging.getLogger("monitor.bot")

# Menu che Telegram mostra quando si digita "/" nella chat.
# L'ordine è quello di visualizzazione: prima ciò che si usa più spesso.
# Cambiare questa lista basta: main.py se ne accorge e la ri-registra.
COMANDI_BOT: list[tuple[str, str]] = [
    ("status", "Stato dell'ultimo controllo e delle piattaforme"),
    ("list", "Elenco delle ricerche con i loro parametri"),
    ("stop", "Sospende tutte le ricerche (il bot resta raggiungibile)"),
    ("riprendi", "Riattiva le ricerche sospese da /stop"),
    ("spegni", "Ferma anche il trigger esterno — poi si riaccende solo dalla dashboard"),
    ("pause", "Sospende una ricerca — /pause nome"),
    ("resume", "Riattiva una ricerca — /resume nome"),
    ("add", "Aggiunge una ricerca — /add nome=… | kw=…"),
    ("remove", "Rimuove una ricerca — /remove nome"),
    ("exclude", "Aggiunge una parola esclusa — /exclude ricerca parola"),
    ("help", "Come si usano i comandi"),
]

AIUTO = """<b>Comandi disponibili</b>

/list — ricerche configurate con i loro parametri
/status — esito dell'ultimo run e stato delle piattaforme
/add … — aggiunge una ricerca (vedi sotto)
/remove &lt;nome&gt; — rimuove una ricerca
/exclude &lt;ricerca&gt; &lt;parola&gt; — aggiunge una parola esclusa
/pause &lt;nome&gt; — sospende una ricerca
/resume &lt;nome&gt; — riattiva una ricerca
/stop — sospende <b>tutte</b> le ricerche
/riprendi — riattiva quelle sospese da /stop
/spegni — ferma il <b>trigger esterno</b>: il monitor non parte più

<b>Sintassi di /add</b>
I parametri vanno sulla stessa riga, separati da <code>|</code>:

<code>/add nome=iphone13 | kw=iphone 13 | piattaforme=ebay,subito | min=150 | max=320 | escluse=rotto,schermo | condizione=usato | intervallo=15 | zona=lombardia</code>

Obbligatori: <code>nome</code> e <code>kw</code>. Tutto il resto ha un default.

<i>Le modifiche vengono scritte nel config.yaml del repo con un commit, quindi
sono permanenti e visibili nella cronologia di GitHub.</i>"""

# Alias accettati per i campi di /add: la forma breve è quella comoda da
# digitare su un telefono, quella lunga coincide con il nome nel YAML.
_ALIAS: dict[str, str] = {
    "nome": "nome",
    "kw": "parole_chiave",
    "parole_chiave": "parole_chiave",
    "keywords": "parole_chiave",
    "piattaforme": "piattaforme",
    "siti": "piattaforme",
    "min": "prezzo_min",
    "prezzo_min": "prezzo_min",
    "max": "prezzo_max",
    "prezzo_max": "prezzo_max",
    "escluse": "parole_escluse",
    "parole_escluse": "parole_escluse",
    "condizione": "condizione",
    "intervallo": "intervallo_minuti",
    "intervallo_minuti": "intervallo_minuti",
    "zona": "zona",
    "raggio": "raggio_km",
    "raggio_km": "raggio_km",
    "solo_titolo": "solo_titolo",
}


class ProcessoreComandi:
    """Legge i comandi Telegram, applica le modifiche e le committa."""

    def __init__(
        self,
        notifier: TelegramNotifier,
        stato: Stato,
        *,
        percorso_config: str,
        tz_locale: str,
        token_github: str = "",
        dry_run: bool = False,
    ) -> None:
        self.notifier = notifier
        self.stato = stato
        self.percorso_config = percorso_config
        self.tz_locale = tz_locale
        self.token_github = token_github
        self.dry_run = dry_run

        self._documento: Any = None
        self._modificato = False
        self._comandi_applicati: list[str] = []

    # -- ciclo principale --------------------------------------------------

    def processa(self) -> bool:
        """
        Esegue tutti i comandi in coda. Restituisce True se config.yaml è
        stato modificato (il chiamante deve ricaricare la configurazione).
        """
        aggiornamenti = self.notifier.leggi_aggiornamenti(self.stato.ultimo_update_id)
        if not aggiornamenti:
            return False

        log.info("Telegram: %d aggiornamenti da processare", len(aggiornamenti))

        for aggiornamento in aggiornamenti:
            # L'offset avanza SEMPRE, anche se il comando fallisce: altrimenti
            # un messaggio malformato verrebbe rielaborato a ogni run per sempre.
            try:
                self.stato.ultimo_update_id = max(
                    self.stato.ultimo_update_id, int(aggiornamento.get("update_id", 0))
                )
                self._gestisci(aggiornamento)
            except Exception as exc:   # nessun comando può far cadere il run
                log.exception("Errore nel processare un comando Telegram: %s", exc)

        if self._modificato:
            self._salva_e_committa()
            return True
        return False

    def _gestisci(self, aggiornamento: dict[str, Any]) -> None:
        messaggio = aggiornamento.get("message") or {}
        testo = str(messaggio.get("text") or "").strip()
        if not testo.startswith("/"):
            return

        mittente = str((messaggio.get("chat") or {}).get("id") or "")
        if mittente != self.notifier.chat_id:
            # Nessuna risposta al mittente non autorizzato: rispondere
            # confermerebbe l'esistenza del bot. Solo log.
            log.warning(
                "Comando ignorato da chat non autorizzata id=%s testo=%r",
                mittente, testo[:60],
            )
            return

        # "/list@NomeBot" nei gruppi: si toglie la parte dopo la chiocciola.
        parti = testo.split(maxsplit=1)
        comando = parti[0].split("@")[0].lower()
        argomenti = parti[1].strip() if len(parti) > 1 else ""

        gestori: dict[str, Callable[[str], None]] = {
            "/start": self._cmd_help,
            "/help": self._cmd_help,
            "/list": self._cmd_list,
            "/status": self._cmd_status,
            "/add": self._cmd_add,
            "/remove": self._cmd_remove,
            "/exclude": self._cmd_exclude,
            "/pause": lambda a: self._cmd_pausa(a, True),
            "/resume": lambda a: self._cmd_pausa(a, False),
            "/stop": self._cmd_stop,
            "/riprendi": self._cmd_riprendi,
            "/spegni": self._cmd_spegni,
        }
        gestore = gestori.get(comando)
        if gestore is None:
            self.notifier.invia_messaggio(
                f"Comando sconosciuto: <code>{esc(comando)}</code>\nUsa /help."
            )
            return

        log.info("Eseguo comando %s", comando)
        gestore(argomenti)

    # -- accesso al documento ---------------------------------------------

    @property
    def documento(self) -> Any:
        """Documento YAML caricato una sola volta e riusato fra i comandi."""
        if self._documento is None:
            self._documento = carica_documento(self.percorso_config)
        return self._documento

    def _segna_modifica(self, descrizione: str) -> None:
        self._modificato = True
        self._comandi_applicati.append(descrizione)

    def _salva_e_committa(self) -> None:
        """Valida, scrive su disco e committa il config modificato."""
        try:
            # Rete di sicurezza: non si committa mai un file che non si
            # riesce a rileggere come configurazione valida.
            configurazione_da_documento(self.documento)
        except ConfigError as exc:
            log.error("Modifiche scartate: produrrebbero un config non valido (%s)", exc)
            self.notifier.invia_messaggio(
                f"❌ Modifiche annullate: il risultato non sarebbe valido.\n"
                f"<code>{esc(str(exc))}</code>"
            )
            return

        salva_documento(self.documento, self.percorso_config)

        if self.dry_run:
            log.info("[prova] commit del config saltato (--dry-run)")
            return

        riepilogo = "; ".join(self._comandi_applicati[:5])
        successo = commit_config(
            serializza_documento(self.documento),
            token=self.token_github,
            repository=repository_corrente(),
            messaggio=f"config: {riepilogo}",
            percorso_repo=self.percorso_config,
            branch=branch_corrente(),
        )
        if successo:
            self.notifier.invia_messaggio(
                f"💾 config.yaml aggiornato sul repo.\n<i>{esc(riepilogo)}</i>"
            )
        else:
            self.notifier.invia_messaggio(
                "⚠️ Modifiche applicate a questo run ma <b>non</b> committate sul repo: "
                "andranno perse al prossimo run.\n"
                "Verifica che il workflow abbia <code>permissions: contents: write</code>."
            )

    # -- comandi di sola lettura ------------------------------------------

    def _cmd_help(self, _: str) -> None:
        self.notifier.invia_messaggio(AIUTO)

    def _cmd_list(self, _: str) -> None:
        try:
            configurazione = configurazione_da_documento(self.documento)
        except ConfigError as exc:
            self.notifier.invia_messaggio(f"❌ config.yaml non valido:\n<code>{esc(str(exc))}</code>")
            return

        if not configurazione.ricerche:
            self.notifier.invia_messaggio("Nessuna ricerca configurata. Usa /add.")
            return

        righe = [f"<b>Ricerche configurate ({len(configurazione.ricerche)})</b>", ""]
        for ricerca in configurazione.ricerche:
            if not ricerca.attiva:
                icona = "🚫"
            elif ricerca.in_pausa:
                icona = "⏸"
            else:
                icona = "▶️"

            prezzo = "—"
            if ricerca.prezzo_min is not None or ricerca.prezzo_max is not None:
                minimo = f"{ricerca.prezzo_min:.0f}" if ricerca.prezzo_min is not None else "0"
                massimo = f"{ricerca.prezzo_max:.0f}" if ricerca.prezzo_max is not None else "∞"
                prezzo = f"{minimo}-{massimo} €"

            ultima = self.stato.ultima_esecuzione(ricerca.nome)
            righe.append(f"{icona} <b>{esc(ricerca.nome)}</b>")
            righe.append(f"   🔎 <code>{esc(ricerca.parole_chiave)}</code>")
            righe.append(
                f"   {esc(', '.join(ricerca.piattaforme))} · {prezzo} · "
                f"ogni {ricerca.intervallo_minuti}′ · {esc(ricerca.condizione)}"
            )
            if ricerca.parole_escluse:
                anteprima = ", ".join(ricerca.parole_escluse[:6])
                if len(ricerca.parole_escluse) > 6:
                    anteprima += f" (+{len(ricerca.parole_escluse) - 6})"
                righe.append(f"   🚫 {esc(anteprima)}")
            righe.append(f"   🕒 ultimo controllo: {esc(formatta_completo(ultima, self.tz_locale))}")
            righe.append("")

        self.notifier.invia_messaggio("\n".join(righe))

    def _cmd_status(self, _: str) -> None:
        run = self.stato.ultimo_run
        righe = ["<b>Stato del monitor</b>", ""]

        if not run:
            righe.append("Nessun run completato finora.")
        else:
            righe.append(f"Ultimo run: <b>{esc(run.get('terminato') or '?')}</b>")
            righe.append(
                f"Durata {run.get('durata_s', 0)}s · "
                f"{run.get('richieste', 0)} richieste · "
                f"{run.get('nuovi', 0)} nuovi · "
                f"{run.get('notificati', 0)} notificati"
            )
            righe.append(f"Esito: <b>{esc(run.get('esito') or '?')}</b>")

        righe.append("")
        righe.append("<b>Piattaforme</b>")
        salute = self.stato.salute_piattaforme()
        if not salute:
            righe.append("Nessuna piattaforma ancora interrogata.")
        for piattaforma, voce in sorted(salute.items()):
            icona = {
                "ok": "✅", "vuoto": "➖", "bloccato": "⛔",
                "errore": "❌", "quarantena": "⏸",
            }.get(str(voce.get("ultimo_esito")), "❔")
            riga = f"{icona} <b>{esc(piattaforma)}</b>: {esc(voce.get('ultimo_esito') or 'mai')}"
            a_vuoto = int(voce.get("run_zero_consecutivi") or 0)
            if a_vuoto:
                riga += f" · {a_vuoto} run a vuoto"
            quarantena = int(voce.get("quarantena_run") or 0)
            if quarantena:
                riga += f" · in pausa per {quarantena} run"
            righe.append(riga)
            if voce.get("ultimo_errore"):
                righe.append(f"   <code>{esc(str(voce['ultimo_errore'])[:150])}</code>")

        # Stato del trigger esterno: è lui che fa partire tutto, e sapere
        # se è acceso vale quanto sapere com'è andato l'ultimo controllo.
        import os
        chiave = os.environ.get("CRONJOB_API_KEY", "")
        job_id = os.environ.get("CRONJOB_JOB_ID", "")
        if chiave and job_id:
            from trigger_esterno import stato_job
            righe.append("")
            try:
                trigger = stato_job(chiave, job_id)
                icona = "🟢" if trigger["attivo"] else "🔴"
                righe.append(
                    f"{icona} <b>Trigger esterno</b>: "
                    + ("attivo" if trigger["attivo"] else "SPENTO")
                )
            except Exception as exc:
                righe.append(f"❔ <b>Trigger esterno</b>: {esc(str(exc)[:80])}")

        errori = run.get("errori") or []
        if errori:
            righe.append("")
            righe.append("<b>Errori dell'ultimo run</b>")
            for errore in errori[:5]:
                righe.append(f"• <code>{esc(str(errore)[:160])}</code>")

        self.notifier.invia_messaggio("\n".join(righe))

    # -- comandi che modificano il config ---------------------------------

    def _cmd_add(self, argomenti: str) -> None:
        if not argomenti:
            self.notifier.invia_messaggio(
                "Uso:\n<code>/add nome=iphone13 | kw=iphone 13 | piattaforme=ebay,subito | max=320</code>"
                "\n\nVedi /help per l'elenco completo dei campi."
            )
            return

        try:
            campi = _analizza_campi(argomenti)
        except ValueError as exc:
            self.notifier.invia_messaggio(f"❌ {esc(str(exc))}")
            return

        if not campi.get("nome") or not campi.get("parole_chiave"):
            self.notifier.invia_messaggio(
                "❌ Servono almeno <code>nome</code> e <code>kw</code>."
            )
            return

        try:
            nome = modifica_aggiungi_ricerca(self.documento, campi)
        except ConfigError as exc:
            self.notifier.invia_messaggio(f"❌ Impossibile aggiungere: {esc(str(exc))}")
            return

        self._segna_modifica(f"aggiunta ricerca '{nome}'")
        self.notifier.invia_messaggio(
            f"✅ Ricerca <b>{esc(nome)}</b> aggiunta.\n"
            f"🔎 <code>{esc(str(campi['parole_chiave']))}</code>\n"
            f"Piattaforme: {esc(', '.join(campi.get('piattaforme') or ['ebay']))}\n\n"
            "<i>Partirà dal prossimo controllo. Al primo giro notifica solo gli "
            "annunci recenti, non tutto lo storico.</i>"
        )

    def _cmd_remove(self, argomenti: str) -> None:
        nome = argomenti.strip().split()[0].lower() if argomenti.strip() else ""
        if not nome:
            self.notifier.invia_messaggio("Uso: <code>/remove &lt;nome&gt;</code>")
            return
        if modifica_rimuovi_ricerca(self.documento, nome):
            self._segna_modifica(f"rimossa ricerca '{nome}'")
            self.notifier.invia_messaggio(f"🗑 Ricerca <b>{esc(nome)}</b> rimossa.")
        else:
            self.notifier.invia_messaggio(
                f"❌ Nessuna ricerca chiamata <b>{esc(nome)}</b>. Usa /list."
            )

    def _cmd_exclude(self, argomenti: str) -> None:
        parti = argomenti.split(maxsplit=1)
        if len(parti) < 2:
            self.notifier.invia_messaggio(
                "Uso: <code>/exclude &lt;ricerca&gt; &lt;parola&gt;</code>\n"
                "La parola può contenere spazi: <code>/exclude iphone13 non funzionante</code>"
            )
            return
        nome, parola = parti[0].strip().lower(), parti[1].strip()
        if modifica_aggiungi_esclusa(self.documento, nome, parola):
            self._segna_modifica(f"esclusa '{parola}' da '{nome}'")
            self.notifier.invia_messaggio(
                f"🚫 <code>{esc(parola)}</code> aggiunta alle parole escluse di "
                f"<b>{esc(nome)}</b>."
            )
        else:
            self.notifier.invia_messaggio(
                f"❌ Nessuna ricerca chiamata <b>{esc(nome)}</b>. Usa /list."
            )

    def _cmd_pausa(self, argomenti: str, in_pausa: bool) -> None:
        nome = argomenti.strip().split()[0].lower() if argomenti.strip() else ""
        azione = "sospesa" if in_pausa else "riattivata"
        if not nome:
            comando = "/pause" if in_pausa else "/resume"
            self.notifier.invia_messaggio(f"Uso: <code>{comando} &lt;nome&gt;</code>")
            return
        if modifica_pausa(self.documento, nome, in_pausa):
            self._segna_modifica(f"ricerca '{nome}' {azione}")
            icona = "⏸" if in_pausa else "▶️"
            self.notifier.invia_messaggio(f"{icona} Ricerca <b>{esc(nome)}</b> {azione}.")
        else:
            self.notifier.invia_messaggio(
                f"❌ Nessuna ricerca chiamata <b>{esc(nome)}</b>. Usa /list."
            )


    def _cmd_stop(self, _: str) -> None:
        """Sospende tutte le ricerche attive con un solo comando."""
        cambiate = modifica_pausa_tutte(self.documento, True)
        if not cambiate:
            self.notifier.invia_messaggio(
                "⏸ Nessuna ricerca attiva da sospendere: è già tutto fermo.\n"
                "Usa /list per vedere lo stato."
            )
            return

        # Si ricorda QUALI sono state sospese: /riprendi riattiverà solo
        # queste, senza risvegliare ciò che avevi messo in pausa a mano.
        self.stato.sospese_da_stop = cambiate
        self._segna_modifica(f"sospese tutte le ricerche ({len(cambiate)})")

        elenco = "\n".join(f"• {esc(n)}" for n in cambiate)
        self.notifier.invia_messaggio(
            f"⏸ <b>Sospese {len(cambiate)} ricerche</b>\n{elenco}\n\n"
            "Nessun marketplace verrà più interrogato. Con /riprendi torni "
            "esattamente a questa situazione.\n"
            "<i>Il monitor continua a girare e a leggere i comandi.</i>"
        )

    def _cmd_riprendi(self, _: str) -> None:
        """Riattiva le ricerche sospese dall'ultimo /stop."""
        memorizzate = self.stato.sospese_da_stop

        if memorizzate:
            cambiate = modifica_pausa_tutte(self.documento, False, solo=set(memorizzate))
            nota = ""
        else:
            # Nessuna memoria: succede se /stop è stato dato prima che questa
            # funzione esistesse, o se lo stato è andato perso. Si riattiva
            # tutto, dicendolo chiaramente invece di fare finta di niente.
            cambiate = modifica_pausa_tutte(self.documento, False)
            nota = (
                "\n\n<i>Non avevo memoria di quali fossero sospese da /stop, "
                "quindi le ho riattivate tutte. Se qualcuna doveva restare "
                "ferma, usa /pause.</i>"
            )

        if not cambiate:
            self.notifier.invia_messaggio(
                "▶️ Nessuna ricerca da riattivare: sono già tutte in funzione.\n"
                "Usa /list per vedere lo stato."
            )
            return

        self.stato.sospese_da_stop = []
        self._segna_modifica(f"riattivate {len(cambiate)} ricerche")

        elenco = "\n".join(f"• {esc(n)}" for n in cambiate)
        self.notifier.invia_messaggio(
            f"▶️ <b>Riattivate {len(cambiate)} ricerche</b>\n{elenco}{nota}\n\n"
            "<i>Ripartono dal prossimo controllo.</i>"
        )


    def _cmd_spegni(self, argomenti: str) -> None:
        """
        Interruttore generale, diverso da /stop che sospende solo le ricerche.
        Senza run il bot non legge più i comandi: da Telegram non si torna
        indietro, quindi pretende conferma esplicita.
        """
        import os
        from trigger_esterno import imposta_attivo, stato_job

        chiave = os.environ.get("CRONJOB_API_KEY", "")
        job_id = os.environ.get("CRONJOB_JOB_ID", "")

        if not chiave or not job_id:
            self.notifier.invia_messaggio(
                "🔌 Il controllo del trigger esterno non è configurato.\n\n"
                "Servono i secret <code>CRONJOB_API_KEY</code> e "
                "<code>CRONJOB_JOB_ID</code>. Vedi il README.\n\n"
                "Nel frattempo /stop sospende tutte le ricerche: il monitor "
                "continua a girare a vuoto ma non interroga più nulla."
            )
            return

        if argomenti.strip().lower() != "conferma":
            try:
                stato = stato_job(chiave, job_id)
                acceso = "acceso" if stato["attivo"] else "già spento"
            except Exception:
                acceso = "stato ignoto"
            self.notifier.invia_messaggio(
                f"⚠️ <b>Vuoi davvero fermare il trigger esterno?</b>\n"
                f"Stato attuale: <b>{esc(acceso)}</b>\n\n"
                "Il monitor smetterà di partire. Di conseguenza <b>il bot non "
                "leggerà più i comandi</b>: da Telegram non potrai riaccenderlo.\n\n"
                "Si riaccende dalla <b>dashboard</b> (pulsante «Riattiva il "
                "trigger») oppure da console.cron-job.org.\n\n"
                "Se è quello che vuoi, manda:\n<code>/spegni conferma</code>\n\n"
                "<i>Se invece ti basta fermare le ricerche tenendo il bot "
                "raggiungibile, usa /stop.</i>"
            )
            return

        riuscito, messaggio = imposta_attivo(chiave, job_id, False)
        if riuscito:
            self.notifier.invia_messaggio(
                "🔌 <b>Trigger esterno disattivato.</b>\n\n"
                "Questo è l'ultimo messaggio: senza run non leggo più i comandi.\n"
                "Per riaccendere, apri la dashboard e premi «Riattiva il trigger»."
            )
        else:
            self.notifier.invia_messaggio(
                f"❌ Non sono riuscito a fermarlo: {esc(messaggio)}"
            )


# ---------------------------------------------------------------------------
# Parsing dei parametri di /add
# ---------------------------------------------------------------------------

def _analizza_campi(argomenti: str) -> dict[str, Any]:
    """Converte "nome=x | kw=y z | max=100" in un dizionario. I ValueError
    che solleva finiscono direttamente su Telegram."""
    campi: dict[str, Any] = {}

    for pezzo in argomenti.replace("\n", "|").split("|"):
        pezzo = pezzo.strip()
        if not pezzo:
            continue
        if "=" not in pezzo:
            raise ValueError(
                f"parametro senza '=': <code>{esc(pezzo[:40])}</code>. "
                "Formato atteso: <code>chiave=valore</code>, separati da |"
            )
        chiave_grezza, valore = pezzo.split("=", 1)
        chiave = _ALIAS.get(chiave_grezza.strip().lower())
        if chiave is None:
            raise ValueError(
                f"parametro sconosciuto: <code>{esc(chiave_grezza.strip())}</code>. "
                f"Ammessi: {', '.join(sorted(set(_ALIAS)))}"
            )
        valore = valore.strip()

        if chiave in ("piattaforme", "parole_escluse"):
            elementi = [v.strip() for v in valore.split(",") if v.strip()]
            if chiave == "piattaforme":
                elementi = [e.lower() for e in elementi]
                ignote = set(elementi) - Piattaforma.valide()
                if ignote:
                    raise ValueError(
                        f"piattaforme sconosciute: {', '.join(sorted(ignote))}. "
                        f"Ammesse: {', '.join(sorted(Piattaforma.valide()))}"
                    )
            campi[chiave] = elementi

        elif chiave in ("prezzo_min", "prezzo_max"):
            try:
                campi[chiave] = float(valore.replace(",", "."))
            except ValueError:
                raise ValueError(f"'{esc(valore)}' non è un prezzo valido") from None

        elif chiave in ("intervallo_minuti", "raggio_km"):
            try:
                campi[chiave] = max(0, int(valore))
            except ValueError:
                raise ValueError(f"'{esc(valore)}' non è un numero intero") from None

        elif chiave == "condizione":
            valore = valore.lower()
            if valore not in Condizione.valide():
                raise ValueError(
                    f"condizione '{esc(valore)}' non valida. "
                    f"Ammesse: {', '.join(sorted(Condizione.valide()))}"
                )
            campi[chiave] = valore

        elif chiave == "solo_titolo":
            campi[chiave] = valore.lower() in ("true", "si", "sì", "1", "yes", "on")

        elif chiave == "nome":
            # Il nome è un identificatore: niente spazi né maiuscole.
            campi[chiave] = valore.lower().replace(" ", "-")

        else:
            campi[chiave] = valore

    # `shlex` non serve al parsing ma normalizza gli apici eventualmente usati
    # attorno alle parole chiave da chi digita da desktop.
    if isinstance(campi.get("parole_chiave"), str):
        testo = campi["parole_chiave"]
        if testo[:1] in ("'", '"'):
            try:
                campi["parole_chiave"] = " ".join(shlex.split(testo))
            except ValueError:
                pass

    return campi
