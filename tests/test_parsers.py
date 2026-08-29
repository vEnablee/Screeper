"""
Test offline dei parser e della logica di filtro.

Non toccano la rete: usano risposte reali salvate in `tests/fixtures/`.
Servono soprattutto a una cosa: quando Vinted o Subito smetteranno di
funzionare — e prima o poi succederà — questi test dicono SE il problema è nel
parsing (test rossi, il sito ha cambiato formato) o nell'accesso (test verdi,
siamo bloccati o l'endpoint è cambiato). Sono due problemi con due rimedi
completamente diversi.

Esecuzione:
    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

RADICE = Path(__file__).resolve().parent.parent
if str(RADICE) not in sys.path:
    sys.path.insert(0, str(RADICE))

from models import Annuncio, Condizione, Impostazioni, Ricerca, adesso_utc  # noqa: E402
from scrapers.base import estrai_prezzo, normalizza_condizione  # noqa: E402
from scrapers.ebay import EbayScraper  # noqa: E402
from scrapers.subito import SubitoScraper  # noqa: E402
from scrapers.vinted import VintedScraper  # noqa: E402
from storage.state import Stato  # noqa: E402
from utils.dates import parse_data  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(nome: str) -> str:
    return (FIXTURES / nome).read_text(encoding="utf-8")


def fixture_json(nome: str) -> dict:
    return json.loads(fixture(nome))


class FintoHTTP:
    """Sostituto del client HTTP: gli scraper lo usano solo per il profilo."""

    def __init__(self) -> None:
        self.profilo = {"user_agent": "test-agent", "impersonate": "chrome131"}
        self.richieste = 0

    def pausa(self, motivo: str = "") -> None:
        return None


def scraper(classe):
    return classe(FintoHTTP(), Impostazioni())


# ---------------------------------------------------------------------------
# Prezzi e condizioni
# ---------------------------------------------------------------------------

class TestPrezzi(unittest.TestCase):

    def test_formato_italiano(self) -> None:
        self.assertEqual(estrai_prezzo("1.234,50 €"), 1234.50)
        self.assertEqual(estrai_prezzo("€ 45"), 45.0)
        self.assertEqual(estrai_prezzo("289,00"), 289.0)
        self.assertEqual(estrai_prezzo("1.200"), 1200.0)

    def test_numeri_e_api(self) -> None:
        self.assertEqual(estrai_prezzo("289.00"), 289.0)
        self.assertEqual(estrai_prezzo(35), 35.0)
        self.assertEqual(estrai_prezzo(12.5), 12.5)

    def test_valori_speciali(self) -> None:
        self.assertEqual(estrai_prezzo("Gratis"), 0.0)
        self.assertIsNone(estrai_prezzo(""))
        self.assertIsNone(estrai_prezzo(None))
        self.assertIsNone(estrai_prezzo("Trattabile"))

    def test_condizioni(self) -> None:
        self.assertEqual(normalizza_condizione("Usato"), Condizione.USATO.value)
        self.assertEqual(normalizza_condizione("Nuovo con cartellino"), Condizione.NUOVO.value)
        self.assertEqual(normalizza_condizione("Ricondizionato"), Condizione.RICONDIZIONATO.value)
        self.assertEqual(normalizza_condizione("Ottime condizioni"), Condizione.USATO.value)
        self.assertEqual(normalizza_condizione(None), Condizione.QUALSIASI.value)


# ---------------------------------------------------------------------------
# Date
# ---------------------------------------------------------------------------

class TestDate(unittest.TestCase):

    def setUp(self) -> None:
        # Riferimento fisso: 28/08/2026 14:00 UTC = 16:00 a Roma.
        self.adesso = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)

    def _parse(self, grezzo):
        return parse_data(grezzo, tz_locale="Europe/Rome", adesso=self.adesso)

    def test_iso(self) -> None:
        data, incerta = self._parse("2026-08-28T07:12:44.000Z")
        self.assertFalse(incerta)
        self.assertEqual(data, datetime(2026, 8, 28, 7, 12, 44, tzinfo=timezone.utc))

    def test_epoch_secondi_e_millisecondi(self) -> None:
        atteso = datetime(2026, 8, 28, 7, 12, 44, tzinfo=timezone.utc)
        epoch = int(atteso.timestamp())
        self.assertEqual(self._parse(epoch)[0], atteso)
        self.assertEqual(self._parse(epoch * 1000)[0], atteso)

    def test_epoch_assurdo_scartato(self) -> None:
        data, incerta = self._parse(12345)
        self.assertIsNone(data)
        self.assertTrue(incerta)

    def test_oggi_con_orario(self) -> None:
        # "Oggi alle 14:32" è ora LOCALE: 12:32 UTC.
        data, incerta = self._parse("Oggi alle 14:32")
        self.assertFalse(incerta)
        self.assertEqual(data, datetime(2026, 8, 28, 12, 32, tzinfo=timezone.utc))

    def test_ieri(self) -> None:
        data, _ = self._parse("Ieri alle 09:05")
        self.assertEqual(data, datetime(2026, 8, 27, 7, 5, tzinfo=timezone.utc))

    def test_oggi_senza_orario_e_incerta(self) -> None:
        _, incerta = self._parse("Oggi")
        self.assertTrue(incerta, "senza orario conosciamo solo il giorno")

    def test_relativo_minuti(self) -> None:
        data, incerta = self._parse("3 min fa")
        self.assertFalse(incerta)
        self.assertEqual(data, self.adesso - timedelta(minutes=3))

    def test_relativo_giorni_e_incerto(self) -> None:
        # Oltre il giorno la granularità è troppo grossa per fidarsi.
        _, incerta = self._parse("2 mesi fa")
        self.assertTrue(incerta)

    def test_data_testuale_italiana(self) -> None:
        data, incerta = self._parse("12 mar alle 09:10")
        self.assertEqual(data, datetime(2026, 3, 12, 8, 10, tzinfo=timezone.utc))
        self.assertTrue(incerta, "l'anno è stato dedotto")

    def test_data_con_trattino_forma_ebay(self) -> None:
        # eBay scrive "28-ago 14:32" nella lista dei risultati.
        data, _ = self._parse("28-ago 14:32")
        self.assertEqual(data, datetime(2026, 8, 28, 12, 32, tzinfo=timezone.utc))

    def test_data_numerica(self) -> None:
        data, incerta = self._parse("27/08/2026 10:30")
        self.assertEqual(data, datetime(2026, 8, 27, 8, 30, tzinfo=timezone.utc))
        self.assertFalse(incerta)

    def test_illeggibile_non_inventa_nulla(self) -> None:
        # È il caso più importante: mai spacciare per recente ciò che non si sa.
        for grezzo in ("", None, "poco tempo addietro", "???", []):
            data, incerta = self._parse(grezzo)
            self.assertIsNone(data, f"{grezzo!r} non doveva produrre una data")
            self.assertTrue(incerta)

    def test_data_effettiva_ripiega_sull_avvistamento(self) -> None:
        avvistamento = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        annuncio = Annuncio(
            piattaforma="subito", id_annuncio="1", titolo="t", url="u",
            data_pubblicazione=None, data_avvistamento=avvistamento, data_incerta=True,
        )
        self.assertEqual(annuncio.data_effettiva, avvistamento)


# ---------------------------------------------------------------------------
# Scraper: eBay
# ---------------------------------------------------------------------------

class TestEbay(unittest.TestCase):

    def setUp(self) -> None:
        self.scraper = scraper(EbayScraper)
        self.dati = fixture_json("ebay_api.json")

    def test_traduzione_completa(self) -> None:
        annuncio = self.scraper._da_json_api(self.dati["itemSummaries"][0])
        assert annuncio is not None
        self.assertEqual(annuncio.piattaforma, "ebay")
        self.assertEqual(annuncio.id_annuncio, "v1|335678901234|0")
        self.assertEqual(annuncio.prezzo, 289.0)
        self.assertEqual(annuncio.valuta, "EUR")
        self.assertTrue(annuncio.spedizione_inclusa)
        self.assertEqual(annuncio.localita, "Milano, MI")
        self.assertEqual(annuncio.venditore, "tech_store_mi")
        self.assertEqual(annuncio.condizione, Condizione.USATO.value)
        self.assertFalse(annuncio.data_incerta)
        self.assertEqual(
            annuncio.data_pubblicazione,
            datetime(2026, 8, 28, 7, 12, 44, tzinfo=timezone.utc),
        )

    def test_spedizione_a_pagamento(self) -> None:
        annuncio = self.scraper._da_json_api(self.dati["itemSummaries"][1])
        assert annuncio is not None
        self.assertFalse(annuncio.spedizione_inclusa)

    def test_data_assente_marcata_incerta(self) -> None:
        annuncio = self.scraper._da_json_api(self.dati["itemSummaries"][1])
        assert annuncio is not None
        self.assertIsNone(annuncio.data_pubblicazione)
        self.assertTrue(annuncio.data_incerta)

    def test_elemento_incompleto_scartato(self) -> None:
        self.assertIsNone(self.scraper._da_json_api({"itemId": "x"}))
        self.assertIsNone(self.scraper._da_json_api({}))

    def test_filtro_api(self) -> None:
        ricerca = Ricerca(
            nome="t", parole_chiave="iphone 13", piattaforme=["ebay"],
            prezzo_min=150, prezzo_max=320, condizione=Condizione.USATO.value,
        )
        filtro = self.scraper._filtro_api(ricerca)
        self.assertIn("price:[150.00..320.00]", filtro)
        self.assertIn("priceCurrency:EUR", filtro)
        self.assertIn("conditionIds:{3000}", filtro)

    # -- ripiego HTML ------------------------------------------------------
    # Nota: questa fixture riproduce il markup osservato in una risposta reale
    # di ebay.it, incluso il testo per lettori di schermo che finiva dentro i
    # titoli. Non è una cattura integrale perché eBay blocca gli IP dei
    # datacenter: la via primaria resta comunque la Browse API.

    def _schede(self):
        from bs4 import BeautifulSoup
        zuppa = BeautifulSoup(fixture("ebay_sch.html"), "lxml")
        return zuppa.select("li.s-item")

    def test_html_scarta_il_segnaposto(self) -> None:
        schede = self._schede()
        self.assertEqual(len(schede), 3, "la fixture ha 3 <li>, di cui uno segnaposto")
        annunci = [a for a in (self.scraper._da_html(s) for s in schede) if a]
        self.assertEqual(len(annunci), 2, "'Shop on eBay' non è un annuncio")

    def test_html_titolo_senza_testo_di_accessibilita(self) -> None:
        annuncio = self.scraper._da_html(self._schede()[1])
        assert annuncio is not None
        self.assertEqual(annuncio.titolo, "Apple iPhone 13 - 128GB - Mezzanotte (Sbloccato)")
        for frase in ("nuova finestra", "nuova scheda", "si apre", "viene aperta"):
            self.assertNotIn(frase, annuncio.titolo.lower())

    def test_html_rimuove_il_prefisso_nuova_inserzione(self) -> None:
        annuncio = self.scraper._da_html(self._schede()[2])
        assert annuncio is not None
        self.assertEqual(annuncio.titolo, "iPhone 13 128GB Blu")

    def test_html_campi_estratti(self) -> None:
        annuncio = self.scraper._da_html(self._schede()[1])
        assert annuncio is not None
        self.assertEqual(annuncio.id_annuncio, "318791528050")
        self.assertEqual(annuncio.url, "https://www.ebay.it/itm/318791528050")
        self.assertEqual(annuncio.prezzo, 279.0)
        self.assertFalse(annuncio.spedizione_inclusa, "+9,90 di spedizione non è inclusa")
        self.assertEqual(annuncio.localita, "Italia")
        self.assertEqual(annuncio.condizione, Condizione.RICONDIZIONATO.value)
        self.assertTrue((annuncio.immagine or "").startswith("https://i.ebayimg.com/"))

    def test_html_prezzo_con_separatore_di_migliaia(self) -> None:
        annuncio = self.scraper._da_html(self._schede()[2])
        assert annuncio is not None
        self.assertEqual(annuncio.prezzo, 1234.50)
        self.assertTrue(annuncio.spedizione_inclusa, "'Spedizione gratis' è inclusa")

    def test_html_senza_data_marca_incerta(self) -> None:
        """Il ripiego HTML non dà una data affidabile: è il motivo per cui al
        primo avvio questi annunci non vengono notificati."""
        annuncio = self.scraper._da_html(self._schede()[2])
        assert annuncio is not None
        self.assertTrue(annuncio.data_incerta)

    def test_url_html_ordina_per_recenti(self) -> None:
        ricerca = Ricerca(nome="t", parole_chiave="iphone 13", piattaforme=["ebay"], prezzo_max=320)
        url = self.scraper._url_html(ricerca, 1)
        self.assertIn("_sop=10", url)      # 10 = inserzioni più recenti
        self.assertIn("_udhi=320", url)


# ---------------------------------------------------------------------------
# Scraper: Vinted
# ---------------------------------------------------------------------------

class TestVinted(unittest.TestCase):
    """
    Fixture ricavata da una risposta reale dell'API catalogo, ridotta ai soli
    campi che il parser usa e con i nomi utente sostituiti.
    """

    def setUp(self) -> None:
        self.scraper = scraper(VintedScraper)
        self.items = fixture_json("vinted_catalog.json")["items"]

    def test_traduzione_completa(self) -> None:
        annuncio = self.scraper._da_json(self.items[0])
        assert annuncio is not None
        self.assertEqual(annuncio.piattaforma, "vinted")
        self.assertTrue(annuncio.id_annuncio.isdigit())
        self.assertTrue(annuncio.titolo)
        self.assertTrue(annuncio.url.startswith("https://www.vinted.it/"))
        self.assertIsNotNone(annuncio.prezzo)
        self.assertEqual(annuncio.valuta, "EUR")
        self.assertEqual(annuncio.venditore, "utente_esempio_1")

    def test_condizione_abbreviata(self) -> None:
        """Vinted scrive "Ottime", non "Ottime condizioni"."""
        self.assertEqual(self.items[0]["status"], "Ottime")
        annuncio = self.scraper._da_json(self.items[0])
        assert annuncio is not None
        self.assertEqual(annuncio.condizione, Condizione.USATO.value)

    def test_spedizione_mai_inclusa(self) -> None:
        annuncio = self.scraper._da_json(self.items[0])
        assert annuncio is not None
        self.assertFalse(annuncio.spedizione_inclusa)

    def test_data_dal_timestamp_della_foto(self) -> None:
        """`created_at_ts` è sempre null nell'API: l'unico appiglio è il
        timestamp della foto in alta risoluzione."""
        self.assertIsNone(self.items[0].get("created_at_ts"))
        atteso = self.items[0]["photo"]["high_resolution"]["timestamp"]
        annuncio = self.scraper._da_json(self.items[0])
        assert annuncio is not None
        self.assertEqual(
            annuncio.data_pubblicazione,
            datetime.fromtimestamp(atteso, tz=timezone.utc),
        )

    def test_senza_timestamp_data_incerta(self) -> None:
        annuncio = self.scraper._da_json(self.items[1])
        assert annuncio is not None
        self.assertIsNone(annuncio.data_pubblicazione)
        self.assertTrue(annuncio.data_incerta)
        # E l'annuncio ripiega sull'avvistamento, mai su una data inventata.
        self.assertEqual(annuncio.data_effettiva, annuncio.data_avvistamento)

    def test_url_relativo_completato(self) -> None:
        self.assertTrue(self.items[1]["url"].startswith("/items/"))
        annuncio = self.scraper._da_json(self.items[1])
        assert annuncio is not None
        self.assertTrue(annuncio.url.startswith("https://www.vinted.it/items/"))

    def test_prezzo_in_formato_vecchio(self) -> None:
        # Le versioni precedenti dell'API mandavano il prezzo come stringa.
        elemento = dict(self.items[0], price="42.00", currency="EUR")
        annuncio = self.scraper._da_json(elemento)
        assert annuncio is not None
        self.assertEqual(annuncio.prezzo, 42.0)

    def test_elemento_incompleto_scartato(self) -> None:
        self.assertIsNone(self.scraper._da_json({"id": 1}))
        self.assertIsNone(self.scraper._da_json({}))
        self.assertIsNone(self.scraper._da_json("non un dizionario"))


# ---------------------------------------------------------------------------
# Scraper: Subito
# ---------------------------------------------------------------------------

class TestSubito(unittest.TestCase):
    """
    La fixture è una risposta REALE dell'API hades, salvata così com'è
    (solo l'id del venditore è anonimizzato). Se questi test diventano rossi,
    Subito ha cambiato il formato dei dati; se restano verdi ma il monitor non
    trova nulla, il problema è l'accesso, non il parsing.
    """

    def setUp(self) -> None:
        self.scraper = scraper(SubitoScraper)
        self.dati = fixture_json("subito_hades.json")
        self.ads = self.dati["ads"]

    def test_traduzione_completa(self) -> None:
        annuncio = self.scraper._da_ad(self.ads[0])
        assert annuncio is not None
        self.assertEqual(annuncio.piattaforma, "subito")
        self.assertTrue(annuncio.titolo)
        self.assertTrue(annuncio.url.startswith("https://www.subito.it/"))
        self.assertIsNotNone(annuncio.prezzo)
        self.assertIsNotNone(annuncio.localita)

    def test_id_estratto_dall_ultimo_segmento_dell_urn(self) -> None:
        # urn: "id:ad:<uuid>:list:657586319" -> l'id è l'ultimo segmento.
        annuncio = self.scraper._da_ad(self.ads[0])
        assert annuncio is not None
        self.assertEqual(annuncio.id_annuncio, self.ads[0]["urn"].rsplit(":", 1)[-1])
        self.assertTrue(annuncio.id_annuncio.isdigit())

    def test_features_in_forma_di_lista(self) -> None:
        # Nella risposta reale `features` è una lista, non una mappa.
        self.assertIsInstance(self.ads[0]["features"], list)
        prezzo = self.scraper._valore_caratteristica(self.ads[0]["features"], "/price")
        self.assertIsNotNone(prezzo, "il prezzo deve uscire dalla forma a lista")
        self.assertIsNotNone(estrai_prezzo(prezzo))

    def test_features_in_forma_di_mappa_ancora_supportate(self) -> None:
        # Forma storica, ancora possibile nei dati incorporati nell'HTML.
        mappa = {"/price": {"uri": "/price", "values": [{"key": "price", "value": "35 €"}]}}
        self.assertEqual(self.scraper._valore_caratteristica(mappa, "/price"), "35 €")

    def test_data_usa_offset_di_fuso(self) -> None:
        """`display` è senza offset: leggerlo come UTC sposterebbe ogni
        annuncio di due ore indietro. Deve vincere `display_iso8601`."""
        date = self.ads[0]["dates"]
        self.assertIn("display_iso8601", date)
        annuncio = self.scraper._da_ad(self.ads[0])
        assert annuncio is not None
        atteso = datetime.fromisoformat(date["display_iso8601"]).astimezone(timezone.utc)
        self.assertEqual(annuncio.data_pubblicazione, atteso)
        self.assertFalse(annuncio.data_incerta)

    def test_immagine_con_parametro_rule(self) -> None:
        """L'URL base delle immagini restituisce 404 senza `?rule=`."""
        annuncio = self.scraper._da_ad(self.ads[0])
        assert annuncio is not None
        if self.ads[0].get("images"):
            self.assertIsNotNone(annuncio.immagine)
            self.assertIn("?rule=", annuncio.immagine or "")
            self.assertTrue((annuncio.immagine or "").startswith("https://"))

    def test_spedizione_disponibile_non_significa_inclusa(self) -> None:
        """Su Subito il prezzo non comprende mai la spedizione."""
        annuncio = self.scraper._da_ad(self.ads[0])
        assert annuncio is not None
        self.assertIn(annuncio.spedizione_inclusa, (False, None))
        self.assertIsNot(annuncio.spedizione_inclusa, True)

    def test_estrazione_da_next_data(self) -> None:
        elementi = self.scraper._estrai_next_data(fixture("subito_next.html"))
        self.assertEqual(len(elementi), len(self.ads))
        annuncio = self.scraper._da_ad(elementi[0])
        assert annuncio is not None
        self.assertEqual(annuncio.id_annuncio, self.ads[0]["urn"].rsplit(":", 1)[-1])

    def test_filtro_zona_confronta_i_valori_geo(self) -> None:
        """La zona è tipicamente una REGIONE, che non compare nella località
        mostrata (comune + sigla provincia): il confronto deve avvenire sui
        campi geo, altrimenti verrebbe scartato tutto."""
        regione = self.ads[0]["geo"]["region"]["value"].lower()
        ricerca = Ricerca(nome="t", parole_chiave="thin client", piattaforme=["subito"])
        ricerca.subito.zona = regione

        tenuti = self.scraper._filtra_zona(self.ads, ricerca)
        self.assertEqual(len(tenuti), 1)
        self.assertEqual(tenuti[0]["urn"], self.ads[0]["urn"])

    def test_filtro_zona_per_comune(self) -> None:
        comune = self.ads[0]["geo"]["town"]["value"].lower()
        ricerca = Ricerca(nome="t", parole_chiave="thin client", piattaforme=["subito"])
        ricerca.subito.zona = comune
        self.assertEqual(len(self.scraper._filtra_zona(self.ads, ricerca)), 1)

    def test_zona_italia_non_filtra(self) -> None:
        ricerca = Ricerca(nome="t", parole_chiave="thin client", piattaforme=["subito"])
        self.assertEqual(len(self.scraper._filtra_zona(self.ads, ricerca)), len(self.ads))

    def test_annuncio_senza_geo_non_viene_scartato(self) -> None:
        ricerca = Ricerca(nome="t", parole_chiave="x", piattaforme=["subito"])
        ricerca.subito.zona = "lombardia"
        self.assertEqual(len(self.scraper._filtra_zona([{"urn": "id:ad:x:list:1"}], ricerca)), 1)


# ---------------------------------------------------------------------------
# Filtri della ricerca
# ---------------------------------------------------------------------------

class TestFiltri(unittest.TestCase):

    def setUp(self) -> None:
        self.ricerca = Ricerca(
            nome="iphone-13",
            parole_chiave="iphone 13",
            piattaforme=["ebay"],
            parole_escluse=["rotto", "non funzionante", "ricambi"],
            prezzo_min=150,
            prezzo_max=320,
        )

    def test_parola_esclusa_su_parole_intere(self) -> None:
        self.assertEqual(self.ricerca.parola_esclusa("iPhone 13 ROTTO"), "rotto")
        # "rottore" contiene "rotto" ma non è la stessa parola.
        self.assertIsNone(self.ricerca.parola_esclusa("iPhone 13 rottoreale"))

    def test_parola_esclusa_multi_parola(self) -> None:
        self.assertEqual(
            self.ricerca.parola_esclusa("iPhone 13 NON  FUNZIONANTE"), "non funzionante"
        )

    def test_fascia_di_prezzo(self) -> None:
        self.assertTrue(self.ricerca.prezzo_ok(200))
        self.assertFalse(self.ricerca.prezzo_ok(100))
        self.assertFalse(self.ricerca.prezzo_ok(400))
        self.assertTrue(self.ricerca.prezzo_ok(None), "prezzo ignoto non deve escludere")

    def test_condizione(self) -> None:
        self.ricerca.condizione = Condizione.USATO.value
        self.assertTrue(self.ricerca.condizione_ok(Condizione.USATO.value))
        self.assertFalse(self.ricerca.condizione_ok(Condizione.NUOVO.value))
        self.assertTrue(
            self.ricerca.condizione_ok(Condizione.QUALSIASI.value),
            "condizione non rilevata dalla piattaforma non deve escludere",
        )

    def test_pipeline_di_filtro(self) -> None:
        from main import filtra
        import logging

        annunci = [
            Annuncio(piattaforma="ebay", id_annuncio="1", titolo="iPhone 13 128GB", url="u1", prezzo=289),
            Annuncio(piattaforma="ebay", id_annuncio="2", titolo="iPhone 13 rotto", url="u2", prezzo=90),
            Annuncio(piattaforma="ebay", id_annuncio="3", titolo="iPhone 13 Pro", url="u3", prezzo=900),
            Annuncio(piattaforma="ebay", id_annuncio="4", titolo="Cover per Samsung", url="u4", prezzo=200),
        ]
        tenuti = filtra(annunci, self.ricerca, logging.getLogger("test"))
        self.assertEqual([a.id_annuncio for a in tenuti], ["1"])


# ---------------------------------------------------------------------------
# Stato
# ---------------------------------------------------------------------------

class TestStato(unittest.TestCase):

    def _annuncio(self, id_annuncio: str = "1", **extra) -> Annuncio:
        campi = dict(
            piattaforma="ebay", id_annuncio=id_annuncio,
            titolo="iPhone 13 128GB", url=f"https://ebay.it/itm/{id_annuncio}",
            prezzo=289.0, venditore="tech_store_mi",
        )
        campi.update(extra)
        return Annuncio(**campi)

    def test_deduplica_per_chiave(self) -> None:
        stato = Stato.nuovo()
        annuncio = self._annuncio()
        self.assertFalse(stato.ha_visto(annuncio))
        stato.marca_visto(annuncio)
        self.assertTrue(stato.ha_visto(annuncio))

    def test_riconosce_ripubblicazione(self) -> None:
        stato = Stato.nuovo()
        stato.marca_visto(self._annuncio("1"))
        # Stesso titolo, prezzo e venditore ma id diverso: è lo stesso oggetto.
        ripubblicato = self._annuncio("2")
        self.assertFalse(stato.ha_visto(ripubblicato))
        self.assertTrue(stato.fingerprint_noto(ripubblicato))

    def test_prezzo_diverso_non_e_ripubblicazione(self) -> None:
        stato = Stato.nuovo()
        stato.marca_visto(self._annuncio("1"))
        self.assertFalse(stato.fingerprint_noto(self._annuncio("2", prezzo=250.0)))

    def test_intervallo_ricerca(self) -> None:
        stato = Stato.nuovo()
        ricerca = Ricerca(nome="r", parole_chiave="x", piattaforme=["ebay"], intervallo_minuti=30)
        self.assertTrue(stato.da_eseguire(ricerca), "mai eseguita: va eseguita")

        stato.registra_esecuzione("r")
        self.assertFalse(stato.da_eseguire(ricerca))
        self.assertTrue(stato.da_eseguire(ricerca, adesso=adesso_utc() + timedelta(minutes=31)))

    def test_quarantena_dopo_blocco(self) -> None:
        from models import EsitoScraper
        stato = Stato.nuovo()
        impostazioni = Impostazioni(run_pausa_dopo_blocco=3)

        stato.registra_esito(
            "vinted", EsitoScraper.BLOCCATO, errore="403", impostazioni=impostazioni
        )
        self.assertTrue(stato.in_quarantena("vinted"))

        for _ in range(3):
            stato.consuma_quarantena("vinted")
        self.assertFalse(stato.in_quarantena("vinted"))

    def test_alert_scraper_rotto_una_sola_volta(self) -> None:
        from models import EsitoScraper
        stato = Stato.nuovo()
        for _ in range(3):
            stato.registra_esito("subito", EsitoScraper.VUOTO)

        self.assertTrue(stato.alert_da_inviare("subito", 3))
        stato.marca_alert_inviato("subito")
        self.assertFalse(stato.alert_da_inviare("subito", 3))

        # Un successo azzera il contatore e riarma l'alert per il futuro.
        stato.registra_esito("subito", EsitoScraper.OK, risultati=5)
        for _ in range(3):
            stato.registra_esito("subito", EsitoScraper.VUOTO)
        self.assertTrue(stato.alert_da_inviare("subito", 3))

    def test_potatura_per_data_e_per_numero(self) -> None:
        stato = Stato.nuovo()
        impostazioni = Impostazioni(storico_giorni=30, storico_max_annunci=5)

        vecchio = (adesso_utc() - timedelta(days=45)).isoformat()
        recente = adesso_utc().isoformat()
        stato.dati["storico"] = (
            [{"titolo": "vecchio", "data_avvistamento": vecchio} for _ in range(4)]
            + [{"titolo": "recente", "data_avvistamento": recente} for _ in range(8)]
        )
        stato.dati["visti"] = {"ebay:vecchio": vecchio, "ebay:nuovo": recente}

        stato.pota(impostazioni)

        self.assertEqual(len(stato.dati["storico"]), 5, "vincolo sul numero massimo")
        self.assertTrue(all(v["titolo"] == "recente" for v in stato.dati["storico"]))
        self.assertNotIn("ebay:vecchio", stato.dati["visti"])
        self.assertIn("ebay:nuovo", stato.dati["visti"])

    def test_serializzazione_ciclo_completo(self) -> None:
        originale = self._annuncio("77", data_pubblicazione=adesso_utc())
        ricostruito = Annuncio.from_dict(originale.to_dict())
        self.assertEqual(ricostruito.chiave, originale.chiave)
        self.assertEqual(ricostruito.fingerprint, originale.fingerprint)
        self.assertEqual(ricostruito.prezzo, originale.prezzo)
        self.assertEqual(ricostruito.data_pubblicazione, originale.data_pubblicazione)


# ---------------------------------------------------------------------------
# Selezione dei nuovi e finestra di primo avvio
# ---------------------------------------------------------------------------

class TestSelezioneNuovi(unittest.TestCase):

    def setUp(self) -> None:
        import logging
        self.log = logging.getLogger("test")
        self.ricerca = Ricerca(nome="r", parole_chiave="iphone 13", piattaforme=["ebay"])
        self.impostazioni = Impostazioni(finestra_primo_avvio_minuti=60)

    def _annunci(self):
        return [
            Annuncio(
                piattaforma="ebay", id_annuncio="recente", titolo="iPhone 13 A", url="u1",
                prezzo=200, data_pubblicazione=adesso_utc() - timedelta(minutes=5),
            ),
            Annuncio(
                piattaforma="ebay", id_annuncio="vecchio", titolo="iPhone 13 B", url="u2",
                prezzo=210, data_pubblicazione=adesso_utc() - timedelta(days=5),
            ),
        ]

    def test_primo_avvio_notifica_solo_i_recenti(self) -> None:
        from main import seleziona_nuovi
        stato = Stato.nuovo()
        nuovi = seleziona_nuovi(
            self._annunci(), self.ricerca, stato, self.impostazioni,
            primo_avvio=True, solo_semina=False, log=self.log,
        )
        self.assertEqual([a.id_annuncio for a in nuovi], ["recente"])
        # Anche quello vecchio è stato marcato come visto: non tornerà mai più.
        self.assertTrue(stato.ha_visto(self._annunci()[1]))

    def test_a_regime_notifica_tutto_cio_che_e_nuovo(self) -> None:
        from main import seleziona_nuovi
        stato = Stato.nuovo()
        nuovi = seleziona_nuovi(
            self._annunci(), self.ricerca, stato, self.impostazioni,
            primo_avvio=False, solo_semina=False, log=self.log,
        )
        self.assertEqual(len(nuovi), 2)

    def test_seconda_esecuzione_non_ripete(self) -> None:
        from main import seleziona_nuovi
        stato = Stato.nuovo()
        seleziona_nuovi(
            self._annunci(), self.ricerca, stato, self.impostazioni,
            primo_avvio=False, solo_semina=False, log=self.log,
        )
        nuovi = seleziona_nuovi(
            self._annunci(), self.ricerca, stato, self.impostazioni,
            primo_avvio=False, solo_semina=False, log=self.log,
        )
        self.assertEqual(nuovi, [])

    def test_seed_non_notifica_nulla(self) -> None:
        from main import seleziona_nuovi
        stato = Stato.nuovo()
        nuovi = seleziona_nuovi(
            self._annunci(), self.ricerca, stato, self.impostazioni,
            primo_avvio=False, solo_semina=True, log=self.log,
        )
        self.assertEqual(nuovi, [])
        self.assertTrue(stato.ha_visto(self._annunci()[0]))

    def test_primo_avvio_non_notifica_le_date_ignote(self) -> None:
        """
        Il caso che al primo run causava centinaia di notifiche: eBay via
        HTML non espone la data, quindi OGNI annuncio risultava "avvistato
        adesso" e cadeva dentro la finestra di primo avvio.
        """
        from main import seleziona_nuovi
        stato = Stato.nuovo()
        annuncio = Annuncio(
            piattaforma="ebay", id_annuncio="x", titolo="iPhone 13 C", url="u",
            prezzo=200, data_pubblicazione=None, data_incerta=True,
        )
        nuovi = seleziona_nuovi(
            [annuncio], self.ricerca, stato, self.impostazioni,
            primo_avvio=True, solo_semina=False, log=self.log,
        )
        self.assertEqual(nuovi, [], "al primo avvio una data ignota non si notifica")
        self.assertTrue(stato.ha_visto(annuncio), "ma va comunque marcato come visto")

    def test_a_regime_la_data_ignota_si_notifica(self) -> None:
        """Dal secondo run in poi 'mai visto prima' basta: se non c'era nella
        pagina del run precedente, è nuovo davvero."""
        from main import seleziona_nuovi
        stato = Stato.nuovo()
        annuncio = Annuncio(
            piattaforma="ebay", id_annuncio="y", titolo="iPhone 13 D", url="u",
            prezzo=200, data_pubblicazione=None, data_incerta=True,
        )
        nuovi = seleziona_nuovi(
            [annuncio], self.ricerca, stato, self.impostazioni,
            primo_avvio=False, solo_semina=False, log=self.log,
        )
        self.assertEqual(len(nuovi), 1)


# ---------------------------------------------------------------------------
# Parsing dei comandi del bot
# ---------------------------------------------------------------------------

class TestComandi(unittest.TestCase):

    def test_add_completo(self) -> None:
        from bot.commands import _analizza_campi
        campi = _analizza_campi(
            "nome=iPhone 13 | kw=iphone 13 | piattaforme=ebay,subito | "
            "min=150 | max=320 | escluse=rotto,ricambi | condizione=usato | intervallo=20"
        )
        self.assertEqual(campi["nome"], "iphone-13")
        self.assertEqual(campi["parole_chiave"], "iphone 13")
        self.assertEqual(campi["piattaforme"], ["ebay", "subito"])
        self.assertEqual(campi["prezzo_min"], 150.0)
        self.assertEqual(campi["prezzo_max"], 320.0)
        self.assertEqual(campi["parole_escluse"], ["rotto", "ricambi"])
        self.assertEqual(campi["intervallo_minuti"], 20)

    def test_piattaforma_sconosciuta(self) -> None:
        from bot.commands import _analizza_campi
        with self.assertRaises(ValueError):
            _analizza_campi("nome=x | kw=y | piattaforme=amazon")

    def test_parametro_senza_uguale(self) -> None:
        from bot.commands import _analizza_campi
        with self.assertRaises(ValueError):
            _analizza_campi("nome=x | questo è sbagliato")

    def test_prezzo_non_numerico(self) -> None:
        from bot.commands import _analizza_campi
        with self.assertRaises(ValueError):
            _analizza_campi("nome=x | kw=y | max=molto")


# ---------------------------------------------------------------------------
# Ciclo completo dei comandi del bot
# ---------------------------------------------------------------------------

class FintoNotifier:
    """Notificatore finto: registra i messaggi e restituisce update predefiniti."""

    def __init__(self, chat_id: str = "123456", aggiornamenti=None) -> None:
        self.chat_id = chat_id
        self.tz_locale = "Europe/Rome"
        self.messaggi: list[str] = []
        self._aggiornamenti = aggiornamenti or []

    def invia_messaggio(self, testo_html: str, **_) -> bool:
        self.messaggi.append(testo_html)
        return True

    def leggi_aggiornamenti(self, offset: int) -> list[dict]:
        return [a for a in self._aggiornamenti if a["update_id"] > offset]


def aggiornamento(update_id: int, testo: str, chat_id: str = "123456") -> dict:
    return {"update_id": update_id, "message": {"text": testo, "chat": {"id": chat_id}}}


class TestCicloComandi(unittest.TestCase):
    """Esercita ProcessoreComandi su una copia temporanea di config.yaml."""

    # Il config di prova è scritto qui dentro, NON copiato da config.yaml:
    # quel file appartiene all'utente e cambia di continuo (ricerche diverse,
    # flag di pausa invertiti). Un test che vi si appoggia si rompe a ogni
    # modifica della configurazione, senza che il codice abbia colpe.
    CONFIG_DI_PROVA = """
impostazioni:
  timezone: Europe/Rome            # commento che deve sopravvivere alle modifiche
  finestra_primo_avvio_minuti: 60

ricerche:
  - nome: alfa
    attiva: true
    in_pausa: false
    intervallo_minuti: 15
    piattaforme: [ebay, subito]
    parole_chiave: "console alfa"
    parole_escluse: [rotto]
    prezzo_min: 100
    prezzo_max: 300
    condizione: qualsiasi
    solo_titolo: true

  - nome: beta
    attiva: true
    in_pausa: false
    intervallo_minuti: 30
    piattaforme: [subito]
    parole_chiave: "oggetto beta"
    parole_escluse: []
    condizione: usato
    solo_titolo: true

  - nome: gamma
    attiva: true
    in_pausa: true                 # sospesa a mano: /riprendi non deve svegliarla
    intervallo_minuti: 60
    piattaforme: [vinted]
    parole_chiave: "cosa gamma"
    parole_escluse: []
    condizione: qualsiasi
    solo_titolo: false
"""

    def setUp(self) -> None:
        import tempfile
        self.cartella = tempfile.mkdtemp()
        self.config = Path(self.cartella) / "config.yaml"
        self.config.write_text(self.CONFIG_DI_PROVA.lstrip(), encoding="utf-8")
        self.stato = Stato.nuovo()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.cartella, ignore_errors=True)

    def _esegui(self, notifier) -> bool:
        """Crea un ProcessoreComandi nuovo, come farebbe un run successivo."""
        from bot.commands import ProcessoreComandi
        processore = ProcessoreComandi(
            notifier, self.stato,
            percorso_config=str(self.config),
            tz_locale="Europe/Rome",
            token_github="",
            dry_run=True,      # niente commit verso GitHub durante i test
        )
        return processore.processa()

    def _configurazione(self):
        from config_loader import carica_configurazione
        return carica_configurazione(self.config)

    def test_list_non_modifica_nulla(self) -> None:
        notifier = FintoNotifier(aggiornamenti=[aggiornamento(1, "/list")])
        self.assertFalse(self._esegui(notifier))
        self.assertEqual(len(notifier.messaggi), 1)
        self.assertIn("alfa", notifier.messaggi[0])

    def test_add_scrive_la_ricerca_nel_file(self) -> None:
        notifier = FintoNotifier(aggiornamenti=[
            aggiornamento(1, "/add nome=nintendo | kw=nintendo switch | piattaforme=ebay | max=180")
        ])
        self.assertTrue(self._esegui(notifier))
        ricerca = self._configurazione().ricerca_per_nome("nintendo")
        self.assertIsNotNone(ricerca)
        assert ricerca is not None
        self.assertEqual(ricerca.parole_chiave, "nintendo switch")
        self.assertEqual(ricerca.piattaforme, ["ebay"])
        self.assertEqual(ricerca.prezzo_max, 180.0)

    def test_add_preserva_i_commenti_del_file(self) -> None:
        """È il motivo per cui si usa ruamel invece di PyYAML."""
        notifier = FintoNotifier(aggiornamenti=[aggiornamento(1, "/add nome=x | kw=y")])
        self._esegui(notifier)
        testo = self.config.read_text(encoding="utf-8")
        self.assertIn("commento che deve sopravvivere", testo)
        self.assertIn("finestra_primo_avvio_minuti", testo)

    def test_add_duplicato_rifiutato(self) -> None:
        notifier = FintoNotifier(aggiornamenti=[
            aggiornamento(1, "/add nome=alfa | kw=qualcosa")
        ])
        self.assertFalse(self._esegui(notifier))
        self.assertIn("esiste già", " ".join(notifier.messaggi))

    def test_pause_e_resume(self) -> None:
        notifier = FintoNotifier(aggiornamenti=[aggiornamento(1, "/pause alfa")])
        self._esegui(notifier)
        ricerca = self._configurazione().ricerca_per_nome("alfa")
        assert ricerca is not None
        self.assertTrue(ricerca.in_pausa)
        self.assertFalse(ricerca.eseguibile)

        self.stato.ultimo_update_id = 0
        notifier = FintoNotifier(aggiornamenti=[aggiornamento(2, "/resume alfa")])
        self._esegui(notifier)
        ricerca = self._configurazione().ricerca_per_nome("alfa")
        assert ricerca is not None
        self.assertFalse(ricerca.in_pausa)

    def test_exclude_con_parola_composta(self) -> None:
        notifier = FintoNotifier(aggiornamenti=[
            aggiornamento(1, "/exclude alfa non funzionante")
        ])
        self.assertTrue(self._esegui(notifier))
        ricerca = self._configurazione().ricerca_per_nome("alfa")
        assert ricerca is not None
        self.assertIn("non funzionante", ricerca.parole_escluse)

    def test_remove(self) -> None:
        notifier = FintoNotifier(aggiornamenti=[aggiornamento(1, "/remove beta")])
        self.assertTrue(self._esegui(notifier))
        self.assertIsNone(self._configurazione().ricerca_per_nome("beta"))

    def test_chat_non_autorizzata_ignorata(self) -> None:
        notifier = FintoNotifier(aggiornamenti=[
            aggiornamento(1, "/remove alfa", chat_id="999999999")
        ])
        self.assertFalse(self._esegui(notifier))
        self.assertEqual(notifier.messaggi, [], "a un estraneo non si risponde nemmeno")
        self.assertIsNotNone(self._configurazione().ricerca_per_nome("alfa"))

    def test_offset_avanza_anche_sui_comandi_errati(self) -> None:
        """Senza questo, un messaggio malformato verrebbe rielaborato per sempre."""
        notifier = FintoNotifier(aggiornamenti=[
            aggiornamento(7, "/add parametro-senza-uguale"),
            aggiornamento(8, "/comando-inesistente"),
        ])
        self._esegui(notifier)
        self.assertEqual(self.stato.ultimo_update_id, 8)

    def test_comandi_multipli_in_un_solo_run(self) -> None:
        notifier = FintoNotifier(aggiornamenti=[
            aggiornamento(1, "/add nome=lego | kw=lego star wars | piattaforme=subito"),
            aggiornamento(2, "/exclude lego minifigure"),
            aggiornamento(3, "/pause lego"),
        ])
        self.assertTrue(self._esegui(notifier))
        ricerca = self._configurazione().ricerca_per_nome("lego")
        assert ricerca is not None
        self.assertIn("minifigure", ricerca.parole_escluse)
        self.assertTrue(ricerca.in_pausa)

    def test_stop_sospende_tutte_le_attive(self) -> None:
        # Nel config di esempio gamma è già in pausa: /stop deve
        # toccare solo le altre due.
        notifier = FintoNotifier(aggiornamenti=[aggiornamento(1, "/stop")])
        self.assertTrue(self._esegui(notifier))

        configurazione = self._configurazione()
        self.assertEqual(sum(1 for r in configurazione.ricerche if r.eseguibile), 0)
        self.assertEqual(
            sorted(self.stato.sospese_da_stop), ["alfa", "beta"]
        )
        self.assertNotIn("gamma", self.stato.sospese_da_stop)

    def test_riprendi_non_risveglia_le_pause_manuali(self) -> None:
        """Il punto delicato: /riprendi deve riportare esattamente alla
        situazione precedente a /stop, non riattivare tutto alla cieca."""
        self._esegui(FintoNotifier(aggiornamenti=[aggiornamento(1, "/stop")]))

        self.stato.ultimo_update_id = 0
        self._documento = None    # rilettura dal file, come in un run nuovo
        self._modificato = False
        self._esegui(FintoNotifier(aggiornamenti=[aggiornamento(2, "/riprendi")]))

        configurazione = self._configurazione()
        attive = {r.nome for r in configurazione.ricerche if r.eseguibile}
        self.assertEqual(attive, {"alfa", "beta"})

        sospesa = configurazione.ricerca_per_nome("gamma")
        assert sospesa is not None
        self.assertTrue(sospesa.in_pausa, "era in pausa a mano: deve restarci")
        self.assertEqual(self.stato.sospese_da_stop, [])

    def test_stop_quando_e_gia_tutto_fermo(self) -> None:
        self._esegui(FintoNotifier(aggiornamenti=[aggiornamento(1, "/stop")]))
        self.stato.ultimo_update_id = 0
        self._documento = None
        self._modificato = False

        notifier = FintoNotifier(aggiornamenti=[aggiornamento(2, "/stop")])
        self.assertFalse(self._esegui(notifier), "niente da cambiare, nessun commit")
        self.assertIn("già tutto fermo", " ".join(notifier.messaggi))

    def test_riprendi_senza_memoria_riattiva_tutto_e_lo_dichiara(self) -> None:
        from config_loader import carica_documento, modifica_pausa_tutte, salva_documento
        documento = carica_documento(self.config)
        modifica_pausa_tutte(documento, True)
        salva_documento(documento, self.config)
        self.stato.sospese_da_stop = []

        notifier = FintoNotifier(aggiornamenti=[aggiornamento(1, "/riprendi")])
        self.assertTrue(self._esegui(notifier))
        testo = " ".join(notifier.messaggi)
        self.assertIn("riattivate tutte", testo.lower())
        self.assertEqual(sum(1 for r in self._configurazione().ricerche if r.eseguibile), 3)

    def test_ricerca_disattivata_non_viene_risvegliata(self) -> None:
        """`attiva: false` è una scelta a lungo termine: né /stop né /riprendi
        devono toccarla."""
        from config_loader import carica_documento, salva_documento, trova_ricerca
        documento = carica_documento(self.config)
        trova_ricerca(documento, "alfa")["attiva"] = False
        salva_documento(documento, self.config)

        self._esegui(FintoNotifier(aggiornamenti=[aggiornamento(1, "/riprendi")]))
        ricerca = self._configurazione().ricerca_per_nome("alfa")
        assert ricerca is not None
        self.assertFalse(ricerca.eseguibile)

    def test_modifica_invalida_non_corrompe_il_file(self) -> None:
        """Il config viene riletto e validato prima di essere scritto."""
        from ruamel.yaml import YAML
        notifier = FintoNotifier(aggiornamenti=[aggiornamento(1, "/add nome=ok | kw=prova")])
        self._esegui(notifier)
        documento = YAML().load(self.config.read_text(encoding="utf-8"))
        self.assertIn("impostazioni", documento)
        self.assertIn("ricerche", documento)
        self._configurazione()   # non deve sollevare


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

