#!/usr/bin/env python3
"""
EU-lagtexter GUI — Sök, välj och analysera lagtexter från EU-kommissionen.

Tkinter-baserat GUI med:
- Sök och filtrera dokument (typ, år, nyckelord)
- Sorterbar dokumentlista med fulla titlar
- Lägg till / ta bort valda dokument
- Artikelvisning med krav markerade i färg, icke-krav gråmarkerade
- Kravextrahering med subjektsidentifiering och -kategorisering
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

# ── API-konstanter ──────────────────────────────────────────────────────────

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
EURLEX_HTML_URL = (
    "https://eur-lex.europa.eu/legal-content/{lang}/TXT/HTML/?uri=CELEX:{celex}"
)

# ── Datamodell ──────────────────────────────────────────────────────────────


@dataclass
class Article:
    number: str          # t.ex. "1", "2", "21"
    title: str           # t.ex. "Innehåll", "Tillämpningsområde"
    paragraphs: list     # lista av Paragraph


@dataclass
class Paragraph:
    number: str          # t.ex. "1", "2", "a", "" (för brödtext)
    text: str
    children: list = field(default_factory=list)  # underpunkter


@dataclass
class Obligation:
    article: str
    paragraph: str
    text: str
    subject: str
    subject_category: str  # "entity", "eu_institution", "member_state", "other"


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
            "R": "Förordning",
            "L": "Direktiv",
            "D": "Beslut",
            "H": "Rekommendation",
        }.get(code, code)

    def __eq__(self, other):
        return isinstance(other, Document) and self.celex == other.celex

    def __hash__(self):
        return hash(self.celex)


# ── API-funktioner ──────────────────────────────────────────────────────────


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
    text = re.sub(r"<script[^>]*>.*?</script>", "", html_text, flags=re.DOTALL | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Artikelparser ───────────────────────────────────────────────────────────


def parse_articles(raw_html: str) -> list[Article]:
    """Parsa HTML från EUR-Lex och extrahera artiklar med stycken."""
    articles = []

    # Hitta alla eli-subdivision-block som innehåller artiklar
    # Mönster: <div class="eli-subdivision" id="art_N">...</div>
    art_pattern = re.compile(
        r'<div\s+class="eli-subdivision"\s+id="(art_\d+)">(.*?)</div>\s*(?=<div\s+class="eli-subdivision"|</div>\s*<div\s+class="eli-subdivision"|$)',
        re.DOTALL,
    )

    # Enklare approach: dela på artikelrubriker
    # Hitta alla "Artikel N" med omgivande kontext
    art_header_pattern = re.compile(
        r'<p[^>]*class="oj-ti-art"[^>]*>(Artikel)\s*\W*(\d+)</p>',
        re.IGNORECASE,
    )

    headers = list(art_header_pattern.finditer(raw_html))
    if not headers:
        # Försök engelska
        art_header_pattern = re.compile(
            r'<p[^>]*class="oj-ti-art"[^>]*>(Article)\s*\W*(\d+)</p>',
            re.IGNORECASE,
        )
        headers = list(art_header_pattern.finditer(raw_html))

    for i, match in enumerate(headers):
        art_num = match.group(2)

        # Extrahera text fram till nästa artikel
        start = match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw_html)
        section_html = raw_html[start:end]

        # Hämta artikelns titel (class="oj-sti-art")
        title_match = re.search(
            r'<p[^>]*class="oj-sti-art"[^>]*>(.*?)</p>', section_html, re.DOTALL
        )
        art_title = strip_html(title_match.group(1)).strip() if title_match else ""

        # Extrahera stycken
        paragraphs = _parse_paragraphs(section_html)

        articles.append(Article(number=art_num, title=art_title, paragraphs=paragraphs))

    return articles


def _parse_paragraphs(section_html: str) -> list[Paragraph]:
    """Parsa stycken inom en artikel."""
    paragraphs = []

    # Hitta numrerade stycken: "1.   Text..." eller bara brödtext
    # EUR-Lex använder <p class="oj-normal"> för vanliga stycken
    p_pattern = re.compile(r'<p[^>]*class="oj-normal"[^>]*>(.*?)</p>', re.DOTALL)

    # Hitta tabellbaserade punktlistor (a), b), c) etc.)
    current_para_num = ""
    current_text_parts = []

    for p_match in p_pattern.finditer(section_html):
        raw = p_match.group(1)
        text = strip_html(raw).strip()
        if not text:
            continue

        # Kolla om stycket börjar med ett nummer: "1.   " eller "1.  "
        num_match = re.match(r"^(\d+)\.\s{2,}", text)
        if num_match:
            # Spara föregående stycke om det finns
            if current_text_parts:
                paragraphs.append(Paragraph(
                    number=current_para_num,
                    text="\n".join(current_text_parts),
                ))

            current_para_num = num_match.group(1)
            current_text_parts = [text[num_match.end():].strip()]
        else:
            # Kolla underpunkter: a), b) etc.
            sub_match = re.match(r"^([a-z]\))\s*", text)
            if sub_match:
                current_text_parts.append(text)
            else:
                # Vanlig fortsättningstext
                if current_text_parts:
                    current_text_parts.append(text)
                else:
                    current_para_num = ""
                    current_text_parts = [text]

    # Sista stycket
    if current_text_parts:
        paragraphs.append(Paragraph(
            number=current_para_num,
            text="\n".join(current_text_parts),
        ))

    return paragraphs


# ── Kravextrahering med subjektsidentifiering ───────────────────────────────

# Subjekt som ska filtreras bort (EU-institutioner, medlemsstater, myndigheter)
EU_SUBJECTS = {
    "kommissionen", "europeiska kommissionen", "europaparlamentet",
    "rådet", "europeiska rådet", "ministerrådet",
    "enisa", "eu-cyclone", "csirt", "csirt-enheter", "csirt-nätverket",
    "samarbetsgruppen", "europeiska datatillsynsmannen",
    "europeiska unionens byrå för cybersäkerhet",
    "the commission", "european commission", "european parliament",
    "the council", "council",
}

MEMBER_STATE_SUBJECTS = {
    "medlemsstaterna", "medlemsstaten", "varje medlemsstat",
    "medlemsstaternas", "den berörda medlemsstaten",
    "de behöriga myndigheterna", "den behöriga myndigheten",
    "behöriga myndigheter", "behörig myndighet",
    "den gemensamma kontaktpunkten", "gemensamma kontaktpunkter",
    "nationella myndigheter", "tillsynsmyndigheten", "tillsynsmyndigheterna",
    "member states", "the member state", "competent authorities",
    "the competent authority",
}

# Mönster för att identifiera krav
OBLIGATION_TRIGGERS_SV = re.compile(
    r"\b(?:ska|skall|måste|bör|är\s+skyldiga?\s+att|åligger|"
    r"ansvarar?\s+för\s+att|krävs\s+att|fordras\s+att)\b",
    re.IGNORECASE,
)

OBLIGATION_TRIGGERS_EN = re.compile(
    r"\b(?:shall|must|is\s+required\s+to|are\s+required\s+to|"
    r"is\s+obliged\s+to|are\s+obliged\s+to)\b",
    re.IGNORECASE,
)

# Kända subjektsmönster (entiteter/verksamheter som vi vill behålla)
ENTITY_SUBJECTS = re.compile(
    r"\b(?:entiteter(?:na)?|väsentliga\s+entiteter(?:na)?|"
    r"viktiga\s+entiteter(?:na)?|"
    r"(?:väsentliga\s+och\s+viktiga|viktiga\s+och\s+väsentliga)\s+entiteter(?:na)?|"
    r"operatörer(?:na)?|tjänsteleverantörer(?:na)?|"
    r"leverantörer(?:na)?|tillhandahållare(?:n)?|"
    r"den\s+berörda\s+entiteten|berörda\s+entiteter(?:na)?|"
    r"verksamhetsutövare(?:n|na)?|"
    r"företag(?:et|en)?|organisationer(?:na)?|"
    r"registreringsenheter(?:na)?|"
    r"entities|essential\s+entities|important\s+entities|"
    r"operators|providers|undertakings)\b",
    re.IGNORECASE,
)


def _clean_text(t: str) -> str:
    """Normalisera whitespace."""
    return re.sub(r"\s+", " ", t).strip()


def _extract_subject(sentence: str, prev_subject: str) -> tuple[str, str]:
    """
    Extrahera subjektet ur en mening.
    Returnerar (subjekt, kategori).
    Kategori: "entity", "eu", "member_state", "other"
    """
    s_lower = sentence.lower()

    # Sök efter specifika entitets-subjekt först
    ent_match = ENTITY_SUBJECTS.search(sentence)
    if ent_match:
        return ent_match.group(0).strip(), "entity"

    # Sök efter EU-institutioner
    for eu_sub in EU_SUBJECTS:
        if eu_sub in s_lower:
            return eu_sub.capitalize(), "eu"

    # Sök efter medlemsstater/myndigheter
    for ms_sub in MEMBER_STATE_SUBJECTS:
        if ms_sub in s_lower:
            return ms_sub.capitalize(), "member_state"

    # Försök hitta subjekt innan "ska"/"shall" etc.
    trigger = OBLIGATION_TRIGGERS_SV.search(sentence) or OBLIGATION_TRIGGERS_EN.search(sentence)
    if trigger:
        before = sentence[:trigger.start()].strip()
        # Ta det sista substantivfrasen innan triggern
        # T.ex. "Väsentliga och viktiga entiteter ska..."
        before_clean = before.rstrip(" ,;:")
        if before_clean:
            # Kolla om det matchar en känd kategori
            b_lower = before_clean.lower()
            for eu_sub in EU_SUBJECTS:
                if eu_sub in b_lower:
                    return before_clean, "eu"
            for ms_sub in MEMBER_STATE_SUBJECTS:
                if ms_sub in b_lower:
                    return before_clean, "member_state"
            if ENTITY_SUBJECTS.search(before_clean):
                return before_clean, "entity"
            # Okänt subjekt — troligen en entitet om det inte är EU
            if len(before_clean) < 100:
                return before_clean, "other"

    # Implicit subjekt — använd föregående
    if prev_subject:
        return prev_subject, "entity_implicit"

    return "(okänt)", "other"


def _is_list_intro(text: str) -> bool:
    """Kolla om texten slutar med kolon, vilket indikerar en följande lista."""
    return text.rstrip().endswith(":")


def extract_obligations_from_articles(articles: list[Article]) -> list[Obligation]:
    """Extrahera alla krav från en lista artiklar, med subjektsidentifiering."""
    obligations = []
    prev_subject = ""
    prev_subject_cat = "other"

    for art in articles:
        for para in art.paragraphs:
            full_text = para.text
            sentences = re.split(r"(?<=[.;])\s+", full_text)

            list_intro_subject = ""
            list_intro_cat = ""

            for sent in sentences:
                sent = _clean_text(sent)
                if not sent or len(sent) < 10:
                    continue

                is_obligation = bool(
                    OBLIGATION_TRIGGERS_SV.search(sent)
                    or OBLIGATION_TRIGGERS_EN.search(sent)
                )

                if is_obligation:
                    subject, cat = _extract_subject(sent, prev_subject)
                    prev_subject = subject
                    prev_subject_cat = cat

                    # Om det är en list-intro ("X ska ha följande uppgifter:")
                    if _is_list_intro(sent):
                        list_intro_subject = subject
                        list_intro_cat = cat

                    obligations.append(Obligation(
                        article=art.number,
                        paragraph=para.number,
                        text=sent,
                        subject=subject,
                        subject_category=cat,
                    ))

                elif list_intro_subject:
                    # Punkter som följer ett intro med krav
                    # t.ex. "a) stödja och underlätta..."
                    if re.match(r"^[a-z]\)", sent):
                        obligations.append(Obligation(
                            article=art.number,
                            paragraph=para.number,
                            text=sent,
                            subject=list_intro_subject,
                            subject_category=list_intro_cat,
                        ))

            # Nollställ list-intro efter stycke
            list_intro_subject = ""
            list_intro_cat = ""

    return obligations


def is_obligation_text(text: str) -> bool:
    """Kolla om en text innehåller kravindikatorer."""
    return bool(
        OBLIGATION_TRIGGERS_SV.search(text)
        or OBLIGATION_TRIGGERS_EN.search(text)
    )


# ── GUI ─────────────────────────────────────────────────────────────────────


class EULagTexterGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EU-lagtexter — Sök & Analysera")
        self.root.geometry("1400x900")
        self.root.minsize(1000, 700)

        self.search_results: list[Document] = []
        self.selected_docs: list[Document] = []
        self.sort_column = "date"
        self.sort_reverse = True
        self._tooltip = None

        self._build_ui()
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
            self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W
        )
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
            state="readonly", width=22,
        ).pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(row1, text="År:").pack(side=tk.LEFT, padx=(0, 4))
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

        self.search_btn = ttk.Button(row2, text="Sök", command=self._do_search)
        self.search_btn.pack(side=tk.LEFT, padx=4)

    def _build_results_panel(self, parent):
        frame = ttk.LabelFrame(
            parent, text="Sökresultat — Tillgängliga dokument", padding=4
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
            btn_frame, text="Lägg till valda >>", command=self._add_selected
        )
        self.add_btn.pack(side=tk.LEFT, padx=4)
        self.add_all_btn = ttk.Button(
            btn_frame, text="Lägg till alla >>>", command=self._add_all
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
            parent, text="Krav på verksamheter (ej EU/myndigheter)", padding=4
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

    # ── Tooltip ─────────────────────────────────────────────────────────

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

    # ── Sök ─────────────────────────────────────────────────────────────

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
            messagebox.showwarning(
                "Sök", "Ange minst ett sökkriterium (typ, år, eller nyckelord)."
            )
            return

        self.search_btn.configure(state="disabled")
        self._status("Söker...")

        def _search():
            try:
                docs = search_documents(
                    doc_type=doc_type, year=year, keyword=keyword, limit=limit
                )
                self.root.after(0, lambda: self._show_results(docs))
            except Exception as e:
                self.root.after(
                    0, lambda: messagebox.showerror("Sökfel", str(e))
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

    # ── Lägg till / ta bort ─────────────────────────────────────────────

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

    def _refresh_selected_tree(self):
        self.selected_tree.delete(*self.selected_tree.get_children())
        for doc in self.selected_docs:
            self.selected_tree.insert(
                "", tk.END, iid=doc.celex,
                values=(doc.celex, doc.doc_type, doc.date, doc.title),
            )
        self.selected_count_var.set(f"{len(self.selected_docs)} dokument")

    # ── Artikelvisning ──────────────────────────────────────────────────

    def _ensure_parsed(self, doc: Document):
        """Säkerställ att dokumentet har hämtats och parsats."""
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
                self.root.after(
                    0, lambda: messagebox.showerror("Fel", str(e))
                )
            finally:
                self.root.after(0, lambda: self._status("Redo."))

        threading.Thread(target=_work, daemon=True).start()

    def _show_article_window(self, doc: Document):
        win = tk.Toplevel(self.root)
        win.title(f"{doc.celex} — {doc.title}")
        win.geometry("1200x850")

        # Rubrik
        ttk.Label(
            win, text=doc.title, wraplength=1150, style="Title.TLabel"
        ).pack(padx=10, pady=(10, 2), anchor=tk.W)
        ttk.Label(
            win,
            text=f"CELEX: {doc.celex}  |  Datum: {doc.date}  |  Typ: {doc.doc_type}"
            f"  |  {len(doc.articles)} artiklar"
            f"  |  {len([o for o in doc.obligations if o.subject_category not in ('eu', 'member_state')])} krav på verksamheter",
        ).pack(padx=10, pady=(0, 5), anchor=tk.W)

        ttk.Separator(win, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=2)

        # Huvudpanel: artikeltext (vänster) + kravlista per subjekt (höger)
        pane = ttk.PanedWindow(win, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Vänster: Artikeltext med färgkodning
        left = ttk.Frame(pane)
        pane.add(left, weight=2)

        ttk.Label(left, text="Artiklar (krav = svart, övrig text = grå)",
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
        text_widget.tag_configure("obligation", foreground="#000000",
                                  font=("Segoe UI", 10))
        text_widget.tag_configure("obligation_entity",
                                  foreground="#000000",
                                  background="#e8f5e9",
                                  font=("Segoe UI", 10, "bold"))
        text_widget.tag_configure("non_obligation", foreground="#999999",
                                  font=("Segoe UI", 10))
        text_widget.tag_configure("separator", foreground="#cccccc")

        # Bygg artikelinnehåll med kravmarkering
        obligation_texts = {o.text for o in doc.obligations
                           if o.subject_category not in ("eu", "member_state")}

        for art in doc.articles:
            # Artikelrubrik
            text_widget.insert(tk.END, f"\nArtikel {art.number}", "article_header")
            if art.title:
                text_widget.insert(tk.END, f"\n{art.title}", "article_title")
            text_widget.insert(tk.END, "\n")

            for para in art.paragraphs:
                if para.number:
                    text_widget.insert(tk.END, f"\n{para.number}.   ", "para_num")

                # Dela texten i meningar för att kunna färgkoda
                sentences = re.split(r"(?<=[.;])\s+", para.text)
                for sent in sentences:
                    sent_clean = _clean_text(sent)
                    if not sent_clean:
                        continue

                    # Kolla om denna mening är ett krav
                    is_obl = is_obligation_text(sent_clean)
                    # Kolla om det är ett krav som rör verksamheter
                    is_entity_obl = sent_clean in obligation_texts

                    if is_entity_obl:
                        text_widget.insert(tk.END, sent_clean + " ",
                                           "obligation_entity")
                    elif is_obl:
                        text_widget.insert(tk.END, sent_clean + " ", "obligation")
                    else:
                        text_widget.insert(tk.END, sent_clean + " ", "non_obligation")

                text_widget.insert(tk.END, "\n")

            text_widget.insert(tk.END, "\n" + "─" * 60 + "\n", "separator")

        text_widget.configure(state="disabled")

        # Höger: Kravlista per subjekt
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

        # Gruppera krav per subjekt (uteslut EU/myndigheter)
        by_subject: dict[str, list[Obligation]] = {}
        for obl in doc.obligations:
            if obl.subject_category in ("eu", "member_state"):
                continue
            subj = obl.subject
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

        # Dubbelklick öppnar fullständig kravtext
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

        # Tooltip för subjekt-trädvy
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

    # ── Kravextrahering ─────────────────────────────────────────────────

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
                entity_obls = [
                    o for o in doc.obligations
                    if o.subject_category not in ("eu", "member_state")
                ]
                total += len(entity_obls)
                self.root.after(
                    0, lambda d=doc, t=total: self._status(
                        f"Analyserat {d.celex}: {len(d.obligations)} krav totalt, "
                        f"{t} på verksamheter"
                    ),
                )
            self.root.after(0, self._refresh_obligations_tree)
            self.root.after(
                0, lambda: self._status(f"Klar — {total} krav på verksamheter.")
            )

        threading.Thread(target=_work, daemon=True).start()

    def _refresh_obligations_tree(self):
        self.oblig_tree.delete(*self.oblig_tree.get_children())
        count = 0
        for doc in self.selected_docs:
            for i, obl in enumerate(doc.obligations):
                if obl.subject_category in ("eu", "member_state"):
                    continue
                iid = f"{doc.celex}__{i}"
                self.oblig_tree.insert(
                    "", tk.END, iid=iid,
                    values=(doc.celex, f"{obl.article}.{obl.paragraph}",
                            obl.subject, obl.text),
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
        win.title(f"Krav — {values[0]} Art. {values[1]}")
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

    # ── Export ──────────────────────────────────────────────────────────

    def _export_obligations(self):
        has_any = any(
            o for d in self.selected_docs for o in d.obligations
            if o.subject_category not in ("eu", "member_state")
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
                        if obl.subject_category in ("eu", "member_state"):
                            continue
                        text = obl.text.replace("\t", " ").replace("\n", " ")
                        f.write(
                            f"{doc.celex}\t{obl.article}.{obl.paragraph}\t"
                            f"{obl.subject}\t{obl.subject_category}\t{text}\n"
                        )
            else:
                for doc in self.selected_docs:
                    entity_obls = [
                        o for o in doc.obligations
                        if o.subject_category not in ("eu", "member_state")
                    ]
                    if not entity_obls:
                        continue
                    f.write(f"{'=' * 80}\n")
                    f.write(f"Dokument: {doc.celex}\n")
                    f.write(f"Titel:    {doc.title}\n")
                    f.write(f"{'─' * 80}\n\n")

                    by_subj: dict[str, list] = {}
                    for obl in entity_obls:
                        by_subj.setdefault(obl.subject, []).append(obl)

                    for subj, obls in sorted(by_subj.items()):
                        f.write(f"  SUBJEKT: {subj}\n")
                        f.write(f"  {'─' * 40}\n")
                        for i, obl in enumerate(obls, 1):
                            f.write(f"  [{i}] Art. {obl.article}.{obl.paragraph}\n")
                            f.write(f"      {obl.text}\n\n")

        self._status(f"Exporterat till {path}")
        messagebox.showinfo("Export", f"Krav exporterade till:\n{path}")

    # ── Status ──────────────────────────────────────────────────────────

    def _status(self, text: str):
        self.status_var.set(text)


def main():
    root = tk.Tk()
    EULagTexterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
