#!/usr/bin/env python3
"""
EU-lagtexter GUI -- Sok, valj och analysera lagtexter fran EU-kommissionen.

Tkinter-baserat GUI med:
- Sok och filtrera dokument (typ, ar, nyckelord)
- Sorterbar dokumentlista med fulla titlar
- Lagg till / ta bort valda dokument
- Artikelvisning i nummerordning med fullstandig text
- Kravextrahering: identifierar krav pa foretag/verksamheter
  - Hanterar sammansatta subjekt ("vasentliga och viktiga entiteter")
  - Normaliserar grammatiska former (bestamt/plural -> obestamd singular)
  - Hanterar passiv form (subjekt fran foregaende kontext)
  - Hanterar att-bisatser (objektbisatser med eget subjekt)
  - Punktlistor = separata krav
  - Filtrerar bort krav pa EU/medlemsstater/myndigheter
  - Artiklar om Inforlivande/Andring/Upphavande/Ikraftträdande = ej relevanta
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import threading
import re
import html as html_mod
import json
import urllib.request
import urllib.parse
from dataclasses import dataclass, field

# -- API-konstanter -----------------------------------------------------------

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
EURLEX_HTML_URL = (
    "https://eur-lex.europa.eu/legal-content/{lang}/TXT/HTML/?uri=CELEX:{celex}"
)

# -- Datamodell ---------------------------------------------------------------


@dataclass
class Article:
    number: str          # t.ex. "1", "2", "21"
    title: str           # t.ex. "Innehall", "Tillampningsomrade"
    paragraphs: list     # lista av Paragraph


@dataclass
class Paragraph:
    number: str          # t.ex. "1", "2", "a", "" (for brodtext)
    text: str
    children: list = field(default_factory=list)  # underpunkter


@dataclass
class Obligation:
    article: str
    paragraph: str
    text: str
    subjects: list       # lista av normaliserade subjekt
    original_subject: str  # den ursprungliga texten
    subject_category: str  # "entity", "eu", "member_state", "other"


@dataclass
class Document:
    celex: str
    title: str
    date: str
    doc_type: str = ""
    raw_html: str = ""
    articles: list = field(default_factory=list)
    obligations: list = field(default_factory=list)

    def type_label(self) -> str:
        code = self.celex[5:6] if len(self.celex) > 5 else ""
        return {
            "R": "Forordning",
            "L": "Direktiv",
            "D": "Beslut",
            "H": "Rekommendation",
        }.get(code, code)

    def __eq__(self, other):
        return isinstance(other, Document) and self.celex == other.celex

    def __hash__(self):
        return hash(self.celex)


# -- API-funktioner -----------------------------------------------------------


def sparql_query(query: str) -> list[dict]:
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        SPARQL_ENDPOINT,
        data=data,
        headers={"Accept": "application/sparql-results+json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    bindings = result.get("results", {}).get("bindings", [])
    return [{k: v["value"] for k, v in row.items()} for row in bindings]


def search_documents(
    doc_type: str = "", year: str = "", keyword: str = "", limit: int = 50
) -> list[Document]:
    filters = []
    if doc_type:
        type_map = {"REG": "REG", "DIR": "DIR", "DEC": "DEC", "RECO": "RECO"}
        rtype = type_map.get(doc_type.upper(), doc_type.upper())
        filters.append(
            f"?work cdm:work_has_resource-type "
            f"<http://publications.europa.eu/resource/authority/resource-type/{rtype}> ."
        )
    if year:
        filters.append(f'FILTER(STRSTARTS(STR(?date), "{year}"))')
    if keyword:
        safe_kw = keyword.replace('"', '\\"')
        filters.append(
            f'FILTER(CONTAINS(LCASE(STR(?title)), LCASE("{safe_kw}")))'
        )
    filter_block = "\n  ".join(filters)
    query = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?celex ?title ?date WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  ?work cdm:work_date_document ?date .
  ?expr cdm:expression_belongs_to_work ?work .
  ?expr cdm:expression_uses_language
        <http://publications.europa.eu/resource/authority/language/SWE> .
  ?expr cdm:expression_title ?title .
  {filter_block}
}}
ORDER BY DESC(?date)
LIMIT {limit}
"""
    rows = sparql_query(query)
    docs = []
    for r in rows:
        d = Document(
            celex=r.get("celex", ""),
            title=r.get("title", ""),
            date=r.get("date", "")[:10],
        )
        d.doc_type = d.type_label()
        docs.append(d)
    return docs


