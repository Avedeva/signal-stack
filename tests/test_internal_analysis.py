import json

import pytest

from signal_stack.models import UploadedFile
from signal_stack.services.company_research import create_company
from signal_stack.services.file_processing import ingest_upload
from signal_stack.services.internal_analysis import analyze_business_records


def test_column_mappings_validate_headers_and_semantic_names():
    payload = b"Created,Stage,Text\n2026-01-02,won,Great support"
    with pytest.raises(ValueError, match="does not match"):
        analyze_business_records("records.csv", payload, column_mapping={"date": "Missing"})
    with pytest.raises(ValueError, match="Unsupported column mapping"):
        analyze_business_records("records.csv", payload, column_mapping={"customer": "Text"})


def test_analysis_tracks_dates_duplicates_sentiment_stages_segments_and_trends():
    payload = (
        "created_at,deal_id,stage,reason,feedback,segment,amount\n"
        "2026-01-03,D1,qualified,price objection,This is difficult,SMB,100\n"
        "2026-01-08,D1,closed-lost,price objection,Unhappy with price,SMB,100\n"
        "2026-01-10,D2,won,,Helpful and easy,Enterprise,200\n"
        "2026-02-10,D3,qualified,security concern,Unsatisfied with privacy,Enterprise,300\n"
        "2026-03-01,D4,qualified,feature request,Excellent support,SMB,400\n"
        "2026-03-02,D5,qualified,price objection,Frustrated with budget,SMB,500\n"
        "not-a-date,D6,open,,Good experience,SMB,10\n"
        "2026-03-02,D5,qualified,price objection,Frustrated with budget,SMB,500\n"
    ).encode()
    result = analyze_business_records("sales.csv", payload)

    assert result["unique_record_count"] == 7
    assert result["duplicate_row_numbers"] == [9]
    assert result["invalid_date_row_numbers"] == [8]
    assert result["record_volume_trend"] == "increased in the latest observed month"
    assert result["sentiment_and_outcomes"]["negative"] >= 1
    assert result["sentiment_and_outcomes"]["positive"] >= 1
    assert result["stage_transitions"][0]["from"] == "qualified"
    assert result["stage_transitions"][0]["to"] == "closed-lost"
    assert result["segments"]["SMB"] >= 4
    assert result["value_total"] == 1610
    assert any(item["kind"] == "pricing objection" for item in result["insights"])
    assert any("Could not parse dates" in limitation for limitation in result["limitations"])


def test_low_sample_and_false_keyword_matches_do_not_create_unsupported_findings():
    payload = b"feedback\nThe debugging banner was updated.\nA banner shows the debugging tools."
    result = analyze_business_records("notes.csv", payload)

    assert result["unique_record_count"] == 2
    assert result["insights"] == []
    assert result["record_volume_trend"] == "insufficient data"
    assert any("Only 2 unique" in text for text in result["limitations"])


def test_configurable_mapping_drives_time_series_analysis():
    payload = b"When,Progress,Reason,Audience\n2026-01-01,open,price concern,SMB\n2026-02-01,won,price concern,SMB"
    result = analyze_business_records("opportunities.csv", payload, column_mapping={"date": "When", "status": "Progress", "reason": "Reason", "segment": "Audience"})

    assert result["column_mapping"]["date"] == "When"
    assert result["period_start"] == "2026-01-01"
    assert result["statuses"] == {"open": 1, "won": 1}


def test_upload_persists_internal_analysis_and_row_coverage(db_session, monkeypatch, tmp_path):
    from dataclasses import replace
    from signal_stack import config
    import signal_stack.services.file_processing as file_processing

    monkeypatch.setattr(file_processing, "settings", replace(config.settings, data_dir=tmp_path / "data"))
    company = create_company(db_session, "Internal Analysis Co", "https://internal-analysis.example")
    csv_payload = b"date,reason,count\n2026-01-01,price objection,4\n2026-01-02,price objection,3"
    record, documents, signals = ingest_upload(db_session, company.id, "sales.csv", csv_payload)

    summary = json.loads(record.analysis_summary_json)
    assert documents == 1
    assert signals == 1
    assert record.record_count == 2
    assert summary["themes"]["pricing objection"]["evidence_rows"] == [2, 3]
    assert db_session.query(UploadedFile).filter_by(company_id=company.id).count() == 1
