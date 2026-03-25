#!/usr/bin/env python3
"""
EU-lagtexter GUI — Sök, välj och analysera lagtexter från EU-kommissionen.

Tkinter-baserat GUI med:
- Sök och filtrera dokument (typ, år, nyckelord)
- Sorterbar dokumentlista med fulla titlar
- Lägg till / ta bort valda dokument
- Artikelvisning med subjekt i grön textfärg
- Högerklicksmeny: godkänn/avvisa subjekt och krav
- Markera text → "Ange subjekt" / "Ange krav"
- Spara/ladda regleringar och krav till JSON
- Inlärning från användarfeedback
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog, simpledialog
import threading
import re
import html as html_mod
import json
import os
import uuid
import urllib.request
import urllib.parse
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional

# ── API-konstanter ───────────────────────────────────────────────────────────

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
EURLEX_HTML_URL = (
    "https://eur-lex.europa.eu/legal-content/{lang}/TXT/HTML/?uri=CELEX:{celex}"
)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ── Feedback-konstanter ──────────────────────────────────────────────────────

SUBJECT_REJECTION_REASONS = [
    "Inte ett subjekt",
    "EU-institution/myndighet",
    "Redan normaliserat annorlunda",
]

OBLIGATION_REJECTION_REASONS = [
    "Inte ett krav",
    "Krav på EU/myndighet",
    "Definitionsmässig text",
]

# ── Enums och dataklasser ────────────────────────────────────────────────────


class FeedbackStatus(Enum):
    AUTO = "auto"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class Article:
    number: str
    title: str
    paragraphs: list


@dataclass
class Paragraph:
    number: str
    text: str
    children: list = field(default_factory=list)


@dataclass
class SubjectAnnotation:
    id: str
    celex: str
    article: str
    paragraph: str
    text_span: str
    char_start: int
    char_end: int
    normalized: str
    source: str  # "auto" | "user"
    status: str = "auto"  # "auto" | "approved" | "rejected"
    rejection_reason: str = ""


@dataclass
class ObligationAnnotation:
    id: str
    celex: str
    article: str
    paragraph: str
    text_span: str
    char_start: int
    char_end: int
    subjects: list
    source: str  # "auto" | "user"
    status: str = "auto"
    rejection_reason: str = ""


@dataclass
class DocumentFeedback:
    celex: str
    subject_annotations: list = field(default_factory=list)
    obligation_annotations: list = field(default_factory=list)


@dataclass
class Obligation:
    article: str
    paragraph: str
    text: str
    subjects: list
    original_subject: str
    subject_category: str


@dataclass
class Document:
    celex: str
    title: str
    date: str
    doc_type: str = ""
    raw_html: str = ""
    articles: list = field(default_factory=list)
    obligations: list = field(default_factory=list)
    feedback: Optional[DocumentFeedback] = None

    def type_label(self) -> str:
        code = self.celex[5:6] if len(self.celex) > 5 else ""
        return {"R": "Förordning", "L": "Direktiv", "D": "Beslut",
                "H": "Rekommendation"}.get(code, code)

    def __eq__(self, other):
        return isinstance(other, Document) and self.celex == other.celex

    def __hash__(self):
        return hash(self.celex)


# ── Persistens ───────────────────────────────────────────────────────────────


class PersistenceManager:
    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def _doc_path(self, celex: str) -> str:
        safe = celex.replace("/", "_").replace(":", "_")
        return os.path.join(self.data_dir, f"{safe}.json")

    def _patterns_path(self) -> str:
        return os.path.join(self.data_dir, "feedback_patterns.json")

    def save_document(self, doc: Document, feedback: DocumentFeedback):
        data = {
            "schema_version": 1,
            "celex": doc.celex,
            "title": doc.title,
            "date": doc.date,
            "doc_type": doc.doc_type,
            "articles": [
                {
                    "number": a.number,
                    "title": a.title,
                    "paragraphs": [
                        {"number": p.number, "text": p.text}
                        for p in a.paragraphs
                    ],
                }
                for a in doc.articles
            ],
            "subject_annotations": [asdict(s) for s in feedback.subject_annotations],
            "obligation_annotations": [asdict(o) for o in feedback.obligation_annotations],
        }
        with open(self._doc_path(doc.celex), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_document(self, celex: str):
        path = self._doc_path(celex)
        if not os.path.exists(path):
            return None, None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        articles = []
        for a in data.get("articles", []):
            paras = [Paragraph(number=p["number"], text=p["text"])
                     for p in a.get("paragraphs", [])]
            articles.append(Article(number=a["number"], title=a.get("title", ""),
                                    paragraphs=paras))

        doc = Document(
            celex=data["celex"], title=data.get("title", ""),
            date=data.get("date", ""), doc_type=data.get("doc_type", ""),
        )
        doc.articles = articles

        fb = DocumentFeedback(celex=celex)
        for s in data.get("subject_annotations", []):
            fb.subject_annotations.append(SubjectAnnotation(**s))
        for o in data.get("obligation_annotations", []):
            fb.obligation_annotations.append(ObligationAnnotation(**o))

        return doc, fb

    def delete_document(self, celex: str):
        path = self._doc_path(celex)
        if os.path.exists(path):
            os.remove(path)

    def list_saved(self) -> list[str]:
        result = []
        if not os.path.isdir(self.data_dir):
            return result
        for fn in os.listdir(self.data_dir):
            if fn.endswith(".json") and fn != "feedback_patterns.json":
                result.append(fn[:-5])
        return result

    def save_patterns(self, patterns: dict):
        with open(self._patterns_path(), "w", encoding="utf-8") as f:
            json.dump(patterns, f, ensure_ascii=False, indent=2)

    def load_patterns(self) -> dict:
        path = self._patterns_path()
        if not os.path.exists(path):
            return {
                "rejected_subjects": {},
                "rejected_obligations": {"patterns": []},
                "approved_subjects": {},
            }
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


# ── Inlärning från feedback ──────────────────────────────────────────────────


class FeedbackLearner:
    AUTO_REJECT_THRESHOLD = 3

    def __init__(self, persistence: PersistenceManager):
        self.persistence = persistence
        self.patterns = persistence.load_patterns()

    def record_subject_rejection(self, normalized: str, reason: str):
        key = normalized.lower()
        rs = self.patterns.setdefault("rejected_subjects", {})
        if key in rs:
            rs[key]["count"] += 1
            rs[key]["reason"] = reason
        else:
            rs[key] = {"reason": reason, "count": 1}
        self._save()

    def record_subject_approval(self, normalized: str):
        key = normalized.lower()
        ap = self.patterns.setdefault("approved_subjects", {})
        ap[key] = ap.get(key, 0) + 1
        self._save()

    def record_obligation_rejection(self, text_span: str, reason: str):
        # Lagra de första 60 tecknen som mönster
        pattern = text_span[:60].strip()
        pats = self.patterns.setdefault("rejected_obligations", {}).setdefault("patterns", [])
        for p in pats:
            if p["pattern"] == pattern:
                p["count"] += 1
                p["reason"] = reason
                self._save()
                return
        pats.append({"pattern": pattern, "reason": reason, "count": 1})
        self._save()

    def record_obligation_approval(self, text_span: str):
        # Ingen specifik mönsterlagring för godkännande just nu
        pass

    def should_auto_reject_subject(self, normalized: str) -> tuple:
        key = normalized.lower()
        entry = self.patterns.get("rejected_subjects", {}).get(key)
        if entry and entry["count"] >= self.AUTO_REJECT_THRESHOLD:
            return True, entry["reason"]
        return False, ""

    def should_auto_reject_obligation(self, text: str) -> tuple:
        for pat in self.patterns.get("rejected_obligations", {}).get("patterns", []):
            if pat["pattern"] in text and pat["count"] >= self.AUTO_REJECT_THRESHOLD:
                return True, pat["reason"]
        return False, ""

    def _save(self):
        self.persistence.save_patterns(self.patterns)


# ── API-funktioner ───────────────────────────────────────────────────────────


def sparql_query(query: str) -> list[dict]:
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        SPARQL_ENDPOINT, data=data,
        headers={"Accept": "application/sparql-results+json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    bindings = result.get("results", {}).get("bindings", [])
    return [{k: v["value"] for k, v in row.items()} for row in bindings]


def search_documents(doc_type="", year="", keyword="", limit=50) -> list[Document]:
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
        filters.append(f'FILTER(CONTAINS(LCASE(STR(?title)), LCASE("{safe_kw}")))')
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
        d = Document(celex=r.get("celex", ""), title=r.get("title", ""),
                     date=r.get("date", "")[:10])
        d.doc_type = d.type_label()
        docs.append(d)
    return docs


def _find_xhtml_manifestation(celex: str, lang: str = "SWE") -> str:
    """Hitta XHTML-manifestation-URL via SPARQL i Cellar."""
    lang_uri = f"http://publications.europa.eu/resource/authority/language/{lang}"
    # Använd CONTAINS + exakt CELEX-matchning (= fungerar inte pga datatypsskillnad)
    query = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT ?manif ?mtype WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  ?expr cdm:expression_belongs_to_work ?work .
  ?expr cdm:expression_uses_language <{lang_uri}> .
  ?manif cdm:manifestation_manifests_expression ?expr .
  OPTIONAL {{ ?manif cdm:manifestation_type ?mtype }}
  FILTER(CONTAINS(?celex, "{celex}") && !CONTAINS(?celex, "R("))
}}
LIMIT 10
"""
    rows = sparql_query(query)
    # Föredra xhtml > fmx4
    for preferred in ("xhtml", "fmx4"):
        for row in rows:
            if row.get("mtype", "") == preferred:
                return row.get("manif", "")
    # Om inget format hittades, returnera första bästa
    if rows:
        return rows[0].get("manif", "")
    return ""


