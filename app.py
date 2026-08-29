"""
Dashboard di SCreeper: Annunci, Statistiche, Ricerche.

Ogni ricerca ha la sua scheda con le sue statistiche: confrontare il prezzo
di un PS5 con quello di una giacca non significa nulla.

I dati arrivano dal Gist scritto dal monitor; senza segreti si ripiega sul
file locale `.state/stato.json`.
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import design
from gist_client import GistClient, GistError

NOME_APP = "SCreeper"
TZ = "Europe/Rome"
GIORNI_STORICO = 30
# Sotto questa soglia la mediana non dice nulla e il giudizio di convenienza
# viene omesso invece di essere inventato.
CAMPIONE_MINIMO = 4
SOGLIA_CONVENIENTE = -0.15   # -15% sulla mediana
SOGLIA_CARO = 0.15

# Quando un annuncio è "appena pubblicato". Il monitor controlla ogni 15
# minuti, quindi un'ora copre abbondantemente ciò che non avevi ancora visto.
MINUTI_NUOVO = 60
ORE_RECENTE = 6

PERCORSO_STATO_LOCALE = Path(".state/stato.json")

st.set_page_config(
    page_title=NOME_APP,
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(design.CSS, unsafe_allow_html=True)


# ===========================================================================
# Dati
# ===========================================================================

def _segreto(nome: str) -> str:
    """Legge un segreto, con ripiego sulle variabili d'ambiente per il locale."""
    try:
        valore = st.secrets.get(nome)  # type: ignore[union-attr]
        if valore:
            return str(valore)
    except Exception:
        pass
    return os.environ.get(nome, "")


def _fuso() -> ZoneInfo:
    try:
        return ZoneInfo(TZ)
    except Exception:
        return ZoneInfo("UTC")


def _data(valore: Any) -> datetime | None:
    if not valore:
        return None
    try:
        dt = datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@st.cache_data(ttl=60, show_spinner=False)
def carica_stato() -> tuple[dict[str, Any], str]:
    """Restituisce `(stato, origine)` con origine "gist" o "locale"."""
    token, gist_id = _segreto("GIST_TOKEN"), _segreto("GIST_ID")
    if token and gist_id:
        return GistClient(token, gist_id).leggi(), "gist"
    if PERCORSO_STATO_LOCALE.is_file():
        with PERCORSO_STATO_LOCALE.open(encoding="utf-8") as f:
            return json.load(f), "locale"
    raise GistError(
        "GIST_TOKEN o GIST_ID non configurati, e nessuno stato locale in "
        f"{PERCORSO_STATO_LOCALE}."
    )


def costruisci_tabella(stato: dict[str, Any]) -> pd.DataFrame:
    """Storico in DataFrame, ordinato dal più recente."""
    righe: list[dict[str, Any]] = []
    zona = _fuso()

    for voce in stato.get("storico") or []:
        pubblicazione = _data(voce.get("data_pubblicazione"))
        avvistamento = _data(voce.get("data_avvistamento"))
        # Stessa regola del monitor: data incerta o assente -> vale
        # l'istante in cui l'abbiamo visto, mai una data inventata.
        effettiva = avvistamento if (voce.get("data_incerta") or not pubblicazione) else pubblicazione
        righe.append({
            "titolo": voce.get("titolo") or "(senza titolo)",
            "prezzo": voce.get("prezzo"),
            "valuta": voce.get("valuta") or "EUR",
            "piattaforma": (voce.get("piattaforma") or "?").lower(),
            "ricerca": voce.get("ricerca") or "senza ricerca",
            "localita": voce.get("localita") or "",
            "condizione": voce.get("condizione") or "",
            "url": voce.get("url") or "",
            "immagine": voce.get("immagine") or "",
            "spedizione_inclusa": voce.get("spedizione_inclusa"),
            "data": effettiva.astimezone(zona) if effettiva else None,
            "data_incerta": bool(voce.get("data_incerta")),
        })

    tabella = pd.DataFrame(righe)
    if tabella.empty:
        return tabella

    tabella = tabella[tabella["data"].notna()]
    limite = datetime.now(_fuso()) - timedelta(days=GIORNI_STORICO)
    tabella = tabella[tabella["data"] >= limite]
    return tabella.sort_values("data", ascending=False)


# ===========================================================================
# Componenti
# ===========================================================================

def esc(testo: Any) -> str:
    return html.escape(str(testo or ""), quote=True)


# Streamlit scarta lo stato di un widget quando smette di disegnarlo, quindi
# cambiando pagina i filtri si perderebbero. Se ne tiene una copia sotto una
# chiave separata, che nessuno cancella.

def _memoria(chiave: str, predefinito: Any) -> Any:
    return st.session_state.get(f"filtro_{chiave}", predefinito)


def _ricorda(chiave: str, valore: Any) -> Any:
    st.session_state[f"filtro_{chiave}"] = valore
    return valore


def _opzioni_valide(chiave: str, disponibili: list[str]) -> list[str]:
    """
    Valori ricordati, ripuliti da quelli non più disponibili.

    Il valore iniziale è la lista vuota, non "tutto selezionato": aprendo la
    dashboard nessun filtro risulta attivo. Preselezionare tutto dava
    l'impressione di filtri accesi da soli.
    """
    ricordati = _memoria(chiave, None)
    if not ricordati:
        return []
    return [v for v in ricordati if v in disponibili]


def _applica_filtro(colonna: pd.Series, selezione: list[str]) -> pd.Series:
    """Maschera del filtro: selezione vuota = nessun filtro, passa tutto."""
    if not selezione:
        return pd.Series(True, index=colonna.index)
    return colonna.isin(selezione)


def intestazione(titolo: str, sottotitolo: str, nome_icona: str) -> None:
    st.markdown(
        f'<div class="intestazione"><h1>{design.icona(nome_icona, 22)}{esc(titolo)}</h1>'
        f'<p>{esc(sottotitolo)}</p></div>',
        unsafe_allow_html=True,
    )


def riquadri(voci: list[tuple[str, str, str, str]]) -> None:
    """Fila di riquadri statistici: (etichetta, valore, nota, icona)."""
    blocchi = "".join(
        f'<div class="riquadro"><div class="etichetta">{design.icona(ic, 13)}{esc(et)}</div>'
        f'<div class="valore">{esc(val)}</div>'
        + (f'<div class="nota">{esc(nota)}</div>' if nota else "")
        + "</div>"
        for et, val, nota, ic in voci
    )
    st.markdown(f'<div class="riquadri">{blocchi}</div>', unsafe_allow_html=True)


def pastiglia_piattaforma(nome: str) -> str:
    classe = f"p-{nome}" if nome in design.COLORI_PIATTAFORMA else "p-altro"
    return f'<span class="pastiglia {classe}"><span class="punto"></span>{esc(nome.capitalize())}</span>'


def stato_vuoto(titolo: str, testo: str, nome_icona: str = "inventario") -> None:
    st.markdown(
        f'<div class="vuoto"><div class="simbolo">{design.icona(nome_icona, 40)}</div>'
        f'<h3>{esc(titolo)}</h3><p>{esc(testo)}</p></div>',
        unsafe_allow_html=True,
    )


