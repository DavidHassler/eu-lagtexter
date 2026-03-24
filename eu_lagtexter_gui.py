#!/usr/bin/env python3
"""
EU-lagtexter GUI — Sök, välj och analysera lagtexter från EU-kommissionen.

Tkinter-baserat GUI med:
- Sök och filtrera dokument (typ, år, nyckelord)
- Sorterbar dokumentlista med fulla titlar
- Lägg till / ta bort valda dokument
- Extrahera krav (obligations) från valda dokument
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import threading
import re
import html
import json
import urllib.request
import urllib.parse
from dataclasses import dataclass, field

# ── API-konstanter ──────────────────────────────────────────────────────────

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
EURLEX_TEXT_URL = (
    "https://eur-lex.europa.eu/legal-content/{lang}/TXT/HTML/?uri=CELEX:{celex}"
)

# ── Datamodell ──────────────────────────────────────────────────────────────

@dataclass
class Document:
    celex: str
    title: str
    date: str
    doc_type: str = ""
    full_text: str = ""
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


def fetch_text(celex: str, lang: str = "SV") -> str:
    url = EURLEX_TEXT_URL.format(lang=lang, celex=celex)
    req = urllib.request.Request(url, headers={"User-Agent": "EU-Lagtexter/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return strip_html(raw)


def strip_html(html_text: str) -> str:
    text = re.sub(r"<style[^>]*>.*?</style>", "", html_text, flags=re.DOTALL | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Kravextrahering ─────────────────────────────────────────────────────────

# Mönster som indikerar krav/skyldigheter i EU-lagtext (svenska och engelska)
OBLIGATION_PATTERNS_SV = [
    r"(?:ska|skall)\s+(?:säkerställa|se till|vidta|uppfylla|följa|tillämpa|anta|genomföra|lämna|underrätta|informera|rapportera|meddela|fastställa|ange|innehålla|upprätta|inrätta|utse|förse|tillhandahålla|övervaka|kontrollera|granska|bedöma|verifiera|dokumentera|registrera)",
    r"(?:ska|skall)\s+\w+(?:a|as|s)\b",
    r"är\s+skyldiga?\s+att",
    r"(?:måste|bör)\s+\w+",
    r"åligger\s+(?:det\s+)?",
    r"(?:ansvarar?|ansvariga?)\s+för\s+att",
    r"(?:krävs|fordras)\s+att",
    r"(?:i\s+enlighet|i\s+överensstämmelse)\s+med",
]

OBLIGATION_PATTERNS_EN = [
    r"shall\s+(?:ensure|provide|establish|take|adopt|apply|submit|notify|inform|report|set up|designate|verify|document|register|maintain|implement|comply)",
    r"shall\s+\w+",
    r"(?:must|is required to|are required to)\s+\w+",
    r"(?:obliged?|obligation)\s+to\s+\w+",
    r"shall\s+be\s+(?:responsible|liable|required)",
]


def extract_obligations(text: str) -> list[dict]:
    """
    Extrahera krav/skyldigheter ur lagtext.
    Returnerar lista med {article, text, pattern_type}.
    """
    obligations = []
    seen = set()

    # Dela upp i stycken
    paragraphs = re.split(r"\n\s*\n", text)

    # Håll reda på aktuell artikel
    current_article = ""
    article_pattern = re.compile(
        r"(?:Artikel|Article)\s+(\d+(?:\.\d+)?)", re.IGNORECASE
    )

    for para in paragraphs:
        para_stripped = para.strip()
        if not para_stripped:
            continue

        # Kolla om det är en artikelrubrik
        art_match = article_pattern.search(para_stripped)
        if art_match:
            current_article = f"Artikel {art_match.group(1)}"

        # Dela stycket i meningar
        sentences = re.split(r"(?<=[.;])\s+", para_stripped)

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 15:
                continue

            # Testa svenska mönster
            for pattern in OBLIGATION_PATTERNS_SV:
                if re.search(pattern, sentence, re.IGNORECASE):
                    key = sentence[:80]
                    if key not in seen:
                        seen.add(key)
                        obligations.append(
                            {
                                "article": current_article,
                                "text": sentence,
                                "lang": "SV",
                            }
                        )
                    break

            # Testa engelska mönster
            for pattern in OBLIGATION_PATTERNS_EN:
                if re.search(pattern, sentence, re.IGNORECASE):
                    key = sentence[:80]
                    if key not in seen:
                        seen.add(key)
                        obligations.append(
                            {
                                "article": current_article,
                                "text": sentence,
                                "lang": "EN",
                            }
                        )
                    break

    return obligations


# ── GUI ─────────────────────────────────────────────────────────────────────

class EULagTexterGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EU-lagtexter — Sök & Analysera")
        self.root.geometry("1400x900")
        self.root.minsize(1000, 700)

        # Data
        self.search_results: list[Document] = []
        self.selected_docs: list[Document] = []
        self.sort_column = "date"
        self.sort_reverse = True

        self._build_ui()
        self._status("Redo. Ange sökkriterier och klicka Sök.")

    # ── UI-byggnad ──────────────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        # Huvudcontainer med PanedWindow (vänster: sök+resultat, höger: valda+krav)
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

        # Statusfält
        self.status_var = tk.StringVar()
        status_bar = ttk.Label(
            self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W
        )
        status_bar.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=2)

    def _build_search_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Sök dokument", padding=8)
        frame.pack(fill=tk.X, padx=5, pady=(5, 2))

        # Rad 1: Typ + År
        row1 = ttk.Frame(frame)
        row1.pack(fill=tk.X, pady=2)

        ttk.Label(row1, text="Typ:").pack(side=tk.LEFT, padx=(0, 4))
        self.type_var = tk.StringVar(value="Alla")
        type_combo = ttk.Combobox(
            row1,
            textvariable=self.type_var,
            values=["Alla", "REG — Förordning", "DIR — Direktiv", "DEC — Beslut", "RECO — Rekommendation"],
            state="readonly",
            width=22,
        )
        type_combo.pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(row1, text="År:").pack(side=tk.LEFT, padx=(0, 4))
        self.year_var = tk.StringVar()
        year_entry = ttk.Entry(row1, textvariable=self.year_var, width=8)
        year_entry.pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(row1, text="Max antal:").pack(side=tk.LEFT, padx=(0, 4))
        self.limit_var = tk.StringVar(value="50")
        limit_entry = ttk.Entry(row1, textvariable=self.limit_var, width=5)
        limit_entry.pack(side=tk.LEFT)

        # Rad 2: Nyckelord + Sök-knapp
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
        frame = ttk.LabelFrame(parent, text="Sökresultat — Tillgängliga dokument", padding=4)
        frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)

        # Treeview med kolumner
        columns = ("celex", "type", "date", "title")
        self.results_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="extended"
        )

        self.results_tree.heading("celex", text="CELEX-nr", command=lambda: self._sort_results("celex"))
        self.results_tree.heading("type", text="Typ", command=lambda: self._sort_results("doc_type"))
        self.results_tree.heading("date", text="Datum", command=lambda: self._sort_results("date"))
        self.results_tree.heading("title", text="Titel", command=lambda: self._sort_results("title"))

        self.results_tree.column("celex", width=130, minwidth=100)
        self.results_tree.column("type", width=90, minwidth=70)
        self.results_tree.column("date", width=90, minwidth=80)
        self.results_tree.column("title", width=500, minwidth=200)

        # Scrollbars
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.results_tree.yview)
        hsb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.results_tree.xview)
        self.results_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.results_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        # Tooltip för full titel
        self.results_tree.bind("<Motion>", self._on_results_motion)
        self.results_tree.bind("<Leave>", self._hide_tooltip)
        self._tooltip = None
        self._tooltip_label = None

        # Knappar
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        self.add_btn = ttk.Button(
            btn_frame, text="Lägg till valda ▶", command=self._add_selected
        )
        self.add_btn.pack(side=tk.LEFT, padx=4)

        self.add_all_btn = ttk.Button(
            btn_frame, text="Lägg till alla ▶▶", command=self._add_all
        )
        self.add_all_btn.pack(side=tk.LEFT, padx=4)

        self.result_count_var = tk.StringVar(value="0 dokument")
        ttk.Label(btn_frame, textvariable=self.result_count_var).pack(side=tk.RIGHT, padx=4)

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

        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.selected_tree.yview)
        hsb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.selected_tree.xview)
        self.selected_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.selected_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        # Tooltip för full titel
        self.selected_tree.bind("<Motion>", self._on_selected_motion)
        self.selected_tree.bind("<Leave>", self._hide_tooltip)

        # Knappar
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        self.remove_btn = ttk.Button(
            btn_frame, text="◀ Ta bort valda", command=self._remove_selected
        )
        self.remove_btn.pack(side=tk.LEFT, padx=4)

        self.remove_all_btn = ttk.Button(
            btn_frame, text="◀◀ Ta bort alla", command=self._remove_all
        )
        self.remove_all_btn.pack(side=tk.LEFT, padx=4)

        self.extract_btn = ttk.Button(
            btn_frame,
            text="Extrahera krav",
            command=self._extract_all_obligations,
        )
        self.extract_btn.pack(side=tk.RIGHT, padx=4)

        self.view_text_btn = ttk.Button(
            btn_frame, text="Visa fulltext", command=self._view_full_text
        )
        self.view_text_btn.pack(side=tk.RIGHT, padx=4)

        self.selected_count_var = tk.StringVar(value="0 dokument")
        ttk.Label(btn_frame, textvariable=self.selected_count_var).pack(side=tk.RIGHT, padx=8)

    def _build_obligations_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Extraherade krav (obligations)", padding=4)
        frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=2)

        # Treeview för krav
        columns = ("doc", "article", "obligation")
        self.oblig_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )

        self.oblig_tree.heading("doc", text="Dokument")
        self.oblig_tree.heading("article", text="Artikel")
        self.oblig_tree.heading("obligation", text="Krav / Skyldighet")

        self.oblig_tree.column("doc", width=130, minwidth=100)
        self.oblig_tree.column("article", width=80, minwidth=60)
        self.oblig_tree.column("obligation", width=500, minwidth=200)

        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.oblig_tree.yview)
        hsb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.oblig_tree.xview)
        self.oblig_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.oblig_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        # Klicka för att se full kravtext
        self.oblig_tree.bind("<Double-1>", self._show_obligation_detail)
        self.oblig_tree.bind("<Motion>", self._on_oblig_motion)
        self.oblig_tree.bind("<Leave>", self._hide_tooltip)

        # Knappar
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        self.export_btn = ttk.Button(
            btn_frame, text="Exportera krav till fil", command=self._export_obligations
        )
        self.export_btn.pack(side=tk.LEFT, padx=4)

        self.oblig_count_var = tk.StringVar(value="0 krav")
        ttk.Label(btn_frame, textvariable=self.oblig_count_var).pack(side=tk.RIGHT, padx=4)

    # ── Tooltip ─────────────────────────────────────────────────────────

    def _show_tooltip(self, widget, text, x, y):
        self._hide_tooltip()
        if not text:
            return
        self._tooltip = tk.Toplevel(widget)
        self._tooltip.wm_overrideredirect(True)
        self._tooltip.wm_geometry(f"+{x + 15}+{y + 10}")

        self._tooltip_label = tk.Label(
            self._tooltip,
            text=text,
            justify=tk.LEFT,
            background="#ffffe0",
            relief=tk.SOLID,
            borderwidth=1,
            font=("Segoe UI", 9),
            wraplength=600,
        )
        self._tooltip_label.pack()

    def _hide_tooltip(self, event=None):
        if self._tooltip:
            self._tooltip.destroy()
            self._tooltip = None

    def _on_results_motion(self, event):
        item = self.results_tree.identify_row(event.y)
        col = self.results_tree.identify_column(event.x)
        if item and col == "#4":  # Titel-kolumnen
            values = self.results_tree.item(item, "values")
            if values and len(values) >= 4:
                self._show_tooltip(
                    self.results_tree, values[3],
                    event.x_root, event.y_root,
                )
                return
        self._hide_tooltip()

    def _on_selected_motion(self, event):
        item = self.selected_tree.identify_row(event.y)
        col = self.selected_tree.identify_column(event.x)
        if item and col == "#4":
            values = self.selected_tree.item(item, "values")
            if values and len(values) >= 4:
                self._show_tooltip(
                    self.selected_tree, values[3],
                    event.x_root, event.y_root,
                )
                return
        self._hide_tooltip()

    def _on_oblig_motion(self, event):
        item = self.oblig_tree.identify_row(event.y)
        col = self.oblig_tree.identify_column(event.x)
        if item and col == "#3":
            values = self.oblig_tree.item(item, "values")
            if values and len(values) >= 3:
                self._show_tooltip(
                    self.oblig_tree, values[2],
                    event.x_root, event.y_root,
                )
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
            messagebox.showwarning("Sök", "Ange minst ett sökkriterium (typ, år, eller nyckelord).")
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
                    0, lambda: messagebox.showerror("Sökfel", f"Kunde inte söka:\n{e}")
                )
            finally:
                self.root.after(0, lambda: self.search_btn.configure(state="normal"))

        threading.Thread(target=_search, daemon=True).start()

    def _show_results(self, docs: list[Document]):
        self.search_results = docs
        self._refresh_results_tree()
        self._status(f"Hittade {len(docs)} dokument.")

    def _refresh_results_tree(self):
        self.results_tree.delete(*self.results_tree.get_children())
        for doc in self.search_results:
            self.results_tree.insert(
                "",
                tk.END,
                iid=doc.celex,
                values=(doc.celex, doc.doc_type, doc.date, doc.title),
            )
        self.result_count_var.set(f"{len(self.search_results)} dokument")

    # ── Sortering ───────────────────────────────────────────────────────

    def _sort_results(self, column: str):
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
        key_fn = key_map.get(column, lambda d: d.celex)
        self.search_results.sort(key=key_fn, reverse=self.sort_reverse)
        self._refresh_results_tree()

        arrow = " ▼" if self.sort_reverse else " ▲"
        heading_map = {"celex": "CELEX-nr", "doc_type": "Typ", "date": "Datum", "title": "Titel"}
        for col, name in heading_map.items():
            suffix = arrow if col == column else ""
            tree_col = {"celex": "celex", "doc_type": "type", "date": "date", "title": "title"}[col]
            self.results_tree.heading(tree_col, text=name + suffix)

    # ── Lägg till / ta bort ─────────────────────────────────────────────

    def _add_selected(self):
        selection = self.results_tree.selection()
        if not selection:
            messagebox.showinfo("Lägg till", "Välj dokument i sökresultaten först.")
            return
        added = 0
        for iid in selection:
            doc = next((d for d in self.search_results if d.celex == iid), None)
            if doc and doc not in self.selected_docs:
                self.selected_docs.append(doc)
                added += 1
        self._refresh_selected_tree()
        self._status(f"La till {added} dokument.")

    def _add_all(self):
        added = 0
        for doc in self.search_results:
            if doc not in self.selected_docs:
                self.selected_docs.append(doc)
                added += 1
        self._refresh_selected_tree()
        self._status(f"La till {added} dokument.")

    def _remove_selected(self):
        selection = self.selected_tree.selection()
        if not selection:
            messagebox.showinfo("Ta bort", "Välj dokument att ta bort.")
            return
        self.selected_docs = [
            d for d in self.selected_docs if d.celex not in selection
        ]
        self._refresh_selected_tree()
        # Rensa krav som tillhör borttagna dokument
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
                "",
                tk.END,
                iid=doc.celex,
                values=(doc.celex, doc.doc_type, doc.date, doc.title),
            )
        self.selected_count_var.set(f"{len(self.selected_docs)} dokument")

    # ── Visa fulltext ───────────────────────────────────────────────────

    def _view_full_text(self):
        selection = self.selected_tree.selection()
        if not selection:
            messagebox.showinfo("Visa text", "Välj ett dokument i listan.")
            return
        celex = selection[0]
        doc = next((d for d in self.selected_docs if d.celex == celex), None)
        if not doc:
            return

        self._status(f"Hämtar fulltext för {celex}...")
        self.view_text_btn.configure(state="disabled")

        def _fetch():
            try:
                text = fetch_text(celex, lang="SV")
                if len(text) < 100:
                    text = fetch_text(celex, lang="EN")
                doc.full_text = text
                self.root.after(0, lambda: self._show_text_window(doc))
            except Exception as e:
                self.root.after(
                    0,
                    lambda: messagebox.showerror("Fel", f"Kunde inte hämta text:\n{e}"),
                )
            finally:
                self.root.after(0, lambda: self.view_text_btn.configure(state="normal"))
                self.root.after(0, lambda: self._status("Redo."))

        threading.Thread(target=_fetch, daemon=True).start()

    def _show_text_window(self, doc: Document):
        win = tk.Toplevel(self.root)
        win.title(f"{doc.celex} — {doc.title}")
        win.geometry("900x700")

        # Titel
        title_lbl = ttk.Label(win, text=doc.title, wraplength=860, style="Title.TLabel")
        title_lbl.pack(padx=10, pady=(10, 5), anchor=tk.W)

        info_lbl = ttk.Label(win, text=f"CELEX: {doc.celex}  |  Datum: {doc.date}  |  Typ: {doc.doc_type}")
        info_lbl.pack(padx=10, pady=(0, 5), anchor=tk.W)

        ttk.Separator(win, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=5)

        text_widget = scrolledtext.ScrolledText(
            win, wrap=tk.WORD, font=("Consolas", 10), padx=10, pady=10
        )
        text_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        text_widget.insert(tk.END, doc.full_text)
        text_widget.configure(state="disabled")

    # ── Kravextrahering ─────────────────────────────────────────────────

    def _extract_all_obligations(self):
        if not self.selected_docs:
            messagebox.showinfo("Krav", "Lägg till dokument först.")
            return

        self.extract_btn.configure(state="disabled")
        self._status("Hämtar och analyserar dokument...")

        def _work():
            total = 0
            for doc in self.selected_docs:
                if not doc.full_text:
                    try:
                        doc.full_text = fetch_text(doc.celex, lang="SV")
                        if len(doc.full_text) < 100:
                            doc.full_text = fetch_text(doc.celex, lang="EN")
                    except Exception:
                        continue
                doc.obligations = extract_obligations(doc.full_text)
                total += len(doc.obligations)
                self.root.after(
                    0,
                    lambda d=doc, t=total: self._status(
                        f"Analyserat {d.celex}: {len(d.obligations)} krav (totalt {t})"
                    ),
                )

            self.root.after(0, lambda: self._refresh_obligations_tree())
            self.root.after(0, lambda: self._status(f"Klar — {total} krav extraherade."))
            self.root.after(0, lambda: self.extract_btn.configure(state="normal"))

        threading.Thread(target=_work, daemon=True).start()

    def _refresh_obligations_tree(self):
        self.oblig_tree.delete(*self.oblig_tree.get_children())
        selected_celex = {d.celex for d in self.selected_docs}
        count = 0
        for doc in self.selected_docs:
            if doc.celex not in selected_celex:
                continue
            for i, obl in enumerate(doc.obligations):
                iid = f"{doc.celex}__{i}"
                self.oblig_tree.insert(
                    "",
                    tk.END,
                    iid=iid,
                    values=(doc.celex, obl["article"], obl["text"]),
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
        win.title(f"Krav — {values[0]} {values[1]}")
        win.geometry("700x300")

        ttk.Label(win, text=f"Dokument: {values[0]}", font=("Segoe UI", 10, "bold")).pack(
            padx=10, pady=(10, 2), anchor=tk.W
        )
        ttk.Label(win, text=f"Artikel: {values[1]}").pack(padx=10, pady=2, anchor=tk.W)
        ttk.Separator(win, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=5)

        text_widget = scrolledtext.ScrolledText(
            win, wrap=tk.WORD, font=("Segoe UI", 10), padx=10, pady=10
        )
        text_widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        text_widget.insert(tk.END, values[2])
        text_widget.configure(state="disabled")

    # ── Export ──────────────────────────────────────────────────────────

    def _export_obligations(self):
        if not any(d.obligations for d in self.selected_docs):
            messagebox.showinfo("Export", "Inga krav att exportera. Kör extraheringen först.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Textfil", "*.txt"), ("CSV", "*.csv"), ("Alla filer", "*.*")],
            title="Spara krav",
        )
        if not path:
            return

        with open(path, "w", encoding="utf-8") as f:
            if path.endswith(".csv"):
                f.write("Dokument\tArtikel\tKrav\n")
                for doc in self.selected_docs:
                    for obl in doc.obligations:
                        text = obl["text"].replace("\t", " ").replace("\n", " ")
                        f.write(f"{doc.celex}\t{obl['article']}\t{text}\n")
            else:
                for doc in self.selected_docs:
                    if not doc.obligations:
                        continue
                    f.write(f"{'═' * 80}\n")
                    f.write(f"Dokument: {doc.celex}\n")
                    f.write(f"Titel:    {doc.title}\n")
                    f.write(f"Datum:    {doc.date}\n")
                    f.write(f"{'─' * 80}\n\n")
                    for i, obl in enumerate(doc.obligations, 1):
                        f.write(f"  [{i}] {obl['article']}\n")
                        f.write(f"      {obl['text']}\n\n")

        self._status(f"Exporterat till {path}")
        messagebox.showinfo("Export", f"Krav exporterade till:\n{path}")

    # ── Status ──────────────────────────────────────────────────────────

    def _status(self, text: str):
        self.status_var.set(text)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    app = EULagTexterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