class TestConfigurazione(unittest.TestCase):

    def test_config_spedito_e_valido(self) -> None:
        """Il config reale deve restare caricabile qualunque cosa contenga:
        è l'unico controllo che ha senso fare su un file che l'utente
        modifica di continuo."""
        from config_loader import carica_configurazione
        configurazione = carica_configurazione(RADICE / "config.yaml")
        self.assertGreaterEqual(len(configurazione.ricerche), 1)
        self.assertEqual(configurazione.impostazioni.timezone, "Europe/Rome")
        for ricerca in configurazione.ricerche:
            self.assertTrue(ricerca.piattaforme, f"{ricerca.nome} senza piattaforme")
            self.assertTrue(ricerca.parole_chiave, f"{ricerca.nome} senza parole chiave")
            self.assertTrue(ricerca.nome.islower(), f"{ricerca.nome} deve essere minuscolo")

    def test_semantica_di_attiva_e_in_pausa(self) -> None:
        from models import Ricerca
        r = Ricerca(nome="x", parole_chiave="y", piattaforme=["ebay"])
        self.assertTrue(r.eseguibile)
        r.in_pausa = True
        self.assertFalse(r.eseguibile, "in pausa non deve girare")
        r.in_pausa, r.attiva = False, False
        self.assertFalse(r.eseguibile, "disattivata non deve girare")

    def test_prezzi_incoerenti_rifiutati(self) -> None:
        from config_loader import ConfigError, _ricerca_da_dict
        with self.assertRaises(ConfigError):
            _ricerca_da_dict(
                {"nome": "x", "parole_chiave": "y", "piattaforme": ["ebay"],
                 "prezzo_min": 300, "prezzo_max": 100},
                0,
            )

    def test_nome_con_spazi_rifiutato(self) -> None:
        from config_loader import ConfigError, _ricerca_da_dict
        with self.assertRaises(ConfigError):
            _ricerca_da_dict(
                {"nome": "nome con spazi", "parole_chiave": "y", "piattaforme": ["ebay"]}, 0
            )