def prezzo_leggibile(valore: Any, valuta: str = "EUR") -> str:
    if valore is None or pd.isna(valore):
        return "n.d."
    simbolo = "€" if valuta == "EUR" else str(valuta)
    return f"{float(valore):,.0f}".replace(",", ".") + f" {simbolo}"


def tempo_relativo(quando: Any) -> str:
    """"8 min fa" invece di "28/08 21:14": guardando una griglia di offerte
    la domanda è sempre "è appena uscito?"."""
    if quando is None or pd.isna(quando):
        return "data ignota"
    scarto = datetime.now(_fuso()) - quando
    secondi = scarto.total_seconds()
    if secondi < 0:
        return "adesso"
    minuti = secondi / 60
    if minuti < 1:
        return "adesso"
    if minuti < 60:
        return f"{int(minuti)} min fa"
    ore = minuti / 60
    if ore < 24:
        return f"{int(ore)} " + ("ora fa" if int(ore) == 1 else "ore fa")
    giorni = ore / 24
    if giorni < 7:
        return f"{int(giorni)} " + ("giorno fa" if int(giorni) == 1 else "giorni fa")
    return quando.strftime("%d/%m")


def novita(quando: Any) -> str:
    """"nuovo" (< 1 ora), "recente" (< 6 ore) o "" per il resto."""
    if quando is None or pd.isna(quando):
        return ""
    minuti = (datetime.now(_fuso()) - quando).total_seconds() / 60
    if minuti < 0:
        return "nuovo"
    if minuti <= MINUTI_NUOVO:
        return "nuovo"
    if minuti <= ORE_RECENTE * 60:
        return "recente"
    return ""


def convenienza(prezzo: Any, mediana: float | None) -> tuple[str, float] | None:
    """
    Colloca un prezzo rispetto alla mediana della sua ricerca. La mediana e
    non la media: bastano due inserzioni fuori mercato a rendere la media
    inutile. None quando il campione è troppo piccolo per un giudizio.
    """
    if prezzo is None or pd.isna(prezzo) or not mediana or mediana <= 0:
        return None
    scarto = float(prezzo) / mediana - 1
    if scarto <= SOGLIA_CONVENIENTE:
        return "conveniente", scarto
    if scarto >= SOGLIA_CARO:
        return "sopra media", scarto
    return "in linea", scarto


def _pastiglia_convenienza(giudizio: tuple[str, float] | None) -> str:
    if giudizio is None:
        return ""
    etichetta, scarto = giudizio
    stile = {
        "conveniente": f"color:{design.STATO['ok']};border-color:{design.STATO['ok']}59;background:{design.STATO['ok']}1f",
        "in linea": f"color:{design.TESTO_3};border-color:{design.BORDO};background:{design.SUPERFICIE_2}",
        "sopra media": f"color:{design.STATO['attenzione']};border-color:{design.STATO['attenzione']}59;background:{design.STATO['attenzione']}1f",
    }[etichetta]
    return (
        f'<span class="pastiglia" style="{stile}">{etichetta} {scarto:+.0%}</span>'
    )


def scheda_annuncio(riga: pd.Series, mediana: float | None, indice: int = 0) -> str:
    """HTML di una scheda annuncio."""
    grado = novita(riga["data"])

    if riga["immagine"]:
        interno = f'<div class="miniatura" style="background-image:url({esc(riga["immagine"])})"></div>'
    else:
        interno = f'<div class="miniatura vuota">{design.icona("inventario", 34)}</div>'
    marchio = (
        '<span class="nuovo"><span class="impulso"></span>nuovo</span>'
        if grado == "nuovo" else ""
    )
    miniatura = f'<div class="involucro-miniatura">{marchio}{interno}</div>'

    spedizione = ""
    if riga["spedizione_inclusa"] is True:
        spedizione = '<span class="spedizione">spedizione inclusa</span>'
    elif riga["spedizione_inclusa"] is False:
        spedizione = '<span class="spedizione">+ spedizione</span>'

    meta = [pastiglia_piattaforma(riga["piattaforma"])]
    giudizio = _pastiglia_convenienza(convenienza(riga["prezzo"], mediana))
    if giudizio:
        meta.append(giudizio)
    if riga["localita"]:
        meta.append(f'<span>{design.icona("luogo", 13)}{esc(riga["localita"])}</span>')

    quando = tempo_relativo(riga["data"])
    if riga["data_incerta"]:
        quando += " (stimata)"

    classi = "scheda" + (" recente" if grado else "")
    return (
        f'<article class="{classi}" style="--i:{indice}">'
        + miniatura
        + '<div class="corpo">'
        + f'<div class="prezzo">{esc(prezzo_leggibile(riga["prezzo"], riga["valuta"]))}{spedizione}</div>'
        + f'<h3 class="titolo">{esc(riga["titolo"])}</h3>'
        + f'<div class="meta">{"".join(meta)}</div>'
        + '<div class="piede">'
        + f'<span class="quando">{design.icona("orologio", 13)}{esc(quando)}</span>'
        + (f'<a class="apri" href="{esc(riga["url"])}" target="_blank" rel="noopener">'
           f'{design.icona("apri", 13)}Apri</a>' if riga["url"] else "")
        + "</div></div></article>"
    )


def griglia_annunci(tabella: pd.DataFrame, mediana: float | None) -> None:
    schede = "".join(
        scheda_annuncio(r, mediana, i) for i, (_, r) in enumerate(tabella.iterrows())
    )
    st.markdown(f'<div class="griglia">{schede}</div>', unsafe_allow_html=True)


# ===========================================================================
# Pagina: Annunci
# ===========================================================================

def statistiche_ricerca(gruppo: pd.DataFrame) -> dict[str, Any]:
    prezzi = gruppo["prezzo"].dropna()
    mediana = float(prezzi.median()) if len(prezzi) >= CAMPIONE_MINIMO else None
    convenienti = 0
    if mediana:
        convenienti = int((prezzi <= mediana * (1 + SOGLIA_CONVENIENTE)).sum())
    return {
        "totale": len(gruppo),
        "con_prezzo": len(prezzi),
        "mediana": mediana,
        "minimo": float(prezzi.min()) if not prezzi.empty else None,
        "massimo": float(prezzi.max()) if not prezzi.empty else None,
        "ultimo": gruppo["data"].max(),
        "convenienti": convenienti,
    }


ORDINAMENTI: dict[str, tuple[str, bool]] = {
    "Più recenti": ("data", False),
    "Meno recenti": ("data", True),
    "Prezzo crescente": ("prezzo", True),
    "Prezzo decrescente": ("prezzo", False),
}


