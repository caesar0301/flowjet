#!/usr/bin/env python3
"""ARS Material Passport ``literature_corpus[]`` -> OMR papers-index + citation map.

Deterministic, stdlib-only, no network, no LLM. Reads an ARS passport (JSON, or
YAML when PyYAML is installed) and writes Oh-My-Research's ``docs/index/
papers-index.json`` (+ ``.md``) and a ``_citation-map.md``, keeping the
citation_key <-> material-ID mapping in lockstep.

Use this direction when ARS did the rigorous research and OMR writes the long
report: skip OMR ``collect``, fast-pass ``analyze`` (the judgment is ARS's
synthesis), then ``synth``.

Usage:
  python csljson_to_omr.py --passport passport.json --workspace <omr-workspace> \
      [--citation-map docs/<mode>/_citation-map.md]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_passport(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore[import-not-found]

            data = yaml.safe_load(text)
        except ImportError as exc:
            raise SystemExit("passport is YAML but PyYAML is not installed; pass JSON instead") from exc
    if isinstance(data, dict):
        return data.get("literature_corpus", [])
    if isinstance(data, list):
        return data
    return []


def csl_author_name(author: dict[str, Any]) -> str:
    if author.get("literal"):
        return str(author["literal"])
    family = author.get("family", "")
    given = author.get("given", "")
    return f"{family}, {given}".strip(", ")


def format_reference(entry: dict[str, Any]) -> str:
    authors = [csl_author_name(a) for a in entry.get("authors", [])]
    author_str = ", ".join(authors) if authors else "Unknown"
    year = entry.get("year", "n.d.")
    title = entry.get("title", "")
    venue = entry.get("venue", "")
    ref = f"{author_str} ({year}). {title}."
    if venue:
        ref += f" *{venue}*."
    ident = entry.get("doi") or entry.get("arxiv_id")
    if ident:
        ref += f" {ident}"
    return ref


def cite_label(entry: dict[str, Any]) -> str:
    authors = entry.get("authors", [])
    family = ""
    if authors:
        family = authors[0].get("family") or authors[0].get("literal", "")
    year = entry.get("year", "")
    return f"({family}, {year})" if family and year else f"({entry.get('citation_key', '?')})"


def main() -> None:
    p = argparse.ArgumentParser(description="ARS literature_corpus -> OMR papers-index")
    p.add_argument("--passport", type=Path, required=True)
    p.add_argument("--workspace", type=Path, default=Path.cwd())
    p.add_argument("--citation-map", type=Path, default=None)
    args = p.parse_args()

    ws = args.workspace.resolve()
    corpus = load_passport(args.passport.resolve())

    papers: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()

    for i, entry in enumerate(corpus, start=1):
        pid = f"P-{i:03d}"
        source = str(entry.get("source_pointer") or "")
        papers.append(
            {
                "id": pid,
                "source": source,
                "title": entry.get("title", ""),
                "collected_at": now,
            }
        )

    # papers-index.json + .md
    idx_path = ws / "docs" / "index" / "papers-index.json"
    existing = {"papers": [], "web": [], "github": [], "search": []}
    if idx_path.exists():
        try:
            loaded = json.loads(idx_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except json.JSONDecodeError:
            pass
    existing["papers"] = papers
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    md_path = ws / "docs" / "index" / "papers-index.md"
    lines = ["# Materials Index", "", "## papers"]
    for item in papers:
        lines.append(f"- [{item['id']}] {item.get('title')} — {item.get('source')}")
    lines.append("")
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    # citation map
    cm = (args.citation_map or ws / "docs" / "_citation-map.md").resolve()
    cm.parent.mkdir(parents=True, exist_ok=True)
    rows = ["| Internal | Public cite | Full reference |", "|----------|-------------|----------------|"]
    for item, entry in zip(papers, corpus):
        rows.append(f"| {item['id']} | {cite_label(entry)} | {format_reference(entry)} |")
    cm.write_text("\n".join(rows) + "\n", encoding="utf-8")

    print(json.dumps({"indexed": len(papers), "citation_map": str(cm)}, indent=2))


if __name__ == "__main__":
    main()
