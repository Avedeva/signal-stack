from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from signal_stack.services.security import FetchResult, normalize_url


@dataclass
class ExtractedDocument:
    url: str
    title: str
    content: str
    published_at: datetime | None = None
    # Keep the original fifth positional argument compatible with file callers.
    emit_signal: bool = True
    modified_at: datetime | None = None
    event_date: datetime | None = None
    is_article: bool = False


def content_hash(content: str) -> str:
    normalized = " ".join(content.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo:
        result = result.astimezone(timezone.utc).replace(tzinfo=None)
    return result


def _jsonld_dates(soup: BeautifulSoup) -> tuple[datetime | None, datetime | None, datetime | None, bool]:
    published = modified = event_date = None
    is_article = False

    def visit(value):
        nonlocal published, modified, event_date, is_article
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if any("article" in str(kind).casefold() or str(kind).casefold() in {"blogposting", "newsarticle"} for kind in types):
                is_article = True
            if published is None:
                published = _date(value.get("datePublished") or value.get("uploadDate"))
            if modified is None:
                modified = _date(value.get("dateModified"))
            is_event = any(str(kind).casefold() == "event" for kind in types)
            if is_event and event_date is None:
                event_date = _date(value.get("startDate"))
            for key in ("@graph", "mainEntity", "mainEntityOfPage", "event"):
                if key in value:
                    visit(value[key])

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return published, modified, event_date, is_article


def _html_dates(soup: BeautifulSoup) -> tuple[datetime | None, datetime | None, datetime | None, bool]:
    published_jsonld, modified_jsonld, event_date, jsonld_article = _jsonld_dates(soup)

    def meta_date(names: set[str], properties: set[str]) -> datetime | None:
        for node in soup.find_all("meta"):
            name = str(node.get("name") or node.get("itemprop") or "").casefold()
            prop = str(node.get("property") or "").casefold()
            if name in names or prop in properties:
                parsed = _date(node.get("content"))
                if parsed:
                    return parsed
        return None

    published = meta_date(
        {"date", "datepublished", "publish-date", "pubdate", "dc.date.issued", "dcterms.issued"},
        {"article:published_time", "og:published_time"},
    ) or published_jsonld
    modified = meta_date(
        {"datemodified", "last-modified", "dcterms.modified"},
        {"article:modified_time", "og:updated_time"},
    ) or modified_jsonld
    if published is None:
        time_node = soup.find("time", attrs={"datetime": True})
        if time_node:
            published = _date(time_node.get("datetime"))
    og_type = soup.find("meta", attrs={"property": re.compile(r"^og:type$", re.I)})
    is_article = jsonld_article or bool(og_type and "article" in str(og_type.get("content", "")).casefold())
    is_article = is_article or bool(soup.find("meta", attrs={"property": re.compile(r"^article:published_time$", re.I)}))
    return published, modified, event_date, is_article


def parse_html(result: FetchResult) -> list[ExtractedDocument]:
    soup = BeautifulSoup(result.text, "html.parser")
    published_at, modified_at, event_date, is_article = _html_dates(soup)
    for element in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        element.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else "Untitled page"
    description = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    paragraphs = [node.get_text(" ", strip=True) for node in soup.select("h1, h2, h3, p, li, blockquote")]
    text = "\n".join(part for part in ([description.get("content", "") if description else ""] + paragraphs) if part)
    if not text.strip():
        text = soup.get_text(" ", strip=True)
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    page_url = urljoin(result.url, canonical.get("href")) if canonical and canonical.get("href") else result.url
    page_url = normalize_url(page_url)
    return [ExtractedDocument(
        page_url, title[:1000], text[:200_000], published_at,
        modified_at=modified_at, event_date=event_date, is_article=is_article,
    )]


def parse_feed(result: FetchResult) -> list[ExtractedDocument]:
    try:
        root = ET.fromstring(result.content)
    except ET.ParseError as exc:
        raise ValueError(f"Could not parse RSS/Atom feed: {exc}") from exc
    docs: list[ExtractedDocument] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "Untitled feed item").strip()
        link = (item.findtext("link") or result.url).strip()
        description = item.findtext("description") or ""
        pub = item.findtext("pubDate") or item.findtext("date")
        docs.append(ExtractedDocument(normalize_url(urljoin(result.url, link)), title[:1000], BeautifulSoup(description, "html.parser").get_text(" ", strip=True)[:200_000], _date(pub)))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//a:entry", ns):
        title = (entry.findtext("a:title", default="Untitled feed item", namespaces=ns) or "Untitled feed item").strip()
        link_node = entry.find("a:link", ns)
        href = link_node.get("href") if link_node is not None else result.url
        summary = entry.findtext("a:summary", default="", namespaces=ns) or entry.findtext("a:content", default="", namespaces=ns)
        published = entry.findtext("a:published", namespaces=ns)
        updated = entry.findtext("a:updated", namespaces=ns)
        docs.append(ExtractedDocument(
            normalize_url(urljoin(result.url, href or result.url)), title[:1000],
            BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)[:200_000],
            _date(published or updated), modified_at=_date(updated) if published else None,
            is_article=True,
        ))
    if not docs:
        raise ValueError("Feed did not contain RSS items or Atom entries")
    return docs


def extract_documents(result: FetchResult, source_type: str) -> list[ExtractedDocument]:
    content_type = result.headers.get("Content-Type", "").lower()
    if source_type in {"rss", "search_feed"} or "xml" in content_type or "rss" in content_type or "atom" in content_type:
        return parse_feed(result)
    return parse_html(result)