def sezione_ricerca(nome: str, gruppo: pd.DataFrame, massimo: int) -> None:
    s = statistiche_ricerca(gruppo)
    nuovi = int(gruppo["data"].apply(lambda d: novita(d) == "nuovo").sum())

    # Intestazione a sinistra, ordinamento a destra: i controlli che agiscono
    # su una sezione stanno accanto a quella sezione, non in fondo alla barra
    # laterale insieme a tutto il resto.
    col_titolo, col_ordine = st.columns([3, 1.15])
    with col_titolo:
        etichetta_nuovi = (
            f'<span class="pastiglia" style="color:{design.STATO["ok"]};'
            f'border-color:{design.STATO["ok"]}59;background:{design.STATO["ok"]}1f">'
            f'{nuovi} nuovi</span>' if nuovi else ""
        )
        st.markdown(
            f'<div class="barra-sezione" style="margin-top:.6rem">'
            f'<span class="nome-sezione">{design.icona("inventario", 16)}'
            f'{len(gruppo)} annunci{etichetta_nuovi}</span></div>',
            unsafe_allow_html=True,
        )
    with col_ordine:
        chiave = f"ordine_{nome}"
        scelta = st.selectbox(
            "Ordina", list(ORDINAMENTI), key=f"widget_{chiave}",
            index=list(ORDINAMENTI).index(_memoria(chiave, "Più recenti")),
            label_visibility="collapsed",
        )
        _ricorda(chiave, scelta)

    colonna, crescente = ORDINAMENTI[scelta]
    gruppo = gruppo.sort_values(colonna, ascending=crescente, na_position="last")

    if s["mediana"]:
        valore_mediana, nota_mediana = prezzo_leggibile(s["mediana"]), f"su {s['con_prezzo']} con prezzo"
    else:
        valore_mediana, nota_mediana = "—", f"servono almeno {CAMPIONE_MINIMO} prezzi"

    riquadri([
        ("Prezzo mediano", valore_mediana, nota_mediana, "prezzo"),
        ("Più basso", prezzo_leggibile(s["minimo"]),
         f"fino a {prezzo_leggibile(s['massimo'])}" if s["massimo"] else "", "giu"),
        ("Convenienti", str(s["convenienti"]) if s["mediana"] else "—",
         f"{abs(SOGLIA_CONVENIENTE):.0%} sotto la mediana" if s["mediana"] else "", "ok"),
        ("Ultimo trovato", tempo_relativo(s["ultimo"]), "", "orologio"),
    ])

    da_mostrare = gruppo.head(massimo)
    griglia_annunci(da_mostrare, s["mediana"])

    if len(gruppo) > len(da_mostrare):
        st.caption(
            f"Altri {len(gruppo) - len(da_mostrare)} annunci non mostrati — "
            "alza il limite nella barra laterale."
        )


def _ricerche_configurate() -> dict[str, bool]:
    """
    Nomi delle ricerche configurate e se sono in funzione.

    Le schede della pagina Annunci si costruiscono da qui e non dall'archivio:
    una ricerca appena creata non ha ancora trovato nulla, e senza questo
    elenco non comparirebbe affatto — dando l'impressione di non essere stata
    salvata. Se la configurazione non è leggibile si torna all'archivio.
    """
    try:
        from config_loader import configurazione_da_documento, documento_da_testo
        testo, _sha, _origine = carica_config()
        configurazione = configurazione_da_documento(documento_da_testo(testo))
        return {r.nome: r.eseguibile for r in configurazione.ricerche}
    except Exception:
        return {}


def _elimina_ricerca(nome: str) -> None:
    """Rimuove una ricerca dalla configurazione, con conferma esplicita."""
    from config_loader import modifica_rimuovi_ricerca

    scrivibile, motivo = _scrittura_possibile()
    chiave = f"conferma_eliminazione_{nome}"

    if not scrivibile:
        st.caption("Per eliminare una ricerca da qui servono i secrets di scrittura.")
        return

    if not st.session_state.get(chiave):
        if st.button("Elimina questa ricerca", key=f"del_tab_{nome}",
                     icon=":material/delete:"):
            st.session_state[chiave] = True
            st.rerun()
        return

    st.warning(
        f"Eliminare **{nome}**? La ricerca sparisce dalla configurazione e i "
        "suoi annunci vengono rimossi dall'archivio al prossimo controllo."
    )
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("Sì, elimina", key=f"si_tab_{nome}", type="primary"):
        if _applica(lambda d: modifica_rimuovi_ricerca(d, nome),
                    f"config: rimossa '{nome}' dalla dashboard"):
            st.session_state.pop(chiave, None)
            st.rerun()
    if c2.button("Annulla", key=f"no_tab_{nome}"):
        st.session_state.pop(chiave, None)
        st.rerun()


def pagina_annunci() -> None:
    stato, origine = _stato_o_stop()
    intestazione(
        "Annunci",
        f"Una scheda per ricerca, ultimi {GIORNI_STORICO} giorni. "
        "La convenienza è calcolata sulla mediana della singola ricerca.",
        "inventario",
    )
    _banner_origine(origine)

    tabella = costruisci_tabella(stato)
    if tabella.empty:
        stato_vuoto(
            "Nessun annuncio in archivio",
            "Il monitor non ha ancora trovato nulla, oppure le ricerche sono sospese. "
            "Appena troverà qualcosa comparirà qui, una scheda per ricerca.",
        )
        return

    with st.sidebar:
        st.markdown(
            f'<div class="titolo-sezione" style="margin-top:.4rem">'
            f'{design.icona("filtro", 13)}Filtri</div>', unsafe_allow_html=True
        )
        st.caption("Nessun filtro attivo: si vede tutto. Aggiungine quando serve.")

        piattaforme = sorted(tabella["piattaforma"].unique())
        scelte_p = _ricorda("piattaforme", st.multiselect(
            "Piattaforme", piattaforme,
            default=_opzioni_valide("piattaforme", piattaforme),
            key="widget_piattaforme", placeholder="tutte",
        ))
        giorni = _ricorda("giorni", st.select_slider(
            "Periodo", options=[1, 3, 7, 14, 30], value=_memoria("giorni", 30),
            format_func=lambda g: f"{g} giorni" if g > 1 else "24 ore",
            key="widget_giorni",
        ))
        solo_nuovi = _ricorda("solo_nuovi", st.toggle(
            "Solo appena pubblicati", value=_memoria("solo_nuovi", False),
            key="widget_nuovi", help=f"Usciti nelle ultime {ORE_RECENTE} ore",
        ))
        solo_convenienti = _ricorda("solo_conv", st.toggle(
            "Solo convenienti", value=_memoria("solo_conv", False),
            key="widget_conv",
            help=f"Almeno il {abs(SOGLIA_CONVENIENTE):.0%} sotto la mediana della propria ricerca",
        ))
        massimo = _ricorda("massimo", st.select_slider(
            "Max per scheda", options=[6, 12, 24, 48], value=_memoria("massimo", 12),
            key="widget_massimo",
        ))
        if st.button("Azzera i filtri", use_container_width=True,
                     icon=":material/filter_alt_off:"):
            for c in ("piattaforme", "giorni", "solo_nuovi", "solo_conv", "massimo"):
                st.session_state.pop(f"filtro_{c}", None)
                st.session_state.pop(f"widget_{c}", None)
            st.rerun()

    limite = datetime.now(_fuso()) - timedelta(days=int(giorni))
    filtrata = tabella[
        _applica_filtro(tabella["piattaforma"], scelte_p) & (tabella["data"] >= limite)
    ]

    nuovi_totali = int(filtrata["data"].apply(lambda d: novita(d) == "nuovo").sum()) if not filtrata.empty else 0
    recenti = int(filtrata["data"].apply(lambda d: bool(novita(d))).sum()) if not filtrata.empty else 0
    riquadri([
        ("Annunci", str(len(filtrata)), f"in {filtrata['ricerca'].nunique()} ricerche"
         if not filtrata.empty else "", "inventario"),
        ("Appena pubblicati", str(nuovi_totali), "nell'ultima ora", "nuovo"),
        ("Recenti", str(recenti), f"nelle ultime {ORE_RECENTE} ore", "tendenza"),
        ("Periodo", f"{giorni} giorni" if giorni > 1 else "24 ore", "", "calendario"),
    ])

    if filtrata.empty:
        stato_vuoto("Nessun annuncio con questi filtri",
                    "Allarga il periodo o azzera i filtri nella barra laterale.")
        return

    # Una scheda per ricerca CONFIGURATA, più quelle che hanno annunci in
    # archivio ma non esistono più: mescolare prodotti diversi nella stessa
    # lista rende impossibile confrontare i prezzi.
    configurate = _ricerche_configurate()
    in_archivio = set(filtrata["ricerca"].unique())
    nomi = sorted(set(configurate) | in_archivio)

    etichette = []
    for n in nomi:
        quanti = int((filtrata["ricerca"] == n).sum())
        if n in configurate and not configurate[n]:
            etichette.append(f"⏸ {n}")
        elif n not in configurate:
            etichette.append(f"{n}  (rimossa)")
        else:
            etichette.append(f"{n}  ({quanti})")

    for scheda, nome in zip(st.tabs(etichette), nomi):
        with scheda:
            if nome in configurate and not configurate[nome]:
                st.info("Ricerca sospesa: il monitor non la sta eseguendo. "
                        "Riattivala dalla pagina Ricerche o con /resume su Telegram.",
                        icon="⏸")
            elif nome not in configurate:
                st.warning("Questa ricerca non è più configurata. I suoi annunci "
                           "spariranno dall'archivio al prossimo controllo.", icon="🗑")

            gruppo = filtrata[filtrata["ricerca"] == nome].copy()
            if solo_nuovi:
                gruppo = gruppo[gruppo["data"].apply(lambda d: bool(novita(d)))]
            if solo_convenienti:
                s = statistiche_ricerca(gruppo)
                if s["mediana"]:
                    gruppo = gruppo[gruppo["prezzo"] <= s["mediana"] * (1 + SOGLIA_CONVENIENTE)]
            if gruppo.empty:
                if solo_nuovi or solo_convenienti or scelte_p:
                    stato_vuoto(
                        "Nessun annuncio con questi filtri",
                        "Prova ad azzerare i filtri nella barra laterale.", "filtro",
                    )
                else:
                    stato_vuoto(
                        "Ancora nessun annuncio",
                        "Il monitor non ha ancora trovato nulla per questa ricerca. "
                        "Al primo avvio notifica solo ciò che è stato pubblicato di "
                        "recente, quindi può volerci qualche ora.", "cerca",
                    )
            else:
                sezione_ricerca(nome, gruppo, int(massimo))
            st.divider()
            _elimina_ricerca(nome)