# ---------------------------------------------------------------------------
# Salvataggio dello stato: marcatori usati dal workflow
# ---------------------------------------------------------------------------

class TestMarcatoriSalvataggio(unittest.TestCase):
    """
    Lo step `if: always()` del workflow è un processo separato da main.py:
    l'unico modo che ha di sapere com'è andato il run è leggere dei marcatori
    su disco. Qui si verifica che li interpreti correttamente.
    """

    def setUp(self) -> None:
        import tempfile
        from storage import state as archivio
        self.cartella = Path(tempfile.mkdtemp())
        self._originali = (
            archivio.CARTELLA_LOCALE,
            archivio.PERCORSO_LOCALE,
            archivio.MARCATORE_CARICATO,
            archivio.MARCATORE_NO_UPLOAD,
        )
        archivio.CARTELLA_LOCALE = self.cartella
        archivio.PERCORSO_LOCALE = self.cartella / "stato.json"
        archivio.MARCATORE_CARICATO = self.cartella / "caricato_sul_gist"
        archivio.MARCATORE_NO_UPLOAD = self.cartella / "no_upload"
        self.archivio = archivio

    def tearDown(self) -> None:
        import shutil
        (
            self.archivio.CARTELLA_LOCALE,
            self.archivio.PERCORSO_LOCALE,
            self.archivio.MARCATORE_CARICATO,
            self.archivio.MARCATORE_NO_UPLOAD,
        ) = self._originali
        shutil.rmtree(self.cartella, ignore_errors=True)

    def test_stato_locale_ciclo_completo(self) -> None:
        stato = Stato.nuovo()
        stato.dati["visti"]["ebay:1"] = "2026-08-28T10:00:00+00:00"
        self.archivio.salva_locale(stato, self.archivio.PERCORSO_LOCALE)
        riletto = self.archivio.carica_locale(self.archivio.PERCORSO_LOCALE)
        assert riletto is not None
        self.assertIn("ebay:1", riletto.dati["visti"])

    def test_dry_run_vieta_il_caricamento(self) -> None:
        """Il bug che aveva reso il --dry-run non del tutto 'a vuoto':
        main.py saltava la scrittura ma lo step di recupero la eseguiva."""
        self.assertFalse(self.archivio.upload_vietato())
        self.archivio.marca_da_non_caricare()
        self.assertTrue(self.archivio.upload_vietato())

    def test_marcatore_di_caricamento_avvenuto(self) -> None:
        self.assertFalse(self.archivio.gia_caricato())
        self.archivio.marca_caricato()
        self.assertTrue(self.archivio.gia_caricato())

    def test_i_due_marcatori_sono_indipendenti(self) -> None:
        self.archivio.marca_caricato()
        self.assertFalse(self.archivio.upload_vietato())

    def test_stato_locale_illeggibile_non_solleva(self) -> None:
        self.archivio.PERCORSO_LOCALE.write_text("{ json rotto", encoding="utf-8")
        self.assertIsNone(self.archivio.carica_locale(self.archivio.PERCORSO_LOCALE))