def fetch_html(celex: str, lang: str = "SV") -> str:
    url = EURLEX_HTML_URL.format(lang=lang, celex=celex)
    req = urllib.request.Request(url, headers={"User-Agent": "EU-Lagtexter/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def strip_html(html_text: str) -> str:
    text = re.sub(r"<style[^>]*>.*?</style>", "", html_text, flags=re.DOTALL | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# -- Artikelparser ------------------------------------------------------------


def parse_articles(raw_html: str) -> list[Article]:
    """Parsa HTML fran EUR-Lex och extrahera artiklar med stycken."""
    articles = []

    art_header_pattern = re.compile(
        r'<p[^>]*class="oj-ti-art"[^>]*>(Artikel)\s*\W*(\d+)</p>',
        re.IGNORECASE,
    )

    headers = list(art_header_pattern.finditer(raw_html))
    if not headers:
        art_header_pattern = re.compile(
            r'<p[^>]*class="oj-ti-art"[^>]*>(Article)\s*\W*(\d+)</p>',
            re.IGNORECASE,
        )
        headers = list(art_header_pattern.finditer(raw_html))

    for i, match in enumerate(headers):
        art_num = match.group(2)

        start = match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw_html)
        section_html = raw_html[start:end]

        title_match = re.search(
            r'<p[^>]*class="oj-sti-art"[^>]*>(.*?)</p>', section_html, re.DOTALL
        )
        art_title = strip_html(title_match.group(1)).strip() if title_match else ""

        paragraphs = _parse_paragraphs(section_html)

        articles.append(Article(number=art_num, title=art_title, paragraphs=paragraphs))

    # Sortera artiklar i nummerordning
    articles.sort(key=lambda a: int(a.number) if a.number.isdigit() else 0)

    return articles


def _parse_paragraphs(section_html: str) -> list[Paragraph]:
    """Parsa stycken inom en artikel."""
    paragraphs = []

    p_pattern = re.compile(r'<p[^>]*class="oj-normal"[^>]*>(.*?)</p>', re.DOTALL)

    current_para_num = ""
    current_text_parts = []

    for p_match in p_pattern.finditer(section_html):
        raw = p_match.group(1)
        text = strip_html(raw).strip()
        if not text:
            continue

        num_match = re.match(r"^(\d+)\.\s{2,}", text)
        if num_match:
            if current_text_parts:
                paragraphs.append(Paragraph(
                    number=current_para_num,
                    text="\n".join(current_text_parts),
                ))

            current_para_num = num_match.group(1)
            current_text_parts = [text[num_match.end():].strip()]
        else:
            sub_match = re.match(r"^([a-z]\))\s*", text)
            if sub_match:
                current_text_parts.append(text)
            else:
                if current_text_parts:
                    current_text_parts.append(text)
                else:
                    current_para_num = ""
                    current_text_parts = [text]

    if current_text_parts:
        paragraphs.append(Paragraph(
            number=current_para_num,
            text="\n".join(current_text_parts),
        ))

    return paragraphs


# -- Subjektsnormalisering ----------------------------------------------------

# Mappning fran alla grammatiska former till normaliserad form (obestamd singular)
SUBJECT_NORMALIZATION = {
    # Entitet
    "entiteten": "entitet",
    "entiteter": "entitet",
    "entiteterna": "entitet",
    "entiteters": "entitet",
    "entiteternas": "entitet",
    # Vasentlig entitet
    "vasentliga entiteten": "vasentlig entitet",
    "vasentliga entiteter": "vasentlig entitet",
    "vasentliga entiteterna": "vasentlig entitet",
    "vasentlig entitet": "vasentlig entitet",
    "de vasentliga entiteterna": "vasentlig entitet",
    "en vasentlig entitet": "vasentlig entitet",
    # Viktig entitet
    "viktiga entiteten": "viktig entitet",
    "viktiga entiteter": "viktig entitet",
    "viktiga entiteterna": "viktig entitet",
    "viktig entitet": "viktig entitet",
    "de viktiga entiteterna": "viktig entitet",
    "en viktig entitet": "viktig entitet",
    # Berord entitet
    "den berorda entiteten": "berord entitet",
    "berorda entiteter": "berord entitet",
    "berorda entiteterna": "berord entitet",
    "de berorda entiteterna": "berord entitet",
    # Operator
    "operatoren": "operator",
    "operatorer": "operator",
    "operatorerna": "operator",
    # Tjanste-/tillhandahallare
    "tjanstleverantoren": "tjanstleverantor",
    "tjanstleverantorer": "tjanstleverantor",
    "tjanstleverantorerna": "tjanstleverantor",
    "tillhandahallaren": "tillhandahallare",
    "tillhandahallare": "tillhandahallare",
    "tillhandahallarna": "tillhandahallare",
    # Leverantor
    "leverantoren": "leverantor",
    "leverantorer": "leverantor",
    "leverantorerna": "leverantor",
    # Verksamhetsutovare
    "verksamhetsutovaren": "verksamhetsutovare",
    "verksamhetsutovare": "verksamhetsutovare",
    "verksamhetsutovarna": "verksamhetsutovare",
    # Foretag
    "foretaget": "foretag",
    "foretagen": "foretag",
    "foretagets": "foretag",
    "foretagens": "foretag",
    # Organisation
    "organisationen": "organisation",
    "organisationer": "organisation",
    "organisationerna": "organisation",
    # Registreringsenhet
    "registreringsenheten": "registreringsenhet",
    "registreringsenheter": "registreringsenhet",
    "registreringsenheterna": "registreringsenhet",
    # Ledningsorgan
    "ledningsorganet": "ledningsorgan",
    "ledningsorganen": "ledningsorgan",
    "ledningsorganets": "ledningsorgan",
    "ledningsorganens": "ledningsorgan",
}

# Bygg aven en version med svenska tecken (a, o, etc.)
_SWEDISH_NORM = {}
_ACCENT_MAP = str.maketrans({
    'a': None, 'o': None,
})


def _build_swedish_normalization():
    """Bygg upp normalisering med riktiga svenska tecken."""
    # Manuell mappning med korrekta svenska tecken
    sw = {
        # Entitet
        "entiteten": "entitet",
        "entiteter": "entitet",
        "entiteterna": "entitet",
        "entiteters": "entitet",
        "entiteternas": "entitet",
        # Vasentlig entitet
        "\u00e4sentliga entiteten": "v\u00e4sentlig entitet",
        "v\u00e4sentliga entiteter": "v\u00e4sentlig entitet",
        "v\u00e4sentliga entiteterna": "v\u00e4sentlig entitet",
        "v\u00e4sentlig entitet": "v\u00e4sentlig entitet",
        "de v\u00e4sentliga entiteterna": "v\u00e4sentlig entitet",
        "en v\u00e4sentlig entitet": "v\u00e4sentlig entitet",
        # Viktig entitet
        "viktiga entiteten": "viktig entitet",
        "viktiga entiteter": "viktig entitet",
        "viktiga entiteterna": "viktig entitet",
        "viktig entitet": "viktig entitet",
        "de viktiga entiteterna": "viktig entitet",
        "en viktig entitet": "viktig entitet",
        # Berord entitet
        "den ber\u00f6rda entiteten": "ber\u00f6rd entitet",
        "ber\u00f6rda entiteter": "ber\u00f6rd entitet",
        "ber\u00f6rda entiteterna": "ber\u00f6rd entitet",
        "de ber\u00f6rda entiteterna": "ber\u00f6rd entitet",
        # Operat\u00f6r
        "operat\u00f6ren": "operat\u00f6r",
        "operat\u00f6rer": "operat\u00f6r",
        "operat\u00f6rerna": "operat\u00f6r",
        # Tj\u00e4nsteleverant\u00f6r
        "tj\u00e4nsteleverant\u00f6ren": "tj\u00e4nsteleverant\u00f6r",
        "tj\u00e4nsteleverant\u00f6rer": "tj\u00e4nsteleverant\u00f6r",
        "tj\u00e4nsteleverant\u00f6rerna": "tj\u00e4nsteleverant\u00f6r",
        "tillhandah\u00e5llaren": "tillhandah\u00e5llare",
        "tillhandah\u00e5llare": "tillhandah\u00e5llare",
        "tillhandah\u00e5llarna": "tillhandah\u00e5llare",
        # Leverant\u00f6r
        "leverant\u00f6ren": "leverant\u00f6r",
        "leverant\u00f6rer": "leverant\u00f6r",
        "leverant\u00f6rerna": "leverant\u00f6r",
        # Verksamhetsut\u00f6vare
        "verksamhetsut\u00f6varen": "verksamhetsut\u00f6vare",
        "verksamhetsut\u00f6vare": "verksamhetsut\u00f6vare",
        "verksamhetsut\u00f6varna": "verksamhetsut\u00f6vare",
        # F\u00f6retag
        "f\u00f6retaget": "f\u00f6retag",
        "f\u00f6retagen": "f\u00f6retag",
        "f\u00f6retagets": "f\u00f6retag",
        "f\u00f6retagens": "f\u00f6retag",
        # Organisation
        "organisationen": "organisation",
        "organisationer": "organisation",
        "organisationerna": "organisation",
        # Registreringsenhet
        "registreringsenheten": "registreringsenhet",
        "registreringsenheter": "registreringsenhet",
        "registreringsenheterna": "registreringsenhet",
        # Ledningsorgan
        "ledningsorganet": "ledningsorgan",
        "ledningsorganen": "ledningsorgan",
        "ledningsorganets": "ledningsorgan",
        "ledningsorganens": "ledningsorgan",
    }
    return sw


SUBJECT_NORM_SV = _build_swedish_normalization()


def normalize_subject(raw: str) -> str:
    """Normalisera ett subjekt till obestamd singular-form."""
    lowered = raw.strip().lower()
    # Forsok exakt matchning
    if lowered in SUBJECT_NORM_SV:
        return SUBJECT_NORM_SV[lowered]
    # Forsok delstrangs-matchning (langsta forst)
    for form, norm in sorted(SUBJECT_NORM_SV.items(), key=lambda x: -len(x[0])):
        if form in lowered:
            return norm
    return raw.strip()


def split_compound_subjects(subject_text: str) -> list[str]:
    """
    Dela sammansatta subjekt.
    "vasentliga och viktiga entiteter" -> ["vasentlig entitet", "viktig entitet"]
    "operatorer och tjanstleverantorer" -> ["operator", "tjanstleverantor"]
    """
    lowered = subject_text.lower().strip()

    # Monster: "X och Y SUBSTANTIV" (t.ex. "vasentliga och viktiga entiteter")
    compound_pattern = re.compile(
        r'([\w\u00e4\u00f6\u00e5]+)\s+och\s+([\w\u00e4\u00f6\u00e5]+)\s+([\w\u00e4\u00f6\u00e5]+)',
        re.IGNORECASE,
    )
    m = compound_pattern.search(lowered)
    if m:
        adj1, adj2, noun = m.group(1), m.group(2), m.group(3)
        # Kolla om det ar adjektiv + adjektiv + substantiv
        # Testa: ar "adj1 noun" och "adj2 noun" kanda?
        candidate1 = f"{adj1} {noun}"
        candidate2 = f"{adj2} {noun}"
        n1 = normalize_subject(candidate1)
        n2 = normalize_subject(candidate2)
        if n1 != candidate1 or n2 != candidate2:
            # Minst en kand -> returnera bada
            return [n1, n2]

    # Monster: "X och Y" (t.ex. "operatorer och tjanstleverantorer")
    and_pattern = re.compile(r'\s+och\s+', re.IGNORECASE)
    if and_pattern.search(lowered):
        parts = and_pattern.split(lowered)
        results = []
        for p in parts:
            p = p.strip()
            if p:
                results.append(normalize_subject(p))
        if len(results) > 1:
            return results

    # Inget sammansatt -> normalisera direkt
    return [normalize_subject(subject_text)]


# -- Kravextrahering med forbattrad subjektsidentifiering ---------------------

# Subjekt som ska filtreras bort (EU-institutioner, medlemsstater, myndigheter)
EU_SUBJECTS_PATTERNS = [
    r"\bkommissionen\b", r"\beuropeiska\s+kommissionen\b",
    r"\beuropaparlamentet\b", r"\br\u00e5det\b", r"\beuropeiska\s+r\u00e5det\b",
    r"\bministerr\u00e5det\b",
    r"\benisa\b", r"\beu[\-\u2013]cyclone\b", r"\bcsirt\b",
    r"\bcsirt[\-\u2013]enheter(?:na)?\b", r"\bcsirt[\-\u2013]n\u00e4tverket\b",
    r"\bsamarbetsgruppen\b",
    r"\beuropeiska\s+datatillsynsmannen\b",
    r"\beuropeiska\s+unionens\s+byr\u00e5\b",
    r"\bthe\s+commission\b", r"\beuropean\s+commission\b",
    r"\beuropean\s+parliament\b", r"\bthe\s+council\b", r"\bcouncil\b",
]

MEMBER_STATE_PATTERNS = [
    r"\bmedlemsstat(?:en|erna|er|ernas|s)?\b",
    r"\bvarje\s+medlemsstat\b",
    r"\bde(?:n)?\s+ber\u00f6rda\s+medlemsstat(?:en|erna)?\b",
    r"\bde\s+beh\u00f6riga\s+myndigheterna?\b",
    r"\bden\s+beh\u00f6riga\s+myndigheten\b",
    r"\bbeh\u00f6rig(?:a)?\s+myndighet(?:en|er|erna)?\b",
    r"\bden\s+gemensamma\s+kontaktpunkten\b",
    r"\bgemensamma\s+kontaktpunkter\b",
    r"\bnationella\s+myndigheter(?:na)?\b",
    r"\btillsynsmyndighet(?:en|erna)?\b",
    r"\bmember\s+states?\b",
    r"\bthe\s+competent\s+authorit(?:y|ies)\b",
    r"\bcompetent\s+authorit(?:y|ies)\b",
]

EU_SUBJECTS_RE = re.compile("|".join(EU_SUBJECTS_PATTERNS), re.IGNORECASE)
MEMBER_STATE_RE = re.compile("|".join(MEMBER_STATE_PATTERNS), re.IGNORECASE)

# Monster for att identifiera krav
OBLIGATION_TRIGGERS_SV = re.compile(
    r"\b(?:ska|skall|m\u00e5ste|b\u00f6r|"
    r"\u00e4r\s+skyldiga?\s+att|\u00e5ligger|"
    r"ansvarar?\s+f\u00f6r\s+att|kr\u00e4vs\s+att|fordras\s+att)\b",
    re.IGNORECASE,
)

OBLIGATION_TRIGGERS_EN = re.compile(
    r"\b(?:shall|must|is\s+required\s+to|are\s+required\s+to|"
    r"is\s+obliged\s+to|are\s+obliged\s+to)\b",
    re.IGNORECASE,
)

# Kanda subjektsmonster for entiteter/verksamheter
ENTITY_SUBJECTS_RE = re.compile(
    r"\b(?:"
    r"(?:v\u00e4sentliga\s+och\s+viktiga|viktiga\s+och\s+v\u00e4sentliga)\s+entiteter(?:na)?|"
    r"v\u00e4sentliga\s+entiteter(?:na)?|viktiga\s+entiteter(?:na)?|"
    r"(?:den\s+)?(?:ber\u00f6rda\s+)?entitet(?:en|er|erna)?|"
    r"operat\u00f6r(?:en|er|erna)?|"
    r"tj\u00e4nsteleverant\u00f6r(?:en|er|erna)?|"
    r"leverant\u00f6r(?:en|er|erna)?|"
    r"tillhandah\u00e5llar(?:en|e|na)?|"
    r"verksamhetsut\u00f6var(?:en|e|na)?|"
    r"f\u00f6retag(?:et|en|ets|ens)?|"
    r"organisation(?:en|er|erna)?|"
    r"registreringsenhet(?:en|er|erna)?|"
    r"ledningsorgan(?:et|en|ets|ens)?|"
    r"entities|essential\s+entities|important\s+entities|"
    r"operators|providers|undertakings"
    r")\b",
    re.IGNORECASE,
)

# Passiv-form-indikatorer
PASSIVE_PATTERN = re.compile(
    r"\b(?:antas|rapporteras|l\u00e4mnas|vidtas|baseras|utf\u00f6rs|genomf\u00f6rs|"
    r"inr\u00e4ttas|fastst\u00e4lls|godk\u00e4nns|meddelas|underr\u00e4ttas|"
    r"s\u00e4kerst\u00e4lls|uppfylls|tillhandah\u00e5lls|inges|"
    r"is\s+adopted|is\s+reported|is\s+submitted|shall\s+be)\b",
    re.IGNORECASE,
)

# Artikelrubriker som gor hela artikeln icke-relevant
NON_RELEVANT_TITLES = {
    "inforlivande", "inf\u00f6rlivande",
    "\u00e4ndring", "andring",
    "upph\u00e4vande", "upphavande",
    "ikrafttr\u00e4dande", "ikrafttradande",
    "\u00f6verg\u00e5ngsbest\u00e4mmelser",
    "transposition", "amendment", "repeal", "entry into force",
    "transitional provisions",
}


def _clean_text(t: str) -> str:
    """Normalisera whitespace."""
    return re.sub(r"\s+", " ", t).strip()


def _is_non_relevant_article(article: Article) -> bool:
    """Kolla om en artikel har en underrubrik som gor den icke-relevant."""
    if article.title:
        title_lower = article.title.strip().lower()
        for nr_title in NON_RELEVANT_TITLES:
            if nr_title in title_lower:
                return True
    return False


def _categorize_subject(subject_text: str) -> str:
    """Kategorisera ett subjekt som 'eu', 'member_state', 'entity' eller 'other'."""
    if EU_SUBJECTS_RE.search(subject_text):
        return "eu"
    if MEMBER_STATE_RE.search(subject_text):
        return "member_state"
    if ENTITY_SUBJECTS_RE.search(subject_text):
        return "entity"
    return "other"


def _extract_subject_from_clause(sentence: str) -> tuple[str, str]:
    """
    Extrahera subjekt fran att-bisatser.
    "Medlemsstaterna ska sakerst\u00e4lla att entiteter vidtar atgarder"
    -> ("entiteter", "entity")
    """
    # Monster: "att SUBJEKT ska/vidtar/..."
    att_clause = re.search(
        r'\batt\s+([\w\u00e4\u00f6\u00e5\s]+?)\s+'
        r'(?:ska|skall|vidtar|genomf\u00f6r|s\u00e4kerst\u00e4ller|antar|'
        r'uppfyller|tillhandah\u00e5ller|rapporterar|meddelar|inr\u00e4ttar|'
        r'utf\u00f6r|har|f\u00e5r|anm\u00e4ler|underrättar)\b',
        sentence,
        re.IGNORECASE,
    )
    if att_clause:
        candidate = att_clause.group(1).strip()
        cat = _categorize_subject(candidate)
        if cat == "entity":
            return candidate, cat
    return "", ""


def _extract_subject(sentence: str, prev_subject: str, prev_cat: str) -> tuple[str, str, str]:
    """
    Extrahera subjektet ur en mening.
    Returnerar (original_subject, category, raw_for_splitting).

    Hanterar:
    - Direkta subjekt ("Entiteter ska...")
    - Att-bisatser ("Medlemsstaterna ska sakerst\u00e4lla att entiteter...")
    - Passiv form (anvander foregaende subjekt)
    """

    # 1. Forst: kolla att-bisatser (objektbisatser)
    clause_subj, clause_cat = _extract_subject_from_clause(sentence)
    if clause_subj and clause_cat == "entity":
        return clause_subj, clause_cat, clause_subj

    # 2. Sok efter entitets-subjekt direkt i meningen
    ent_match = ENTITY_SUBJECTS_RE.search(sentence)
    if ent_match:
        raw = ent_match.group(0).strip()
        return raw, "entity", raw

    # 3. Kolla om passiv form -> anvand foregaende subjekt
    if PASSIVE_PATTERN.search(sentence) and prev_subject:
        return prev_subject, prev_cat, prev_subject

    # 4. Sok efter EU-institutioner
    eu_match = EU_SUBJECTS_RE.search(sentence)
    if eu_match:
        return eu_match.group(0).strip(), "eu", eu_match.group(0).strip()

    # 5. Sok efter medlemsstater/myndigheter
    ms_match = MEMBER_STATE_RE.search(sentence)
    if ms_match:
        # Men kolla AVEN bisatser har
        clause_subj2, clause_cat2 = _extract_subject_from_clause(sentence)
        if clause_subj2:
            return clause_subj2, clause_cat2, clause_subj2
        return ms_match.group(0).strip(), "member_state", ms_match.group(0).strip()

    # 6. Forsok hitta subjekt fore "ska"/"shall" etc.
    trigger = OBLIGATION_TRIGGERS_SV.search(sentence) or OBLIGATION_TRIGGERS_EN.search(sentence)
    if trigger:
        before = sentence[:trigger.start()].strip().rstrip(" ,;:")
        if before and len(before) < 100:
            cat = _categorize_subject(before)
            return before, cat, before

    # 7. Implicit subjekt fran foregaende kontext
    if prev_subject and prev_cat == "entity":
        return prev_subject, "entity", prev_subject

    return "(ok\u00e4nt)", "other", ""


def _is_list_intro(text: str) -> bool:
    """Kolla om texten slutar med kolon -> foljande lista."""
    return text.rstrip().endswith(":")


def extract_obligations_from_articles(articles: list[Article]) -> list[Obligation]:
    """Extrahera alla krav fran en lista artiklar, med subjektsidentifiering."""
    obligations = []
    prev_subject = ""
    prev_cat = "other"

    for art in articles:
        # Skippa icke-relevanta artiklar (Inforlivande, Andring, etc.)
        if _is_non_relevant_article(art):
            continue

        for para in art.paragraphs:
            full_text = para.text
            sentences = re.split(r"(?<=[.;])\s+", full_text)

            list_intro_subject = ""
            list_intro_cat = ""
            list_intro_raw = ""

            for sent in sentences:
                sent = _clean_text(sent)
                if not sent or len(sent) < 10:
                    continue

                is_obligation = bool(
                    OBLIGATION_TRIGGERS_SV.search(sent)
                    or OBLIGATION_TRIGGERS_EN.search(sent)
                )

                # Kolla om det ar en punkt i en lista (a), b), etc.)
                is_list_item = bool(re.match(r"^[a-z]\)", sent))

                if is_obligation:
                    raw_subj, cat, raw_for_split = _extract_subject(
                        sent, prev_subject, prev_cat
                    )

                    # Uppdatera kontext for passiv/implicit
                    if cat == "entity":
                        prev_subject = raw_subj
                        prev_cat = cat

                    # Om det ar en list-intro ("X ska ha foljande uppgifter:")
                    if _is_list_intro(sent):
                        list_intro_subject = raw_subj
                        list_intro_cat = cat
                        list_intro_raw = raw_for_split

                    # Dela sammansatta subjekt
                    subjects = split_compound_subjects(raw_subj)

                    obligations.append(Obligation(
                        article=art.number,
                        paragraph=para.number,
                        text=sent,
                        subjects=subjects,
                        original_subject=raw_subj,
                        subject_category=cat,
                    ))

                elif is_list_item and list_intro_subject:
                    # Punktlistor: varje punkt ar ett separat krav
                    subjects = split_compound_subjects(list_intro_subject)
                    obligations.append(Obligation(
                        article=art.number,
                        paragraph=para.number,
                        text=sent,
                        subjects=subjects,
                        original_subject=list_intro_subject,
                        subject_category=list_intro_cat,
                    ))

                elif is_list_item and prev_subject and prev_cat == "entity":
                    # Lista utan explicit intro men med foregaende subjekt
                    subjects = split_compound_subjects(prev_subject)
                    obligations.append(Obligation(
                        article=art.number,
                        paragraph=para.number,
                        text=sent,
                        subjects=subjects,
                        original_subject=prev_subject,
                        subject_category=prev_cat,
                    ))

            # Nollstall list-intro efter stycke
            list_intro_subject = ""
            list_intro_cat = ""
            list_intro_raw = ""

    return obligations


def is_obligation_relevant(obl: Obligation) -> bool:
    """Ar kravet relevant (dvs. riktat mot foretag/verksamheter)?"""
    return obl.subject_category not in ("eu", "member_state")


def is_obligation_text(text: str) -> bool:
    """Kolla om en text innehaller kravindikatorer."""
    return bool(
        OBLIGATION_TRIGGERS_SV.search(text)
        or OBLIGATION_TRIGGERS_EN.search(text)
    )


# -- GUI ----------------------------------------------------------------------


class EULagTexterGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EU-lagtexter \u2014 S\u00f6k & Analysera")
        self.root.geometry("1400x900")
        self.root.minsize(1000, 700)

        self.search_results: list[Document] = []
        self.selected_docs: list[Document] = []
        self.sort_column = "date"
        self.sort_reverse = True
        self._tooltip = None

        self._build_ui()
        self._status("Redo. Ange s\u00f6kkriterier och klicka S\u00f6k.")

    def _build_ui(self):
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        main_pane = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        left_frame = ttk.Frame(main_pane)
        right_frame = ttk.Frame(main_pane)
        main_pane.add(left_frame, weight=1)
        main_pane.add(right_frame, weight=1)

        self._build_search_panel(left_frame)
        self._build_results_panel(left_frame)
        self._build_selected_panel(right_frame)
        self._build_obligations_panel(right_frame)

        self.status_var = tk.StringVar()
        status_bar = ttk.Label(
            self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W
        )
        status_bar.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=2)

    def _build_search_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="S\u00f6k dokument", padding=8)
        frame.pack(fill=tk.X, padx=5, pady=(5, 2))

        row1 = ttk.Frame(frame)
        row1.pack(fill=tk.X, pady=2)

        ttk.Label(row1, text="Typ:").pack(side=tk.LEFT, padx=(0, 4))
        self.type_var = tk.StringVar(value="Alla")
        ttk.Combobox(
            row1, textvariable=self.type_var,
            values=["Alla", "REG \u2014 F\u00f6rordning", "DIR \u2014 Direktiv",
                    "DEC \u2014 Beslut", "RECO \u2014 Rekommendation"],
            state="readonly", width=22,
        ).pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(row1, text="\u00c5r:").pack(side=tk.LEFT, padx=(0, 4))
        self.year_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.year_var, width=8).pack(
            side=tk.LEFT, padx=(0, 15)
        )

        ttk.Label(row1, text="Max antal:").pack(side=tk.LEFT, padx=(0, 4))
        self.limit_var = tk.StringVar(value="50")
        ttk.Entry(row1, textvariable=self.limit_var, width=5).pack(side=tk.LEFT)

        row2 = ttk.Frame(frame)
        row2.pack(fill=tk.X, pady=2)

        ttk.Label(row2, text="Nyckelord i titel:").pack(side=tk.LEFT, padx=(0, 4))
        self.keyword_var = tk.StringVar()
        kw_entry = ttk.Entry(row2, textvariable=self.keyword_var, width=40)
        kw_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        kw_entry.bind("<Return>", lambda e: self._do_search())

        self.search_btn = ttk.Button(row2, text="S\u00f6k", command=self._do_search)
        self.search_btn.pack(side=tk.LEFT, padx=4)

    def _build_results_panel(self, parent):
        frame = ttk.LabelFrame(
            parent, text="S\u00f6kresultat \u2014 Tillg\u00e4ngliga dokument", padding=4
        )
        frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)

        columns = ("celex", "type", "date", "title")
        self.results_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="extended"
        )
        self.results_tree.heading("celex", text="CELEX-nr",
                                  command=lambda: self._sort_results("celex"))
        self.results_tree.heading("type", text="Typ",
                                  command=lambda: self._sort_results("doc_type"))
        self.results_tree.heading("date", text="Datum",
                                  command=lambda: self._sort_results("date"))
        self.results_tree.heading("title", text="Titel",
                                  command=lambda: self._sort_results("title"))
        self.results_tree.column("celex", width=130, minwidth=100)
        self.results_tree.column("type", width=90, minwidth=70)
        self.results_tree.column("date", width=90, minwidth=80)
        self.results_tree.column("title", width=500, minwidth=200)

        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL,
                             command=self.results_tree.yview)
        hsb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL,
                             command=self.results_tree.xview)
        self.results_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.results_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        self.results_tree.bind("<Motion>", self._on_tree_motion)
        self.results_tree.bind("<Leave>", self._hide_tooltip)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        self.add_btn = ttk.Button(
            btn_frame, text="L\u00e4gg till valda >>", command=self._add_selected
        )
        self.add_btn.pack(side=tk.LEFT, padx=4)
        self.add_all_btn = ttk.Button(
            btn_frame, text="L\u00e4gg till alla >>>", command=self._add_all
        )
        self.add_all_btn.pack(side=tk.LEFT, padx=4)

        self.result_count_var = tk.StringVar(value="0 dokument")
        ttk.Label(btn_frame, textvariable=self.result_count_var).pack(
            side=tk.RIGHT, padx=4
        )

    def _build_selected_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Valda dokument", padding=4)
        frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(5, 2))

        columns = ("celex", "type", "date", "title")
        self.selected_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="extended"
        )
        self.selected_tree.heading("celex", text="CELEX-nr")
        self.selected_tree.heading("type", text="Typ")
        self.selected_tree.heading("date", text="Datum")
        self.selected_tree.heading("title", text="Titel")
        self.selected_tree.column("celex", width=130, minwidth=100)
        self.selected_tree.column("type", width=90, minwidth=70)
        self.selected_tree.column("date", width=90, minwidth=80)
        self.selected_tree.column("title", width=400, minwidth=200)

        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL,
                             command=self.selected_tree.yview)
        hsb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL,
                             command=self.selected_tree.xview)
        self.selected_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.selected_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        self.selected_tree.bind("<Motion>",
                                lambda e: self._on_tree_motion(e, self.selected_tree))
        self.selected_tree.bind("<Leave>", self._hide_tooltip)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        ttk.Button(
            btn_frame, text="<< Ta bort valda", command=self._remove_selected
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            btn_frame, text="<<< Ta bort alla", command=self._remove_all
        ).pack(side=tk.LEFT, padx=4)

        ttk.Button(
            btn_frame, text="Visa artiklar & krav",
            command=self._open_article_viewer,
        ).pack(side=tk.RIGHT, padx=4)

        self.selected_count_var = tk.StringVar(value="0 dokument")
        ttk.Label(btn_frame, textvariable=self.selected_count_var).pack(
            side=tk.RIGHT, padx=8
        )

    def _build_obligations_panel(self, parent):
        frame = ttk.LabelFrame(
            parent, text="Krav p\u00e5 verksamheter (ej EU/myndigheter)", padding=4
        )
        frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)

        columns = ("doc", "article", "subject", "obligation")
        self.oblig_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )
        self.oblig_tree.heading("doc", text="Dokument")
        self.oblig_tree.heading("article", text="Art.")
        self.oblig_tree.heading("subject", text="Subjekt")
        self.oblig_tree.heading("obligation", text="Krav / Skyldighet")
        self.oblig_tree.column("doc", width=120, minwidth=90)
        self.oblig_tree.column("article", width=50, minwidth=40)
        self.oblig_tree.column("subject", width=140, minwidth=80)
        self.oblig_tree.column("obligation", width=400, minwidth=200)

        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL,
                             command=self.oblig_tree.yview)
        hsb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL,
                             command=self.oblig_tree.xview)
        self.oblig_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.oblig_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        self.oblig_tree.bind("<Double-1>", self._show_obligation_detail)
        self.oblig_tree.bind("<Motion>",
                             lambda e: self._on_tree_motion(e, self.oblig_tree, col=4))
        self.oblig_tree.bind("<Leave>", self._hide_tooltip)

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        ttk.Button(
            btn_frame, text="Extrahera krav",
            command=self._extract_all_obligations,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Button(
            btn_frame, text="Exportera till fil",
            command=self._export_obligations,
        ).pack(side=tk.LEFT, padx=4)

        self.oblig_count_var = tk.StringVar(value="0 krav")
        ttk.Label(btn_frame, textvariable=self.oblig_count_var).pack(
            side=tk.RIGHT, padx=4
        )

    # -- Tooltip ---------------------------------------------------------------

    def _show_tooltip(self, widget, text, x, y):
        self._hide_tooltip()
        if not text:
            return
        self._tooltip = tk.Toplevel(widget)
        self._tooltip.wm_overrideredirect(True)
        self._tooltip.wm_geometry(f"+{x + 15}+{y + 10}")
        tk.Label(
            self._tooltip, text=text, justify=tk.LEFT,
            background="#ffffe0", relief=tk.SOLID, borderwidth=1,
            font=("Segoe UI", 9), wraplength=600,
        ).pack()

    def _hide_tooltip(self, event=None):
        if self._tooltip:
            self._tooltip.destroy()
            self._tooltip = None

    def _on_tree_motion(self, event, tree=None, col=4):
        if tree is None:
            tree = self.results_tree
        item = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        col_str = f"#{col}"
        if item and column == col_str:
            values = tree.item(item, "values")
            if values and len(values) >= col:
                self._show_tooltip(tree, values[col - 1], event.x_root, event.y_root)
                return
        self._hide_tooltip()

    # -- Sok -------------------------------------------------------------------

    def _do_search(self):
        doc_type = self.type_var.get().split("\u2014")[0].strip()
        if doc_type == "Alla":
            doc_type = ""
        year = self.year_var.get().strip()
        keyword = self.keyword_var.get().strip()
        try:
            limit = int(self.limit_var.get())
        except ValueError:
            limit = 50

        if not doc_type and not year and not keyword:
            messagebox.showwarning(
                "S\u00f6k", "Ange minst ett s\u00f6kkriterium (typ, \u00e5r, eller nyckelord)."
            )
            return

        self.search_btn.configure(state="disabled")
        self._status("S\u00f6ker...")

        def _search():
            try:
                docs = search_documents(
                    doc_type=doc_type, year=year, keyword=keyword, limit=limit
                )
                self.root.after(0, lambda: self._show_results(docs))
            except Exception as e:
                self.root.after(
                    0, lambda: messagebox.showerror("S\u00f6kfel", str(e))
                )
            finally:
                self.root.after(
                    0, lambda: self.search_btn.configure(state="normal")
                )

        threading.Thread(target=_search, daemon=True).start()

    def _show_results(self, docs):
        self.search_results = docs
        self._refresh_results_tree()
        self._status(f"Hittade {len(docs)} dokument.")

    def _refresh_results_tree(self):
        self.results_tree.delete(*self.results_tree.get_children())
        for doc in self.search_results:
            self.results_tree.insert(
                "", tk.END, iid=doc.celex,
                values=(doc.celex, doc.doc_type, doc.date, doc.title),
            )
        self.result_count_var.set(f"{len(self.search_results)} dokument")

    def _sort_results(self, column):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        key_map = {
            "celex": lambda d: d.celex,
            "doc_type": lambda d: d.doc_type,
            "date": lambda d: d.date,
            "title": lambda d: d.title.lower(),
        }
        self.search_results.sort(
            key=key_map.get(column, lambda d: d.celex),
            reverse=self.sort_reverse,
        )
        self._refresh_results_tree()

    # -- Lagg till / ta bort ---------------------------------------------------

    def _add_selected(self):
        sel = self.results_tree.selection()
        if not sel:
            messagebox.showinfo("L\u00e4gg till", "V\u00e4lj dokument i s\u00f6kresultaten.")
            return
        for iid in sel:
            doc = next((d for d in self.search_results if d.celex == iid), None)
            if doc and doc not in self.selected_docs:
                self.selected_docs.append(doc)
        self._refresh_selected_tree()

    def _add_all(self):
        for doc in self.search_results:
            if doc not in self.selected_docs:
                self.selected_docs.append(doc)
        self._refresh_selected_tree()

    def _remove_selected(self):
        sel = self.selected_tree.selection()
        if not sel:
            return
        self.selected_docs = [d for d in self.selected_docs if d.celex not in sel]
        self._refresh_selected_tree()
        self._refresh_obligations_tree()

    def _remove_all(self):
        self.selected_docs.clear()
        self._refresh_selected_tree()
        self.oblig_tree.delete(*self.oblig_tree.get_children())
        self.oblig_count_var.set("0 krav")

    def _refresh_selected_tree(self):
        self.selected_tree.delete(*self.selected_tree.get_children())
        for doc in self.selected_docs:
            self.selected_tree.insert(
                "", tk.END, iid=doc.celex,
                values=(doc.celex, doc.doc_type, doc.date, doc.title),
            )
        self.selected_count_var.set(f"{len(self.selected_docs)} dokument")

    # -- Artikelvisning --------------------------------------------------------

    def _ensure_parsed(self, doc: Document):
        """Sakerst\u00e4ll att dokumentet har hamtats och parsats."""
        if not doc.raw_html:
            doc.raw_html = fetch_html(doc.celex, lang="SV")
            if len(doc.raw_html) < 500:
                doc.raw_html = fetch_html(doc.celex, lang="EN")
        if not doc.articles:
            doc.articles = parse_articles(doc.raw_html)
        if not doc.obligations:
            doc.obligations = extract_obligations_from_articles(doc.articles)

    def _open_article_viewer(self):
        sel = self.selected_tree.selection()
        if not sel:
            messagebox.showinfo("Visa", "V\u00e4lj ett dokument i listan.")
            return
        celex = sel[0]
        doc = next((d for d in self.selected_docs if d.celex == celex), None)
        if not doc:
            return

        self._status(f"H\u00e4mtar och analyserar {celex}...")

        def _work():
            try:
                self._ensure_parsed(doc)
                self.root.after(0, lambda: self._show_article_window(doc))
            except Exception as e:
                self.root.after(
                    0, lambda: messagebox.showerror("Fel", str(e))
                )
            finally:
                self.root.after(0, lambda: self._status("Redo."))

        threading.Thread(target=_work, daemon=True).start()

    def _show_article_window(self, doc: Document):
        win = tk.Toplevel(self.root)
        win.title(f"{doc.celex} \u2014 {doc.title}")
        win.geometry("1200x850")

        relevant_count = len([o for o in doc.obligations if is_obligation_relevant(o)])

        # Rubrik
        ttk.Label(
            win, text=doc.title, wraplength=1150, style="Title.TLabel"
        ).pack(padx=10, pady=(10, 2), anchor=tk.W)
        ttk.Label(
            win,
            text=f"CELEX: {doc.celex}  |  Datum: {doc.date}  |  Typ: {doc.doc_type}"
            f"  |  {len(doc.articles)} artiklar"
            f"  |  {relevant_count} krav p\u00e5 verksamheter",
        ).pack(padx=10, pady=(0, 5), anchor=tk.W)

        ttk.Separator(win, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=2)

        # Huvudpanel: artikeltext (vanster) + kravlista per subjekt (hoger)
        pane = ttk.PanedWindow(win, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Vanster: Artikeltext med fargkodning
        left = ttk.Frame(pane)
        pane.add(left, weight=2)

        ttk.Label(left, text="Artiklar (svart = krav p\u00e5 verksamheter, gr\u00e5 = \u00f6vrig text)",
                  font=("Segoe UI", 9, "italic")).pack(anchor=tk.W, padx=5)

        text_widget = tk.Text(
            left, wrap=tk.WORD, font=("Segoe UI", 10),
            padx=10, pady=10, spacing1=2, spacing3=2,
        )
        text_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL,
                                     command=text_widget.yview)
        text_widget.configure(yscrollcommand=text_scroll.set)
        text_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        text_widget.pack(fill=tk.BOTH, expand=True)

        # Definiera taggar
        text_widget.tag_configure("article_header", font=("Segoe UI", 12, "bold"),
                                  spacing1=12, spacing3=4)
        text_widget.tag_configure("article_title", font=("Segoe UI", 10, "italic"),
                                  spacing3=6)
        text_widget.tag_configure("para_num", font=("Segoe UI", 10, "bold"))
        text_widget.tag_configure("obligation_relevant", foreground="#000000",
                                  font=("Segoe UI", 10))
        text_widget.tag_configure("non_relevant", foreground="#999999",
                                  font=("Segoe UI", 10))
        text_widget.tag_configure("separator", foreground="#cccccc")

        # Bygg ett set av kravtexter som ar relevanta (for verksamheter)
        relevant_obligation_texts = {
            o.text for o in doc.obligations if is_obligation_relevant(o)
        }
        # Aven alla kravtexter (inklusive EU/medlemsstat) for att kunna
        # skilja "krav men pa EU" fran "ingen krav alls"
        all_obligation_texts = {o.text for o in doc.obligations}

        non_relevant_articles = {
            art.number for art in doc.articles if _is_non_relevant_article(art)
        }

        for art in doc.articles:
            is_non_rel_art = art.number in non_relevant_articles

            # Artikelrubrik
            header_tag = "non_relevant" if is_non_rel_art else "article_header"
            text_widget.insert(tk.END, f"\nArtikel {art.number}", header_tag)
            if art.title:
                title_tag = "non_relevant" if is_non_rel_art else "article_title"
                text_widget.insert(tk.END, f"\n{art.title}", title_tag)
            text_widget.insert(tk.END, "\n")

            for para in art.paragraphs:
                if para.number:
                    num_tag = "non_relevant" if is_non_rel_art else "para_num"
                    text_widget.insert(tk.END, f"\n{para.number}.   ", num_tag)

                # Dela texten i meningar for att kunna fargkoda
                sentences = re.split(r"(?<=[.;])\s+", para.text)
                for sent in sentences:
                    sent_clean = _clean_text(sent)
                    if not sent_clean:
                        continue

                    if is_non_rel_art:
                        # Hela artikeln ar icke-relevant
                        text_widget.insert(tk.END, sent_clean + " ", "non_relevant")
                    elif sent_clean in relevant_obligation_texts:
                        # Krav pa verksamheter -> svart (normal)
                        text_widget.insert(tk.END, sent_clean + " ",
                                           "obligation_relevant")
                    else:
                        # Ovrig text (inklusive krav pa EU/medlemsstater) -> gra
                        text_widget.insert(tk.END, sent_clean + " ", "non_relevant")

                text_widget.insert(tk.END, "\n")

            text_widget.insert(tk.END, "\n" + "\u2500" * 60 + "\n", "separator")

        text_widget.configure(state="disabled")

        # Hoger: Kravlista per subjekt
        right = ttk.Frame(pane)
        pane.add(right, weight=1)

        ttk.Label(right, text="Krav per subjekt (verksamheter)",
                  font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, padx=5, pady=(0, 5))

        # Treeview med subjektkategorier
        subj_tree = ttk.Treeview(right, show="tree headings", columns=("text",))
        subj_tree.heading("#0", text="Subjekt / Artikel")
        subj_tree.heading("text", text="Krav")
        subj_tree.column("#0", width=180, minwidth=120)
        subj_tree.column("text", width=350, minwidth=200)

        subj_scroll = ttk.Scrollbar(right, orient=tk.VERTICAL,
                                     command=subj_tree.yview)
        subj_tree.configure(yscrollcommand=subj_scroll.set)
        subj_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        subj_tree.pack(fill=tk.BOTH, expand=True)

        # Gruppera krav per normaliserat subjekt (uteslut EU/myndigheter)
        by_subject: dict[str, list[Obligation]] = {}
        for obl in doc.obligations:
            if not is_obligation_relevant(obl):
                continue
            # Varje krav kan ha flera subjekt
            for subj in obl.subjects:
                by_subject.setdefault(subj, []).append(obl)

        for subj, obls in sorted(by_subject.items()):
            parent_id = subj_tree.insert(
                "", tk.END, text=f"{subj} ({len(obls)} krav)", open=False,
                values=("",),
            )
            for obl in obls:
                obl_text = obl.text[:200] + "..." if len(obl.text) > 200 else obl.text
                subj_tree.insert(
                    parent_id, tk.END,
                    text=f"Art. {obl.article}.{obl.paragraph}",
                    values=(obl_text,),
                )

        # Dubbelklick oppnar fullstandig kravtext
        def _on_subj_double_click(event):
            item = subj_tree.selection()
            if not item:
                return
            vals = subj_tree.item(item[0], "values")
            if vals and vals[0]:
                detail_win = tk.Toplevel(win)
                detail_win.title("Kravtext")
                detail_win.geometry("600x300")
                st = scrolledtext.ScrolledText(
                    detail_win, wrap=tk.WORD, font=("Segoe UI", 10)
                )
                st.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
                st.insert(tk.END, vals[0])
                st.configure(state="disabled")

        subj_tree.bind("<Double-1>", _on_subj_double_click)

        # Tooltip for subjekt-tradvy
        def _on_subj_motion(event):
            item = subj_tree.identify_row(event.y)
            col = subj_tree.identify_column(event.x)
            if item and col == "#1":
                vals = subj_tree.item(item, "values")
                if vals and vals[0]:
                    self._show_tooltip(subj_tree, vals[0],
                                       event.x_root, event.y_root)
                    return
            self._hide_tooltip()

        subj_tree.bind("<Motion>", _on_subj_motion)
        subj_tree.bind("<Leave>", self._hide_tooltip)

    # -- Kravextrahering -------------------------------------------------------

    def _extract_all_obligations(self):
        if not self.selected_docs:
            messagebox.showinfo("Krav", "L\u00e4gg till dokument f\u00f6rst.")
            return

        self._status("H\u00e4mtar och analyserar dokument...")

        def _work():
            total = 0
            for doc in self.selected_docs:
                try:
                    self._ensure_parsed(doc)
                except Exception:
                    continue
                entity_obls = [
                    o for o in doc.obligations if is_obligation_relevant(o)
                ]
                total += len(entity_obls)
                self.root.after(
                    0, lambda d=doc, t=total: self._status(
                        f"Analyserat {d.celex}: {len(d.obligations)} krav totalt, "
                        f"{t} p\u00e5 verksamheter"
                    ),
                )
            self.root.after(0, self._refresh_obligations_tree)
            self.root.after(
                0, lambda: self._status(f"Klar \u2014 {total} krav p\u00e5 verksamheter.")
            )

        threading.Thread(target=_work, daemon=True).start()

    def _refresh_obligations_tree(self):
        self.oblig_tree.delete(*self.oblig_tree.get_children())
        count = 0
        for doc in self.selected_docs:
            for i, obl in enumerate(doc.obligations):
                if not is_obligation_relevant(obl):
                    continue
                # Visa varje normaliserat subjekt
                subj_display = ", ".join(obl.subjects)
                iid = f"{doc.celex}__{i}"
                self.oblig_tree.insert(
                    "", tk.END, iid=iid,
                    values=(doc.celex, f"{obl.article}.{obl.paragraph}",
                            subj_display, obl.text),
                )
                count += 1
        self.oblig_count_var.set(f"{count} krav")

    def _show_obligation_detail(self, event):
        item = self.oblig_tree.selection()
        if not item:
            return
        values = self.oblig_tree.item(item[0], "values")
        if not values:
            return
        win = tk.Toplevel(self.root)
        win.title(f"Krav \u2014 {values[0]} Art. {values[1]}")
        win.geometry("700x300")
        ttk.Label(win, text=f"Dokument: {values[0]}",
                  font=("Segoe UI", 10, "bold")).pack(padx=10, pady=(10, 2), anchor=tk.W)
        ttk.Label(win, text=f"Artikel: {values[1]}").pack(padx=10, pady=2, anchor=tk.W)
        ttk.Label(win, text=f"Subjekt: {values[2]}").pack(padx=10, pady=2, anchor=tk.W)
        ttk.Separator(win, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=5)
        st = scrolledtext.ScrolledText(
            win, wrap=tk.WORD, font=("Segoe UI", 10), padx=10, pady=10
        )
        st.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        st.insert(tk.END, values[3])
        st.configure(state="disabled")

    # -- Export ----------------------------------------------------------------

    def _export_obligations(self):
        has_any = any(
            o for d in self.selected_docs for o in d.obligations
            if is_obligation_relevant(o)
        )
        if not has_any:
            messagebox.showinfo("Export", "Inga krav att exportera.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Textfil", "*.txt"), ("CSV", "*.csv"),
                       ("Alla filer", "*.*")],
            title="Spara krav",
        )
        if not path:
            return

        with open(path, "w", encoding="utf-8") as f:
            if path.endswith(".csv"):
                f.write("Dokument\tArtikel\tSubjekt\tKategori\tKrav\n")
                for doc in self.selected_docs:
                    for obl in doc.obligations:
                        if not is_obligation_relevant(obl):
                            continue
                        text = obl.text.replace("\t", " ").replace("\n", " ")
                        subj_display = ", ".join(obl.subjects)
                        f.write(
                            f"{doc.celex}\t{obl.article}.{obl.paragraph}\t"
                            f"{subj_display}\t{obl.subject_category}\t{text}\n"
                        )
            else:
                for doc in self.selected_docs:
                    entity_obls = [
                        o for o in doc.obligations if is_obligation_relevant(o)
                    ]
                    if not entity_obls:
                        continue
                    f.write(f"{'=' * 80}\n")
                    f.write(f"Dokument: {doc.celex}\n")
                    f.write(f"Titel:    {doc.title}\n")
                    f.write(f"{'\u2500' * 80}\n\n")

                    # Gruppera per normaliserat subjekt
                    by_subj: dict[str, list] = {}
                    for obl in entity_obls:
                        for subj in obl.subjects:
                            by_subj.setdefault(subj, []).append(obl)

                    for subj, obls in sorted(by_subj.items()):
                        f.write(f"  SUBJEKT: {subj}\n")
                        f.write(f"  {'\u2500' * 40}\n")
                        for i, obl in enumerate(obls, 1):
                            f.write(f"  [{i}] Art. {obl.article}.{obl.paragraph}\n")
                            f.write(f"      {obl.text}\n\n")

        self._status(f"Exporterat till {path}")
        messagebox.showinfo("Export", f"Krav exporterade till:\n{path}")

    # -- Status ----------------------------------------------------------------

    def _status(self, text: str):
        self.status_var.set(text)


def main():
    root = tk.Tk()
    EULagTexterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