# ===========================================================================
# Pagina: Statistiche
# ===========================================================================

def _altair():
    """
    Importa Altair solo quando serve, e non fa cadere l'app se non si carica.

    Motivo concreto: su Streamlit Community Cloud, con Python 3.14, l'import
    di Altair solleva un TypeError (usa `TypedDict(..., closed=True)`, che è
    PEP 728 e su quella versione non regge). Con l'import in cima al file
    l'intera dashboard diventava una pagina di errore. Isolandolo, si perdono
    i grafici e nient'altro: annunci, statistiche e gestione continuano a
    funzionare.

    La soluzione definitiva è fissare Python 3.12 nelle impostazioni dell'app.
    """
    try:
        import altair as alt
        return alt
    except Exception as exc:   # TypeError, ImportError, incompatibilità varie
        st.session_state["_altair_errore"] = f"{type(exc).__name__}: {exc}"
        return None


def _avviso_grafici_non_disponibili() -> None:
    st.info(
        "I grafici non sono disponibili in questo ambiente: la libreria Altair "
        "non si è caricata. Sotto trovi gli stessi dati in tabella.\n\n"
        "Per risolvere: impostazioni dell'app su Streamlit Cloud → "
        "**Python version → 3.12** → *Save* e riavvia.",
        icon="📊",
    )
    dettaglio = st.session_state.get("_altair_errore")
    if dettaglio:
        st.caption(f"Dettaglio tecnico: `{dettaglio[:160]}`")


def _scala_piattaforme(alt, valori: list[str]):
    """Colore legato all'entità: filtrare non ridipinge le serie rimaste."""
    return alt.Scale(
        domain=valori,
        range=[design.colore_piattaforma(v) for v in valori],
    )


def _tema_grafico(gr):
    return gr.configure_view(strokeWidth=0).configure_axis(
        grid=True, gridColor=design.BORDO, gridOpacity=0.55, domain=False,
        tickColor=design.BORDO, labelColor=design.TESTO_3, titleColor=design.TESTO_3,
        labelFontSize=11, titleFontSize=11, titleFontWeight="normal", titlePadding=10,
    ).configure_legend(
        labelColor=design.TESTO_2, titleColor=design.TESTO_3,
        labelFontSize=11, titleFontSize=11, symbolType="circle", symbolSize=90,
        orient="top", direction="horizontal", offset=6, titleFontWeight="normal",
    ).configure(background=design.SUPERFICIE_0)


def grafico_per_giorno(tabella: pd.DataFrame) -> None:
    dati = (
        tabella.assign(giorno=tabella["data"].dt.date)
        .groupby(["giorno", "piattaforma"], as_index=False)
        .size()
        .rename(columns={"size": "annunci"})
    )
    if dati.empty:
        return

    alt = _altair()
    if alt is None:
        _avviso_grafici_non_disponibili()
        st.dataframe(dati, use_container_width=True, hide_index=True)
        return

    piattaforme = sorted(dati["piattaforma"].unique())
    grafico = (
        alt.Chart(dati)
        .mark_bar(
            cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
            # 2px di superficie fra i segmenti: senza, il confine sparisce.
            stroke=design.SUPERFICIE_0, strokeWidth=2,
        )
        .encode(
            x=alt.X("giorno:T", title=None, axis=alt.Axis(format="%d/%m", tickCount=8)),
            y=alt.Y("annunci:Q", title="annunci trovati", stack="zero"),
            color=alt.Color("piattaforma:N", title=None, scale=_scala_piattaforme(alt, piattaforme)),
            tooltip=[
                alt.Tooltip("giorno:T", title="Giorno", format="%d/%m/%Y"),
                alt.Tooltip("piattaforma:N", title="Piattaforma"),
                alt.Tooltip("annunci:Q", title="Annunci"),
            ],
        )
        .properties(height=240)
    )
    st.altair_chart(_tema_grafico(grafico), use_container_width=True)