def fetch_html(celex: str, lang: str = "SV") -> str:
    """Hämta XHTML/HTML-innehåll via Cellar (undviker WAF-blockering)."""
    lang_map = {"SV": "SWE", "EN": "ENG", "DE": "DEU", "FR": "FRA"}
    cellar_lang = lang_map.get(lang, lang)

    manif_url = _find_xhtml_manifestation(celex, cellar_lang)
    if manif_url:
        req = urllib.request.Request(
            manif_url,
            headers={"Accept": "application/xhtml+xml, text/html, text/xml",
                     "User-Agent": "EU-Lagtexter/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8", errors="replace")

    # Sista utväg: direkt EUR-Lex (kan blockeras av WAF)
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


# ── Artikelparser ────────────────────────────────────────────────────────────


def parse_articles(raw_html: str) -> list[Article]:
    articles = []
    # \xa0 = non-breaking space, \s = whitespace — EUR-Lex använder båda
    art_header_pattern = re.compile(
        r'<p[^>]*class="oj-ti-art"[^>]*>\s*(?:Artikel|Article)\s*[\s\xa0\W]*(\d+)\s*</p>',
        re.IGNORECASE)
    headers = list(art_header_pattern.finditer(raw_html))
    if not headers:
        # Fallback: enklare mönster utan class-attribut
        art_header_pattern = re.compile(
            r'<p[^>]*>\s*(?:Artikel|Article)\s*[\s\xa0\W]*(\d+)\s*</p>',
            re.IGNORECASE)
        headers = list(art_header_pattern.finditer(raw_html))

    for i, match in enumerate(headers):
        art_num = match.group(1)
        start = match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw_html)
        section_html = raw_html[start:end]
        title_match = re.search(
            r'<p[^>]*class="oj-sti-art"[^>]*>(.*?)</p>', section_html, re.DOTALL)
        art_title = strip_html(title_match.group(1)).strip() if title_match else ""
        paragraphs = _parse_paragraphs(section_html)
        articles.append(Article(number=art_num, title=art_title, paragraphs=paragraphs))

    articles.sort(key=lambda a: int(a.number) if a.number.isdigit() else 0)
    return articles


def _parse_paragraphs(section_html: str) -> list[Paragraph]:
    paragraphs = []
    p_pattern = re.compile(r'<p[^>]*class="oj-normal"[^>]*>(.*?)</p>', re.DOTALL)
    current_para_num = ""
    current_text_parts = []

    for p_match in p_pattern.finditer(section_html):
        raw = p_match.group(1)
        text = strip_html(raw).strip()
        if not text:
            continue
        num_match = re.match(r"^(\d+)\.\s[\s\xa0]+", text)
        if num_match:
            if current_text_parts:
                paragraphs.append(Paragraph(number=current_para_num,
                                            text="\n".join(current_text_parts)))
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
        paragraphs.append(Paragraph(number=current_para_num,
                                    text="\n".join(current_text_parts)))
    return paragraphs


# ── Subjektsnormalisering ────────────────────────────────────────────────────

SUBJECT_NORM_SV = {
    "entiteten": "entitet", "entiteter": "entitet", "entiteterna": "entitet",
    "entiteters": "entitet", "entiteternas": "entitet",
    "väsentliga entiteten": "väsentlig entitet",
    "väsentliga entiteter": "väsentlig entitet",
    "väsentliga entiteterna": "väsentlig entitet",
    "väsentlig entitet": "väsentlig entitet",
    "de väsentliga entiteterna": "väsentlig entitet",
    "en väsentlig entitet": "väsentlig entitet",
    "viktiga entiteten": "viktig entitet",
    "viktiga entiteter": "viktig entitet",
    "viktiga entiteterna": "viktig entitet",
    "viktig entitet": "viktig entitet",
    "de viktiga entiteterna": "viktig entitet",
    "en viktig entitet": "viktig entitet",
    "den berörda entiteten": "berörd entitet",
    "berörda entiteter": "berörd entitet",
    "berörda entiteterna": "berörd entitet",
    "de berörda entiteterna": "berörd entitet",
    "operatören": "operatör", "operatörer": "operatör",
    "operatörerna": "operatör",
    "tjänsteleverantören": "tjänsteleverantör",
    "tjänsteleverantörer": "tjänsteleverantör",
    "tjänsteleverantörerna": "tjänsteleverantör",
    "tillhandahållaren": "tillhandahållare",
    "tillhandahållare": "tillhandahållare",
    "tillhandahållarna": "tillhandahållare",
    "leverantören": "leverantör", "leverantörer": "leverantör",
    "leverantörerna": "leverantör",
    "verksamhetsutövaren": "verksamhetsutövare",
    "verksamhetsutövare": "verksamhetsutövare",
    "verksamhetsutövarna": "verksamhetsutövare",
    "företaget": "företag", "företagen": "företag",
    "företagets": "företag", "företagens": "företag",
    "organisationen": "organisation", "organisationer": "organisation",
    "organisationerna": "organisation",
    "registreringsenheten": "registreringsenhet",
    "registreringsenheter": "registreringsenhet",
    "registreringsenheterna": "registreringsenhet",
    "ledningsorganet": "ledningsorgan", "ledningsorganen": "ledningsorgan",
    "ledningsorganets": "ledningsorgan", "ledningsorganens": "ledningsorgan",
}


def normalize_subject(raw: str) -> str:
    lowered = raw.strip().lower()
    if lowered in SUBJECT_NORM_SV:
        return SUBJECT_NORM_SV[lowered]
    for form, norm in sorted(SUBJECT_NORM_SV.items(), key=lambda x: -len(x[0])):
        if form in lowered:
            return norm
    return raw.strip()


