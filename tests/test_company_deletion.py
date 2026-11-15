from pathlib import Path

from sqlalchemy import select

from signal_stack.models import Company, InterviewSession, OnboardingStage, ProfileAttribute, Source, UploadedFile
from signal_stack.services.company_research import create_company
from signal_stack.services.onboarding import run_onboarding
from signal_stack.services.source_discovery import discover_sources
from signal_stack.services.company_deletion import delete_company


def test_delete_company_removes_only_its_records_and_uploads(db_session, monkeypatch, tmp_path):
    import signal_stack.services.company_deletion as deletion
    from signal_stack.config import settings

    company = create_company(db_session, "Delete Me", "https://delete.example")
    survivor = create_company(db_session, "Keep Me", "https://keep.example")
    source = discover_sources(db_session, company.id)[0]
    survivor_source = discover_sources(db_session, survivor.id)[0]
    run_onboarding(db_session, company.id)
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    victim_file = upload_root / "victim.txt"
    survivor_file = upload_root / "survivor.txt"
    victim_file.write_text("private company file", encoding="utf-8")
    survivor_file.write_text("keep this file", encoding="utf-8")
    db_session.add_all([
        UploadedFile(company_id=company.id, filename="victim.txt", stored_path=str(victim_file), content_hash="a" * 64),
        UploadedFile(company_id=survivor.id, filename="survivor.txt", stored_path=str(survivor_file), content_hash="b" * 64),
    ])
    db_session.commit()
    monkeypatch.setattr(deletion, "settings", type("Settings", (), {"data_dir": tmp_path, "prepare": lambda self: None})())

    result = delete_company(db_session, company.id)

    assert result["company_name"] == "Delete Me"
    assert result["files_removed"] == 1
    assert not victim_file.exists()
    assert survivor_file.exists()
    assert db_session.get(Company, company.id) is None
    assert db_session.get(Company, survivor.id) is not None
    for model in (ProfileAttribute, Source, InterviewSession, OnboardingStage, UploadedFile):
        assert db_session.scalar(select(model).where(model.company_id == company.id)) is None
    assert db_session.get(Source, survivor_source.id) is not None
    assert db_session.get(Source, source.id) is None


def test_delete_company_refuses_to_remove_upload_paths_outside_upload_root(db_session, monkeypatch, tmp_path):
    import signal_stack.services.company_deletion as deletion

    company = create_company(db_session, "Boundary Co", "https://boundary.example")
    outside = tmp_path / "important.txt"
    outside.write_text("leave intact", encoding="utf-8")
    db_session.add(UploadedFile(company_id=company.id, filename="important.txt", stored_path=str(outside), content_hash="c" * 64))
    db_session.commit()
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    monkeypatch.setattr(deletion, "settings", type("Settings", (), {"data_dir": tmp_path, "prepare": lambda self: None})())

    result = delete_company(db_session, company.id)

    assert result["files_removed"] == 0
    assert result["file_errors"] == 1
    assert outside.read_text(encoding="utf-8") == "leave intact"
