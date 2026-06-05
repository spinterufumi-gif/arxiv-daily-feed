#!/usr/bin/env python3
"""
Fetch a daily arXiv feed once, convert Atom XML to JSON, and save it for the iPhone app.
This script intentionally makes only one arXiv API request per run.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_DIR = Path("public")
OUTPUT_PATH = OUTPUT_DIR / "papers.json"
STATUS_PATH = OUTPUT_DIR / "feed_status.json"

# Edit these defaults if you want a broader or narrower daily feed.
CATEGORIES = [
    "cond-mat.mes-hall",
    "cond-mat.mtrl-sci",
    "cond-mat.str-el",
    "cond-mat.supr-con",
    "cond-mat.stat-mech",
    "nlin.PS",
    "physics.comp-ph",
]
MAX_RESULTS = 100

ARXIV_API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "ArxivDailyReader/0.5 personal GitHub Actions feed (contact: add-your-email@example.com)"

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_query() -> str:
    return " OR ".join(f"cat:{cat}" for cat in CATEGORIES)


def fetch_atom() -> bytes:
    params = {
        "search_query": build_query(),
        "start": "0",
        "max_results": str(MAX_RESULTS),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = ARXIV_API_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read()


def text_or_none(parent: ET.Element, tag: str) -> str | None:
    item = parent.find(tag)
    if item is None or item.text is None:
        return None
    return " ".join(item.text.split())


def normalize_arxiv_id(entry_id: str) -> str:
    # entry_id examples:
    #   http://arxiv.org/abs/2401.01234v1
    #   https://arxiv.org/abs/cond-mat/9901001v2
    arxiv_id = entry_id.rstrip("/").split("/abs/")[-1]
    return arxiv_id


def parse_atom(data: bytes) -> list[dict]:
    root = ET.fromstring(data)
    papers: list[dict] = []

    for entry in root.findall(f"{ATOM}entry"):
        entry_id = text_or_none(entry, f"{ATOM}id") or ""
        arxiv_id = normalize_arxiv_id(entry_id)
        title = text_or_none(entry, f"{ATOM}title") or "Untitled"
        summary = text_or_none(entry, f"{ATOM}summary") or ""
        published = text_or_none(entry, f"{ATOM}published")
        updated = text_or_none(entry, f"{ATOM}updated")

        authors = []
        for author in entry.findall(f"{ATOM}author"):
            name = text_or_none(author, f"{ATOM}name")
            if name:
                authors.append(name)

        categories = []
        primary = entry.find(f"{ARXIV}primary_category")
        if primary is not None and primary.attrib.get("term"):
            categories.append(primary.attrib["term"])
        for category in entry.findall(f"{ATOM}category"):
            term = category.attrib.get("term")
            if term and term not in categories:
                categories.append(term)

        abstract_url = entry_id if entry_id else f"https://arxiv.org/abs/{arxiv_id}"
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        for link in entry.findall(f"{ATOM}link"):
            href = link.attrib.get("href")
            title_attr = link.attrib.get("title", "").lower()
            rel = link.attrib.get("rel", "").lower()
            link_type = link.attrib.get("type", "").lower()
            if href and (title_attr == "pdf" or link_type == "application/pdf"):
                pdf_url = href
            elif href and rel == "alternate":
                abstract_url = href

        papers.append(
            {
                "arxivID": arxiv_id,
                "title": title,
                "authors": authors,
                "summary": summary,
                "published": published,
                "updated": updated,
                "categories": categories,
                "abstractURL": abstract_url,
                "pdfURL": pdf_url,
                "score": 0.0,
            }
        )

    return papers


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started_at = utc_now_iso()

    try:
        # The workflow calls arXiv once per run. This small delay helps if the job is manually re-run.
        time.sleep(3)
        atom = fetch_atom()
        papers = parse_atom(atom)
        payload = {
            "generatedAt": utc_now_iso(),
            "source": "arXiv API via GitHub Actions",
            "query": build_query(),
            "maxResults": MAX_RESULTS,
            "papers": papers,
        }
        OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        STATUS_PATH.write_text(
            json.dumps(
                {
                    "ok": True,
                    "startedAt": started_at,
                    "finishedAt": utc_now_iso(),
                    "paperCount": len(papers),
                    "message": "updated",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Wrote {OUTPUT_PATH} with {len(papers)} papers")
        return 0
    except Exception as exc:
        STATUS_PATH.write_text(
            json.dumps(
                {
                    "ok": False,
                    "startedAt": started_at,
                    "finishedAt": utc_now_iso(),
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