def grafico_prezzi(tabella: pd.DataFrame, nome_ricerca: str) -> None:
    """Distribuzione dei prezzi di UNA ricerca: è l'unica scala che ha senso."""
    prezzi = tabella[tabella["ricerca"] == nome_ricerca]["prezzo"].dropna()
    if len(prezzi) < CAMPIONE_MINIMO:
        st.caption(f"Servono almeno {CAMPIONE_MINIMO} prezzi per una distribuzione leggibile.")
        return

    mediana = float(prezzi.median())

    alt = _altair()
    if alt is None:
        _avviso_grafici_non_disponibili()
        st.dataframe(
            prezzi.describe().to_frame("prezzo (EUR)").round(0),
            use_container_width=True,
        )
        return

    dati = pd.DataFrame({"prezzo": prezzi})
    istogramma = (
        alt.Chart(dati)
        .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=design.ACCENTO)
        .encode(
            x=alt.X("prezzo:Q", bin=alt.Bin(maxbins=14), title="prezzo (€)"),
            y=alt.Y("count():Q", title="annunci"),
            tooltip=[alt.Tooltip("count():Q", title="Annunci"),
                     alt.Tooltip("prezzo:Q", bin=alt.Bin(maxbins=14), title="Fascia")],
        )
        .properties(height=200)
    )
    riga = (
        alt.Chart(pd.DataFrame({"m": [mediana]}))
        .mark_rule(color=design.STATO["attenzione"], strokeWidth=2, strokeDash=[4, 3])
        .encode(x="m:Q", tooltip=alt.Tooltip("m:Q", title="Mediana", format=".0f"))
    )
    st.altair_chart(_tema_grafico(istogramma + riga), use_container_width=True)
    st.caption(f"La linea tratteggiata è la mediana: {prezzo_leggibile(mediana)}. "
               f"Tutto ciò che sta a sinistra di {prezzo_leggibile(mediana * (1 + SOGLIA_CONVENIENTE))} "
               "è marcato come conveniente.")


def pagina_statistiche() -> None:
    stato, origine = _stato_o_stop()
    intestazione("Statistiche", "Andamento, prezzi e salute del monitor.", "grafico")
    _banner_origine(origine)

    tabella = costruisci_tabella(stato)
    run = stato.get("ultimo_run") or {}
    fine_run = _data(run.get("terminato"))

    with st.sidebar:
        st.markdown(
            f'<div class="titolo-sezione" style="margin-top:.4rem">'
            f'{design.icona("filtro", 13)}Filtri</div>', unsafe_allow_html=True
        )
        st.caption("Nessun filtro attivo: si vede tutto.")
        piattaforme = sorted(tabella["piattaforma"].unique()) if not tabella.empty else []
        scelte_p = _ricorda("stat_piattaforme", st.multiselect(
            "Piattaforme", piattaforme,
            default=_opzioni_valide("stat_piattaforme", piattaforme),
            key="widget_stat_piat", placeholder="tutte",
        ))
        giorni = _ricorda("stat_giorni", st.select_slider(
            "Periodo", options=[3, 7, 14, 30], value=_memoria("stat_giorni", 30),
            format_func=lambda g: f"{g} giorni", key="widget_stat_giorni",
        ))
        if st.button("Azzera i filtri", use_container_width=True,
                     key="azzera_stat", icon=":material/filter_alt_off:"):
            for c in ("stat_piattaforme", "stat_giorni"):
                st.session_state.pop(f"filtro_{c}", None)
                st.session_state.pop(f"widget_{c.replace('stat_piattaforme','stat_piat').replace('stat_giorni','stat_giorni')}", None)
            st.rerun()

    if not tabella.empty:
        limite = datetime.now(_fuso()) - timedelta(days=int(giorni))
        tabella = tabella[
            _applica_filtro(tabella["piattaforma"], scelte_p) & (tabella["data"] >= limite)
        ]

    riquadri([
        ("Ultimo controllo",
         tempo_relativo(fine_run.astimezone(_fuso())) if fine_run else "mai",
         str(run.get("esito") or ""), "aggiorna"),
        ("Durata", f"{run.get('durata_s', 0)} s", f"{run.get('richieste', 0)} richieste HTTP", "orologio"),
        ("Notificati", str(run.get("notificati", 0) or 0), "nell'ultimo controllo", "ok"),
        ("In archivio", str(len(tabella)), f"ultimi {giorni} giorni", "inventario"),
    ])

    if fine_run and datetime.now(timezone.utc) - fine_run > timedelta(hours=6):
        st.warning(
            f"L'ultimo controllo risale a più di 6 ore fa "
            f"({fine_run.astimezone(_fuso()):%d/%m %H:%M}). Controlla il trigger "
            "esterno su cron-job.org.",
            icon="⚠️",
        )

    nomi = sorted(tabella["ricerca"].unique()) if not tabella.empty else []
    schede = st.tabs(["Generale"] + [f"{n}" for n in nomi])

    with schede[0]:
        st.markdown(f'<div class="titolo-sezione">{design.icona("grafico", 13)}'
                    'Annunci trovati per giorno</div>', unsafe_allow_html=True)
        if tabella.empty:
            st.caption("Nessun dato con questi filtri.")
        else:
            grafico_per_giorno(tabella)
            with st.expander("Vedi i dati in tabella"):
                st.dataframe(
                    tabella.assign(giorno=tabella["data"].dt.date)
                    .groupby(["giorno", "piattaforma"], as_index=False).size()
                    .rename(columns={"size": "annunci"}),
                    use_container_width=True, hide_index=True,
                )

        st.markdown(f'<div class="titolo-sezione">{design.icona("negozio", 13)}'
                    'Salute delle piattaforme</div>', unsafe_allow_html=True)
        salute = stato.get("piattaforme") or {}
        in_uso = set(design.PIATTAFORME_UTILIZZABILI)
        if not tabella.empty:
            in_uso |= set(tabella["piattaforma"].unique())
        salute = {k: v for k, v in salute.items() if k in in_uso}
        if not salute:
            st.caption("Nessuna piattaforma ancora interrogata.")
        else:
            etichette = {"ok": "funziona", "vuoto": "nessun risultato",
                         "bloccato": "bloccata", "errore": "errore",
                         "quarantena": "in pausa dopo un blocco"}
            st.dataframe(
                pd.DataFrame([
                    {
                        "piattaforma": nome,
                        "stato": etichette.get(str(v.get("ultimo_esito")),
                                               v.get("ultimo_esito") or "mai eseguita"),
                        "controlli a vuoto": int(v.get("run_zero_consecutivi") or 0),
                        "ultimo successo": (
                            tempo_relativo(_data(v.get("ultimo_ok")).astimezone(_fuso()))
                            if _data(v.get("ultimo_ok")) else "—"
                        ),
                        "ultimo errore": (v.get("ultimo_errore") or "")[:110],
                    }
                    for nome, v in sorted(salute.items())
                ]),
                use_container_width=True, hide_index=True,
            )

    for scheda, nome in zip(schede[1:], nomi):
        with scheda:
            gruppo = tabella[tabella["ricerca"] == nome]
            s = statistiche_ricerca(gruppo)
            riquadri([
                ("Annunci", str(s["totale"]), f"ultimi {giorni} giorni", "inventario"),
                ("Prezzo mediano", prezzo_leggibile(s["mediana"]) if s["mediana"] else "—",
                 f"su {s['con_prezzo']} con prezzo", "prezzo"),
                ("Più basso", prezzo_leggibile(s["minimo"]),
                 f"fino a {prezzo_leggibile(s['massimo'])}" if s["massimo"] else "", "giu"),
                ("Convenienti", str(s["convenienti"]) if s["mediana"] else "—",
                 f"{abs(SOGLIA_CONVENIENTE):.0%} sotto la mediana" if s["mediana"] else "", "ok"),
            ])
            st.markdown(f'<div class="titolo-sezione">{design.icona("prezzo", 13)}'
                        'Distribuzione dei prezzi</div>', unsafe_allow_html=True)
            grafico_prezzi(tabella, nome)