# ---------------------------------------------------------------------------
# Opzioni della riga di comando
# ---------------------------------------------------------------------------

class TestOpzioni(unittest.TestCase):

    def test_dry_run_implica_nessun_effetto_collaterale(self) -> None:
        """
        `--dry-run` deve disattivare anche la lettura dei comandi Telegram:
        altrimenti il comando viene consumato dalla coda ed eseguito, ma la
        risposta finisce solo nei log e chi l'ha mandato aspetta invano.
        """
        import main
        opzioni = main.analizza_argomenti(["--dry-run"])
        self.assertTrue(opzioni.dry_run)
        # `main()` applica le implicazioni, non `analizza_argomenti()`.
        self.assertFalse(opzioni.no_notify)

        import argparse
        from unittest import mock
        with mock.patch.object(main, "esegui", return_value=0) as finto:
            main.main(["--dry-run"])
        applicate: argparse.Namespace = finto.call_args[0][0]
        self.assertTrue(applicate.no_notify, "il dry-run non deve notificare")
        self.assertTrue(applicate.no_bot, "il dry-run non deve consumare i comandi")

    def test_run_normale_lascia_il_bot_attivo(self) -> None:
        import main
        from unittest import mock
        with mock.patch.object(main, "esegui", return_value=0) as finto:
            main.main([])
        applicate = finto.call_args[0][0]
        self.assertFalse(applicate.no_notify)
        self.assertFalse(applicate.no_bot)