def split_compound_subjects(subject_text: str) -> list[str]:
    lowered = subject_text.lower().strip()
    compound_pattern = re.compile(
        r'([\w\u00e4\u00f6\u00e5]+)\s+och\s+([\w\u00e4\u00f6\u00e5]+)\s+([\w\u00e4\u00f6\u00e5]+)',
        re.IGNORECASE)
    m = compound_pattern.search(lowered)
    if m:
        adj1, adj2, noun = m.group(1), m.group(2), m.group(3)
        candidate1 = f"{adj1} {noun}"
        candidate2 = f"{adj2} {noun}"
        n1 = normalize_subject(candidate1)
        n2 = normalize_subject(candidate2)
        if n1 != candidate1 or n2 != candidate2:
            return [n1, n2]

    and_pattern = re.compile(r'\s+och\s+', re.IGNORECASE)
    if and_pattern.search(lowered):
        parts = and_pattern.split(lowered)
        results = [normalize_subject(p.strip()) for p in parts if p.strip()]
        if len(results) > 1:
            return results

    return [normalize_subject(subject_text)]


# ── Kravextrahering ──────────────────────────────────────────────────────────

EU_SUBJECTS_RE = re.compile(
    r"\b(?:kommissionen|europeiska\s+kommissionen|europaparlamentet|"
    r"rådet|europeiska\s+rådet|ministerrådet|enisa|eu[\-–]cyclone|"
    r"csirt|csirt[\-–]enheter(?:na)?|csirt[\-–]nätverket|"
    r"samarbetsgruppen|europeiska\s+datatillsynsmannen|"
    r"europeiska\s+unionens\s+byrå|the\s+commission|european\s+commission|"
    r"european\s+parliament|the\s+council|council)\b", re.IGNORECASE)

MEMBER_STATE_RE = re.compile(
    r"\b(?:medlemsstat(?:en|erna|er|ernas|s)?|varje\s+medlemsstat|"
    r"de(?:n)?\s+berörda\s+medlemsstat(?:en|erna)?|"
    r"de\s+behöriga\s+myndigheterna?|den\s+behöriga\s+myndigheten|"
    r"behörig(?:a)?\s+myndighet(?:en|er|erna)?|"
    r"den\s+gemensamma\s+kontaktpunkten|gemensamma\s+kontaktpunkter|"
    r"nationella\s+myndigheter(?:na)?|tillsynsmyndighet(?:en|erna)?|"
    r"member\s+states?|the\s+competent\s+authorit(?:y|ies)|"
    r"competent\s+authorit(?:y|ies))\b", re.IGNORECASE)

OBLIGATION_TRIGGERS_SV = re.compile(
    r"\b(?:ska|skall|måste|bör|är\s+skyldiga?\s+att|åligger|"
    r"ansvarar?\s+för\s+att|krävs\s+att|fordras\s+att)\b", re.IGNORECASE)

OBLIGATION_TRIGGERS_EN = re.compile(
    r"\b(?:shall|must|is\s+required\s+to|are\s+required\s+to|"
    r"is\s+obliged\s+to|are\s+obliged\s+to)\b", re.IGNORECASE)

ENTITY_SUBJECTS_RE = re.compile(
    r"\b(?:(?:väsentliga\s+och\s+viktiga|viktiga\s+och\s+väsentliga)\s+entiteter(?:na)?|"
    r"väsentliga\s+entiteter(?:na)?|viktiga\s+entiteter(?:na)?|"
    r"(?:den\s+)?(?:berörda\s+)?entitet(?:en|er|erna)?|"
    r"operatör(?:en|er|erna)?|tjänsteleverantör(?:en|er|erna)?|"
    r"leverantör(?:en|er|erna)?|tillhandahållar(?:en|e|na)?|"
    r"verksamhetsutövar(?:en|e|na)?|företag(?:et|en|ets|ens)?|"
    r"organisation(?:en|er|erna)?|registreringsenhet(?:en|er|erna)?|"
    r"ledningsorgan(?:et|en|ets|ens)?|"
    r"entities|essential\s+entities|important\s+entities|"
    r"operators|providers|undertakings)\b", re.IGNORECASE)

PASSIVE_PATTERN = re.compile(
    r"\b(?:antas|rapporteras|lämnas|vidtas|baseras|utförs|genomförs|"
    r"inrättas|fastställs|godkänns|meddelas|underrättas|"
    r"säkerställs|uppfylls|tillhandahålls|inges|"
    r"is\s+adopted|is\s+reported|is\s+submitted|shall\s+be)\b", re.IGNORECASE)

NON_RELEVANT_TITLES = {
    "införlivande", "ändring", "upphävande", "ikraftträdande",
    "övergångsbestämmelser", "transposition", "amendment", "repeal",
    "entry into force", "transitional provisions",
}


