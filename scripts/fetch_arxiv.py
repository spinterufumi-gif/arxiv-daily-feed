#!/usr/bin/env python3
"""
Fetch a daily arXiv feed once, convert Atom XML to JSON, and save it for the iPhone app.

This version is intentionally conservative:
- one arXiv API request per successful run
- no failure of the whole GitHub Actions job on HTTP 429
- existing public/papers.json is kept when arXiv rate-limits the request
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
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
MAX_RESULTS = 75

ARXIV_API_URL = "https://export.arxiv.org/api/query"
CONTACT = os.environ.get("ARXIV_CONTACT", "personal-use-no-contact-set")
USER_AGENT = f"ArxivDailyReader/0.6 personal GitHub Actions feed ({CONTACT})"

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class ArxivRateLimited(Exception):
    def __init__(self, status_code: int, retry_after: str | None = None):
        self.status_code = status_code
        self.retry_after = retry_after
        msg = f"HTTP {status_code}: arXiv rate-limited or temporarily refused the request"
        if retry_after:
            msg += f"; Retry-After={retry_after}"
        super().__init__(msg)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_query() -> str:
    return " OR ".join(f"cat:{cat}" for cat in CATEGORIES)


def write_status(**kwargs) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(kwargs, ensure_ascii=False, indent=2), encoding="utf-8")


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
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (429, 503):
            raise ArxivRateLimited(exc.code, exc.headers.get("Retry-After")) from exc
        raise


def text_or_none(parent: ET.Element, tag: str) -> str | None:
    item = parent.find(tag)
    if item is None or item.text is None:
        return None
    return " ".join(item.text.split())


def normalize_arxiv_id(entry_id: str) -> str:
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
        # A small delay helps when a manual re-run is triggered immediately after another run.
        time.sleep(5)
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
        write_status(
            ok=True,
            rateLimited=False,
            startedAt=started_at,
            finishedAt=utc_now_iso(),
            paperCount=len(papers),
            message="updated",
        )
        print(f"Wrote {OUTPUT_PATH} with {len(papers)} papers")
        return 0

    except ArxivRateLimited as exc:
        # Important: do not delete or overwrite papers.json here.
        # The iPhone app can continue using the previous successful feed.
        previous_feed_exists = OUTPUT_PATH.exists()
        write_status(
            ok=False,
            rateLimited=True,
            startedAt=started_at,
            finishedAt=utc_now_iso(),
            previousFeedExists=previous_feed_exists,
            message=str(exc),
            suggestion="arXiv returned 429/503. Do not re-run repeatedly. Wait at least 30-60 minutes, preferably until the next scheduled run.",
        )
        print(f"Rate limited: {exc}", file=sys.stderr)
        print("Kept existing public/papers.json. Exiting with success so the workflow can commit feed_status.json.")
        return 0

    except Exception as exc:
        write_status(
            ok=False,
            rateLimited=False,
            startedAt=started_at,
            finishedAt=utc_now_iso(),
            message=str(exc),
        )
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