# ---------------------------------------------------------------------------
# Gist
# ---------------------------------------------------------------------------

class TestGist(unittest.TestCase):

    def test_compressione_ciclo_completo(self) -> None:
        from gist_client import comprimi, decomprimi
        stato = {"visti": {"ebay:1": "2026-08-28T10:00:00+00:00"}, "storico": [{"titolo": "à è ì"}]}
        self.assertEqual(decomprimi(comprimi(stato)), stato)

    def test_accetta_json_in_chiaro(self) -> None:
        from gist_client import decomprimi
        self.assertEqual(decomprimi('{"a": 1}'), {"a": 1})

    def test_contenuto_vuoto(self) -> None:
        from gist_client import decomprimi
        self.assertEqual(decomprimi(""), {})

    def test_compressione_efficace(self) -> None:
        from gist_client import comprimi
        stato = {"storico": [{"titolo": f"iPhone 13 128GB numero {i}",
                              "url": f"https://www.ebay.it/itm/{i}",
                              "piattaforma": "ebay", "prezzo": 289.0} for i in range(1000)]}
        compresso = len(comprimi(stato))
        grezzo = len(json.dumps(stato))
        self.assertLess(compresso, grezzo / 5, "gzip deve comprimere almeno 5:1 su questi dati")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# Ritmo delle richieste
# ---------------------------------------------------------------------------

class TestRitmoPerDominio(unittest.TestCase):
    """
    L'attesa fra le richieste deve valere per SITO, non globalmente.

    Distanziare una richiesta a Subito da una a eBay non protegge da nulla —
    i due siti non si parlano — e su GitHub Actions ogni secondo in più
    avvicina il job al minuto successivo, che viene fatturato per intero.
    """

    def _client(self, orologio):
        from scrapers.http import ClientHTTP
        client = ClientHTTP.__new__(ClientHTTP)
        import random
        client._rng = random.Random(1)
        client.delay_min, client.delay_max = 4.0, 4.0
        client._ultima_per_host = {}
        self.attese: list[float] = []
        # Si sostituiscono orologio e sleep: il test non deve dormire davvero.
        import scrapers.http as modulo
        self._modulo = modulo
        self._tempo_reale = modulo.time.monotonic
        self._sleep_reale = modulo.time.sleep
        modulo.time.monotonic = lambda: orologio[0]
        def finto_sleep(s):
            self.attese.append(s)
            orologio[0] += s
        modulo.time.sleep = finto_sleep
        return client

    def tearDown(self) -> None:
        modulo = getattr(self, "_modulo", None)
        if modulo is not None:
            modulo.time.monotonic = self._tempo_reale
            modulo.time.sleep = self._sleep_reale

    def test_domini_diversi_non_si_aspettano(self) -> None:
        orologio = [1000.0]
        c = self._client(orologio)
        for url in ("https://www.ebay.it/sch", "https://hades.subito.it/v1",
                    "https://www.vinted.it/api"):
            c._rispetta_ritmo(url)
            c._ultima_per_host[c._host(url)] = orologio[0]
        self.assertEqual(self.attese, [], "tre siti diversi: nessuna attesa")

    def test_stesso_dominio_viene_distanziato(self) -> None:
        orologio = [1000.0]
        c = self._client(orologio)
        url = "https://hades.subito.it/v1/search/items"
        c._rispetta_ritmo(url)
        c._ultima_per_host[c._host(url)] = orologio[0]
        c._rispetta_ritmo(url)
        self.assertEqual(len(self.attese), 1)
        self.assertAlmostEqual(self.attese[0], 4.0, places=1)

    def test_tempo_gia_trascorso_viene_scalato(self) -> None:
        """Se fra le due richieste è passato del tempo per conto suo, si
        attende solo la differenza."""
        orologio = [1000.0]
        c = self._client(orologio)
        url = "https://www.vinted.it/api/v2/catalog/items"
        c._ultima_per_host[c._host(url)] = orologio[0]
        orologio[0] += 3.0            # sono già passati 3 secondi
        c._rispetta_ritmo(url)
        self.assertAlmostEqual(self.attese[0], 1.0, places=1)

    def test_host_estratto_correttamente(self) -> None:
        from scrapers.http import ClientHTTP
        self.assertEqual(ClientHTTP._host("https://Www.EBAY.it/sch?x=1"), "www.ebay.it")
        self.assertEqual(ClientHTTP._host("https://hades.subito.it/v1/x"), "hades.subito.it")
        # Sottodomini diversi sono host diversi: è il comportamento voluto,
        # hades.subito.it e www.subito.it sono serviti da infrastrutture diverse.
        self.assertNotEqual(ClientHTTP._host("https://www.subito.it/a"),
                            ClientHTTP._host("https://hades.subito.it/a"))


# ---------------------------------------------------------------------------
# Menu dei comandi Telegram
# ---------------------------------------------------------------------------

class TestMenuComandi(unittest.TestCase):
    """
    Il menu che compare digitando "/" vive sui server di Telegram, non nel
    nostro stato. Fidarsi di un'impronta locale si era rivelato sbagliato:
    l'impronta descriveva la lista dei comandi, non COME venivano registrati,
    quindi correggere la registrazione non faceva scattare nulla e il menu
    restava rotto indefinitamente.
    """

    def _notifier(self, registrati, esito_set=True):
        from notifiers.telegram import TelegramNotifier
        n = TelegramNotifier.__new__(TelegramNotifier)
        n.token = "123:AAA"
        n.chat_id = "1"
        n.tz_locale = "Europe/Rome"
        n.abilitato = True
        n.inviati = n.falliti = 0
        n._ultimo_invio = 0.0
        self.chiamate: list[tuple[str, dict]] = []

        def finto(metodo, payload, tentativi=2):
            self.chiamate.append((metodo, payload))
            if metodo == "getMyCommands":
                return registrati
            if metodo == "setMyCommands":
                return True if esito_set else None
            return None

        n._chiama = finto  # type: ignore[method-assign]
        return n

    def test_registra_se_telegram_non_ha_nulla(self) -> None:
        from bot.commands import COMANDI_BOT
        n = self._notifier(registrati=[])
        self.assertTrue(n.sincronizza_comandi(COMANDI_BOT))
        metodi = [m for m, _ in self.chiamate]
        self.assertEqual(metodi, ["getMyCommands", "setMyCommands"])

    def test_non_ripete_se_gia_allineato(self) -> None:
        from bot.commands import COMANDI_BOT
        gia = [{"command": n, "description": d} for n, d in COMANDI_BOT]
        n = self._notifier(registrati=gia)
        self.assertTrue(n.sincronizza_comandi(COMANDI_BOT))
        self.assertEqual([m for m, _ in self.chiamate], ["getMyCommands"])

    def test_riallinea_se_le_descrizioni_differiscono(self) -> None:
        from bot.commands import COMANDI_BOT
        diverso = [{"command": n, "description": "vecchia descrizione"} for n, d in COMANDI_BOT]
        n = self._notifier(registrati=diverso)
        n.sincronizza_comandi(COMANDI_BOT)
        self.assertIn("setMyCommands", [m for m, _ in self.chiamate])

    def test_nessun_language_code_nel_payload(self) -> None:
        """Con language_code i comandi valgono SOLO per chi ha il client in
        quella lingua: tutti gli altri ricadono sulla lista predefinita, che
        resta vuota, e non vedono alcun suggerimento."""
        from bot.commands import COMANDI_BOT
        n = self._notifier(registrati=[])
        n.sincronizza_comandi(COMANDI_BOT)
        payload = dict(self.chiamate[-1][1])
        self.assertNotIn("language_code", payload)
        self.assertEqual(payload["scope"], {"type": "default"})

    def test_comandi_conformi_alle_regole_di_telegram(self) -> None:
        from bot.commands import COMANDI_BOT
        for nome, descrizione in COMANDI_BOT:
            self.assertRegex(nome, r"^[a-z0-9_]{1,32}$", f"nome non valido: {nome}")
            self.assertTrue(descrizione, f"{nome} senza descrizione")
            self.assertLessEqual(len(descrizione), 256, f"{nome}: descrizione troppo lunga")

    def test_i_comandi_del_menu_esistono_davvero(self) -> None:
        """Un comando nel menu che il bot non sa eseguire è peggio che assente."""
        sorgente = (RADICE / "bot" / "commands.py").read_text(encoding="utf-8")
        from bot.commands import COMANDI_BOT
        for nome, _ in COMANDI_BOT:
            self.assertIn(f'"/{nome}"', sorgente, f"/{nome} è nel menu ma non ha un gestore")