# ===========================================================================
# Utilità comuni alle pagine
# ===========================================================================

def _stato_o_stop() -> tuple[dict[str, Any], str]:
    try:
        stato, origine = carica_stato()
    except GistError as exc:
        st.error(f"Impossibile leggere lo stato.\n\n{exc}")
        st.info(
            "Servono i secrets **GIST_TOKEN** (PAT classico con scope `gist`) e "
            "**GIST_ID**, gli stessi usati dal monitor su GitHub Actions."
        )
        st.stop()
    except Exception as exc:
        st.error(f"Errore imprevisto: {exc}")
        st.stop()

    if not stato:
        stato_vuoto("Archivio vuoto",
                    "Il Gist è raggiungibile ma il monitor non ha ancora completato un controllo.")
        st.stop()
    return stato, origine


def _banner_origine(origine: str) -> None:
    if origine == "locale":
        st.info(f"Modalità locale: dati letti da `{PERCORSO_STATO_LOCALE}`, non dal Gist.", icon="📁")


# ===========================================================================
# Pagina: Ricerche — gestione con scrittura su config.yaml
# ===========================================================================

def _config_github() -> tuple[str, str]:
    return _segreto("GITHUB_TOKEN"), _segreto("GITHUB_REPOSITORY")


def _branch() -> str:
    return _segreto("GITHUB_BRANCH") or "main"


@st.cache_data(ttl=15, show_spinner=False)
def carica_config() -> tuple[str, str, str]:
    """`(testo, sha, origine)` della configurazione, da GitHub o da disco."""
    from config_loader import leggi_config_da_github

    token, repository = _config_github()
    if token and repository:
        testo, sha = leggi_config_da_github(
            token=token, repository=repository, branch=_branch()
        )
        return testo, sha, "github"

    percorso = Path("config.yaml")
    if percorso.is_file():
        return percorso.read_text(encoding="utf-8"), "", "locale"
    raise RuntimeError("config.yaml non raggiungibile")


def _scrittura_possibile() -> tuple[bool, str]:
    """La pagina può modificare la configurazione? In caso contrario, perché."""
    token, repository = _config_github()
    if not token or not repository:
        return False, (
            "Per modificare le ricerche servono i secrets **GITHUB_TOKEN** "
            "(Personal Access Token *classic* con scope `repo`) e "
            "**GITHUB_REPOSITORY** (nella forma `utente/repo`)."
        )
    if not _segreto("DASHBOARD_PASSWORD"):
        return False, (
            "**GITHUB_TOKEN è configurato ma manca DASHBOARD_PASSWORD.** "
            "Senza password chiunque abbia il link potrebbe modificare le tue "
            "ricerche e fare commit sul repository, quindi la pagina resta in "
            "sola lettura. Aggiungi il secret `DASHBOARD_PASSWORD` per sbloccarla."
        )
    return True, ""


def _autenticato() -> bool:
    if st.session_state.get("accesso_ok"):
        return True
    with st.form("accesso"):
        st.markdown("Questa pagina modifica la configurazione del monitor.")
        password = st.text_input("Password", type="password", label_visibility="collapsed",
                                 placeholder="Password della dashboard")
        if st.form_submit_button("Sblocca", type="primary"):
            if password and password == _segreto("DASHBOARD_PASSWORD"):
                st.session_state["accesso_ok"] = True
                st.rerun()
            else:
                st.error("Password errata.")
    return False


def _applica(modifica, messaggio: str) -> bool:
    """
    Applica una modifica al config e la committa. Rilegge la versione
    corrente e ne passa lo `sha`: se nel frattempo il bot ha modificato il
    file, la scrittura viene rifiutata invece di cancellarlo.
    """
    from config_loader import (
        ConfigError, commit_config, configurazione_da_documento,
        documento_da_testo, leggi_config_da_github, serializza_documento,
    )

    token, repository = _config_github()
    try:
        testo, sha = leggi_config_da_github(
            token=token, repository=repository, branch=_branch()
        )
        documento = documento_da_testo(testo)
        modifica(documento)
        configurazione_da_documento(documento)   # validazione prima di scrivere
    except ConfigError as exc:
        st.error(f"Modifica rifiutata: {exc}")
        return False

    esito = commit_config(
        serializza_documento(documento),
        token=token, repository=repository, messaggio=messaggio,
        branch=_branch(), sha_atteso=sha,
    )
    if esito:
        carica_config.clear()
        st.success(messaggio)
    else:
        st.error(
            "Commit non riuscito. Possibili cause: il token non ha lo scope "
            "`repo`, oppure il file è cambiato mentre lo modificavi."
        )
    return esito


def _campi_ricerca(ricerca, prefisso: str) -> dict[str, Any]:
    """Campi del modulo, condivisi fra creazione e modifica."""
    from models import Condizione

    disponibili = list(design.PIATTAFORME_UTILIZZABILI)
    # Una piattaforma già configurata resta selezionabile anche se non è più
    # fra quelle proposte: toglierla d'ufficio dalla lista la cancellerebbe
    # dalla ricerca al primo salvataggio, senza che nessuno l'abbia chiesto.
    if ricerca:
        disponibili += [p for p in ricerca.piattaforme if p not in disponibili]

    c1, c2 = st.columns([2, 1])
    with c1:
        parole = st.text_input(
            "Parole chiave", value=ricerca.parole_chiave if ricerca else "",
            key=f"{prefisso}_kw", placeholder="ps5 pro",
            help="Devono comparire tutte nel titolo, se 'solo titolo' è attivo.",
        )
    with c2:
        piattaforme = st.multiselect(
            "Piattaforme", sorted(disponibili),
            default=ricerca.piattaforme if ricerca else ["subito"],
            key=f"{prefisso}_piat",
            help="eBay non compare perché senza credenziali API viene "
                 "rifiutato con 403 dagli indirizzi dei runner GitHub.",
        )

    c1, c2, c3 = st.columns(3)
    with c1:
        prezzo_min = st.number_input(
            "Prezzo minimo (€)", min_value=0, max_value=1_000_000, step=10,
            value=int(ricerca.prezzo_min) if ricerca and ricerca.prezzo_min else 0,
            key=f"{prefisso}_min",
            help="È il filtro più efficace: elimina da solo gli accessori.",
        )
    with c2:
        prezzo_max = st.number_input(
            "Prezzo massimo (€)", min_value=0, max_value=1_000_000, step=10,
            value=int(ricerca.prezzo_max) if ricerca and ricerca.prezzo_max else 0,
            key=f"{prefisso}_max", help="0 = nessun limite.",
        )
    with c3:
        condizione = st.selectbox(
            "Condizione", sorted(Condizione.valide()),
            index=sorted(Condizione.valide()).index(ricerca.condizione) if ricerca else
            sorted(Condizione.valide()).index("qualsiasi"),
            key=f"{prefisso}_cond",
        )

    escluse = st.text_area(
        "Parole escluse", value=", ".join(ricerca.parole_escluse) if ricerca else "",
        key=f"{prefisso}_escl", height=68, placeholder="rotto, ricambi, non funzionante",
        help="Separate da virgola. Il confronto è su parole intere, senza distinzione "
             "fra maiuscole e minuscole. Evita di escludere gli accessori che spesso "
             "accompagnano l'oggetto: escluderesti anche le inserzioni valide.",
    )

    c0, c1, c2, c3 = st.columns(4)
    with c0:
        eta = st.number_input(
            "Età max (giorni)", min_value=0, max_value=365, step=1,
            value=int(ricerca.eta_massima_giorni) if ricerca and ricerca.eta_massima_giorni else 0,
            key=f"{prefisso}_eta",
            help="Scarta gli annunci pubblicati più di N giorni fa, così vedi "
                 "solo i recenti. 0 = nessun limite. Gli annunci di data "
                 "ignota passano comunque: su Vinted molti non hanno data.",
        )
    with c1:
        intervallo = st.number_input(
            "Ogni quanti minuti", min_value=5, max_value=1440, step=5,
            value=ricerca.intervallo_minuti if ricerca else 30, key=f"{prefisso}_int",
            help="Tempo minimo fra due controlli DI QUESTA ricerca, applicato "
                 "sopra il trigger esterno che sveglia il monitor ogni 15 "
                 "minuti. Viene arrotondato per eccesso a un multiplo di 15: "
                 "un valore di 20 diventa 30, uno di 5 diventa 15. Più basso "
                 "significa scoprire prima ma anche più richieste al sito.",
        )
    with c2:
        zona = st.text_input(
            "Zona (Subito)", value=ricerca.subito.zona if ricerca else "italia",
            key=f"{prefisso}_zona", help="Regione, città, o 'italia' per non filtrare.",
        )
    with c3:
        solo_titolo = st.toggle(
            "Solo nel titolo", value=ricerca.solo_titolo if ricerca else True,
            key=f"{prefisso}_st",
            help="Se attivo, le parole chiave devono comparire nel titolo e non "
                 "soltanto nella descrizione.",
        )

    return {
        "parole_chiave": parole.strip(),
        "piattaforme": piattaforme,
        "prezzo_min": float(prezzo_min) if prezzo_min else None,
        "prezzo_max": float(prezzo_max) if prezzo_max else None,
        "condizione": condizione,
        "parole_escluse": [p.strip() for p in escluse.split(",") if p.strip()],
        "intervallo_minuti": int(intervallo),
        "eta_massima_giorni": int(eta) if eta else None,
        "zona": zona.strip().lower() or "italia",
        "solo_titolo": bool(solo_titolo),
    }


