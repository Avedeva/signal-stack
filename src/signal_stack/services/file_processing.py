from __future__ import annotations

import csv
import hashlib
import io
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from sqlalchemy.orm import Session

from signal_stack.config import settings
from signal_stack.models import Source, UploadedFile, utcnow
from signal_stack.services.extraction import ExtractedDocument
from signal_stack.services.intelligence import _persist_documents
from signal_stack.services.internal_analysis import analyze_business_records, serialize_analysis


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".pdf", ".txt"}


def _safe_xml(payload: bytes):
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("XLSX files containing DTD or entity declarations are not supported")
    return ET.fromstring(payload)


def _extract(filename: str, payload: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("Supported uploads are CSV, XLSX, PDF, and TXT")
    if not payload or len(payload) > MAX_UPLOAD_BYTES:
        raise ValueError("Upload must be non-empty and no larger than 10 MB")
    if suffix == ".txt":
        try:
            return payload.decode("utf-8-sig")[:500_000]
        except UnicodeDecodeError as exc:
            raise ValueError("TXT files must be UTF-8 encoded") from exc
    if suffix == ".csv":
        try:
            decoded = payload.decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(decoded)))[:5000]
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ValueError("Could not read UTF-8 CSV") from exc
        return "\n".join(" | ".join(cell.strip()[:1000] for cell in row[:50]) for row in rows)[:500_000]
    if suffix == ".xlsx":
        if not payload.startswith(b"PK"):
            raise ValueError("XLSX signature did not match the file extension")
        try:
            package = zipfile.ZipFile(io.BytesIO(payload))
            infos = package.infolist()
            if len(infos) > 2000 or sum(item.file_size for item in infos) > 50 * 1024 * 1024:
                raise ValueError("XLSX package expands beyond the configured safety limit")
            ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships", "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
            workbook_root = _safe_xml(package.read("xl/workbook.xml"))
            rels_root = _safe_xml(package.read("xl/_rels/workbook.xml.rels"))
            relationships = {item.get("Id"): item.get("Target") for item in rels_root.findall("rel:Relationship", ns)}
            shared_strings = []
            if "xl/sharedStrings.xml" in package.namelist():
                strings_root = _safe_xml(package.read("xl/sharedStrings.xml"))
                for item in strings_root.findall("m:si", ns):
                    shared_strings.append("".join(node.text or "" for node in item.findall(".//m:t", ns)))
            lines = []
            for sheet in workbook_root.findall("m:sheets/m:sheet", ns)[:20]:
                lines.append(f"Sheet: {sheet.get('name', 'Sheet')}")
                target = relationships.get(sheet.get(f"{{{ns['r']}}}id"))
                if not target:
                    continue
                sheet_path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
                root = _safe_xml(package.read(sheet_path))
                for row in root.findall(".//m:sheetData/m:row", ns)[:5000]:
                    cells = []
                    for cell in row.findall("m:c", ns)[:50]:
                        value = cell.findtext("m:v", default="", namespaces=ns)
                        if cell.get("t") == "s" and value:
                            try:
                                value = shared_strings[int(value)]
                            except (ValueError, IndexError):
                                value = ""
                        elif cell.get("t") == "inlineStr":
                            value = "".join(node.text or "" for node in cell.findall(".//m:t", ns))
                        if value:
                            cells.append(value[:1000])
                    if cells:
                        lines.append(" | ".join(cells))
                    if len(lines) > 12000:
                        break
            return "\n".join(lines)[:500_000]
        except Exception as exc:
            raise ValueError(f"Could not read XLSX file: {exc}") from exc
    if not payload.startswith(b"%PDF-"):
        raise ValueError("PDF signature did not match the file extension")
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(payload), strict=False)
        if len(reader.pages) > 300:
            raise ValueError("PDF is limited to 300 pages")
        return "\n".join(page.extract_text() or "" for page in reader.pages)[:500_000]
    except ImportError:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(payload)) as pdf:
                if len(pdf.pages) > 300:
                    raise ValueError("PDF is limited to 300 pages")
                return "\n".join(page.extract_text() or "" for page in pdf.pages)[:500_000]
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not read PDF file: {exc}") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not read PDF file: {exc}") from exc


def ingest_upload(
    session: Session,
    company_id: int,
    filename: str,
    payload: bytes,
    *,
    column_mapping: dict[str, str] | None = None,
) -> tuple[UploadedFile, int, int]:
    safe_name = Path(filename.replace("\\", "/")).name[:500]
    text = _extract(safe_name, payload)
    if not text.strip():
        raise ValueError("No text could be extracted from this file")
    analysis = analyze_business_records(safe_name, payload, column_mapping=column_mapping, extracted_text=text)
    insights = analysis["insights"]
    insight_summary = " ".join(str(item["statement"]) for item in insights) if insights else "No repeated, evidence-supported internal pattern was identified."
    digest = hashlib.sha256(payload).hexdigest()
    target = settings.data_dir / "uploads" / f"{company_id}-{digest}{Path(safe_name).suffix.lower()}"
    settings.prepare()
    target.write_bytes(payload)
    uploaded = UploadedFile(
        company_id=company_id,
        filename=safe_name,
        stored_path=str(target),
        content_hash=digest,
        status="processing",
        analysis_summary_json=serialize_analysis(analysis),
        record_count=int(analysis["unique_record_count"]),
        invalid_record_count=int(analysis["invalid_row_count"]),
    )
    session.add(uploaded)
    source_url = f"internal://company/{company_id}/uploads"
    source = next((row for row in session.query(Source).filter(Source.company_id == company_id, Source.url == source_url).all()), None)
    if source is None:
        source = Source(company_id=company_id, url=source_url, title="Internal company uploads", category="internal", source_type="internal", reason="Explicit user-provided internal evidence; uploaded files are untrusted data and are not executed.", reliability=0.65, relevance=0.65, status="approved")
        session.add(source)
        session.flush()
    source.status = "approved"
    source.last_status = "internal-only"
    source.last_checked_at = utcnow()
    title = f"Internal file: {safe_name}"
    if insights:
        title += " — " + "; ".join(str(item["kind"]) for item in insights[:4])
    analyzed_content = (
        f"{text}\n\nStructured internal analysis observations: {insight_summary}\n"
        f"Unique records: {analysis['unique_record_count']}; period: {analysis['period_start'] or 'unknown'} to {analysis['period_end'] or 'unknown'}; "
        f"evidence quality: {analysis['evidence_quality']}. Limitations: {' '.join(str(item) for item in analysis['limitations']) or 'none recorded'}."
    )[:500_000]
    document = ExtractedDocument(
        url=f"internal://company/{company_id}/upload/{digest}",
        title=title,
        content=analyzed_content,
        emit_signal=bool(insights),
    )
    try:
        new_count, signal_count = _persist_documents(session, company_id, source, [document])
        uploaded.status = "processed"
        session.commit()
        return uploaded, new_count, signal_count
    except Exception:
        target.unlink(missing_ok=True)
        raise

