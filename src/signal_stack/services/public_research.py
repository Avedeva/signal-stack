"""Opt-in, provenance-preserving public search for source candidates.

Search snippets are discovery metadata only. They never become verified company
facts, documents, signals, or approved sources until a reviewer validates and
crawls the original source through the guarded source workflow.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_stack.config import settings
from signal_stack.models import Company, ProfileAttribute, Source, SourceAudit
from signal_stack.services.security import normalize_url
from signal_stack.services.source_discovery import upsert_source


logger = logging.getLogger(__name__)
TAVILY_URL = "https://api.tavily.com/search"
MAX_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    content: str
    published_date: str | None = None
    score: float | None = None


@dataclass(frozen=True)
class ResearchDiscoveryResult:
    sources: list[Source]
    completed_queries: int
    errors: list[str]


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]: ...


class PublicSearchError(RuntimeError):
    pass


def validate_search_result(value: dict[str, object]) -> SearchResult:
    """Validate untrusted provider output before it reaches persisted source fields."""
    title = " ".join(str(value.get("title") or "").split())[:500]
    content = " ".join(str(value.get("content") or "").split())[:2000]
    if not title:
        raise ValueError("Search result did not contain a title")
    url = normalize_url(str(value.get("url") or ""))
    published = value.get("published_date")
    score_value = value.get("score")
    try:
        score = min(1.0, max(0.0, float(score_value))) if score_value is not None else None
    except (TypeError, ValueError):
        score = None
    return SearchResult(title, url, content, str(published)[:80] if published else None, score)


class TavilySearchProvider:
    """Minimal Tavily Search API client. It does not request generated answers or raw pages."""

    name = "tavily"

    def __init__(self, api_key: str, *, timeout: tuple[float, float] = (4.0, 12.0)):
        if not api_key.strip():
            raise ValueError("Tavily search requires SIGNAL_STACK_TAVILY_API_KEY")
        self._api_key = api_key.strip()
        self._timeout = timeout

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        clean_query = " ".join(query.split())
        if not clean_query or len(clean_query) > 500:
            raise ValueError("Search query must contain 1 to 500 characters")
        bounded_limit = min(10, max(1, int(limit)))
        try:
            response = requests.post(
                TAVILY_URL,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={
                    "query": clean_query,
                    "search_depth": "basic",
                    "max_results": bounded_limit,
                    "topic": "general",
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_published_date": True,
                    "safe_search": True,
                },
                timeout=self._timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise PublicSearchError(f"Tavily request failed ({type(exc).__name__})") from None
        try:
            if not 200 <= response.status_code < 300:
                raise PublicSearchError(f"Tavily returned HTTP {response.status_code}")
            chunks = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise PublicSearchError("Tavily response exceeded the 1 MB limit")
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks).decode("utf-8"))
            raw_results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(raw_results, list):
                raise PublicSearchError("Tavily returned an invalid result structure")
            results = []
            for item in raw_results[:bounded_limit]:
                if isinstance(item, dict):
                    try:
                        results.append(validate_search_result(item))
                    except (TypeError, ValueError):
                        logger.warning("Tavily returned a result that failed validation; result was ignored")
            return results
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicSearchError(f"Tavily returned invalid JSON ({type(exc).__name__})") from None
        finally:
            response.close()


def configured_search_provider() -> SearchProvider | None:
    provider_name = settings.research_search_provider.casefold()
    if provider_name in {"", "none"}:
        return None
    if provider_name == "tavily":
        return TavilySearchProvider(settings.tavily_api_key)
    raise ValueError(f"Unsupported public research search provider: {provider_name}")


def _queries(session: Session, company: Company) -> list[tuple[str, str]]:
    # Only source-stated/company-published profile context is sent to the search API.
    public_keys = {"company_description", "offerings_observed", "industry", "customer_segments", "geography"}
    rows = session.scalars(
        select(ProfileAttribute).where(
            ProfileAttribute.company_id == company.id,
            ProfileAttribute.key.in_(public_keys),
            ProfileAttribute.status.in_({"source-stated", "browser-stated", "assignment-provided"}),
        )
    ).all()
    profile = {row.key: row.value.strip() for row in rows if row.value.strip()}
    product = profile.get("offerings_observed") or profile.get("company_description") or "products and services"
    industry = profile.get("industry") or "industry"
    geography = profile.get("geography") or ""
    segment = profile.get("customer_segments") or "customers"
    location = f" {geography[:70]}" if geography else ""
    return [
        ("market", f'"{company.name}" {product[:90]} overview products services customers{location}'),
        ("market", f'"{company.name}" {industry[:80]} funding partnerships product announcements'),
        ("competitors", f'"{company.name}" {industry[:80]} direct competitors alternatives {segment[:70]}{location}'),
        ("customers", f'"{company.name}" {segment[:80]} customer complaints expectations reviews'),
    ]


def discover_public_research_sources(
    session: Session,
    company_id: int,
    *,
    provider: SearchProvider | None = None,
    results_per_query: int = 5,
) -> ResearchDiscoveryResult:
    """Search public context and persist only unapproved source candidates with provenance."""
    company = session.get(Company, company_id)
    if not company:
        raise ValueError("Company not found")
    selected_provider = provider if provider is not None else configured_search_provider()
    if selected_provider is None:
        return ResearchDiscoveryResult([], 0, ["No public search provider is configured; set SIGNAL_STACK_RESEARCH_SEARCH_PROVIDER=tavily and its API key."])

    added: list[Source] = []
    errors: list[str] = []
    completed = 0
    visited_urls: set[str] = set()
    for category, query in _queries(session, company):
        try:
            search_results = selected_provider.search(query, limit=min(10, max(1, results_per_query)))
            completed += 1
        except Exception as exc:
            error = f"{category} search failed ({type(exc).__name__})"
            errors.append(error)
            logger.warning("Public company research query failed (%s)", type(exc).__name__)
            continue
        for raw_result in search_results[:10]:
            try:
                item = raw_result if isinstance(raw_result, SearchResult) else validate_search_result(raw_result)
                # Revalidate even provider-created SearchResult instances.
                item = validate_search_result({
                    "title": item.title, "url": item.url, "content": item.content,
                    "published_date": item.published_date, "score": item.score,
                })
            except (TypeError, ValueError):
                logger.warning("Public company research result failed validation; result was ignored")
                continue
            if item.url in visited_urls:
                continue
            visited_urls.add(item.url)
            prior = session.scalar(select(Source).where(Source.company_id == company_id, Source.url == item.url))
            source = upsert_source(
                session,
                company_id,
                url=item.url,
                title=item.title,
                category=category,
                source_type="website",
                reason=f"Found by {selected_provider.name} public search. Search snippets are unverified discovery metadata; validate and crawl the original source before using factual claims.",
                reliability=0.45,
            )
            if prior is None:
                session.flush()
                score_note = f"Provider relevance score {item.score:.2f}; this is search ranking, not a factual confidence score." if item.score is not None else "Provider supplied no relevance score."
                publication_note = item.published_date or "unknown publication date"
                excerpt = item.content[:700] or "No excerpt returned."
                audit = SourceAudit(
                    company_id=company_id,
                    source_id=source.id,
                    status="search-result-unverified",
                    recommendation=(
                        f"{selected_provider.name} query: {query[:300]} | Result title: {item.title} | "
                        f"Published: {publication_note} | {score_note} | Unverified snippet: {excerpt}"
                    )[:2000],
                )
                session.add(audit)
                added.append(source)
    session.commit()
    return ResearchDiscoveryResult(added, completed, errors)
