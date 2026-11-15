from __future__ import annotations

import csv
import io
import json
import posixpath
import re
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET


MAX_ROWS = 5000
SEMANTICS = {"date", "status", "reason", "feedback", "segment", "value", "record_id", "count"}
ALIASES = {
    "date": {"date", "created_at", "closed_at", "updated_at", "timestamp", "reported_at", "event_date", "month"},
    "status": {"status", "stage", "deal_stage", "outcome", "result", "ticket_status"},
    "reason": {"reason", "loss_reason", "lost_reason", "objection", "category", "issue_type"},
    "feedback": {"feedback", "comment", "comments", "note", "notes", "description", "ticket_description", "customer_feedback", "request", "message"},
    "segment": {"segment", "customer_segment", "account_type", "industry", "customer_type"},
    "value": {"revenue", "amount", "deal_value", "contract_value", "mrr", "arr", "sales_value"},
    "record_id": {"id", "record_id", "ticket_id", "opportunity_id", "deal_id", "customer_id"},
    "count": {"count", "quantity", "volume", "frequency", "occurrences"},
}
THEMES = {
    "pricing objection": re.compile(r"\b(price|pricing|cost|expensive|budget)\b", re.I),
    "timing objection": re.compile(r"\b(timing|timeline|delay|deadline|later)\b", re.I),
    "integration objection": re.compile(r"\b(integration|integrate|compatibility)\b", re.I),
    "security objection": re.compile(r"\b(security|privacy|compliance)\b", re.I),
    "product request": re.compile(r"\b(feature request|product request|requested feature|roadmap request)\b", re.I),
    "support issue": re.compile(r"\b(complaint|support issue|ticket|bug|error|incident|outage|service issue)\b", re.I),
    "lost deal": re.compile(r"\b(lost deal|closed-lost|no decision|churn|did not proceed)\b", re.I),
}
POSITIVE = {"good", "helpful", "easy", "satisfied", "resolved", "recommend", "excellent", "love"}
NEGATIVE = {"bad", "difficult", "unhappy", "unsatisfied", "unresolved", "complaint", "poor", "hate", "frustrated"}
WINS = {"won", "closed-won", "success", "successful"}
LOSSES = {"lost", "closed-lost", "churned", "cancelled", "canceled"}


def _normal_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().casefold()).strip("_")


def _shared_xlsx_rows(payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    if not payload.startswith(b"PK"):
        raise ValueError("XLSX signature did not match the file extension")
    with zipfile.ZipFile(io.BytesIO(payload)) as package:
        infos = package.infolist()
        if len(infos) > 2000 or sum(item.file_size for item in infos) > 50 * 1024 * 1024:
            raise ValueError("XLSX package expands beyond the configured safety limit")
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships", "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}

        def safe_xml(path: str):
            data = package.read(path)
            upper = data.upper()
            if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
                raise ValueError("XLSX DTD and entity declarations are not supported")
            return ET.fromstring(data)

        workbook = safe_xml("xl/workbook.xml")
        relationships = safe_xml("xl/_rels/workbook.xml.rels")
        targets = {item.get("Id"): item.get("Target") for item in relationships.findall("rel:Relationship", ns)}
        shared = []
        if "xl/sharedStrings.xml" in package.namelist():
            shared_root = safe_xml("xl/sharedStrings.xml")
            shared = ["".join(node.text or "" for node in item.findall(".//m:t", ns)) for item in shared_root.findall("m:si", ns)]
        sheets = workbook.findall("m:sheets/m:sheet", ns)
        if not sheets:
            raise ValueError("XLSX file has no worksheets")
        target = targets.get(sheets[0].get(f"{{{ns['r']}}}id"))
        if not target:
            raise ValueError("XLSX first worksheet relationship is missing")
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        root = safe_xml(path)
        matrix = []
        for row in root.findall(".//m:sheetData/m:row", ns)[: MAX_ROWS + 1]:
            values: dict[int, str] = {}
            for cell in row.findall("m:c", ns)[:200]:
                ref = cell.get("r", "")
                letters = re.match(r"([A-Z]+)", ref)
                if not letters:
                    continue
                column = 0
                for char in letters.group(1):
                    column = column * 26 + ord(char) - 64
                column -= 1
                value = cell.findtext("m:v", default="", namespaces=ns)
                if cell.get("t") == "s" and value:
                    try:
                        value = shared[int(value)]
                    except (ValueError, IndexError):
                        value = ""
                elif cell.get("t") == "inlineStr":
                    value = "".join(node.text or "" for node in cell.findall(".//m:t", ns))
                values[column] = (value or "")[:1000]
            if values:
                matrix.append([values.get(index, "") for index in range(max(values) + 1)])
    if not matrix:
        raise ValueError("XLSX first worksheet contains no rows")
    headers = [str(value).strip() for value in matrix[0]]
    rows = [{headers[index]: value for index, value in enumerate(row) if index < len(headers)} for row in matrix[1:]]
    return headers, rows


