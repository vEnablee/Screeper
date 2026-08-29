"""
Sistema di design: colori, icone e stili in un posto solo.

I colori delle serie sono i primi tre slot di una palette categorica
verificata su superficie scura: luminosità, croma, separazione per daltonismo
(ΔE 9.4 su 8 richiesto), separazione a vista normale (20.9 su 15) e contrasto
≥ 3:1. Sostituendoli a mano quelle garanzie decadono.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Colori
# ---------------------------------------------------------------------------

SUPERFICIE_0 = "#121211"   # fondo pagina
SUPERFICIE_1 = "#1a1a19"   # schede
SUPERFICIE_2 = "#232322"   # sopraelevazione, stati hover
BORDO = "#2e2e2c"
BORDO_FORTE = "#3d3d3a"

TESTO_1 = "#f2f2ee"        # titoli
TESTO_2 = "#c3c2b7"        # corpo
TESTO_3 = "#8a8a80"        # note, metadati

# Serie categoriche: una per piattaforma, assegnate in ordine fisso e mai
# ruotate. Il colore segue l'entità, non la sua posizione in classifica.
COLORI_PIATTAFORMA: dict[str, str] = {
    "ebay": "#3987e5",     # slot 1 — blu
    "subito": "#d95926",   # slot 2 — arancio
    "vinted": "#199e70",   # slot 3 — verde acqua
}
COLORE_NEUTRO = "#6f6f66"

# Piattaforme proposte nell'interfaccia. eBay è escluso: senza credenziali API
# ripiega sull'HTML, rifiutato con 403 dagli indirizzi dei runner GitHub. Per
# riabilitarlo basta aggiungerlo qui. Il colore resta definito sopra, così gli
# annunci eBay già in archivio mantengono la loro tinta.
PIATTAFORME_UTILIZZABILI: tuple[str, ...] = ("subito", "vinted")

# Colori di stato: riservati, mai riusati come "quarta serie".
STATO = {
    "ok": "#199e70",
    "attenzione": "#c98500",
    "errore": "#e66767",
    "inattivo": "#6f6f66",
}

ACCENTO = "#3987e5"


def colore_piattaforma(nome: str) -> str:
    return COLORI_PIATTAFORMA.get(str(nome).lower(), COLORE_NEUTRO)


# ---------------------------------------------------------------------------
# Icone — Material Symbols ridisegnate come SVG in linea.
# Ereditano currentColor, quindi si tingono dal contesto senza duplicati.
# ---------------------------------------------------------------------------

def _svg(percorso: str, dimensione: int = 16) -> str:
    return (
        f'<svg class="icona" width="{dimensione}" height="{dimensione}" '
        f'viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true">'
        f'<path d="{percorso}"/></svg>'
    )


_PERCORSI = {
    "luogo": "M480-480q33 0 56.5-23.5T560-560q0-33-23.5-56.5T480-640q-33 0-56.5 23.5T400-560q0 33 23.5 56.5T480-480Zm0 294q122-112 181-203.5T720-552q0-109-69.5-178.5T480-800q-101 0-170.5 69.5T240-552q0 71 59 162.5T480-186Zm0 106Q319-217 239.5-334.5T160-552q0-150 96.5-239T480-880q127 0 223.5 89T800-552q0 100-79.5 217.5T480-80Z",
    "orologio": "m612-292 56-56-148-148v-184h-80v216l172 172ZM480-80q-83 0-156-31.5T197-197q-54-54-85.5-127T80-480q0-83 31.5-156T197-763q54-54 127-85.5T480-880q83 0 156 31.5T763-763q54 54 85.5 127T880-480q0 83-31.5 156T763-197q-54 54-127 85.5T480-80Z",
    "apri": "M200-120q-33 0-56.5-23.5T120-200v-560q0-33 23.5-56.5T200-840h280v80H200v560h560v-280h80v280q0 33-23.5 56.5T760-120H200Zm188-212-56-56 372-372H600v-80h240v240h-80v-104L388-332Z",
    "negozio": "M840-680H120v-80h720v80ZM160-120v-240h-80v-80l40-200h720l40 200v80h-80v240h-80v-240H600v240H160Zm80-80h280v-160H240v160Z",
    "prezzo": "M853-478 526-805q-11-11-25.5-17.5T469-829H180q-33 0-56.5 23.5T100-749v289q0 17 6.5 31.5T124-403l327 327q23 23 57 23t57-23l288-288q23-23 23-56.5T853-478ZM280-600q-25 0-42.5-17.5T220-660q0-25 17.5-42.5T280-720q25 0 42.5 17.5T340-660q0 25-17.5 42.5T280-600Z",
    "cerca": "M784-120 532-372q-30 24-69 38t-83 14q-109 0-184.5-75.5T120-580q0-109 75.5-184.5T380-840q109 0 184.5 75.5T640-580q0 44-14 83t-38 69l252 252-56 56ZM380-400q75 0 127.5-52.5T560-580q0-75-52.5-127.5T380-760q-75 0-127.5 52.5T200-580q0 75 52.5 127.5T380-400Z",
    "grafico": "M120-120v-80l80-80v160h-80Zm160 0v-240l80-80v320h-80Zm160 0v-320l80 81v239h-80Zm160 0v-239l80-80v319h-80Zm160 0v-400l80-80v480h-80ZM120-327v-113l280-280 160 160 280-280v113L560-447 400-607 120-327Z",
    "impostazioni": "m370-80-16-128q-13-5-24.5-12T307-235l-119 50L78-375l103-78q-1-7-1-13.5v-27q0-6.5 1-13.5L78-585l110-190 119 50q11-8 23-15t24-12l16-128h220l16 128q13 5 24.5 12t22.5 15l119-50 110 190-103 78q1 7 1 13.5v27q0 6.5-2 13.5l103 78-110 190-118-50q-11 8-23 15t-24 12L590-80H370Zm112-260q58 0 99-41t41-99q0-58-41-99t-99-41q-59 0-99.5 41T342-480q0 58 40.5 99t99.5 41Z",
    "pausa": "M520-200v-560h240v560H520Zm-320 0v-560h240v560H200Z",
    "riproduci": "M320-200v-560l440 280-440 280Z",
    "aggiorna": "M480-160q-134 0-227-93t-93-227q0-134 93-227t227-93q69 0 132 28.5T720-690v-110h80v280H520v-80h168q-32-56-87.5-88T480-720q-100 0-170 70t-70 170q0 100 70 170t170 70q77 0 139-44t87-116h84q-28 106-114 173t-196 67Z",
    "avviso": "M109-120q-11 0-20-5.5T75-140q-5-9-5.5-19.5T75-180l371-640q6-10 15.5-15t19.5-5q10 0 19.5 5t15.5 15l371 640q6 10 5.5 20.5T887-140q-5 9-14 14.5t-20 5.5H109Zm371-120q17 0 28.5-11.5T520-280q0-17-11.5-28.5T480-320q-17 0-28.5 11.5T440-280q0 17 11.5 28.5T480-240Zm0-120q17 0 28.5-11.5T520-400v-120q0-17-11.5-28.5T480-560q-17 0-28.5 11.5T440-520v120q0 17 11.5 28.5T480-360Z",
    "ok": "M382-240 154-468l57-57 171 171 367-367 57 57-424 424Z",
    "elimina": "M280-120q-33 0-56.5-23.5T200-200v-520h-40v-80h200v-40h240v40h200v80h-40v520q0 33-23.5 56.5T680-120H280Zm120-160h80v-360h-80v360Zm160 0h80v-360h-80v360Z",
    "aggiungi": "M440-440H200v-80h240v-240h80v240h240v80H520v240h-80v-240Z",
    "ordina": "M120-240v-80h240v80H120Zm0-200v-80h480v80H120Zm0-200v-80h720v80H120Z",
    "su": "M440-160v-487L216-423l-56-57 320-320 320 320-56 57-224-224v487h-80Z",
    "giu": "M440-800v487L216-537l-56 57 320 320 320-320-56-57-224 224v-487h-80Z",
    "nuovo": "m422-232 207-248H469l29-227-185 267h139l-30 208ZM320-80l40-280H160l360-520h80l-40 320h240L400-80h-80Z",
    "filtro": "M440-160q-17 0-28.5-11.5T400-200v-240L168-736q-15-20-4.5-42t36.5-22h560q26 0 36.5 22t-4.5 42L560-440v240q0 17-11.5 28.5T520-160h-80Z",
    "calendario": "M200-80q-33 0-56.5-23.5T120-160v-560q0-33 23.5-56.5T200-800h40v-80h80v80h320v-80h80v80h40q33 0 56.5 23.5T840-720v560q0 33-23.5 56.5T760-80H200Zm0-80h560v-400H200v400Z",
    "tendenza": "m136-240-56-56 296-298 160 160 208-206H640v-80h240v240h-80v-104L536-320 376-480 136-240Z",
    "inventario": "M200-80q-33 0-56.5-23.5T120-160v-451q-18-11-29-28.5T80-680v-120q0-33 23.5-56.5T160-880h640q33 0 56.5 23.5T880-800v120q0 23-11 40.5T840-611v451q0 33-23.5 56.5T760-80H200Zm0-520v440h560v-440H200Zm-40-80h640v-120H160v120Zm200 280h240v-80H360v80Z",
}


def icona(nome: str, dimensione: int = 16) -> str:
    """SVG in linea di un'icona, o stringa vuota se il nome è ignoto."""
    percorso = _PERCORSI.get(nome)
    return _svg(percorso, dimensione) if percorso else ""