def _scheda_ricerca(ricerca, modificabile: bool, conteggi: dict[str, int]) -> None:
    from config_loader import modifica_pausa, modifica_rimuovi_ricerca, trova_ricerca

    attiva = ricerca.eseguibile
    classe = "attiva" if attiva else "sospesa"
    etichetta = "in funzione" if attiva else ("sospesa" if ricerca.attiva else "disattivata")
    colore = design.STATO["ok"] if attiva else design.TESTO_3

    dettagli = [
        f"<span><b>{len(ricerca.piattaforme)}</b> "
        + " ".join(pastiglia_piattaforma(p) for p in ricerca.piattaforme) + "</span>",
        f"<span>prezzo <b>{prezzo_leggibile(ricerca.prezzo_min)}</b> – "
        f"<b>{prezzo_leggibile(ricerca.prezzo_max)}</b></span>",
        f"<span>ogni <b>{ricerca.intervallo_minuti}</b> min</span>",
        f"<span>condizione <b>{esc(ricerca.condizione)}</b></span>",
        (f"<span>max <b>{ricerca.eta_massima_giorni}</b> giorni</span>"
         if ricerca.eta_massima_giorni else ""),
        f"<span><b>{conteggi.get(ricerca.nome, 0)}</b> trovati in {GIORNI_STORICO} giorni</span>",
    ]
    escluse = ""
    if ricerca.parole_escluse:
        mostrate = "".join(f"<code>{esc(p)}</code>" for p in ricerca.parole_escluse[:10])
        resto = f" +{len(ricerca.parole_escluse) - 10}" if len(ricerca.parole_escluse) > 10 else ""
        escluse = f'<div class="escluse">esclude {mostrate}{resto}</div>'

    st.markdown(
        f'<div class="ricerca {classe}">'
        f'<div class="riga1"><span class="nome">{esc(ricerca.nome)}</span>'
        f'<span class="chiavi">{esc(ricerca.parole_chiave)}</span>'
        f'<span class="pastiglia" style="color:{colore};border-color:{colore}59;'
        f'background:{colore}1f">{etichetta}</span></div>'
        f'<div class="dettagli">{"".join(dettagli)}</div>{escluse}</div>',
        unsafe_allow_html=True,
    )

    if not modificabile:
        return

    c1, c2, c3 = st.columns([1, 1, 4])
    with c1:
        if ricerca.in_pausa:
            if st.button("Riattiva", key=f"on_{ricerca.nome}", use_container_width=True):
                _applica(lambda d: modifica_pausa(d, ricerca.nome, False),
                         f"config: riattivata '{ricerca.nome}' dalla dashboard")
                st.rerun()
        else:
            if st.button("Sospendi", key=f"off_{ricerca.nome}", use_container_width=True):
                _applica(lambda d: modifica_pausa(d, ricerca.nome, True),
                         f"config: sospesa '{ricerca.nome}' dalla dashboard")
                st.rerun()
    with c2:
        if st.button("Elimina", key=f"del_{ricerca.nome}", use_container_width=True):
            st.session_state[f"conferma_{ricerca.nome}"] = True

    if st.session_state.get(f"conferma_{ricerca.nome}"):
        st.warning(f"Eliminare **{ricerca.nome}**? Gli annunci già in archivio restano.")
        c1, c2, _ = st.columns([1, 1, 4])
        if c1.button("Sì, elimina", key=f"si_{ricerca.nome}", type="primary"):
            _applica(lambda d: modifica_rimuovi_ricerca(d, ricerca.nome),
                     f"config: rimossa '{ricerca.nome}' dalla dashboard")
            st.session_state.pop(f"conferma_{ricerca.nome}", None)
            st.rerun()
        if c2.button("Annulla", key=f"no_{ricerca.nome}"):
            st.session_state.pop(f"conferma_{ricerca.nome}", None)
            st.rerun()

    with st.expander("Modifica parametri"):
        with st.form(f"modifica_{ricerca.nome}"):
            campi = _campi_ricerca(ricerca, f"mod_{ricerca.nome}")
            if st.form_submit_button("Salva", type="primary"):
                if not campi["parole_chiave"] or not campi["piattaforme"]:
                    st.error("Parole chiave e almeno una piattaforma sono obbligatorie.")
                else:
                    def aggiorna(documento, campi=campi, nome=ricerca.nome):
                        voce = trova_ricerca(documento, nome)
                        if voce is None:
                            return
                        voce["parole_chiave"] = campi["parole_chiave"]
                        voce["piattaforme"] = campi["piattaforme"]
                        voce["parole_escluse"] = campi["parole_escluse"]
                        voce["prezzo_min"] = campi["prezzo_min"]
                        voce["prezzo_max"] = campi["prezzo_max"]
                        voce["condizione"] = campi["condizione"]
                        voce["intervallo_minuti"] = campi["intervallo_minuti"]
                        voce["eta_massima_giorni"] = campi["eta_massima_giorni"]
                        voce["solo_titolo"] = campi["solo_titolo"]
                        sub = voce.get("subito")
                        if isinstance(sub, dict):
                            sub["zona"] = campi["zona"]
                        else:
                            voce["subito"] = {"zona": campi["zona"], "raggio_km": 0}

                    if _applica(aggiorna, f"config: modificata '{ricerca.nome}' dalla dashboard"):
                        st.rerun()


