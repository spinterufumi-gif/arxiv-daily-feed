#!/usr/bin/env python3
"""
Fetch a daily arXiv feed, convert Atom XML to JSON, and save it for the iPhone app.

This version is intentionally conservative:
- one arXiv API request per successful run
- optional cooldown so repeated manual runs do not hammer arXiv
- retries with long timeouts for temporary read timeouts
- existing public/papers.json is kept on HTTP 429/503 or timeouts
"""
from __future__ import annotations

import json
import os
import socket
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
MAX_RESULTS = int(os.environ.get("ARXIV_MAX_RESULTS", "50"))

ARXIV_API_URL = "https://export.arxiv.org/api/query"
CONTACT = os.environ.get("ARXIV_CONTACT", "personal-use-no-contact-set")
USER_AGENT = f"ArxivDailyReader/0.7 personal GitHub Actions feed ({CONTACT})"

# Network safety settings.
HTTP_TIMEOUT_SECONDS = int(os.environ.get("ARXIV_HTTP_TIMEOUT_SECONDS", "180"))
RETRY_DELAYS_SECONDS = [0, 90, 300]
MIN_FETCH_INTERVAL_SECONDS = int(os.environ.get("ARXIV_MIN_FETCH_INTERVAL_SECONDS", str(6 * 60 * 60)))
FORCE_FETCH = os.environ.get("FORCE_FETCH", "false").lower() in {"1", "true", "yes"}

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


class ArxivTemporaryFailure(Exception):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def build_query() -> str:
    return " OR ".join(f"cat:{cat}" for cat in CATEGORIES)


def write_status(**kwargs) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(kwargs, ensure_ascii=False, indent=2), encoding="utf-8")


def read_previous_status() -> dict:
    if not STATUS_PATH.exists():
        return {}
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def should_skip_for_cooldown() -> tuple[bool, str | None]:
    if FORCE_FETCH or not OUTPUT_PATH.exists():
        return False, None
    status = read_previous_status()
    previous_started = parse_iso_utc(status.get("startedAt"))
    previous_finished = parse_iso_utc(status.get("finishedAt"))
    last_time = previous_finished or previous_started
    if last_time is None:
        return False, None
    elapsed = (utc_now() - last_time).total_seconds()
    if elapsed < MIN_FETCH_INTERVAL_SECONDS:
        remaining_min = int((MIN_FETCH_INTERVAL_SECONDS - elapsed + 59) // 60)
        return True, f"Skipped to avoid repeated arXiv access. Try again after about {remaining_min} minutes, or use force=true from Run workflow."
    return False, None


def request_once() -> bytes:
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
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as res:
            return res.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (429, 503):
            raise ArxivRateLimited(exc.code, exc.headers.get("Retry-After")) from exc
        raise
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        raise ArxivTemporaryFailure(str(exc)) from exc


def fetch_atom() -> bytes:
    last_error: Exception | None = None
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        if delay:
            print(f"Waiting {delay} seconds before retry {attempt}...")
            time.sleep(delay)
        try:
            print(f"Fetching arXiv feed, attempt {attempt}, max_results={MAX_RESULTS}, timeout={HTTP_TIMEOUT_SECONDS}s")
            return request_once()
        except ArxivRateLimited:
            raise
        except ArxivTemporaryFailure as exc:
            last_error = exc
            print(f"Temporary arXiv/network failure on attempt {attempt}: {exc}", file=sys.stderr)
    raise ArxivTemporaryFailure(f"The read operation timed out or failed after {len(RETRY_DELAYS_SECONDS)} attempts: {last_error}")


def text_or_none(parent: ET.Element, tag: str) -> str | None:
    item = parent.find(tag)
    if item is None or item.text is None:
        return None
    return " ".join(item.text.split())


def normalize_arxiv_id(entry_id: str) -> str:
    return entry_id.rstrip("/").split("/abs/")[-1]


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

    skip, reason = should_skip_for_cooldown()
    if skip:
        write_status(
            ok=True,
            skipped=True,
            rateLimited=False,
            startedAt=started_at,
            finishedAt=utc_now_iso(),
            previousFeedExists=OUTPUT_PATH.exists(),
            message=reason,
        )
        print(reason)
        return 0

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
            skipped=False,
            rateLimited=False,
            startedAt=started_at,
            finishedAt=utc_now_iso(),
            paperCount=len(papers),
            message="updated",
        )
        print(f"Wrote {OUTPUT_PATH} with {len(papers)} papers")
        return 0

    except ArxivRateLimited as exc:
        previous_feed_exists = OUTPUT_PATH.exists()
        write_status(
            ok=False,
            skipped=False,
            rateLimited=True,
            startedAt=started_at,
            finishedAt=utc_now_iso(),
            previousFeedExists=previous_feed_exists,
            message=str(exc),
            suggestion="arXiv returned 429/503. Do not re-run repeatedly. Wait at least several hours, preferably until the next scheduled run.",
        )
        print(f"Rate limited: {exc}", file=sys.stderr)
        print("Kept existing public/papers.json. Exiting with success so the workflow can commit feed_status.json.")
        return 0

    except ArxivTemporaryFailure as exc:
        previous_feed_exists = OUTPUT_PATH.exists()
        write_status(
            ok=False,
            skipped=False,
            rateLimited=False,
            timedOut=True,
            startedAt=started_at,
            finishedAt=utc_now_iso(),
            previousFeedExists=previous_feed_exists,
            message=str(exc),
            suggestion="arXiv or the network did not respond in time. Existing papers.json was kept. Avoid repeated manual re-runs; try later or use the next scheduled run.",
        )
        print(f"Temporary failure: {exc}", file=sys.stderr)
        print("Kept existing public/papers.json. Exiting with success so the workflow can commit feed_status.json.")
        return 0

    except Exception as exc:
        write_status(
            ok=False,
            skipped=False,
            rateLimited=False,
            startedAt=started_at,
            finishedAt=utc_now_iso(),
            message=str(exc),
        )
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