# ---------------------------------------------------------------------------
# Foglio di stile
# ---------------------------------------------------------------------------

CSS = """
<style>
:root {
  --sup-0: #121211;  --sup-1: #1a1a19;  --sup-2: #232322;
  --bordo: #2e2e2c;  --bordo-forte: #3d3d3a;
  --t1: #f2f2ee;     --t2: #c3c2b7;     --t3: #8a8a80;
  --accento: #3987e5;
  --ebay: #3987e5;   --subito: #d95926; --vinted: #199e70;
  --raggio: 12px;
}

/* Respiro della pagina: il default di Streamlit è troppo stretto in alto
   e troppo largo ai lati sugli schermi grandi. */
.block-container { padding-top: 2.2rem !important; padding-bottom: 4rem !important; max-width: 1400px; }
#MainMenu, footer { visibility: hidden; }

.icona { vertical-align: -0.15em; flex: none; }

/* ---------- intestazione di pagina ---------- */
.intestazione { margin: 0 0 1.6rem; }
.intestazione h1 {
  font-size: 1.65rem; font-weight: 650; letter-spacing: -0.02em;
  color: var(--t1); margin: 0 0 .3rem; display: flex; align-items: center; gap: .6rem;
}
.intestazione p { color: var(--t3); font-size: .92rem; margin: 0; }

/* ---------- riquadri statistici ---------- */
.riquadri { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: .8rem; margin-bottom: 1.6rem; }
.riquadro {
  background: var(--sup-1); border: 1px solid var(--bordo);
  border-radius: var(--raggio); padding: .95rem 1.1rem;
}
.riquadro .etichetta {
  color: var(--t3); font-size: .72rem; text-transform: uppercase;
  letter-spacing: .07em; font-weight: 600; margin-bottom: .35rem;
  display: flex; align-items: center; gap: .35rem;
}
.riquadro .valore { color: var(--t1); font-size: 1.55rem; font-weight: 650; line-height: 1.15; letter-spacing: -0.02em; }
.riquadro .nota { color: var(--t3); font-size: .78rem; margin-top: .2rem; }

/* ---------- griglia degli annunci ---------- */
.griglia { display: grid; grid-template-columns: repeat(auto-fill, minmax(272px, 1fr)); gap: 1rem; }
.scheda {
  background: var(--sup-1); border: 1px solid var(--bordo); border-radius: var(--raggio);
  overflow: hidden; display: flex; flex-direction: column;
  transition: border-color .15s ease, transform .15s ease;
}
.scheda:hover { border-color: var(--bordo-forte); transform: translateY(-2px); }
.scheda .miniatura {
  height: 168px; background: var(--sup-2) center/cover no-repeat;
  border-bottom: 1px solid var(--bordo); position: relative;
}
.scheda .miniatura.vuota { display: flex; align-items: center; justify-content: center; color: #4a4a45; }
.scheda .corpo { padding: .85rem .95rem 1rem; display: flex; flex-direction: column; gap: .5rem; flex: 1; }
.scheda .prezzo { color: var(--t1); font-size: 1.3rem; font-weight: 680; letter-spacing: -0.02em; line-height: 1; }
.scheda .prezzo .spedizione { font-size: .72rem; font-weight: 500; color: var(--t3); margin-left: .4rem; letter-spacing: 0; }
.scheda .titolo {
  color: var(--t2); font-size: .88rem; font-weight: 500; line-height: 1.4; margin: 0;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.scheda .meta { display: flex; flex-wrap: wrap; align-items: center; gap: .45rem; color: var(--t3); font-size: .75rem; }
.scheda .meta span { display: inline-flex; align-items: center; gap: .25rem; }
.scheda .piede {
  margin-top: auto; padding-top: .7rem; border-top: 1px solid var(--bordo);
  display: flex; align-items: center; justify-content: space-between; gap: .5rem;
}
.scheda .quando { color: var(--t3); font-size: .74rem; display: inline-flex; align-items: center; gap: .3rem; }
.scheda .apri {
  display: inline-flex; align-items: center; gap: .35rem; text-decoration: none;
  background: var(--sup-2); color: var(--t1); border: 1px solid var(--bordo-forte);
  padding: .38rem .7rem; border-radius: 8px; font-size: .78rem; font-weight: 550;
  transition: background .15s ease, border-color .15s ease;
}
.scheda .apri:hover { background: var(--accento); border-color: var(--accento); color: #fff; }

/* ---------- etichette di piattaforma ---------- */
.pastiglia {
  display: inline-flex; align-items: center; gap: .3rem;
  padding: .16rem .5rem; border-radius: 999px; font-size: .7rem;
  font-weight: 600; letter-spacing: .02em; border: 1px solid;
}
.pastiglia .punto { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.p-ebay   { color: var(--ebay);   border-color: color-mix(in srgb, var(--ebay) 35%, transparent);   background: color-mix(in srgb, var(--ebay) 12%, transparent); }
.p-subito { color: var(--subito); border-color: color-mix(in srgb, var(--subito) 35%, transparent); background: color-mix(in srgb, var(--subito) 12%, transparent); }
.p-vinted { color: var(--vinted); border-color: color-mix(in srgb, var(--vinted) 35%, transparent); background: color-mix(in srgb, var(--vinted) 12%, transparent); }
.p-altro  { color: var(--t3);     border-color: var(--bordo); background: var(--sup-2); }

/* ---------- stato vuoto ---------- */
.vuoto {
  background: var(--sup-1); border: 1px dashed var(--bordo-forte); border-radius: var(--raggio);
  padding: 3rem 2rem; text-align: center; color: var(--t3);
}
.vuoto .simbolo { color: #45453f; margin-bottom: .8rem; }
.vuoto h3 { color: var(--t2); font-size: 1rem; font-weight: 600; margin: 0 0 .4rem; }
.vuoto p { font-size: .86rem; margin: 0 auto; max-width: 460px; line-height: 1.6; }

/* ---------- scheda di una ricerca configurata ---------- */
.ricerca {
  background: var(--sup-1); border: 1px solid var(--bordo); border-left: 3px solid var(--bordo-forte);
  border-radius: var(--raggio); padding: 1rem 1.15rem; margin-bottom: .7rem;
}
.ricerca.attiva { border-left-color: var(--vinted); }
.ricerca.sospesa { border-left-color: var(--t3); }
.ricerca .riga1 { display: flex; align-items: center; gap: .6rem; margin-bottom: .55rem; flex-wrap: wrap; }
.ricerca .nome { color: var(--t1); font-size: 1rem; font-weight: 640; letter-spacing: -0.01em; }
.ricerca .chiavi {
  font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .82rem;
  color: var(--t2); background: var(--sup-2); border: 1px solid var(--bordo);
  padding: .2rem .5rem; border-radius: 6px;
}
.ricerca .dettagli { display: flex; flex-wrap: wrap; gap: .35rem .9rem; color: var(--t3); font-size: .78rem; }
.ricerca .dettagli b { color: var(--t2); font-weight: 600; }
.ricerca .escluse { margin-top: .5rem; color: var(--t3); font-size: .74rem; line-height: 1.6; }
.ricerca .escluse code {
  background: var(--sup-2); border: 1px solid var(--bordo); border-radius: 4px;
  padding: .05rem .32rem; margin-right: .25rem; color: var(--t3); font-size: .72rem;
}

/* ---------- barra dei filtri ---------- */
.titolo-sezione {
  color: var(--t3); font-size: .74rem; text-transform: uppercase; letter-spacing: .08em;
  font-weight: 650; margin: 1.8rem 0 .7rem; display: flex; align-items: center; gap: .4rem;
}

/* ---------- indicatore di novità ---------- */
.nuovo {
  position: absolute; top: .55rem; left: .55rem; z-index: 2;
  display: inline-flex; align-items: center; gap: .3rem;
  padding: .2rem .55rem; border-radius: 999px;
  font-size: .68rem; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
  color: #0b1f16; background: var(--vinted);
  box-shadow: 0 2px 10px rgba(25,158,112,.45);
}
.nuovo .impulso {
  width: 6px; height: 6px; border-radius: 50%; background: currentColor;
  animation: impulso 2s ease-in-out infinite;
}
@keyframes impulso { 0%,100% { opacity: 1; } 50% { opacity: .25; } }

.scheda.recente { border-color: color-mix(in srgb, var(--vinted) 45%, var(--bordo)); }
.scheda.recente .quando { color: var(--vinted); font-weight: 600; }

/* ---------- comparsa scaglionata ---------- */
.scheda {
  animation: comparsa .34s cubic-bezier(.22,.8,.3,1) both;
  animation-delay: calc(var(--i, 0) * 26ms);
}
@keyframes comparsa {
  from { opacity: 0; transform: translateY(10px); }
  to   { opacity: 1; transform: none; }
}

/* ---------- miniatura: leggero avvicinamento al passaggio ---------- */
.scheda .miniatura { transition: transform .4s cubic-bezier(.22,.8,.3,1); transform-origin: center; }
.scheda:hover .miniatura { transform: scale(1.045); }
.scheda .involucro-miniatura { overflow: hidden; position: relative; }

/* ---------- barra di sezione con controlli a destra ---------- */
.barra-sezione {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: 1rem; margin: 2rem 0 .2rem; padding-bottom: .55rem;
  border-bottom: 1px solid var(--bordo);
}
.barra-sezione .nome-sezione {
  display: flex; align-items: center; gap: .45rem;
  color: var(--t1); font-size: 1.05rem; font-weight: 640; letter-spacing: -0.01em;
}
.barra-sezione .conteggio { color: var(--t3); font-size: .8rem; font-weight: 500; }

/* ---------- rispetto delle preferenze di movimento ---------- */
@media (prefers-reduced-motion: reduce) {
  .scheda, .scheda .miniatura, .nuovo .impulso { animation: none !important; transition: none !important; }
  .scheda:hover { transform: none; }
  .scheda:hover .miniatura { transform: none; }
}

/* ---------- controlli Streamlit un po' meno "demo" ---------- */
div[data-testid="stSidebarUserContent"] { padding-top: 1rem; }
.stButton button { border-radius: 8px; font-weight: 550; }
div[data-baseweb="select"] > div { border-radius: 8px; border-color: var(--bordo); }
hr { border-color: var(--bordo); }
</style>
"""
