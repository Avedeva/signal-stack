"""Safely remove a company and its isolated data."""
from pathlib import Path

from sqlalchemy.orm import Session

from signal_stack.config import settings
from signal_stack.models import Company, UploadedFile


def delete_company(session: Session, company_id: int) -> dict[str, int | str]:
    company = session.get(Company, company_id)
    if company is None:
        raise ValueError("Company not found")
    name = company.name
    upload_root = (settings.data_dir / "uploads").resolve()
    paths: list[Path] = []
    invalid_paths = 0
    for upload in session.query(UploadedFile).filter_by(company_id=company_id).all():
        candidate = Path(upload.stored_path).resolve()
        if candidate.parent == upload_root:
            paths.append(candidate)
        else:
            invalid_paths += 1
    session.delete(company)
    session.commit()
    removed = 0
    file_errors = invalid_paths
    for path in paths:
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            file_errors += 1
    return {"company_name": name, "files_removed": removed, "file_errors": file_errors}
