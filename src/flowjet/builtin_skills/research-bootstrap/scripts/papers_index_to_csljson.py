#!/usr/bin/env python3
"""OMR papers-index.json -> ARS Material Passport ``literature_corpus[]``.

Deterministic, stdlib-only, no network, no LLM. Reads Oh-My-Research's paper
index (``docs/index/papers-index.json``) and an optional ``_citation-map.md``
enrichment, then emits a JSON passport (JSON is valid YAML, so ARS's YAML
loaders accept it). Entries missing a derivable ``year`` or ``authors`` are
written to ``rejection_log.yaml`` — never coerced to placeholders (mirrors ARS's
own adapter discipline).

Metadata provenance (never invented):
  * ``citation_key``, ``title``, ``source_pointer`` — from the OMR index.
  * ``arxiv_id`` / ``doi`` — regex-derived from the ``source`` URL.
  * ``year`` — from the citation map if present, else derived from ``arxiv_id``
    (``2401.12345`` -> 2024). No source and no citation map -> rejected.
  * ``authors`` / ``venue`` — from the citation map only.

Usage:
  python papers_index_to_csljson.py --workspace <omr-workspace> \
      [--citation-map docs/<mode>/_citation-map.md] [--output passport.json]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_ARXIV_NEW = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5})(?:v\d+)?")
_ARXIV_LEGACY = re.compile(r"arxiv\.org/abs/([A-Za-z][A-Za-z0-9.-]*/\d{7}(?:v\d+)?)")
_DOI = re.compile(r"(?:doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s]+)")
_CITE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_:-]*$")


def load_papers_index(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "docs" / "index" / "papers-index.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return data.get("papers", [])


def parse_citation_map(path: Path | None) -> dict[str, dict[str, Any]]:
    """Parse OMR ``_citation-map.md`` rows into ``{id: {authors, year, venue, ref}}``.

    Row shape: ``| P-001 | (Smith, 2025) | Smith, J. (2025). Title. Venue. DOI |``
    """
    if not path or not path.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        pid, _cite, ref = cells[0], cells[1], cells[2]
        if not pid or pid.startswith("-") or pid.lower() in ("internal", "id"):
            continue
        result[pid] = {"ref": ref, **parse_reference(ref)}
    return result


def parse_reference(ref: str) -> dict[str, Any]:
    """Best-effort parse of an APA-ish reference string into authors/year/venue."""
    authors: list[dict[str, str]] = []
    year: int | None = None
    venue: str | None = None

    year_m = re.search(r"\((\d{4})\)", ref)
    if year_m:
        year = int(year_m.group(1))

    # "Family, G. (2025). Title. Venue. DOI" -> authors before the year paren.
    head = ref.split("(")[0].strip().rstrip("., ")
    for part in head.split("&"):
        part = part.strip().rstrip("., ")
        if not part:
            continue
        if " " in part and "," in part:
            family, _, given = part.partition(",")
            authors.append({"family": family.strip(), "given": given.strip()})
        else:
            authors.append({"literal": part})

    # Venue: first segment after "Title." heuristic is unreliable; leave as None.
    return {"authors": authors, "year": year, "venue": venue}


def derive_year(source: str) -> int | None:
    m = _ARXIV_NEW.search(source) or _ARXIV_LEGACY.search(source)
    if m:
        ident = m.group(1)
        if re.match(r"\d{4}\.\d{4,5}", ident):
            return 2000 + int(ident[:2])
    return None


def derive_identifiers(source: str) -> tuple[str | None, str | None]:
    arxiv_id = None
    m = _ARXIV_NEW.search(source) or _ARXIV_LEGACY.search(source)
    if m:
        arxiv_id = m.group(0).split("/")[-1] if "arxiv.org" in source else m.group(1)
    doi = None
    m = _DOI.search(source)
    if m:
        doi = m.group(1)
    return arxiv_id, doi


def sanitize_cite_key(key: str) -> str:
    if _CITE_KEY_RE.match(key):
        return key
    slug = re.sub(r"[^A-Za-z0-9_:-]+", "_", key).strip("_")
    return slug or f"entry_{abs(hash(key)) % 100000}"


def build_entry(paper: dict[str, Any], enrichment: dict[str, Any] | None) -> dict[str, Any] | None:
    pid = str(paper.get("id", ""))
    source = str(paper.get("source") or "")
    title = str(paper.get("title") or source)

    enrich = enrichment or {}
    arxiv_id, doi = derive_identifiers(source)

    year = enrich.get("year") or derive_year(source)
    authors = enrich.get("authors") or []

    if year is None or not authors:
        return None

    entry: dict[str, Any] = {
        "citation_key": sanitize_cite_key(pid),
        "title": title,
        "authors": authors,
        "year": int(year),
        "source_pointer": source or str(paper.get("path", "")),
        "obtained_via": "other",
        "adapter_name": "oh-my-research-bridge",
        "adapter_version": "0.1.0",
        "obtained_at": paper.get("collected_at"),
    }
    if doi:
        entry["doi"] = doi
    if arxiv_id:
        entry["arxiv_id"] = arxiv_id
    if enrich.get("venue"):
        entry["venue"] = enrich["venue"]
    return entry


def main() -> None:
    p = argparse.ArgumentParser(description="OMR papers-index -> ARS literature_corpus passport")
    p.add_argument("--workspace", type=Path, default=Path.cwd())
    p.add_argument("--citation-map", type=Path, default=None)
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args()

    ws = args.workspace.resolve()
    papers = load_papers_index(ws)
    enrich = parse_citation_map(args.citation_map.resolve() if args.citation_map else None)

    entries: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []

    for paper in papers:
        pid = str(paper.get("id", ""))
        entry = build_entry(paper, enrich.get(pid))
        if entry is None:
            rejections.append(
                {
                    "citation_key": sanitize_cite_key(pid),
                    "reason": "missing year or authors",
                    "note": "run OMR analyze to enrich, or provide --citation-map",
                }
            )
        else:
            entries.append(entry)

    out = (args.output or ws / "passport.json").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"literature_corpus": entries}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if rejections:
        log = (ws / "rejection_log.yaml").resolve()
        lines = ["# rejection log — entries missing year/authors", ""]
        for r in rejections:
            lines.append(f"- citation_key: {r['citation_key']}")
            lines.append(f"  reason: {r['reason']}")
            lines.append(f"  note: {r['note']}")
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({"emitted": len(entries), "rejected": len(rejections), "output": str(out)}, indent=2))


if __name__ == "__main__":
    main()