# ---------------------------------------------------------------------------
# Recupero dopo una pausa lunga
# ---------------------------------------------------------------------------

class TestRecuperoDopoPausa(unittest.TestCase):
    """
    Con il monitor fermo di notte, al risveglio la prima pagina di risultati
    può essersi riempita e gli annunci più vecchi essere scivolati in
    seconda. Misurato su Vinted: la prima pagina copre ~23 ore e in nove ore
    di notte si riempie per tre quarti. Senza recupero, quelli si perdono.
    """

    def setUp(self) -> None:
        import logging
        from models import Impostazioni, Ricerca
        self.log = logging.getLogger("test")
        self.impostazioni = Impostazioni(pagine_per_ricerca=1, pagine_primo_avvio=2)
        self.ricerca = Ricerca(nome="r", parole_chiave="x", piattaforme=["subito"],
                               intervallo_minuti=15)
        self.pagine_richieste: list[int] = []

    class _FintoScraper:
        via = "api"

        def __init__(self, registro):
            self._registro = registro

        def cerca(self, ricerca, pagine):
            self._registro.append(pagine)
            return []

    def _esegui(self, stato):
        from main import esegui_ricerca
        class FintoHTTP:
            richieste = 0
            def pausa(self, motivo=""): pass
        esegui_ricerca(
            self.ricerca,
            scrapers={"subito": self._FintoScraper(self.pagine_richieste)},
            stato=stato, impostazioni=self.impostazioni, http=FintoHTTP(),
            quarantena=set(), solo_piattaforma=None, log=self.log,
        )

    def test_primo_avvio_legge_due_pagine(self) -> None:
        self._esegui(Stato.nuovo())
        self.assertEqual(self.pagine_richieste, [2])

    def test_a_regime_basta_una_pagina(self) -> None:
        stato = Stato.nuovo()
        stato.registra_esecuzione("r")     # eseguita adesso
        self._esegui(stato)
        self.assertEqual(self.pagine_richieste, [1])

    def test_dopo_una_notte_torna_a_due_pagine(self) -> None:
        stato = Stato.nuovo()
        vecchia = adesso_utc() - timedelta(hours=9)
        stato.dati["ricerche"]["r"] = {
            "ultima_esecuzione": vecchia.isoformat(),
            "ultimo_nuovo": None, "totale_notificati": 0,
        }
        self._esegui(stato)
        self.assertEqual(self.pagine_richieste, [2], "9 ore di pausa: serve il recupero")

    def test_una_pausa_breve_non_attiva_il_recupero(self) -> None:
        """Il cron di GitHub ritarda spesso di qualche minuto: un ritardo
        fisiologico non deve raddoppiare le richieste a ogni giro."""
        stato = Stato.nuovo()
        poco_fa = adesso_utc() - timedelta(minutes=20)   # intervallo 15, soglia 45
        stato.dati["ricerche"]["r"] = {
            "ultima_esecuzione": poco_fa.isoformat(),
            "ultimo_nuovo": None, "totale_notificati": 0,
        }
        self._esegui(stato)
        self.assertEqual(self.pagine_richieste, [1])


# ---------------------------------------------------------------------------
# Dashboard: tempo relativo e indicatore di novità
# ---------------------------------------------------------------------------

class TestPresentazioneAnnunci(unittest.TestCase):
    """
    "8 min fa" al posto di "28/08 21:14": guardando una griglia di offerte la
    domanda è sempre "è appena uscito?", non "che ora era".
    """

    def setUp(self) -> None:
        import app
        self.app = app
        self.ora = datetime.now(app._fuso())

    def _fa(self, minuti: float):
        return self.ora - timedelta(minutes=minuti)

    def test_scale_del_tempo_relativo(self) -> None:
        casi = [
            (0.2, "adesso"), (8, "8 min fa"), (59, "59 min fa"),
            (95, "1 ora fa"), (300, "5 ore fa"),
            (1500, "1 giorno fa"), (4400, "3 giorni fa"),
        ]
        for minuti, atteso in casi:
            self.assertEqual(self.app.tempo_relativo(self._fa(minuti)), atteso, f"a -{minuti} min")

    def test_oltre_la_settimana_torna_alla_data(self) -> None:
        vecchio = self._fa(20 * 24 * 60)
        self.assertEqual(self.app.tempo_relativo(vecchio), vecchio.strftime("%d/%m"))

    def test_data_assente(self) -> None:
        self.assertEqual(self.app.tempo_relativo(None), "data ignota")

    def test_soglie_di_novita(self) -> None:
        self.assertEqual(self.app.novita(self._fa(10)), "nuovo")
        self.assertEqual(self.app.novita(self._fa(59)), "nuovo")
        self.assertEqual(self.app.novita(self._fa(120)), "recente")
        self.assertEqual(self.app.novita(self._fa(59 * 6)), "recente")
        self.assertEqual(self.app.novita(self._fa(7 * 60)), "")
        self.assertEqual(self.app.novita(None), "")

    def test_badge_nuovo_solo_sui_freschi(self) -> None:
        import pandas as pd
        base = {
            "titolo": "PS5 Pro", "prezzo": 350.0, "valuta": "EUR", "piattaforma": "subito",
            "ricerca": "ps5-pro", "localita": "Milano", "condizione": "usato",
            "url": "https://x/1", "immagine": "", "spedizione_inclusa": None,
            "data_incerta": False,
        }
        fresco = self.app.scheda_annuncio(pd.Series({**base, "data": self._fa(5)}), 400.0)
        vecchio = self.app.scheda_annuncio(pd.Series({**base, "data": self._fa(3000)}), 400.0)
        self.assertIn('class="nuovo"', fresco)
        self.assertNotIn('class="nuovo"', vecchio)

    def test_ordinamenti_coerenti(self) -> None:
        for etichetta, (colonna, crescente) in self.app.ORDINAMENTI.items():
            self.assertIn(colonna, ("data", "prezzo"))
            self.assertIsInstance(crescente, bool)
        self.assertEqual(self.app.ORDINAMENTI["Prezzo crescente"], ("prezzo", True))
        self.assertEqual(self.app.ORDINAMENTI["Più recenti"], ("data", False))

    def test_ebay_fuori_dalle_piattaforme_proponibili(self) -> None:
        import design
        self.assertNotIn("ebay", design.PIATTAFORME_UTILIZZABILI)
        # Il colore resta definito: se in archivio ci sono ancora annunci eBay
        # devono continuare a essere mostrati con la loro tinta.
        self.assertIn("ebay", design.COLORI_PIATTAFORMA)


# ---------------------------------------------------------------------------
# Avvio del workflow su richiesta
# ---------------------------------------------------------------------------

class TestAvvioWorkflow(unittest.TestCase):
    """
    Il cron di GitHub parte con 10-20 minuti di ritardo sul piano gratuito;
    un workflow_dispatch parte in pochi secondi. È il pulsante che rende
    immediati i comandi Telegram senza dover aprire GitHub.
    """

    def _risposta(self, codice, testo=""):
        class R:
            status_code = codice
            text = testo
        return R()

    def _con_risposta(self, risposta):
        from unittest import mock
        import config_loader
        return mock.patch.object(config_loader.requests, "post", return_value=risposta)

    def test_avvio_riuscito(self) -> None:
        from config_loader import avvia_workflow
        with self._con_risposta(self._risposta(204)) as finto:
            ok, messaggio = avvia_workflow(token="t", repository="u/r")
        self.assertTrue(ok)
        url = finto.call_args[0][0]
        self.assertIn("/actions/workflows/monitor.yml/dispatches", url)
        self.assertEqual(finto.call_args.kwargs["json"]["ref"], "main")

    def test_token_senza_permessi(self) -> None:
        from config_loader import avvia_workflow
        with self._con_risposta(self._risposta(403)):
            ok, messaggio = avvia_workflow(token="t", repository="u/r")
        self.assertFalse(ok)
        self.assertIn("scope `repo`", messaggio)

    def test_workflow_inesistente(self) -> None:
        from config_loader import avvia_workflow
        with self._con_risposta(self._risposta(404)):
            ok, messaggio = avvia_workflow(token="t", repository="u/r")
        self.assertFalse(ok)
        self.assertIn("non trovato", messaggio)

    def test_senza_trigger_dispatch(self) -> None:
        from config_loader import avvia_workflow
        with self._con_risposta(self._risposta(422)):
            ok, messaggio = avvia_workflow(token="t", repository="u/r")
        self.assertFalse(ok)
        self.assertIn("workflow_dispatch", messaggio)

    def test_senza_credenziali_non_chiama_nulla(self) -> None:
        from config_loader import avvia_workflow
        from unittest import mock
        import config_loader
        with mock.patch.object(config_loader.requests, "post") as finto:
            ok, _ = avvia_workflow(token="", repository="u/r")
        self.assertFalse(ok)
        finto.assert_not_called()

    def test_errore_di_rete_non_solleva(self) -> None:
        from config_loader import avvia_workflow
        from unittest import mock
        import config_loader
        import requests as req
        with mock.patch.object(config_loader.requests, "post",
                               side_effect=req.RequestException("timeout")):
            ok, messaggio = avvia_workflow(token="t", repository="u/r")
        self.assertFalse(ok)
        self.assertIn("rete", messaggio)


class TestNomeApplicazione(unittest.TestCase):

    def test_il_nome_e_coerente_ovunque(self) -> None:
        import app
        self.assertEqual(app.NOME_APP, "SCreeper")
        workflow = (RADICE / ".github" / "workflows" / "monitor.yml").read_text(encoding="utf-8")
        self.assertIn("name: SCreeper", workflow)

    def test_il_file_di_stato_non_e_stato_rinominato(self) -> None:
        """Rinominarlo avrebbe fatto ripartire da zero il Gist esistente,
        rinotificando tutto lo storico."""
        from gist_client import NOME_FILE_STATO
        self.assertEqual(NOME_FILE_STATO, "stato_monitor.json.gz.b64")


# ---------------------------------------------------------------------------
# Avviso di avvenuto controllo
# ---------------------------------------------------------------------------

