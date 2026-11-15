from __future__ import annotations

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session as OrmSession, sessionmaker

from signal_stack.config import settings


class Base(DeclarativeBase):
    pass


def _engine_for(url: str):
    kwargs = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
    db_engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(db_engine, "connect")
        def _enable_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return db_engine


engine = _engine_for(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(OrmSession, "do_orm_execute")
def _require_authenticated_principal(execute_state):
    """Enforce authentication at the ORM boundary for production operations."""
    if settings.environment == "production" and not execute_state.session.info.get("principal"):
        raise PermissionError("An authenticated principal is required for database operations")


@event.listens_for(OrmSession, "before_flush")
def _require_authenticated_write(session, _flush_context, _instances):
    if settings.environment == "production" and not session.info.get("principal"):
        raise PermissionError("An authenticated principal is required for database operations")


def init_db(target_engine=None) -> None:
    from signal_stack import models  # noqa: F401
    from sqlalchemy import text

    settings.prepare()
    target = target_engine or engine
    Base.metadata.create_all(target)
    # Migrations are additive and versioned; existing rows/tables are never dropped.
    with target.begin() as connection:
        migration_table_exists = "schema_migrations" in inspect(target).get_table_names()
        current = connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar() if migration_table_exists else None
        current = current or 0
        if current < 1:
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (1, CURRENT_TIMESTAMP)"))
        if current < 2:
            additions = {
                "companies": {
                    "brief_weekday": "INTEGER NOT NULL DEFAULT 0",
                    "brief_hour": "INTEGER NOT NULL DEFAULT 8",
                    "weekly_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
                    "monthly_audit_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
                    "source_refresh_hours": "INTEGER NOT NULL DEFAULT 24",
                },
                "interview_sessions": {
                    "generation_method": "VARCHAR(48) NOT NULL DEFAULT 'deterministic-profile'",
                    "generation_note": "TEXT",
                },
                "automation_schedules": {"config_signature": "VARCHAR(200)"},
            }
            for table, columns in additions.items():
                present = {column["name"] for column in inspect(target).get_columns(table)}
                for column, definition in columns.items():
                    if column not in present:
                        connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (2, CURRENT_TIMESTAMP)"))
            current = 2
        if current < 3:
            additions = {
                "developments": {"verification_status": "VARCHAR(40) NOT NULL DEFAULT 'not-reviewed'"},
                "signals": {"recency": "FLOAT NOT NULL DEFAULT 0.5"},
            }
            for table, columns in additions.items():
                present = {column["name"] for column in inspect(target).get_columns(table)}
                for column, definition in columns.items():
                    if column not in present:
                        connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (3, CURRENT_TIMESTAMP)"))
            current = 3
        if current < 4:
            present = {column["name"] for column in inspect(target).get_columns("sources")}
            if "relevance" not in present:
                connection.exec_driver_sql('ALTER TABLE "sources" ADD COLUMN "relevance" FLOAT NOT NULL DEFAULT 0.5')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (4, CURRENT_TIMESTAMP)"))
            current = 4
        if current < 5:
            # onboarding_stages is created additively by metadata.create_all above.
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (5, CURRENT_TIMESTAMP)"))
            current = 5
        if current < 6:
            additions = {
                "event_date": "DATETIME",
                "modified_at": "DATETIME",
                "is_article": "BOOLEAN NOT NULL DEFAULT FALSE",
            }
            present = {column["name"] for column in inspect(target).get_columns("documents")}
            for column, definition in additions.items():
                if column not in present:
                    connection.exec_driver_sql(f'ALTER TABLE "documents" ADD COLUMN "{column}" {definition}')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (6, CURRENT_TIMESTAMP)"))
            current = 6
        if current < 7:
            # Company classification history is created additively by metadata.create_all above.
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (7, CURRENT_TIMESTAMP)"))
            current = 7
        if current < 8:
            present = {column["name"] for column in inspect(target).get_columns("onboarding_stages")}
            if "input_fingerprint" not in present:
                connection.exec_driver_sql('ALTER TABLE "onboarding_stages" ADD COLUMN "input_fingerprint" VARCHAR(64)')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (8, CURRENT_TIMESTAMP)"))
            current = 8
        if current < 9:
            additions = {
                "factual_claim": "TEXT NOT NULL DEFAULT ''",
                "interpretation": "TEXT NOT NULL DEFAULT ''",
                "affected_business_area": "VARCHAR(240) NOT NULL DEFAULT 'unassessed'",
                "recommended_action": "TEXT NOT NULL DEFAULT ''",
                "analysis_confidence": "FLOAT NOT NULL DEFAULT 0",
                "confidence_rationale": "TEXT NOT NULL DEFAULT ''",
                "analysis_method": "VARCHAR(80) NOT NULL DEFAULT 'legacy-unassessed'",
            }
            present = {column["name"] for column in inspect(target).get_columns("developments")}
            for column, definition in additions.items():
                if column not in present:
                    connection.exec_driver_sql(f'ALTER TABLE "developments" ADD COLUMN "{column}" {definition}')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (9, CURRENT_TIMESTAMP)"))
            current = 9
        if current < 10:
            additions = {
                "competitor_type": "VARCHAR(32) NOT NULL DEFAULT 'unclassified'",
                "evidence_snippet": "TEXT",
                "discovery_confidence": "FLOAT NOT NULL DEFAULT 0",
            }
            present = {column["name"] for column in inspect(target).get_columns("competitors")}
            for column, definition in additions.items():
                if column not in present:
                    connection.exec_driver_sql(f'ALTER TABLE "competitors" ADD COLUMN "{column}" {definition}')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (10, CURRENT_TIMESTAMP)"))
            current = 10
        if current < 11:
            present = {column["name"] for column in inspect(target).get_columns("interview_sessions")}
            if "context_fingerprint" not in present:
                connection.exec_driver_sql('ALTER TABLE "interview_sessions" ADD COLUMN "context_fingerprint" VARCHAR(64)')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (11, CURRENT_TIMESTAMP)"))
            current = 11
        if current < 12:
            present = {column["name"] for column in inspect(target).get_columns("developments")}
            if "signal_classification_method" not in present:
                connection.exec_driver_sql('ALTER TABLE "developments" ADD COLUMN "signal_classification_method" VARCHAR(80) NOT NULL DEFAULT \'legacy-unassessed\'')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (12, CURRENT_TIMESTAMP)"))
            current = 12
        if current < 13:
            present = {column["name"] for column in inspect(target).get_columns("sources")}
            if "relevance_reason" not in present:
                connection.exec_driver_sql('ALTER TABLE "sources" ADD COLUMN "relevance_reason" TEXT NOT NULL DEFAULT \'\'')
            # verification_history is created additively by metadata.create_all above.
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (13, CURRENT_TIMESTAMP)"))
            current = 13
        if current < 14:
            additions = {
                "last_attempted_at": "DATETIME",
                "last_success_at": "DATETIME",
                "next_due_at": "DATETIME",
                "refresh_claimed_until": "DATETIME",
                "refresh_failure_count": "INTEGER NOT NULL DEFAULT 0",
            }
            present = {column["name"] for column in inspect(target).get_columns("sources")}
            for column, definition in additions.items():
                if column not in present:
                    connection.exec_driver_sql(f'ALTER TABLE "sources" ADD COLUMN "{column}" {definition}')
            connection.exec_driver_sql('CREATE INDEX IF NOT EXISTS "ix_sources_next_due_at" ON "sources" ("next_due_at")')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (14, CURRENT_TIMESTAMP)"))
            current = 14
        if current < 15:
            additions = {"previous_status": "VARCHAR(32)", "actor_identity": "VARCHAR(240)"}
            present = {column["name"] for column in inspect(target).get_columns("source_audits")}
            for column, definition in additions.items():
                if column not in present:
                    connection.exec_driver_sql(f'ALTER TABLE "source_audits" ADD COLUMN "{column}" {definition}')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (15, CURRENT_TIMESTAMP)"))
            current = 15
        if current < 16:
            additions = {
                "analysis_summary_json": "TEXT NOT NULL DEFAULT '{}'",
                "record_count": "INTEGER NOT NULL DEFAULT 0",
                "invalid_record_count": "INTEGER NOT NULL DEFAULT 0",
            }
            present = {column["name"] for column in inspect(target).get_columns("uploaded_files")}
            for column, definition in additions.items():
                if column not in present:
                    connection.exec_driver_sql(f'ALTER TABLE "uploaded_files" ADD COLUMN "{column}" {definition}')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (16, CURRENT_TIMESTAMP)"))
            current = 16
        if current < 17:
            additions = {
                "products_services": "TEXT NOT NULL DEFAULT ''",
                "target_market": "TEXT NOT NULL DEFAULT ''",
                "customer_segments": "TEXT NOT NULL DEFAULT ''",
                "geographic_focus": "TEXT NOT NULL DEFAULT ''",
                "competitive_relationship": "TEXT NOT NULL DEFAULT ''",
                "supporting_source_urls_json": "TEXT NOT NULL DEFAULT '[]'",
                "discovered_at": "DATETIME",
            }
            present = {column["name"] for column in inspect(target).get_columns("competitors")}
            for column, definition in additions.items():
                if column not in present:
                    connection.exec_driver_sql(f'ALTER TABLE "competitors" ADD COLUMN "{column}" {definition}')
            connection.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (17, CURRENT_TIMESTAMP)"))

