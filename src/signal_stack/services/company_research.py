from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_stack.models import Company, CrawlRun, Document, ProfileAttribute, ProfileVersion, Source, utcnow
from signal_stack.services.extraction import content_hash, extract_documents
from signal_stack.services.security import fetch_public_url, normalize_url


def profile_values(session: Session, company_id: int) -> dict[str, str]:
    rows = session.scalars(select(ProfileAttribute).where(ProfileAttribute.company_id == company_id)).all()
    return {row.key: row.value for row in rows}


def save_profile_value(
    session: Session, company_id: int, key: str, value: str, *, status: str = "founder-provided",
    evidence_url: str | None = None, evidence_snippet: str | None = None,
) -> ProfileAttribute:
    row = session.scalar(select(ProfileAttribute).where(ProfileAttribute.company_id == company_id, ProfileAttribute.key == key))
    if row is None:
        row = ProfileAttribute(company_id=company_id, key=key, value=value.strip(), status=status)
        session.add(row)
    else:
        row.value = value.strip()
        row.status = status
    row.evidence_url = evidence_url
    row.evidence_snippet = evidence_snippet
    return row


def create_company(session: Session, name: str, website: str, *, timezone: str = "Asia/Kolkata") -> Company:
    company_name = " ".join(name.strip().split())
    if not company_name or len(company_name) > 240:
        raise ValueError("Company name is required and must be at most 240 characters")
    normalized = normalize_url(website)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Time zone must be a valid IANA identifier, such as Asia/Kolkata") from exc
    existing = session.scalar(select(Company).where(Company.website == normalized))
    if existing:
        return existing
    company = Company(name=company_name, website=normalized, timezone=timezone)
    session.add(company)
    session.flush()
    save_profile_value(session, company.id, "company_name", company_name, status="founder-provided")
    save_profile_value(session, company.id, "website", normalized, status="user-provided")
    session.commit()
    return company


def research_company(session: Session, company: Company) -> dict[str, str]:
    """Fetch the public homepage and store source-attributed profile observations."""
    source = session.scalar(select(Source).where(Source.company_id == company.id, Source.url == company.website))
    run = CrawlRun(company_id=company.id, source_id=source.id if source else None)
    session.add(run)
    session.commit()
    try:
        response = fetch_public_url(company.website)
        docs = extract_documents(response, "website")
        if not docs:
            raise ValueError("No readable company homepage content was returned")
        document = docs[0]
    except Exception as exc:
        run.status = "failed"
        run.error = str(exc)[:2000]
        run.ended_at = utcnow()
        if source:
            source.last_status = "failed"
            source.last_error = str(exc)[:2000]
            source.last_checked_at = utcnow()
        session.commit()
        raise
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(response.text, "html.parser")
    description_node = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    description = (description_node.get("content") or "").strip() if description_node else ""
    h1 = [x.get_text(" ", strip=True) for x in soup.find_all("h1")][:5]
    excerpt = " ".join(document.content.split())[:1400]
    observations = {
        "website_title": document.title,
        "company_description": description or (" ".join(h1)[:800] if h1 else excerpt[:800]),
        "offerings_observed": "; ".join(h1) if h1 else excerpt[:800],
    }
    first_party_candidates = []
    company_host = urlsplit(company.website).hostname
    for node in soup.find_all(["a", "link"], href=True)[:500]:
        href = node.get("href", "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        try:
            candidate_url = normalize_url(urljoin(company.website, href))
        except ValueError:
            continue
        if urlsplit(candidate_url).hostname != company_host or candidate_url == company.website:
            continue
        label = " ".join(node.get_text(" ", strip=True).split()) or node.get("title", "") or href
        lowered = f"{label} {href}".lower()
        if not any(term in lowered for term in ("rss", "atom", "feed", "blog", "news", "press", "update", "support", "help", "faq", "community", "review")):
            continue
        category = "customers" if any(term in lowered for term in ("support", "help", "faq", "community", "review")) else "market"
        source_type = "rss" if any(term in lowered for term in ("rss", "atom", "feed")) or candidate_url.lower().endswith((".xml", ".rss")) else "website"
        existing_source = session.scalar(select(Source).where(Source.company_id == company.id, Source.url == candidate_url))
        if existing_source:
            continue
        first_party_candidates.append(Source(
            company_id=company.id, url=candidate_url, title=(label or "First-party page")[:500],
            category=category, source_type=source_type,
            reason=f"Discovered on the company’s own homepage as ‘{label[:180]}’; potentially relevant as a first-party {category} source. Review before approval.",
            reliability=.7, relevance=.65, status="candidate",
        ))
    session.add_all(first_party_candidates)
    for key, value in observations.items():
        if value:
            save_profile_value(
                session, company.id, key, value, status="source-stated",
                evidence_url=document.url, evidence_snippet=excerpt[:700],
            )
    digest = content_hash(f"{document.title}\n{document.content}")
    existing = session.scalar(select(Document).where(Document.company_id == company.id, Document.canonical_url == document.url, Document.content_hash == digest))
    if existing:
        existing.last_seen_at = utcnow()
    else:
        latest = session.scalar(select(Document).where(Document.company_id == company.id, Document.canonical_url == document.url).order_by(Document.version.desc()))
        session.add(Document(
            company_id=company.id, source_id=source.id if source else None, canonical_url=document.url,
            title=document.title, discovered_at=utcnow(), content_hash=digest, content=document.content,
            category="market", source_reliability=source.reliability if source else .75,
            version=(latest.version + 1 if latest else 1),
        ))
    run.status = "succeeded"
    run.documents_found = 1
    run.ended_at = utcnow()
    if source:
        source.last_status = "healthy"
        source.last_error = None
        source.last_checked_at = utcnow()
    session.commit()
    return observations


def save_profile_version(session: Session, company_id: int, reason: str) -> ProfileVersion:
    current = profile_values(session, company_id)
    previous = session.scalar(
        select(ProfileVersion).where(ProfileVersion.company_id == company_id).order_by(ProfileVersion.version.desc())
    )
    version = ProfileVersion(
        company_id=company_id,
        version=(previous.version + 1 if previous else 1),
        reason=reason[:500],
        snapshot_json=json.dumps(current, ensure_ascii=False, sort_keys=True),
    )
    session.add(version)
    session.flush()
    return version

