#!/usr/bin/env python3
"""
EU-lagtexter — Verktyg för att söka och läsa lagtexter från EU-kommissionen.

Använder EUR-Lex SPARQL-endpoint för att söka dokument och
CELLAR REST API / EUR-Lex för att hämta fulltext.
"""

import urllib.request
import urllib.parse
import json
import html
import re
import sys
import textwrap


SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
EURLEX_TEXT_URL = "https://eur-lex.europa.eu/legal-content/{lang}/TXT/HTML/?uri=CELEX:{celex}"


def sparql_query(query: str) -> list[dict]:
    """Kör en SPARQL-fråga mot CELLAR-endpointen och returnera resultat."""
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        SPARQL_ENDPOINT,
        data=data,
        headers={"Accept": "application/sparql-results+json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    bindings = result.get("results", {}).get("bindings", [])
    return [{k: v["value"] for k, v in row.items()} for row in bindings]


def search_documents(doc_type: str = "", year: str = "", keyword: str = "", limit: int = 30) -> list[dict]:
    """Sök EU-dokument via SPARQL. Returnerar lista med celex och titel."""
    filters = []

    if doc_type:
        type_map = {
            "REG": "REG",
            "DIR": "DIR",
            "DEC": "DEC",
            "RECO": "RECO",
        }
        rtype = type_map.get(doc_type.upper(), doc_type.upper())
        filters.append(
            f'?work cdm:work_has_resource-type '
            f'<http://publications.europa.eu/resource/authority/resource-type/{rtype}> .'
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
  ?expr cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/SWE> .
  ?expr cdm:expression_title ?title .
  {filter_block}
}}
ORDER BY DESC(?date)
LIMIT {limit}
"""
    return sparql_query(query)


def fetch_text(celex: str, lang: str = "SV") -> str:
    """Hämta fulltext för ett dokument via EUR-Lex."""
    url = EURLEX_TEXT_URL.format(lang=lang, celex=celex)
    req = urllib.request.Request(url, headers={"User-Agent": "EU-Lagtexter/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return strip_html(raw)


def strip_html(html_text: str) -> str:
    """Enkel HTML-till-text-konvertering."""
    text = re.sub(r"<style[^>]*>.*?</style>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # Ta bort överflödiga blankrader
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def print_table(docs: list[dict]):
    """Skriv ut dokumentlista som numrerad tabell."""
    if not docs:
        print("\nInga dokument hittades.")
        return

    print(f"\n{'Nr':>4}  {'CELEX-nr':<16} {'Datum':<12} Titel")
    print("─" * 80)
    for i, doc in enumerate(docs, 1):
        title = doc.get("title", "—")
        if len(title) > 50:
            title = title[:47] + "..."
        celex = doc.get("celex", "—")
        date = doc.get("date", "—")[:10]
        print(f"{i:>4}  {celex:<16} {date:<12} {title}")


def display_menu():
    """Visa huvudmenyn."""
    print("\n╔══════════════════════════════════════════════════╗")
    print("║       EU-lagtexter — Sök & Läs lagtexter        ║")
    print("╠══════════════════════════════════════════════════╣")
    print("║  1. Sök efter dokumenttyp                        ║")
    print("║  2. Sök efter år                                 ║")
    print("║  3. Sök med nyckelord                            ║")
    print("║  4. Kombinerad sökning                           ║")
    print("║  5. Hämta dokument direkt (ange CELEX-nr)        ║")
    print("║  6. Avsluta                                      ║")
    print("╚══════════════════════════════════════════════════╝")


def choose_doc_type() -> str:
    """Låt användaren välja dokumenttyp."""
    print("\nDokumenttyper:")
    print("  REG  — Förordning (Regulation)")
    print("  DIR  — Direktiv (Directive)")
    print("  DEC  — Beslut (Decision)")
    print("  RECO — Rekommendation (Recommendation)")
    return input("Ange typ (eller tryck Enter för alla): ").strip()


def select_and_read(docs: list[dict]):
    """Låt användaren välja ett dokument från listan och läsa det."""
    if not docs:
        return

    while True:
        val = input("\nAnge nr för att läsa (eller 'b' för att gå tillbaka): ").strip()
        if val.lower() == "b":
            return
        try:
            idx = int(val) - 1
            if 0 <= idx < len(docs):
                celex = docs[idx]["celex"]
                print(f"\nHämtar {celex}...")
                try:
                    text = fetch_text(celex)
                    print("\n" + "═" * 80)
                    print(text)
                    print("═" * 80)

                    save = input("\nSpara till fil? (j/n): ").strip().lower()
                    if save == "j":
                        filename = f"{celex}.txt"
                        with open(filename, "w", encoding="utf-8") as f:
                            f.write(text)
                        print(f"Sparat till {filename}")
                except Exception as e:
                    print(f"Fel vid hämtning: {e}")

                    # Försök på engelska om svenska inte finns
                    try_en = input("Försök på engelska? (j/n): ").strip().lower()
                    if try_en == "j":
                        try:
                            text = fetch_text(celex, lang="EN")
                            print("\n" + "═" * 80)
                            print(text)
                            print("═" * 80)
                        except Exception as e2:
                            print(f"Kunde inte hämta: {e2}")
            else:
                print("Ogiltigt nummer.")
        except ValueError:
            print("Ange ett nummer eller 'b'.")


def main():
    print("Ansluter till EU:s CELLAR-databas...")

    while True:
        display_menu()
        choice = input("\nVälj (1-6): ").strip()

        if choice == "1":
            doc_type = choose_doc_type()
            print("\nSöker...")
            try:
                docs = search_documents(doc_type=doc_type)
                print_table(docs)
                select_and_read(docs)
            except Exception as e:
                print(f"Fel: {e}")

        elif choice == "2":
            year = input("Ange år (t.ex. 2024): ").strip()
            print("\nSöker...")
            try:
                docs = search_documents(year=year)
                print_table(docs)
                select_and_read(docs)
            except Exception as e:
                print(f"Fel: {e}")

        elif choice == "3":
            keyword = input("Ange sökord: ").strip()
            print("\nSöker...")
            try:
                docs = search_documents(keyword=keyword)
                print_table(docs)
                select_and_read(docs)
            except Exception as e:
                print(f"Fel: {e}")

        elif choice == "4":
            doc_type = choose_doc_type()
            year = input("Ange år (eller Enter för alla): ").strip()
            keyword = input("Ange sökord (eller Enter för alla): ").strip()
            print("\nSöker...")
            try:
                docs = search_documents(doc_type=doc_type, year=year, keyword=keyword)
                print_table(docs)
                select_and_read(docs)
            except Exception as e:
                print(f"Fel: {e}")

        elif choice == "5":
            celex = input("Ange CELEX-nr (t.ex. 32016R0679): ").strip()
            if celex:
                print(f"\nHämtar {celex}...")
                try:
                    text = fetch_text(celex)
                    print("\n" + "═" * 80)
                    print(text)
                    print("═" * 80)

                    save = input("\nSpara till fil? (j/n): ").strip().lower()
                    if save == "j":
                        filename = f"{celex}.txt"
                        with open(filename, "w", encoding="utf-8") as f:
                            f.write(text)
                        print(f"Sparat till {filename}")
                except Exception as e:
                    print(f"Fel: {e}")

        elif choice == "6":
            print("Hej då!")
            break

        else:
            print("Ogiltigt val.")


if __name__ == "__main__":
    main()