def read_business_table(filename: str, payload: bytes) -> tuple[list[str], list[dict[str, str]]]:
    suffix = Path(filename).suffix.casefold()
    if suffix == ".csv":
        try:
            reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
            if not reader.fieldnames:
                raise ValueError("CSV requires a header row")
            headers = [str(item or "").strip() for item in reader.fieldnames]
            rows = []
            for row in reader:
                if len(rows) >= MAX_ROWS:
                    break
                rows.append({key: str(value or "").strip()[:1000] for key, value in row.items() if key and key is not None})
            return headers, rows
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ValueError("Could not read UTF-8 CSV business records") from exc
    if suffix == ".xlsx":
        return _shared_xlsx_rows(payload)
    return [], []


def _parse_date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _number(value: str) -> float | None:
    text = (value or "").strip().replace(",", "").replace("$", "").replace("£", "").replace("€", "")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def analyze_business_records(
    filename: str,
    payload: bytes,
    *,
    column_mapping: dict[str, str] | None = None,
    extracted_text: str = "",
) -> dict[str, object]:
    headers, rows = read_business_table(filename, payload)
    if not headers:
        rows = [{"notes": line.strip()} for line in extracted_text.splitlines() if line.strip()][:MAX_ROWS]
        headers = ["notes"] if rows else []
    normalized_to_original = {_normal_header(header): header for header in headers if header}
    supplied = column_mapping or {}
    unknown_semantics = set(supplied) - SEMANTICS
    if unknown_semantics:
        raise ValueError(f"Unsupported column mapping field(s): {', '.join(sorted(unknown_semantics))}")
    mapping: dict[str, str] = {}
    for semantic, header in supplied.items():
        if header not in headers:
            raise ValueError(f"Column mapping for {semantic} does not match an uploaded column")
        mapping[semantic] = header
    for semantic, aliases in ALIASES.items():
        if semantic in mapping:
            continue
        for normalized, original in normalized_to_original.items():
            if normalized in aliases:
                mapping[semantic] = original
                break

    nonempty = [(index + 2, row) for index, row in enumerate(rows) if any(str(value).strip() for value in row.values())]
    seen = set()
    duplicates = []
    unique = []
    for row_number, row in nonempty:
        fingerprint = tuple((header, str(row.get(header, "")).strip().casefold()) for header in headers)
        if fingerprint in seen:
            duplicates.append(row_number)
        else:
            seen.add(fingerprint)
            unique.append((row_number, row))
    date_header = mapping.get("date")
    status_header = mapping.get("status")
    reason_headers = [mapping[key] for key in ("reason", "feedback") if key in mapping]
    segment_header = mapping.get("segment")
    value_header = mapping.get("value")
    count_header = mapping.get("count")
    record_id_header = mapping.get("record_id")

    parsed_dates: list[tuple[int, date]] = []
    invalid_dates: list[int] = []
    theme_rows: dict[str, list[int]] = defaultdict(list)
    sentiment = Counter()
    status_counts = Counter()
    segment_counts = Counter()
    values = []
    transitions: dict[str, list[tuple[date, str, int]]] = defaultdict(list)
    explicit_counts: dict[str, float] = defaultdict(float)

    for row_number, row in unique:
        date_value = _parse_date(str(row.get(date_header, ""))) if date_header else None
        if date_header:
            if date_value:
                parsed_dates.append((row_number, date_value))
            elif str(row.get(date_header, "")).strip():
                invalid_dates.append(row_number)
        status = str(row.get(status_header, "")).strip().casefold() if status_header else ""
        if status:
            status_counts[status] += 1
            if record_id_header and date_value:
                record_id = str(row.get(record_id_header, "")).strip()
                if record_id:
                    transitions[record_id].append((date_value, status, row_number))
        segment = str(row.get(segment_header, "")).strip() if segment_header else ""
        if segment:
            segment_counts[segment] += 1
        metric = _number(str(row.get(value_header, ""))) if value_header else None
        if metric is not None:
            values.append((row_number, metric))
        explicit_count = _number(str(row.get(count_header, ""))) if count_header else None
        row_text = " ".join(str(row.get(header, "")) for header in (reason_headers or headers)).strip()
        for theme, pattern in THEMES.items():
            if pattern.search(row_text):
                theme_rows[theme].append(row_number)
                if explicit_count is not None:
                    explicit_counts[theme] += max(0.0, explicit_count)
        words = set(re.findall(r"[a-z]+", row_text.casefold()))
        has_positive = bool(words & POSITIVE)
        has_negative = bool(words & NEGATIVE)
        if has_positive and has_negative:
            sentiment["mixed"] += 1
        elif has_positive:
            sentiment["positive"] += 1
        elif has_negative:
            sentiment["negative"] += 1
        if status in WINS:
            sentiment["won"] += 1
        if status in LOSSES:
            sentiment["lost"] += 1

    monthly = Counter(value.strftime("%Y-%m") for _row, value in parsed_dates)
    trend = "insufficient data"
    if len(unique) >= 5 and len(monthly) >= 2:
        periods = sorted(monthly)
        if monthly[periods[-1]] > monthly[periods[-2]]:
            trend = "increased in the latest observed month"
        elif monthly[periods[-1]] < monthly[periods[-2]]:
            trend = "decreased in the latest observed month"
        else:
            trend = "unchanged between the latest two observed months"
    stage_moves = []
    for record_id, samples in transitions.items():
        ordered = sorted(samples)
        stage_moves.extend((record_id, before[1], after[1], before[2], after[2]) for before, after in zip(ordered, ordered[1:]) if before[1] != after[1])

    themes = {
        theme: {"row_count": len(row_numbers), "evidence_rows": row_numbers[:30], "explicit_count_total": round(explicit_counts[theme], 2)}
        for theme, row_numbers in sorted(theme_rows.items())
    }
    sufficient_sample = len(unique) >= 5
    insights = []
    for theme, details in themes.items():
        row_count = int(details["row_count"])
        explicit_total = float(details["explicit_count_total"])
        if row_count >= 3 or (explicit_total >= 3 and count_header):
            caveat = "" if sufficient_sample else f" Only {len(unique)} unique record(s) were available; this is not enough to establish a trend."
            insights.append({
                "kind": theme,
                "statement": f"Observed {row_count} matching record(s) for {theme}" + (f" with an explicit reported count of {explicit_total:g}." if explicit_total else ".") + caveat,
                "evidence_rows": details["evidence_rows"],
                "confidence": "moderate" if sufficient_sample else "low",
            })
    if sufficient_sample and len(monthly) >= 2 and trend != "insufficient data":
        insights.append({"kind": "record_volume_trend", "statement": f"Observed record volume {trend}; counts are based on {len(unique)} unique rows.", "evidence_rows": [row for row, _ in parsed_dates[-30:]], "confidence": "moderate"})
    if stage_moves:
        insights.append({"kind": "stage_movement", "statement": f"Observed {len(stage_moves)} stage transition(s) across {len(transitions)} identified record(s).", "evidence_rows": sorted({row for move in stage_moves for row in (move[3], move[4])})[:30], "confidence": "moderate" if sufficient_sample else "low"})

    limitations = []
    if len(unique) < 5:
        limitations.append(f"Only {len(unique)} unique record(s); no population-level or recurring-trend conclusion is supported.")
    if duplicates:
        limitations.append(f"Detected {len(duplicates)} duplicate row(s); duplicates were excluded from counts.")
    if invalid_dates:
        limitations.append(f"Could not parse dates in {len(invalid_dates)} row(s); those rows are excluded from time-series analysis.")
    if not date_header:
        limitations.append("No date column was mapped; time trends cannot be assessed.")
    if not reason_headers and len(headers) > 1:
        limitations.append("No reason or feedback column was mapped; theme detection used all available columns.")

    return {
        "row_count": len(rows),
        "unique_record_count": len(unique),
        "duplicate_row_numbers": duplicates[:100],
        "invalid_row_count": max(0, len(rows) - len(nonempty)),
        "invalid_date_row_numbers": invalid_dates[:100],
        "period_start": min((value.isoformat() for _row, value in parsed_dates), default=None),
        "period_end": max((value.isoformat() for _row, value in parsed_dates), default=None),
        "column_mapping": mapping,
        "themes": themes,
        "sentiment_and_outcomes": dict(sentiment),
        "statuses": dict(status_counts),
        "segments": dict(segment_counts),
        "monthly_record_counts": dict(sorted(monthly.items())),
        "record_volume_trend": trend,
        "value_record_count": len(values),
        "value_total": round(sum(value for _row, value in values), 2) if values else None,
        "stage_transitions": [{"record_id": row[0], "from": row[1], "to": row[2], "from_row": row[3], "to_row": row[4]} for row in stage_moves[:100]],
        "insights": insights,
        "limitations": limitations,
        "evidence_quality": "moderate" if sufficient_sample and not duplicates else "limited",
    }


def serialize_analysis(analysis: dict[str, object]) -> str:
    return json.dumps(analysis, ensure_ascii=False, sort_keys=True)