def pagina_ricerche() -> None:
    from config_loader import configurazione_da_documento, documento_da_testo, modifica_aggiungi_ricerca

    intestazione("Ricerche", "Crea, modifica e sospendi le ricerche del monitor.", "impostazioni")

    try:
        testo, _sha, origine = carica_config()
        configurazione = configurazione_da_documento(documento_da_testo(testo))
    except Exception as exc:
        st.error(f"Impossibile leggere la configurazione: {exc}")
        st.stop()
        return

    scrivibile, motivo = _scrittura_possibile()
    if not scrivibile:
        st.info(motivo + "\n\nLe ricerche restano gestibili dal bot Telegram.", icon="🔒")
    elif not _autenticato():
        return

    if origine == "locale":
        st.caption("Configurazione letta dal file locale, non da GitHub.")

    # Quanti annunci ha prodotto ciascuna ricerca: dà contesto immediato.
    conteggi: dict[str, int] = {}
    try:
        stato, _ = carica_stato()
        tabella = costruisci_tabella(stato)
        if not tabella.empty:
            conteggi = tabella["ricerca"].value_counts().to_dict()
    except Exception:
        pass

    attive = sum(1 for r in configurazione.ricerche if r.eseguibile)
    riquadri([
        ("Ricerche", str(len(configurazione.ricerche)), "configurate", "cerca"),
        ("In funzione", str(attive), f"{len(configurazione.ricerche) - attive} sospese", "riproduci"),
        ("Annunci in archivio", str(sum(conteggi.values())), f"ultimi {GIORNI_STORICO} giorni", "inventario"),
    ])

    if not configurazione.ricerche:
        stato_vuoto("Nessuna ricerca configurata",
                    "Creane una qui sotto, oppure usa /add dal bot Telegram.", "cerca")

    for ricerca in configurazione.ricerche:
        _scheda_ricerca(ricerca, scrivibile, conteggi)

    if not scrivibile:
        return

    st.markdown(f'<div class="titolo-sezione">{design.icona("aggiungi", 13)}Nuova ricerca</div>',
                unsafe_allow_html=True)
    with st.form("nuova_ricerca"):
        nome = st.text_input(
            "Nome", placeholder="ps5-pro",
            help="Identificatore: minuscole, cifre, trattini. Serve anche per i comandi Telegram.",
        )
        campi = _campi_ricerca(None, "nuova")
        if st.form_submit_button("Crea ricerca", type="primary"):
            pulito = nome.strip().lower().replace(" ", "-")
            if not pulito or not campi["parole_chiave"] or not campi["piattaforme"]:
                st.error("Nome, parole chiave e almeno una piattaforma sono obbligatori.")
            else:
                campi["nome"] = pulito
                if _applica(lambda d, c=campi: modifica_aggiungi_ricerca(d, c),
                            f"config: aggiunta '{pulito}' dalla dashboard"):
                    st.rerun()

    st.caption(
        "Ogni modifica diventa un commit su `config.yaml`. Il monitor la applica "
        "al controllo successivo, e la trovi anche con /list su Telegram."
    )


# ===========================================================================
# Navigazione
# ===========================================================================

def _pannello_trigger() -> None:
    """
    Stato del trigger esterno con l'interruttore. La dashboard è l'unico
    posto da cui si può riaccendere: a trigger spento il bot non legge più i
    comandi.
    """
    chiave = _segreto("CRONJOB_API_KEY")
    job_id = _segreto("CRONJOB_JOB_ID")
    if not chiave or not job_id:
        return

    from trigger_esterno import imposta_attivo, stato_job

    try:
        stato = stato_job(chiave, job_id)
    except Exception as exc:
        st.caption(f"Trigger esterno: stato non leggibile ({str(exc)[:60]})")
        return

    if stato["attivo"]:
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:.4rem;'
            f'color:{design.STATO["ok"]};font-size:.82rem;font-weight:600">'
            f'{design.icona("riproduci", 14)}Trigger esterno attivo</div>',
            unsafe_allow_html=True,
        )
        if st.button("Ferma il trigger", use_container_width=True,
                     icon=":material/power_settings_new:"):
            riuscito, messaggio = imposta_attivo(chiave, job_id, False)
            if riuscito:
                st.warning("Trigger fermato. Il monitor non partirà più finché "
                           "non lo riattivi da qui.")
                st.rerun()
            else:
                st.error(messaggio)
    else:
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:.4rem;'
            f'color:{design.STATO["errore"]};font-size:.82rem;font-weight:600">'
            f'{design.icona("pausa", 14)}Trigger esterno SPENTO</div>',
            unsafe_allow_html=True,
        )
        st.caption("Il monitor non sta partendo. Nessun controllo, nessuna notifica.")
        if st.button("Riattiva il trigger", use_container_width=True,
                     type="primary", icon=":material/play_arrow:"):
            riuscito, messaggio = imposta_attivo(chiave, job_id, True)
            if riuscito:
                st.success("Trigger riattivato.")
                st.rerun()
            else:
                st.error(messaggio)


def main() -> None:
    with st.sidebar:
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:.55rem;padding:.2rem 0 1rem;'
            f'color:{design.TESTO_1};font-weight:650;font-size:1.02rem;letter-spacing:-0.01em">'
            f'{design.icona("cerca", 19)}{NOME_APP}</div>',
            unsafe_allow_html=True,
        )

    pagine = [
        st.Page(pagina_annunci, title="Annunci", icon=":material/inventory_2:", default=True),
        st.Page(pagina_statistiche, title="Statistiche", icon=":material/insights:"),
        st.Page(pagina_ricerche, title="Ricerche", icon=":material/tune:"),
    ]
    st.navigation(pagine).run()

    with st.sidebar:
        st.divider()
        _pannello_trigger()

        token, repository = _config_github()
        if token and repository:
            # Il cron schedulato di GitHub parte con 10-20 minuti di ritardo:
            # questo pulsante fa partire un controllo in pochi secondi. È il
            # modo per avere una risposta immediata ai comandi Telegram senza
            # aprire GitHub dal telefono.
            if st.button("Controlla adesso", use_container_width=True,
                         type="primary", icon=":material/bolt:"):
                from config_loader import avvia_workflow
                riuscito, messaggio = avvia_workflow(
                    token=token, repository=repository, branch=_branch()
                )
                if riuscito:
                    st.success(
                        "Controllo avviato. Fra circa un minuto trovi qui i "
                        "risultati e su Telegram le risposte ai comandi in coda."
                    )
                else:
                    st.error(messaggio)

        if st.button("Aggiorna dati", use_container_width=True,
                     icon=":material/refresh:"):
            carica_stato.clear()
            carica_config.clear()
            st.rerun()

        try:
            stato, _ = carica_stato()
            aggiornato = _data(stato.get("aggiornato_il"))
            if aggiornato:
                st.caption(f"Ultimo controllo {tempo_relativo(aggiornato.astimezone(_fuso()))}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