def _clean_text(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def _is_non_relevant_article(article: Article) -> bool:
    if article.title:
        title_lower = article.title.strip().lower()
        for nr_title in NON_RELEVANT_TITLES:
            if nr_title in title_lower:
                return True
    return False


def _categorize_subject(subject_text: str) -> str:
    if EU_SUBJECTS_RE.search(subject_text):
        return "eu"
    if MEMBER_STATE_RE.search(subject_text):
        return "member_state"
    if ENTITY_SUBJECTS_RE.search(subject_text):
        return "entity"
    return "other"


def _extract_subject_from_clause(sentence: str):
    att_clause = re.search(
        r'\batt\s+([\w\u00e4\u00f6\u00e5\s]+?)\s+'
        r'(?:ska|skall|vidtar|genomför|säkerställer|antar|'
        r'uppfyller|tillhandahåller|rapporterar|meddelar|inrättar|'
        r'utför|har|får|anmäler|underrättar)\b',
        sentence, re.IGNORECASE)
    if att_clause:
        candidate = att_clause.group(1).strip()
        cat = _categorize_subject(candidate)
        if cat == "entity":
            return candidate, cat
    return "", ""


def _extract_subject(sentence, prev_subject, prev_cat):
    clause_subj, clause_cat = _extract_subject_from_clause(sentence)
    if clause_subj and clause_cat == "entity":
        return clause_subj, clause_cat, clause_subj

    ent_match = ENTITY_SUBJECTS_RE.search(sentence)
    if ent_match:
        raw = ent_match.group(0).strip()
        return raw, "entity", raw

    if PASSIVE_PATTERN.search(sentence) and prev_subject:
        return prev_subject, prev_cat, prev_subject

    eu_match = EU_SUBJECTS_RE.search(sentence)
    if eu_match:
        return eu_match.group(0).strip(), "eu", eu_match.group(0).strip()

    ms_match = MEMBER_STATE_RE.search(sentence)
    if ms_match:
        clause_subj2, clause_cat2 = _extract_subject_from_clause(sentence)
        if clause_subj2:
            return clause_subj2, clause_cat2, clause_subj2
        return ms_match.group(0).strip(), "member_state", ms_match.group(0).strip()

    trigger = OBLIGATION_TRIGGERS_SV.search(sentence) or OBLIGATION_TRIGGERS_EN.search(sentence)
    if trigger:
        before = sentence[:trigger.start()].strip().rstrip(" ,;:")
        if before and len(before) < 100:
            cat = _categorize_subject(before)
            return before, cat, before

    if prev_subject and prev_cat == "entity":
        return prev_subject, "entity", prev_subject

    return "(okänt)", "other", ""


def _is_list_intro(text: str) -> bool:
    return text.rstrip().endswith(":")


def is_obligation_text(text: str) -> bool:
    return bool(OBLIGATION_TRIGGERS_SV.search(text) or OBLIGATION_TRIGGERS_EN.search(text))


def is_obligation_relevant(obl: Obligation) -> bool:
    return obl.subject_category not in ("eu", "member_state")


def extract_obligations_from_articles(articles: list) -> list:
    obligations = []
    prev_subject = ""
    prev_cat = "other"

    for art in articles:
        if _is_non_relevant_article(art):
            continue
        for para in art.paragraphs:
            full_text = para.text
            sentences = re.split(r"(?<=[.;])\s+", full_text)
            list_intro_subject = ""
            list_intro_cat = ""

            for sent in sentences:
                sent = _clean_text(sent)
                if not sent or len(sent) < 10:
                    continue

                is_obl = is_obligation_text(sent)
                is_list_item = bool(re.match(r"^[a-z]\)", sent))

                if is_obl:
                    raw_subj, cat, raw_for_split = _extract_subject(
                        sent, prev_subject, prev_cat)
                    if cat == "entity":
                        prev_subject = raw_subj
                        prev_cat = cat
                    if _is_list_intro(sent):
                        list_intro_subject = raw_subj
                        list_intro_cat = cat
                    subjects = split_compound_subjects(raw_subj)
                    obligations.append(Obligation(
                        article=art.number, paragraph=para.number,
                        text=sent, subjects=subjects,
                        original_subject=raw_subj, subject_category=cat))

                elif is_list_item and list_intro_subject:
                    subjects = split_compound_subjects(list_intro_subject)
                    obligations.append(Obligation(
                        article=art.number, paragraph=para.number,
                        text=sent, subjects=subjects,
                        original_subject=list_intro_subject,
                        subject_category=list_intro_cat))

                elif is_list_item and prev_subject and prev_cat == "entity":
                    subjects = split_compound_subjects(prev_subject)
                    obligations.append(Obligation(
                        article=art.number, paragraph=para.number,
                        text=sent, subjects=subjects,
                        original_subject=prev_subject,
                        subject_category=prev_cat))

            list_intro_subject = ""
            list_intro_cat = ""

    return obligations


# ── Bygg annotationer från automatisk extraktion ─────────────────────────────


def build_annotations(doc: Document, obligations: list, learner: FeedbackLearner) -> DocumentFeedback:
    """Konvertera automatiskt extraherade krav till annotationer."""
    fb = DocumentFeedback(celex=doc.celex)
    seen_subj_spans = set()

    for obl in obligations:
        if not is_obligation_relevant(obl):
            continue

        # Hitta stycke
        para_text = ""
        for art in doc.articles:
            if art.number != obl.article:
                continue
            for para in art.paragraphs:
                if para.number != obl.paragraph:
                    continue
                para_text = para.text
                break

        # Krav-annotation
        obl_start = para_text.find(obl.text) if para_text else 0
        if obl_start < 0:
            obl_start = 0
        obl_end = obl_start + len(obl.text)

        obl_ann = ObligationAnnotation(
            id=uuid.uuid4().hex[:12], celex=doc.celex,
            article=obl.article, paragraph=obl.paragraph,
            text_span=obl.text, char_start=obl_start, char_end=obl_end,
            subjects=obl.subjects, source="auto")

        should_rej, reason = learner.should_auto_reject_obligation(obl.text)
        if should_rej:
            obl_ann.status = "rejected"
            obl_ann.rejection_reason = reason

        fb.obligation_annotations.append(obl_ann)

        # Subjekt-annotation
        if obl.original_subject and para_text:
            subj_start = para_text.lower().find(obl.original_subject.lower())
            if subj_start >= 0:
                key = (obl.article, obl.paragraph, subj_start)
                if key not in seen_subj_spans:
                    seen_subj_spans.add(key)
                    subj_ann = SubjectAnnotation(
                        id=uuid.uuid4().hex[:12], celex=doc.celex,
                        article=obl.article, paragraph=obl.paragraph,
                        text_span=obl.original_subject,
                        char_start=subj_start,
                        char_end=subj_start + len(obl.original_subject),
                        normalized=", ".join(obl.subjects), source="auto")

                    should_rej_s, reason_s = learner.should_auto_reject_subject(
                        ", ".join(obl.subjects))
                    if should_rej_s:
                        subj_ann.status = "rejected"
                        subj_ann.rejection_reason = reason_s

                    fb.subject_annotations.append(subj_ann)

    return fb


# ── GUI ──────────────────────────────────────────────────────────────────────


class EULagTexterGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EU-lagtexter — Sök & Analysera")
        self.root.geometry("1400x900")
        self.root.minsize(1000, 700)

        self.persistence = PersistenceManager()
        self.learner = FeedbackLearner(self.persistence)

        self.search_results: list[Document] = []
        self.selected_docs: list[Document] = []
        self.sort_column = "date"
        self.sort_reverse = True
        self._tooltip = None

        self._build_ui()
        self._load_saved_docs()
        self._status("Redo. Ange sökkriterier och klicka Sök.")

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
            self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=2)

    def _build_search_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Sök dokument", padding=8)
        frame.pack(fill=tk.X, padx=5, pady=(5, 2))

        row1 = ttk.Frame(frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="Typ:").pack(side=tk.LEFT, padx=(0, 4))
        self.type_var = tk.StringVar(value="Alla")
        ttk.Combobox(
            row1, textvariable=self.type_var,
            values=["Alla", "REG — Förordning", "DIR — Direktiv",
                    "DEC — Beslut", "RECO — Rekommendation"],
            state="readonly", width=22).pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(row1, text="År:").pack(side=tk.LEFT, padx=(0, 4))
        self.year_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.year_var, width=8).pack(
            side=tk.LEFT, padx=(0, 15))

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

        self.search_btn = ttk.Button(row2, text="Sök", command=self._do_search)
        self.search_btn.pack(side=tk.LEFT, padx=4)

    def _build_results_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Sökresultat — Tillgängliga dokument",
                               padding=4)
        frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)

        columns = ("celex", "type", "date", "title")
        self.results_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="extended")
        for col, text, w in [("celex", "CELEX-nr", 130), ("type", "Typ", 90),
                              ("date", "Datum", 90), ("title", "Titel", 500)]:
            self.results_tree.heading(
                col, text=text,
                command=lambda c=col: self._sort_results(
                    {"celex": "celex", "type": "doc_type", "date": "date",
                     "title": "title"}[c]))
            self.results_tree.column(col, width=w, minwidth=max(70, w - 60))

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
        self.add_btn = ttk.Button(btn_frame, text="Lägg till valda >>",
                                  command=self._add_selected)
        self.add_btn.pack(side=tk.LEFT, padx=4)
        self.add_all_btn = ttk.Button(btn_frame, text="Lägg till alla >>>",
                                      command=self._add_all)
        self.add_all_btn.pack(side=tk.LEFT, padx=4)

        self.result_count_var = tk.StringVar(value="0 dokument")
        ttk.Label(btn_frame, textvariable=self.result_count_var).pack(
            side=tk.RIGHT, padx=4)

    def _build_selected_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Valda dokument", padding=4)
        frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(5, 2))

        columns = ("celex", "type", "date", "title")
        self.selected_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="extended")
        for col, text, w in [("celex", "CELEX-nr", 130), ("type", "Typ", 90),
                              ("date", "Datum", 90), ("title", "Titel", 400)]:
            self.selected_tree.heading(col, text=text)
            self.selected_tree.column(col, width=w, minwidth=max(70, w - 60))

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
        ttk.Button(btn_frame, text="<< Ta bort valda",
                   command=self._remove_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="<<< Ta bort alla",
                   command=self._remove_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Radera sparad",
                   command=self._delete_saved).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Visa artiklar & krav",
                   command=self._open_article_viewer).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn_frame, text="Spara",
                   command=self._save_selected).pack(side=tk.RIGHT, padx=4)

        self.selected_count_var = tk.StringVar(value="0 dokument")
        ttk.Label(btn_frame, textvariable=self.selected_count_var).pack(
            side=tk.RIGHT, padx=8)

    def _build_obligations_panel(self, parent):
        frame = ttk.LabelFrame(
            parent, text="Krav på verksamheter (ej EU/myndigheter)", padding=4)
        frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)

        columns = ("doc", "article", "subject", "obligation")
        self.oblig_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse")
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
        ttk.Button(btn_frame, text="Extrahera krav",
                   command=self._extract_all_obligations).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Exportera till fil",
                   command=self._export_obligations).pack(side=tk.LEFT, padx=4)

        self.oblig_count_var = tk.StringVar(value="0 krav")
        ttk.Label(btn_frame, textvariable=self.oblig_count_var).pack(
            side=tk.RIGHT, padx=4)

    # ── Tooltip ──────────────────────────────────────────────────────────────

    def _show_tooltip(self, widget, text, x, y):
        self._hide_tooltip()
        if not text:
            return
        self._tooltip = tk.Toplevel(widget)
        self._tooltip.wm_overrideredirect(True)
        self._tooltip.wm_geometry(f"+{x + 15}+{y + 10}")
        tk.Label(self._tooltip, text=text, justify=tk.LEFT,
                 background="#ffffe0", relief=tk.SOLID, borderwidth=1,
                 font=("Segoe UI", 9), wraplength=600).pack()

    def _hide_tooltip(self, event=None):
        if self._tooltip:
            self._tooltip.destroy()
            self._tooltip = None

    def _on_tree_motion(self, event, tree=None, col=4):
        if tree is None:
            tree = self.results_tree
        item = tree.identify_row(event.y)
        column = tree.identify_column(event.x)
        if item and column == f"#{col}":
            values = tree.item(item, "values")
            if values and len(values) >= col:
                self._show_tooltip(tree, values[col - 1], event.x_root, event.y_root)
                return
        self._hide_tooltip()

    # ── Sök ──────────────────────────────────────────────────────────────────

    def _do_search(self):
        doc_type = self.type_var.get().split("—")[0].strip()
        if doc_type == "Alla":
            doc_type = ""
        year = self.year_var.get().strip()
        keyword = self.keyword_var.get().strip()
        try:
            limit = int(self.limit_var.get())
        except ValueError:
            limit = 50

        if not doc_type and not year and not keyword:
            messagebox.showwarning("Sök",
                                   "Ange minst ett sökkriterium (typ, år, eller nyckelord).")
            return

        self.search_btn.configure(state="disabled")
        self._status("Söker...")

        def _search():
            try:
                docs = search_documents(doc_type=doc_type, year=year,
                                        keyword=keyword, limit=limit)
                self.root.after(0, lambda: self._show_results(docs))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Sökfel", str(e)))
            finally:
                self.root.after(0, lambda: self.search_btn.configure(state="normal"))

        threading.Thread(target=_search, daemon=True).start()

    def _show_results(self, docs):
        self.search_results = docs
        self._refresh_results_tree()
        self._status(f"Hittade {len(docs)} dokument.")

    def _refresh_results_tree(self):
        self.results_tree.delete(*self.results_tree.get_children())
        for doc in self.search_results:
            self.results_tree.insert("", tk.END, iid=doc.celex,
                                     values=(doc.celex, doc.doc_type, doc.date, doc.title))
        self.result_count_var.set(f"{len(self.search_results)} dokument")

    def _sort_results(self, column):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        key_map = {"celex": lambda d: d.celex, "doc_type": lambda d: d.doc_type,
                   "date": lambda d: d.date, "title": lambda d: d.title.lower()}
        self.search_results.sort(key=key_map.get(column, lambda d: d.celex),
                                 reverse=self.sort_reverse)
        self._refresh_results_tree()

    # ── Lägg till / ta bort / spara ──────────────────────────────────────────

    def _add_selected(self):
        sel = self.results_tree.selection()
        if not sel:
            messagebox.showinfo("Lägg till", "Välj dokument i sökresultaten.")
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

    def _save_selected(self):
        saved = 0
        for doc in self.selected_docs:
            if doc.articles and doc.feedback:
                self.persistence.save_document(doc, doc.feedback)
                saved += 1
        self._status(f"Sparade {saved} dokument.")
        if saved == 0:
            messagebox.showinfo("Spara",
                                "Inga analyserade dokument att spara. "
                                "Klicka 'Extrahera krav' först.")

    def _delete_saved(self):
        sel = self.selected_tree.selection()
        if not sel:
            messagebox.showinfo("Radera", "Välj ett dokument att radera.")
            return
        for celex in sel:
            self.persistence.delete_document(celex)
        self._status(f"Raderade {len(sel)} sparade dokument.")

    def _load_saved_docs(self):
        """Ladda sparade dokument vid uppstart."""
        for celex in self.persistence.list_saved():
            doc, fb = self.persistence.load_document(celex)
            if doc:
                doc.doc_type = doc.type_label()
                doc.feedback = fb
                if doc not in self.selected_docs:
                    self.selected_docs.append(doc)
        self._refresh_selected_tree()

    def _refresh_selected_tree(self):
        self.selected_tree.delete(*self.selected_tree.get_children())
        for doc in self.selected_docs:
            self.selected_tree.insert("", tk.END, iid=doc.celex,
                                      values=(doc.celex, doc.doc_type, doc.date,
                                              doc.title))
        self.selected_count_var.set(f"{len(self.selected_docs)} dokument")

    # ── Artikelvisning ───────────────────────────────────────────────────────

    def _ensure_parsed(self, doc: Document):
        if not doc.raw_html:
            doc.raw_html = fetch_html(doc.celex, lang="SV")
            if len(doc.raw_html) < 500:
                doc.raw_html = fetch_html(doc.celex, lang="EN")
        if not doc.articles:
            doc.articles = parse_articles(doc.raw_html)
        if not doc.obligations:
            doc.obligations = extract_obligations_from_articles(doc.articles)
        if not doc.feedback:
            # Kolla om det finns sparad feedback
            _, saved_fb = self.persistence.load_document(doc.celex)
            if saved_fb:
                doc.feedback = saved_fb
            else:
                doc.feedback = build_annotations(doc, doc.obligations, self.learner)

    def _open_article_viewer(self):
        sel = self.selected_tree.selection()
        if not sel:
            messagebox.showinfo("Visa", "Välj ett dokument i listan.")
            return
        celex = sel[0]
        doc = next((d for d in self.selected_docs if d.celex == celex), None)
        if not doc:
            return

        self._status(f"Hämtar och analyserar {celex}...")

        def _work():
            try:
                self._ensure_parsed(doc)
                self.root.after(0, lambda: self._show_article_window(doc))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Fel", str(e)))
            finally:
                self.root.after(0, lambda: self._status("Redo."))

        threading.Thread(target=_work, daemon=True).start()

    def _show_article_window(self, doc: Document):
        win = tk.Toplevel(self.root)
        win.title(f"{doc.celex} — {doc.title}")
        win.geometry("1300x900")

        fb = doc.feedback
        relevant_count = len([o for o in fb.obligation_annotations
                              if o.status != "rejected"])

        # Rubrik
        ttk.Label(win, text=doc.title, wraplength=1250,
                  style="Title.TLabel").pack(padx=10, pady=(10, 2), anchor=tk.W)
        ttk.Label(win, text=(
            f"CELEX: {doc.celex}  |  Datum: {doc.date}  |  Typ: {doc.doc_type}"
            f"  |  {len(doc.articles)} artiklar  |  {relevant_count} krav"
        )).pack(padx=10, pady=(0, 5), anchor=tk.W)

        ttk.Separator(win, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=2)

        pane = ttk.PanedWindow(win, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # ── Vänster: Artikeltext ─────────────────────────────────────────
        left = ttk.Frame(pane)
        pane.add(left, weight=2)

        legend = ttk.Frame(left)
        legend.pack(fill=tk.X, padx=5, pady=(0, 3))
        ttk.Label(legend, text="Svart = krav  ", font=("Segoe UI", 9)).pack(
            side=tk.LEFT)
        tk.Label(legend, text="Grön = subjekt  ", fg="#006600",
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        ttk.Label(legend, text="Grå = övrigt  ",
                  font=("Segoe UI", 9), foreground="#999999").pack(side=tk.LEFT)
        ttk.Label(legend, text="  |  Högerklicka för att godkänna/avvisa/ange",
                  font=("Segoe UI", 8, "italic")).pack(side=tk.LEFT, padx=10)

        text_widget = tk.Text(
            left, wrap=tk.WORD, font=("Segoe UI", 10),
            padx=10, pady=10, spacing1=2, spacing3=2, undo=False)
        text_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL,
                                     command=text_widget.yview)
        text_widget.configure(yscrollcommand=text_scroll.set)
        text_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        text_widget.pack(fill=tk.BOTH, expand=True)

        # Taggar
        text_widget.tag_configure("article_header",
                                  font=("Segoe UI", 12, "bold"),
                                  spacing1=12, spacing3=4)
        text_widget.tag_configure("article_title",
                                  font=("Segoe UI", 10, "italic"), spacing3=6)
        text_widget.tag_configure("para_num", font=("Segoe UI", 10, "bold"))
        text_widget.tag_configure("obligation_active",
                                  foreground="#000000", font=("Segoe UI", 10))
        text_widget.tag_configure("obligation_rejected",
                                  foreground="#bbbbbb",
                                  font=("Segoe UI", 10, "overstrike"))
        text_widget.tag_configure("non_relevant",
                                  foreground="#999999", font=("Segoe UI", 10))
        text_widget.tag_configure("subject_auto",
                                  foreground="#228B22", font=("Segoe UI", 10))
        text_widget.tag_configure("subject_approved",
                                  foreground="#006600",
                                  font=("Segoe UI", 10, "bold"))
        text_widget.tag_configure("subject_rejected",
                                  foreground="#999999", font=("Segoe UI", 10))
        text_widget.tag_configure("separator", foreground="#cccccc")

        # Prioritetsordning: subjekt-taggar överst så grönt syns
        text_widget.tag_raise("subject_auto")
        text_widget.tag_raise("subject_approved")
        text_widget.tag_raise("obligation_rejected")

        # Index-mappning: (article, paragraph) -> text widget start-index
        para_indices = {}  # (art_num, para_num) -> (start_index_str, text_len)

        # Bygga set av aktiva kravtexter
        active_obl_texts = set()
        rejected_obl_texts = set()
        for obl_ann in fb.obligation_annotations:
            if obl_ann.status == "rejected":
                rejected_obl_texts.add(obl_ann.text_span)
            else:
                active_obl_texts.add(obl_ann.text_span)

        non_relevant_articles = {
            art.number for art in doc.articles if _is_non_relevant_article(art)
        }

        for art in doc.articles:
            is_non_rel = art.number in non_relevant_articles
            h_tag = "non_relevant" if is_non_rel else "article_header"
            text_widget.insert(tk.END, f"\nArtikel {art.number}", h_tag)
            if art.title:
                t_tag = "non_relevant" if is_non_rel else "article_title"
                text_widget.insert(tk.END, f"\n{art.title}", t_tag)
            text_widget.insert(tk.END, "\n")

            for para in art.paragraphs:
                if para.number:
                    n_tag = "non_relevant" if is_non_rel else "para_num"
                    text_widget.insert(tk.END, f"\n{para.number}.   ", n_tag)

                # Spara start-position för stycket
                para_start = text_widget.index(tk.INSERT)

                sentences = re.split(r"(?<=[.;])\s+", para.text)
                for sent in sentences:
                    sent_clean = _clean_text(sent)
                    if not sent_clean:
                        continue
                    if is_non_rel:
                        tag = "non_relevant"
                    elif sent_clean in active_obl_texts:
                        tag = "obligation_active"
                    elif sent_clean in rejected_obl_texts:
                        tag = "obligation_rejected"
                    else:
                        tag = "non_relevant"
                    text_widget.insert(tk.END, sent_clean + " ", tag)

                para_end = text_widget.index(tk.INSERT)
                para_indices[(art.number, para.number)] = (para_start, para_end)
                text_widget.insert(tk.END, "\n")

            text_widget.insert(tk.END, "\n" + "─" * 60 + "\n", "separator")

        # Applicera subjekt-taggar ovanpå bastaggarna
        ann_tag_map = {}  # tag_name -> annotation object
        for subj_ann in fb.subject_annotations:
            key = (subj_ann.article, subj_ann.paragraph)
            if key not in para_indices:
                continue
            para_start_idx, _ = para_indices[key]

            if subj_ann.status == "approved":
                vis_tag = "subject_approved"
            elif subj_ann.status == "rejected":
                vis_tag = "subject_rejected"
            else:
                vis_tag = "subject_auto"

            # Beräkna offset i text-widgeten
            start_idx = f"{para_start_idx} + {subj_ann.char_start} chars"
            end_idx = f"{para_start_idx} + {subj_ann.char_end} chars"

            # Unik tagg per annotation
            ann_tag = f"subj_{subj_ann.id}"
            text_widget.tag_add(ann_tag, start_idx, end_idx)
            text_widget.tag_add(vis_tag, start_idx, end_idx)
            ann_tag_map[ann_tag] = subj_ann

        # Unika taggar för krav-annotationer (för högerklick)
        for obl_ann in fb.obligation_annotations:
            key = (obl_ann.article, obl_ann.paragraph)
            if key not in para_indices:
                continue
            para_start_idx, _ = para_indices[key]
            start_idx = f"{para_start_idx} + {obl_ann.char_start} chars"
            end_idx = f"{para_start_idx} + {obl_ann.char_end} chars"
            ann_tag = f"obl_{obl_ann.id}"
            text_widget.tag_add(ann_tag, start_idx, end_idx)
            ann_tag_map[ann_tag] = obl_ann

        text_widget.configure(state="disabled")

        # ── Höger: Kravlista per subjekt ─────────────────────────────────
        right = ttk.Frame(pane)
        pane.add(right, weight=1)

        ttk.Label(right, text="Krav per subjekt (verksamheter)",
                  font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, padx=5,
                                                       pady=(0, 5))

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

        self._populate_subject_tree(subj_tree, fb)

        # ── Högerklicksmeny ──────────────────────────────────────────────

        def _refresh_viewer():
            """Uppdatera taggar efter feedback-ändring."""
            text_widget.configure(state="normal")

            # Uppdatera krav-set
            active_obl_texts.clear()
            rejected_obl_texts.clear()
            for oa in fb.obligation_annotations:
                if oa.status == "rejected":
                    rejected_obl_texts.add(oa.text_span)
                else:
                    active_obl_texts.add(oa.text_span)

            # Uppdatera alla subjekt-taggar
            for sa in fb.subject_annotations:
                ann_tag = f"subj_{sa.id}"
                ranges = text_widget.tag_ranges(ann_tag)
                if not ranges:
                    continue
                # Ta bort gamla visuella taggar
                for vt in ("subject_auto", "subject_approved", "subject_rejected"):
                    text_widget.tag_remove(vt, ranges[0], ranges[1])
                # Sätt ny
                if sa.status == "approved":
                    vt = "subject_approved"
                elif sa.status == "rejected":
                    vt = "subject_rejected"
                else:
                    vt = "subject_auto"
                text_widget.tag_add(vt, ranges[0], ranges[1])

            # Uppdatera kravmenings-taggar
            # Ommarkera hela texten baserat på nya statusar
            for oa in fb.obligation_annotations:
                ann_tag = f"obl_{oa.id}"
                ranges = text_widget.tag_ranges(ann_tag)
                if not ranges:
                    continue
                for vt in ("obligation_active", "obligation_rejected", "non_relevant"):
                    text_widget.tag_remove(vt, ranges[0], ranges[1])
                if oa.status == "rejected":
                    text_widget.tag_add("obligation_rejected", ranges[0], ranges[1])
                else:
                    text_widget.tag_add("obligation_active", ranges[0], ranges[1])

            text_widget.tag_raise("subject_auto")
            text_widget.tag_raise("subject_approved")
            text_widget.configure(state="disabled")

            # Uppdatera subjekt-trädvy
            subj_tree.delete(*subj_tree.get_children())
            self._populate_subject_tree(subj_tree, fb)

            # Autospara
            self.persistence.save_document(doc, fb)

        def _on_right_click(event):
            menu = tk.Menu(text_widget, tearoff=0)
            clicked_index = text_widget.index(f"@{event.x},{event.y}")
            tags_at = text_widget.tag_names(clicked_index)

            # Hitta subjekt och krav under markören
            clicked_subj = None
            clicked_obl = None
            for tag_name in tags_at:
                if tag_name in ann_tag_map:
                    ann = ann_tag_map[tag_name]
                    if isinstance(ann, SubjectAnnotation):
                        clicked_subj = ann
                    elif isinstance(ann, ObligationAnnotation):
                        clicked_obl = ann

            # Textmarkering?
            has_sel = bool(text_widget.tag_ranges("sel"))

            any_item = False

            # ── Subjekt-meny ──
            if clicked_subj:
                any_item = True
                subj_label = f"Subjekt: \"{clicked_subj.text_span[:40]}\""
                menu.add_command(label=subj_label, state="disabled")

                if clicked_subj.status != "approved":
                    menu.add_command(
                        label="✓ Godkänn subjekt",
                        command=lambda s=clicked_subj: (
                            _set_subj_status(s, "approved"), _refresh_viewer()))
                if clicked_subj.status != "rejected":
                    rej_menu = tk.Menu(menu, tearoff=0)
                    for reason in SUBJECT_REJECTION_REASONS:
                        rej_menu.add_command(
                            label=reason,
                            command=lambda s=clicked_subj, r=reason: (
                                _set_subj_status(s, "rejected", r),
                                _refresh_viewer()))
                    menu.add_cascade(label="✗ Avvisa subjekt", menu=rej_menu)
                menu.add_separator()

            # ── Krav-meny ──
            if clicked_obl:
                any_item = True
                obl_label = f"Krav: \"{clicked_obl.text_span[:40]}...\""
                menu.add_command(label=obl_label, state="disabled")

                if clicked_obl.status != "approved":
                    menu.add_command(
                        label="✓ Godkänn krav",
                        command=lambda o=clicked_obl: (
                            _set_obl_status(o, "approved"), _refresh_viewer()))
                if clicked_obl.status != "rejected":
                    rej_menu = tk.Menu(menu, tearoff=0)
                    for reason in OBLIGATION_REJECTION_REASONS:
                        rej_menu.add_command(
                            label=reason,
                            command=lambda o=clicked_obl, r=reason: (
                                _set_obl_status(o, "rejected", r),
                                _refresh_viewer()))
                    menu.add_cascade(label="✗ Avvisa krav", menu=rej_menu)
                menu.add_separator()

            # ── Markera ny subjekt/krav ──
            if has_sel:
                any_item = True
                menu.add_command(
                    label="Ange markerad text som subjekt",
                    command=lambda: _mark_new_subject(text_widget, doc, fb,
                                                      para_indices, ann_tag_map,
                                                      _refresh_viewer))
                menu.add_command(
                    label="Ange markerad text som krav",
                    command=lambda: _mark_new_obligation(text_widget, doc, fb,
                                                         para_indices, ann_tag_map,
                                                         _refresh_viewer))

            if any_item:
                menu.tk_popup(event.x_root, event.y_root)

        def _set_subj_status(subj_ann, status, reason=""):
            subj_ann.status = status
            subj_ann.rejection_reason = reason
            if status == "approved":
                self.learner.record_subject_approval(subj_ann.normalized)
            elif status == "rejected":
                self.learner.record_subject_rejection(subj_ann.normalized, reason)

        def _set_obl_status(obl_ann, status, reason=""):
            obl_ann.status = status
            obl_ann.rejection_reason = reason
            if status == "approved":
                self.learner.record_obligation_approval(obl_ann.text_span)
            elif status == "rejected":
                self.learner.record_obligation_rejection(obl_ann.text_span, reason)

        def _mark_new_subject(tw, doc, fb, pi, atm, refresh_fn):
            sel_ranges = tw.tag_ranges("sel")
            if not sel_ranges or len(sel_ranges) < 2:
                return
            sel_start, sel_end = str(sel_ranges[0]), str(sel_ranges[1])
            tw.configure(state="normal")
            selected_text = tw.get(sel_start, sel_end).strip()
            tw.configure(state="disabled")
            if not selected_text:
                return

            # Hitta vilken artikel/stycke
            art_num, para_num = _find_para_at_index(tw, sel_start, pi)

            # Fråga efter normalisering
            normalized = simpledialog.askstring(
                "Normalisera subjekt",
                f"Markerad text: \"{selected_text}\"\n\n"
                f"Ange normaliserad form (obestämd singular):",
                initialvalue=normalize_subject(selected_text),
                parent=win)
            if not normalized:
                return

            # Beräkna char_start relativt styckets start
            p_key = (art_num, para_num)
            if p_key in pi:
                para_start_str = pi[p_key][0]
                # Beräkna offset
                para_start_line, para_start_col = map(
                    int, para_start_str.split("."))
                sel_start_line, sel_start_col = map(
                    int, sel_start.split("."))
                sel_end_line, sel_end_col = map(int, sel_end.split("."))
                # Enkel offset-beräkning (fungerar bra inom samma rad)
                char_start = 0
                char_end = len(selected_text)
            else:
                char_start = 0
                char_end = len(selected_text)

            new_ann = SubjectAnnotation(
                id=uuid.uuid4().hex[:12], celex=doc.celex,
                article=art_num, paragraph=para_num,
                text_span=selected_text,
                char_start=char_start, char_end=char_end,
                normalized=normalized, source="user",
                status="approved")

            fb.subject_annotations.append(new_ann)

            # Applicera tagg
            tw.configure(state="normal")
            ann_tag = f"subj_{new_ann.id}"
            tw.tag_add(ann_tag, sel_start, sel_end)
            tw.tag_add("subject_approved", sel_start, sel_end)
            tw.tag_raise("subject_approved")
            tw.configure(state="disabled")
            atm[ann_tag] = new_ann

            self.learner.record_subject_approval(normalized)
            refresh_fn()

        def _mark_new_obligation(tw, doc, fb, pi, atm, refresh_fn):
            sel_ranges = tw.tag_ranges("sel")
            if not sel_ranges or len(sel_ranges) < 2:
                return
            sel_start, sel_end = str(sel_ranges[0]), str(sel_ranges[1])
            tw.configure(state="normal")
            selected_text = tw.get(sel_start, sel_end).strip()
            tw.configure(state="disabled")
            if not selected_text:
                return

            art_num, para_num = _find_para_at_index(tw, sel_start, pi)

            # Fråga vilka subjekt kravet gäller
            subj_str = simpledialog.askstring(
                "Subjekt för krav",
                f"Kravtext: \"{selected_text[:100]}...\"\n\n"
                f"Ange subjekt (kommaseparerade):",
                initialvalue="",
                parent=win)
            if not subj_str:
                return

            subjects = [s.strip() for s in subj_str.split(",") if s.strip()]

            p_key = (art_num, para_num)
            if p_key in pi:
                char_start = 0
                char_end = len(selected_text)
            else:
                char_start = 0
                char_end = len(selected_text)

            new_obl = ObligationAnnotation(
                id=uuid.uuid4().hex[:12], celex=doc.celex,
                article=art_num, paragraph=para_num,
                text_span=selected_text,
                char_start=char_start, char_end=char_end,
                subjects=subjects, source="user",
                status="approved")

            fb.obligation_annotations.append(new_obl)

            tw.configure(state="normal")
            ann_tag = f"obl_{new_obl.id}"
            tw.tag_add(ann_tag, sel_start, sel_end)
            # Ta bort grå och lägg till svart
            tw.tag_remove("non_relevant", sel_start, sel_end)
            tw.tag_add("obligation_active", sel_start, sel_end)
            tw.configure(state="disabled")
            atm[ann_tag] = new_obl

            refresh_fn()

        def _find_para_at_index(tw, idx_str, pi):
            """Hitta vilken artikel/stycke en position befinner sig i."""
            for (art_num, para_num), (p_start, p_end) in pi.items():
                if tw.compare(idx_str, ">=", p_start) and tw.compare(idx_str, "<=", p_end):
                    return art_num, para_num
            return "?", ""

        # Bind högerklick
        text_widget.bind("<Button-3>", _on_right_click)

        # Dubbelklick i subjekt-trädvy
        def _on_subj_double_click(event):
            item = subj_tree.selection()
            if not item:
                return
            vals = subj_tree.item(item[0], "values")
            if vals and vals[0]:
                dw = tk.Toplevel(win)
                dw.title("Kravtext")
                dw.geometry("600x300")
                st = scrolledtext.ScrolledText(dw, wrap=tk.WORD,
                                                font=("Segoe UI", 10))
                st.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
                st.insert(tk.END, vals[0])
                st.configure(state="disabled")

        subj_tree.bind("<Double-1>", _on_subj_double_click)

        # Tooltip
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

    def _populate_subject_tree(self, subj_tree, fb: DocumentFeedback):
        """Fyll subjekt-trädvyn med godkända/väntande krav per subjekt."""
        by_subject = {}
        for obl_ann in fb.obligation_annotations:
            if obl_ann.status == "rejected":
                continue
            for subj in obl_ann.subjects:
                by_subject.setdefault(subj, []).append(obl_ann)

        for subj, obls in sorted(by_subject.items()):
            parent_id = subj_tree.insert(
                "", tk.END, text=f"{subj} ({len(obls)} krav)",
                open=False, values=("",))
            for obl in obls:
                obl_text = (obl.text_span[:200] + "..."
                            if len(obl.text_span) > 200 else obl.text_span)
                subj_tree.insert(
                    parent_id, tk.END,
                    text=f"Art. {obl.article}.{obl.paragraph}",
                    values=(obl_text,))

    # ── Kravextrahering ──────────────────────────────────────────────────────

    def _extract_all_obligations(self):
        if not self.selected_docs:
            messagebox.showinfo("Krav", "Lägg till dokument först.")
            return

        self._status("Hämtar och analyserar dokument...")

        def _work():
            total = 0
            for doc in self.selected_docs:
                try:
                    self._ensure_parsed(doc)
                except Exception:
                    continue
                relevant = [o for o in doc.feedback.obligation_annotations
                            if o.status != "rejected"]
                total += len(relevant)
                self.root.after(
                    0, lambda d=doc, t=total: self._status(
                        f"Analyserat {d.celex}: {t} krav på verksamheter"))
            self.root.after(0, self._refresh_obligations_tree)
            self.root.after(0, lambda: self._status(
                f"Klar — {total} krav på verksamheter."))

        threading.Thread(target=_work, daemon=True).start()

    def _refresh_obligations_tree(self):
        self.oblig_tree.delete(*self.oblig_tree.get_children())
        count = 0
        for doc in self.selected_docs:
            if not doc.feedback:
                continue
            for i, obl_ann in enumerate(doc.feedback.obligation_annotations):
                if obl_ann.status == "rejected":
                    continue
                subj_display = ", ".join(obl_ann.subjects)
                iid = f"{doc.celex}__{i}"
                self.oblig_tree.insert(
                    "", tk.END, iid=iid,
                    values=(doc.celex,
                            f"{obl_ann.article}.{obl_ann.paragraph}",
                            subj_display, obl_ann.text_span))
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
        win.title(f"Krav — {values[0]} Art. {values[1]}")
        win.geometry("700x300")
        ttk.Label(win, text=f"Dokument: {values[0]}",
                  font=("Segoe UI", 10, "bold")).pack(
            padx=10, pady=(10, 2), anchor=tk.W)
        ttk.Label(win, text=f"Artikel: {values[1]}").pack(
            padx=10, pady=2, anchor=tk.W)
        ttk.Label(win, text=f"Subjekt: {values[2]}").pack(
            padx=10, pady=2, anchor=tk.W)
        ttk.Separator(win, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=5)
        st = scrolledtext.ScrolledText(
            win, wrap=tk.WORD, font=("Segoe UI", 10), padx=10, pady=10)
        st.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        st.insert(tk.END, values[3])
        st.configure(state="disabled")

    # ── Export ────────────────────────────────────────────────────────────────

    def _export_obligations(self):
        has_any = any(
            oa for d in self.selected_docs if d.feedback
            for oa in d.feedback.obligation_annotations
            if oa.status != "rejected")
        if not has_any:
            messagebox.showinfo("Export", "Inga krav att exportera.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Textfil", "*.txt"), ("CSV", "*.csv"),
                       ("Alla filer", "*.*")],
            title="Spara krav")
        if not path:
            return

        with open(path, "w", encoding="utf-8") as f:
            if path.endswith(".csv"):
                f.write("Dokument\tArtikel\tSubjekt\tStatus\tKrav\n")
                for doc in self.selected_docs:
                    if not doc.feedback:
                        continue
                    for oa in doc.feedback.obligation_annotations:
                        if oa.status == "rejected":
                            continue
                        text = oa.text_span.replace("\t", " ").replace("\n", " ")
                        subj = ", ".join(oa.subjects)
                        f.write(f"{doc.celex}\t{oa.article}.{oa.paragraph}\t"
                                f"{subj}\t{oa.status}\t{text}\n")
            else:
                for doc in self.selected_docs:
                    if not doc.feedback:
                        continue
                    active = [oa for oa in doc.feedback.obligation_annotations
                              if oa.status != "rejected"]
                    if not active:
                        continue
                    f.write(f"{'=' * 80}\n")
                    f.write(f"Dokument: {doc.celex}\n")
                    f.write(f"Titel:    {doc.title}\n")
                    f.write(f"{'─' * 80}\n\n")

                    by_subj = {}
                    for oa in active:
                        for subj in oa.subjects:
                            by_subj.setdefault(subj, []).append(oa)

                    for subj, obls in sorted(by_subj.items()):
                        f.write(f"  SUBJEKT: {subj}\n")
                        f.write(f"  {'─' * 40}\n")
                        for i, oa in enumerate(obls, 1):
                            f.write(f"  [{i}] Art. {oa.article}.{oa.paragraph}"
                                    f" [{oa.status}]\n")
                            f.write(f"      {oa.text_span}\n\n")

        self._status(f"Exporterat till {path}")
        messagebox.showinfo("Export", f"Krav exporterade till:\n{path}")

    # ── Status ────────────────────────────────────────────────────────────────

    def _status(self, text: str):
        self.status_var.set(text)


def main():
    root = tk.Tk()
    EULagTexterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