class TestAvvisoControllo(unittest.TestCase):
    """Il messaggio deve dire cosa ha girato, non solo quanti annunci."""

    def setUp(self) -> None:
        from datetime import timezone
        from models import EsitoScraper, Impostazioni
        self.imp = Impostazioni()
        self.fine = datetime(2026, 8, 29, 9, 15, tzinfo=timezone.utc)
        self.stato = Stato.nuovo()
        self.stato.registra_esito("subito", EsitoScraper.OK, risultati=19)
        self.stato.registra_esito("vinted", EsitoScraper.OK, risultati=43)
        self.dettaglio = [
            {"nome": "ps5-pro", "trovati": 19, "filtrati": 7, "nuovi": 2},
            {"nome": "etb-rivali", "trovati": 27, "filtrati": 19, "nuovi": 0},
        ]

    def _testo(self, **extra):
        from main import componi_stato_controllo
        campi = dict(stato=self.stato, impostazioni=self.imp, dettaglio=self.dettaglio,
                     saltate=[], nuovi=2, notificati=2, richieste=7, errori=[],
                     terminato=self.fine)
        campi.update(extra)
        return componi_stato_controllo(**campi)

    def test_elenca_le_ricerche_eseguite(self) -> None:
        testo = self._testo()
        self.assertIn("ps5-pro", testo)
        self.assertIn("etb-rivali", testo)
        self.assertIn("7 rilevanti", testo)
        self.assertIn("2 nuovi", testo)

    def test_le_ricerche_senza_novita_non_dicono_zero(self) -> None:
        """"0 nuovi" ripetuto su ogni riga è rumore: un trattino basta."""
        self.assertIn("—", self._testo())

    def test_elenca_le_ricerche_in_attesa(self) -> None:
        testo = self._testo(saltate=["etb-scintille"])
        self.assertIn("etb-scintille", testo)
        self.assertIn("in attesa", testo)

    def test_stato_delle_piattaforme(self) -> None:
        testo = self._testo()
        self.assertIn("subito", testo)
        self.assertIn("vinted", testo)
        self.assertIn("7 richieste", testo)

    def test_nessuna_ricerca_da_eseguire(self) -> None:
        testo = self._testo(dettaglio=[], nuovi=0, notificati=0)
        self.assertIn("nessuna ricerca da eseguire", testo)

    def test_gli_errori_cambiano_intestazione(self) -> None:
        testo = self._testo(errori=["vinted: BLOCCATO — HTTP 403"])
        self.assertIn("Controllo con errori", testo)
        self.assertIn("403", testo)

    def test_html_degli_errori_neutralizzato(self) -> None:
        testo = self._testo(errori=["<script>alert(1)</script> & rotto"])
        self.assertNotIn("<script>", testo)
        self.assertIn("&lt;script&gt;", testo)

    def test_nomi_delle_ricerche_neutralizzati(self) -> None:
        """Un nome con caratteri speciali romperebbe il parsing di Telegram."""
        testo = self._testo(dettaglio=[{"nome": "a<b>&c", "trovati": 1,
                                        "filtrati": 1, "nuovi": 0}])
        self.assertNotIn("<b>&c", testo)

    def test_modalita_valide(self) -> None:
        from config_loader import _impostazioni_da_dict
        for valore in ("mai", "sempre", "aggiorna"):
            self.assertEqual(
                _impostazioni_da_dict({"notifica_ogni_controllo": valore}).notifica_ogni_controllo,
                valore,
            )

    def test_modalita_sconosciuta_ripiega_su_aggiorna(self) -> None:
        from config_loader import _impostazioni_da_dict
        self.assertEqual(
            _impostazioni_da_dict({"notifica_ogni_controllo": "boh"}).notifica_ogni_controllo,
            "aggiorna",
        )


class TestMessaggioDiStatoRiscritto(unittest.TestCase):

    def _notifier(self, esito_edit, esito_send=12345):
        from notifiers.telegram import TelegramNotifier
        n = TelegramNotifier.__new__(TelegramNotifier)
        n.token, n.chat_id, n.tz_locale = "1:AA", "9", "Europe/Rome"
        n.abilitato, n.inviati, n.falliti, n._ultimo_invio = True, 0, 0, 0.0
        self.chiamate: list[str] = []

        def finto(metodo, payload, tentativi=2):
            self.chiamate.append(metodo)
            if metodo == "editMessageText":
                return esito_edit
            if metodo == "sendMessage":
                return {"message_id": esito_send} if esito_send else None
            return None

        n._chiama = finto  # type: ignore[method-assign]
        n._ritmo = lambda: None  # type: ignore[method-assign]
        return n

    def test_primo_invio_restituisce_l_id(self) -> None:
        n = self._notifier(esito_edit=None)
        self.assertEqual(n.invia_stato_controllo("ciao", None), 12345)
        self.assertEqual(self.chiamate, ["sendMessage"])

    def test_le_volte_successive_riscrive(self) -> None:
        n = self._notifier(esito_edit=True)
        self.assertEqual(n.invia_stato_controllo("ciao", 555), 555)
        self.assertEqual(self.chiamate, ["editMessageText"])

    def test_messaggio_cancellato_ne_manda_uno_nuovo(self) -> None:
        """Se cancelli il messaggio dalla chat, la modifica fallisce: si
        riparte con uno nuovo invece di perdere l'avviso per sempre."""
        n = self._notifier(esito_edit=None)
        self.assertEqual(n.invia_stato_controllo("ciao", 555), 12345)
        self.assertEqual(self.chiamate, ["editMessageText", "sendMessage"])

    def test_invio_silenzioso(self) -> None:
        """Il messaggio di stato non deve far suonare il telefono."""
        from notifiers.telegram import TelegramNotifier
        n = TelegramNotifier.__new__(TelegramNotifier)
        n.token, n.chat_id, n.abilitato = "1:AA", "9", True
        n.inviati = n.falliti = 0
        n._ultimo_invio = 0.0
        n._ritmo = lambda: None  # type: ignore[method-assign]
        catturato = {}

        def finto(metodo, payload, tentativi=2):
            catturato.update(payload)
            return {"message_id": 1}

        n._chiama = finto  # type: ignore[method-assign]
        n.invia_stato_controllo("x", None)
        self.assertTrue(catturato.get("disable_notification"))


class TestElusioneDeiFiltri(unittest.TestCase):
    """
    Su Subito è pratica diffusa spaziare le lettere ("s c a m b i o") per
    aggirare i filtri automatici del sito. Il confronto su parole intere non
    le vede, quindi si controlla anche il testo con gli spazi rimossi.
    """

    def setUp(self) -> None:
        from models import Ricerca
        self.ricerca = Ricerca(
            nome="t", parole_chiave="ps5 pro", piattaforme=["subito"],
            parole_escluse=["scambio", "controller", "ricambi", "fat",
                            "non funzionante", "scatola vuota"],
        )

    def test_parola_spaziata_viene_riconosciuta(self) -> None:
        self.assertEqual(self.ricerca.parola_esclusa("Ps5 pro. s c a m b i o"), "scambio")
        self.assertEqual(self.ricerca.parola_esclusa("PS5 pro r i c a m b i"), "ricambi")

    def test_parola_dentro_un_altra_viene_riconosciuta(self) -> None:
        self.assertEqual(
            self.ricerca.parola_esclusa("Aimcontroller Custom PRO PS5"), "controller"
        )

    def test_espressione_composta_spaziata(self) -> None:
        self.assertEqual(
            self.ricerca.parola_esclusa("PS5 con s c a t o l a v u o t a"), "scatola vuota"
        )

    def test_i_termini_corti_non_generano_falsi_positivi(self) -> None:
        """"fat" ha tre lettere: cercarla senza confini di parola la
        troverebbe dentro "fatto", "fatica", "perfetta"."""
        for testo in ("PS5 Pro fatto benissimo", "PS5 Pro perfetta", "PS5 Pro fatica poco"):
            self.assertIsNone(self.ricerca.parola_esclusa(testo), testo)

    def test_gli_annunci_legittimi_passano(self) -> None:
        for testo in ("PS5 PRO 2TB + Blu-ray + DualSense",
                      "ps5 pro nuovo leggi bene",
                      "Sony ps5 pro 2 tb"):
            self.assertIsNone(self.ricerca.parola_esclusa(testo), testo)

    def test_la_forma_normale_continua_a_funzionare(self) -> None:
        self.assertEqual(self.ricerca.parola_esclusa("PS5 Pro con controller"), "controller")
        self.assertEqual(self.ricerca.parola_esclusa("PS5 Pro NON FUNZIONANTE"), "non funzionante")


class TestQuarantenaSenzaEffettiCollaterali(unittest.TestCase):

    def test_controllare_la_quarantena_non_crea_la_voce(self) -> None:
        """Interrogare lo stato di una piattaforma non configurata la faceva
        comparire fra quelle "mai eseguite" nei messaggi."""
        stato = Stato.nuovo()
        self.assertFalse(stato.in_quarantena("ebay"))
        self.assertEqual(stato.salute_piattaforme(), {})

    def test_registrare_un_esito_invece_la_crea(self) -> None:
        from models import EsitoScraper
        stato = Stato.nuovo()
        stato.registra_esito("subito", EsitoScraper.OK, risultati=3)
        self.assertIn("subito", stato.salute_piattaforme())


class TestPotaturaPiattaforme(unittest.TestCase):
    """
    Una piattaforma tolta dalla configurazione restava nello stato per
    sempre, con il suo ultimo esito — tipicamente "bloccato" — e continuava a
    comparire in /status, nell'heartbeat e nella dashboard, facendo credere a
    un guasto ormai inesistente.
    """

    def setUp(self) -> None:
        from models import EsitoScraper, Impostazioni
        self.stato = Stato.nuovo()
        self.stato.registra_esito("ebay", EsitoScraper.BLOCCATO,
                                  errore="403", impostazioni=Impostazioni())
        self.stato.registra_esito("subito", EsitoScraper.OK, risultati=15)
        self.stato.registra_esito("vinted", EsitoScraper.OK, risultati=42)

    def test_rimuove_le_piattaforme_non_piu_usate(self) -> None:
        rimosse = self.stato.pota_piattaforme({"subito", "vinted"})
        self.assertEqual(rimosse, ["ebay"])
        self.assertEqual(sorted(self.stato.salute_piattaforme()), ["subito", "vinted"])

    def test_conserva_quelle_in_uso(self) -> None:
        self.stato.pota_piattaforme({"ebay", "subito", "vinted"})
        self.assertEqual(len(self.stato.salute_piattaforme()), 3)

    def test_insieme_vuoto_non_cancella_nulla(self) -> None:
        """Se per un errore la configurazione risultasse senza piattaforme,
        azzerare lo stato sarebbe peggio che lasciarlo com'è."""
        self.assertEqual(self.stato.pota_piattaforme(set()), [])
        self.assertEqual(len(self.stato.salute_piattaforme()), 3)

    def test_una_ricerca_sospesa_conserva_la_sua_piattaforma(self) -> None:
        """L'insieme si costruisce da TUTTE le ricerche, anche in pausa:
        sospendere una ricerca non deve cancellare lo storico di salute."""
        from models import Configurazione, Impostazioni, Ricerca
        c = Configurazione(
            impostazioni=Impostazioni(),
            ricerche=[
                Ricerca(nome="a", parole_chiave="x", piattaforme=["subito"]),
                Ricerca(nome="b", parole_chiave="y", piattaforme=["vinted"], in_pausa=True),
            ],
        )
        in_uso = {p for r in c.ricerche for p in r.piattaforme}
        self.assertEqual(in_uso, {"subito", "vinted"})
        self.stato.pota_piattaforme(in_uso)
        self.assertIn("vinted", self.stato.salute_piattaforme())


# ---------------------------------------------------------------------------
# Controllo del trigger esterno
# ---------------------------------------------------------------------------

class TestTriggerEsterno(unittest.TestCase):
    """
    Spegnere il trigger è un'operazione asimmetrica: da Telegram si può
    fermare ma non riaccendere, perché senza run il bot non legge più i
    comandi. Il codice deve renderlo esplicito, non scoprirlo dopo.
    """

    def _risposta(self, codice, corpo=None):
        class R:
            status_code = codice
            text = ""
            def json(self_inner):
                return corpo or {}
        return R()

    def test_lettura_stato(self) -> None:
        from unittest import mock
        import trigger_esterno
        corpo = {"jobDetails": {"enabled": True, "title": "SCreeper",
                                "nextExecution": 1787000000, "lastStatus": 1}}
        with mock.patch.object(trigger_esterno.requests, "get",
                               return_value=self._risposta(200, corpo)):
            stato = trigger_esterno.stato_job("k", "1")
        self.assertTrue(stato["attivo"])
        self.assertEqual(stato["titolo"], "SCreeper")

    def test_spegnimento(self) -> None:
        from unittest import mock
        import trigger_esterno
        with mock.patch.object(trigger_esterno.requests, "patch",
                               return_value=self._risposta(200)) as finto:
            ok, messaggio = trigger_esterno.imposta_attivo("k", "1", False)
        self.assertTrue(ok)
        self.assertEqual(finto.call_args.kwargs["json"], {"job": {"enabled": False}})

    def test_accensione(self) -> None:
        from unittest import mock
        import trigger_esterno
        with mock.patch.object(trigger_esterno.requests, "patch",
                               return_value=self._risposta(200)) as finto:
            ok, _ = trigger_esterno.imposta_attivo("k", "1", True)
        self.assertTrue(ok)
        self.assertEqual(finto.call_args.kwargs["json"], {"job": {"enabled": True}})

    def test_chiave_rifiutata(self) -> None:
        from unittest import mock
        import trigger_esterno
        with mock.patch.object(trigger_esterno.requests, "patch",
                               return_value=self._risposta(401)):
            ok, messaggio = trigger_esterno.imposta_attivo("k", "1", False)
        self.assertFalse(ok)
        self.assertIn("401", messaggio)

    def test_senza_credenziali_non_chiama_nulla(self) -> None:
        from unittest import mock
        import trigger_esterno
        with mock.patch.object(trigger_esterno.requests, "patch") as finto:
            ok, _ = trigger_esterno.imposta_attivo("", "1", False)
        self.assertFalse(ok)
        finto.assert_not_called()

    def test_errore_di_rete_non_solleva(self) -> None:
        from unittest import mock
        import requests as req
        import trigger_esterno
        with mock.patch.object(trigger_esterno.requests, "patch",
                               side_effect=req.RequestException("giu")):
            ok, messaggio = trigger_esterno.imposta_attivo("k", "1", False)
        self.assertFalse(ok)
        self.assertIn("raggiungibile", messaggio)

    def test_spegni_pretende_conferma(self) -> None:
        """Senza la parola 'conferma' deve solo avvisare, non spegnere."""
        import os
        from unittest import mock
        import trigger_esterno

        os.environ["CRONJOB_API_KEY"] = "k"
        os.environ["CRONJOB_JOB_ID"] = "1"
        try:
            notifier = FintoNotifier(aggiornamenti=[aggiornamento(1, "/spegni")])
            with mock.patch.object(trigger_esterno, "imposta_attivo") as finto, \
                 mock.patch.object(trigger_esterno, "stato_job",
                                   return_value={"attivo": True, "titolo": "x",
                                                 "prossima_esecuzione": None,
                                                 "ultimo_esito": 1}):
                from bot.commands import ProcessoreComandi
                ProcessoreComandi(notifier, Stato.nuovo(),
                                  percorso_config=str(RADICE / "config.yaml"),
                                  tz_locale="Europe/Rome", dry_run=True).processa()
            finto.assert_not_called()
            testo = " ".join(notifier.messaggi)
            self.assertIn("/spegni conferma", testo)
            self.assertIn("non leggerà più i comandi", testo)
        finally:
            os.environ.pop("CRONJOB_API_KEY", None)
            os.environ.pop("CRONJOB_JOB_ID", None)

    def test_il_menu_elenca_spegni(self) -> None:
        from bot.commands import COMANDI_BOT
        nomi = [n for n, _ in COMANDI_BOT]
        self.assertIn("spegni", nomi)


class TestFiltriAzzerati(unittest.TestCase):
    """
    Aprendo la dashboard nessun filtro deve risultare attivo: si vede tutto,
    e i filtri li mette chi guarda. Preselezionare ogni voce dava
    l'impressione di filtri accesi da soli e nascondeva la differenza fra
    "non ho filtrato" e "ho scelto tutto".
    """

    def setUp(self) -> None:
        import app, streamlit as st
        self.app = app
        for chiave in list(st.session_state.keys()):
            if str(chiave).startswith("filtro_"):
                del st.session_state[chiave]

    def test_senza_memoria_nessuna_selezione(self) -> None:
        self.assertEqual(self.app._opzioni_valide("piattaforme", ["subito", "vinted"]), [])

    def test_la_selezione_viene_ricordata(self) -> None:
        import streamlit as st
        st.session_state["filtro_piattaforme"] = ["subito"]
        self.assertEqual(self.app._opzioni_valide("piattaforme", ["subito", "vinted"]), ["subito"])

    def test_i_valori_scomparsi_vengono_scartati(self) -> None:
        import streamlit as st
        st.session_state["filtro_piattaforme"] = ["subito", "ebay"]
        self.assertEqual(self.app._opzioni_valide("piattaforme", ["subito", "vinted"]), ["subito"])

    def test_selezione_vuota_non_filtra_nulla(self) -> None:
        import pandas as pd
        colonna = pd.Series(["subito", "vinted", "subito"])
        maschera = self.app._applica_filtro(colonna, [])
        self.assertTrue(maschera.all(), "vuoto deve voler dire 'tutto'")

    def test_selezione_piena_filtra(self) -> None:
        import pandas as pd
        colonna = pd.Series(["subito", "vinted", "subito"])
        maschera = self.app._applica_filtro(colonna, ["subito"])
        self.assertEqual(list(maschera), [True, False, True])


class TestPotaturaRicerche(unittest.TestCase):
    """
    Eliminare una ricerca deve avere un effetto visibile: senza potatura i
    suoi annunci resterebbero in archivio per trenta giorni, con una scheda
    dedicata a qualcosa che non si cerca più.
    """

    def setUp(self) -> None:
        self.stato = Stato.nuovo()
        self.stato.dati["storico"] = [
            {"ricerca": "ps5-pro", "titolo": "a", "data_avvistamento": adesso_utc().isoformat()},
            {"ricerca": "vecchia", "titolo": "b", "data_avvistamento": adesso_utc().isoformat()},
        ]
        self.stato.dati["ricerche"] = {"ps5-pro": {}, "vecchia": {}}
        self.stato.dati["statistiche"]["per_giorno"] = {
            "2026-08-29": {"totale": 10, "per_piattaforma": {"subito": 10},
                           "per_ricerca": {"ps5-pro": 3, "vecchia": 7}}
        }

    def test_rimuove_annunci_statistiche_e_stato(self) -> None:
        rimossi = self.stato.pota_ricerche({"ps5-pro"})
        self.assertEqual(rimossi["storico"], 1)
        self.assertEqual(sorted(self.stato.dati["ricerche"]), ["ps5-pro"])
        self.assertEqual(
            self.stato.dati["statistiche"]["per_giorno"]["2026-08-29"]["per_ricerca"],
            {"ps5-pro": 3},
        )

    def test_non_dimentica_gli_annunci_gia_visti(self) -> None:
        """Se un giorno ricrei la stessa ricerca, ricordare cosa era già stato
        visto evita di rinotificare mezzo marketplace."""
        self.stato.dati["visti"] = {"subito:1": adesso_utc().isoformat()}
        self.stato.pota_ricerche({"ps5-pro"})
        self.assertIn("subito:1", self.stato.dati["visti"])

    def test_insieme_vuoto_non_cancella_nulla(self) -> None:
        self.assertEqual(self.stato.pota_ricerche(set()), {})
        self.assertEqual(len(self.stato.dati["storico"]), 2)

    def test_annunci_senza_ricerca_restano(self) -> None:
        self.stato.dati["storico"].append({"titolo": "orfano"})
        self.stato.pota_ricerche({"ps5-pro"})
        self.assertTrue(any(v.get("titolo") == "orfano" for v in self.stato.dati["storico"]))


class TestSchedeDaConfigurazione(unittest.TestCase):
    """
    Le schede della pagina Annunci si costruiscono dalla configurazione e non
    dall'archivio: una ricerca appena creata non ha ancora trovato nulla e
    senza questo non comparirebbe, dando l'impressione di non essere stata
    salvata.
    """

    def test_configurazione_illeggibile_non_rompe_la_pagina(self) -> None:
        import app
        from unittest import mock
        with mock.patch.object(app, "carica_config", side_effect=RuntimeError("giu")):
            self.assertEqual(app._ricerche_configurate(), {})

    def test_riporta_lo_stato_di_esecuzione(self) -> None:
        import app
        from unittest import mock
        documento = """
impostazioni:
  timezone: Europe/Rome
ricerche:
  - nome: attiva
    attiva: true
    in_pausa: false
    piattaforme: [subito]
    parole_chiave: "x"
  - nome: sospesa
    attiva: true
    in_pausa: true
    piattaforme: [subito]
    parole_chiave: "y"
"""
        with mock.patch.object(app, "carica_config", return_value=(documento, "sha", "test")):
            risultato = app._ricerche_configurate()
        self.assertEqual(risultato, {"attiva": True, "sospesa": False})


class TestIntervalloEffettivo(unittest.TestCase):
    """
    `intervallo_minuti` è un freno applicato sopra il trigger esterno, non uno
    scheduler autonomo: l'intervallo reale è arrotondato per eccesso alla
    cadenza dei risvegli. È la cosa che sorprende chi imposta 20 minuti e ne
    ottiene 30.
    """

    def _simula(self, intervallo: int, cadenza: int, durata: int = 120) -> list[int]:
        from models import Ricerca
        stato = Stato.nuovo()
        ricerca = Ricerca(nome="r", parole_chiave="x", piattaforme=["subito"],
                          intervallo_minuti=intervallo)
        inizio = adesso_utc()
        eseguita = []
        for minuti in range(0, durata + 1, cadenza):
            adesso = inizio + timedelta(minutes=minuti)
            if stato.da_eseguire(ricerca, adesso=adesso):
                eseguita.append(minuti)
                stato.dati["ricerche"]["r"] = {
                    "ultima_esecuzione": adesso.isoformat(),
                    "ultimo_nuovo": None, "totale_notificati": 0,
                }
        return eseguita

    def test_intervallo_uguale_alla_cadenza(self) -> None:
        self.assertEqual(self._simula(15, 15), [0, 15, 30, 45, 60, 75, 90, 105, 120])

    def test_intervallo_sotto_la_cadenza_non_accelera(self) -> None:
        """Mettere 5 minuti con risvegli ogni 15 non fa girare più spesso."""
        self.assertEqual(self._simula(5, 15), self._simula(15, 15))

    def test_intervallo_intermedio_viene_arrotondato_per_eccesso(self) -> None:
        """20 minuti con risvegli ogni 15 diventano 30, non 20."""
        self.assertEqual(self._simula(20, 15), [0, 30, 60, 90, 120])

    def test_multipli_esatti(self) -> None:
        self.assertEqual(self._simula(30, 15), [0, 30, 60, 90, 120])
        self.assertEqual(self._simula(60, 15), [0, 60, 120])

    def test_la_prima_esecuzione_e_sempre_immediata(self) -> None:
        for intervallo in (5, 30, 240):
            self.assertEqual(self._simula(intervallo, 15)[0], 0)

    def test_tolleranza_sui_ritardi_del_trigger(self) -> None:
        """Un risveglio in ritardo di pochi secondi non deve far saltare il
        controllo fino al giro dopo."""
        from models import Ricerca
        stato = Stato.nuovo()
        ricerca = Ricerca(nome="r", parole_chiave="x", piattaforme=["subito"],
                          intervallo_minuti=15)
        inizio = adesso_utc()
        stato.dati["ricerche"]["r"] = {"ultima_esecuzione": inizio.isoformat(),
                                       "ultimo_nuovo": None, "totale_notificati": 0}
        quasi = inizio + timedelta(minutes=14, seconds=45)
        self.assertTrue(stato.da_eseguire(ricerca, adesso=quasi))


class TestPiattaformeMaiUsate(unittest.TestCase):
    """
    Interrogare lo stato di una piattaforma non configurata non deve crearne
    la voce: eBay compariva come "mai eseguito" nei messaggi Telegram pur non
    essendo in nessuna ricerca, perché il controllo della quarantena e quello
    dell'avviso la creavano come effetto collaterale.
    """

    def test_nessuna_delle_due_letture_crea_la_voce(self) -> None:
        stato = Stato.nuovo()
        stato.in_quarantena("ebay")
        stato.alert_da_inviare("ebay", 3)
        self.assertEqual(stato.salute_piattaforme(), {})

    def test_le_letture_restano_corrette(self) -> None:
        from models import EsitoScraper, Impostazioni
        stato = Stato.nuovo()
        stato.registra_esito("vinted", EsitoScraper.BLOCCATO,
                             errore="403", impostazioni=Impostazioni(run_pausa_dopo_blocco=3))
        self.assertTrue(stato.in_quarantena("vinted"))
        self.assertFalse(stato.in_quarantena("subito"))

    def test_avviso_dopo_la_soglia(self) -> None:
        from models import EsitoScraper
        stato = Stato.nuovo()
        for _ in range(3):
            stato.registra_esito("subito", EsitoScraper.VUOTO)
        self.assertTrue(stato.alert_da_inviare("subito", 3))
        self.assertFalse(stato.alert_da_inviare("vinted", 3))
