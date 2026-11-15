from sqlalchemy import create_engine, text

from signal_stack.database import init_db
from signal_stack.models import Company
from sqlalchemy import inspect


def test_additive_migration_preserves_existing_company_rows():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE companies (id INTEGER PRIMARY KEY, name VARCHAR(240) NOT NULL, website VARCHAR(1000) NOT NULL, created_at DATETIME NOT NULL, timezone VARCHAR(80) NOT NULL, active BOOLEAN NOT NULL)")
        connection.exec_driver_sql("INSERT INTO companies(id, name, website, created_at, timezone, active) VALUES (9, 'Existing Co', 'https://existing.example/', CURRENT_TIMESTAMP, 'Asia/Kolkata', 1)")
    init_db(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT name, brief_hour, weekly_enabled FROM companies WHERE id=9")).one()
        version = connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar()
    assert row.name == "Existing Co"
    assert row.brief_hour == 8
    assert row.weekly_enabled in (True, 1)
    assert version == 17
    engine.dispose()


def test_onboarding_migration_adds_stage_table_and_preserves_existing_rows():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE companies (id INTEGER PRIMARY KEY, name VARCHAR(240) NOT NULL, website VARCHAR(1000) NOT NULL, created_at DATETIME NOT NULL, timezone VARCHAR(80) NOT NULL, active BOOLEAN NOT NULL)")
        connection.exec_driver_sql("INSERT INTO companies(id, name, website, created_at, timezone, active) VALUES (18, 'Keep Me', 'https://keep.example/', CURRENT_TIMESTAMP, 'Asia/Kolkata', 1)")
        connection.exec_driver_sql("CREATE TABLE onboarding_stages (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, stage VARCHAR(48) NOT NULL, status VARCHAR(24) NOT NULL, attempts INTEGER NOT NULL, started_at DATETIME, completed_at DATETIME, error TEXT, output_summary TEXT)")
        connection.exec_driver_sql("INSERT INTO onboarding_stages(id,company_id,stage,status,attempts,output_summary) VALUES (7,18,'research','succeeded',1,'old stage')")
        connection.exec_driver_sql("CREATE TABLE interview_sessions (id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,status VARCHAR(32) NOT NULL,generation_method VARCHAR(48) NOT NULL,generation_note TEXT,created_at DATETIME NOT NULL,completed_at DATETIME)")
        connection.exec_driver_sql("INSERT INTO interview_sessions(id,company_id,status,generation_method,created_at) VALUES (8,18,'active','deterministic-profile',CURRENT_TIMESTAMP)")
    init_db(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name FROM companies WHERE id=18")).scalar_one() == "Keep Me"
        assert connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar_one() == 17
    stage_columns = {column["name"] for column in inspect(engine).get_columns("onboarding_stages")}
    assert "onboarding_stages" in inspect(engine).get_table_names()
    assert "input_fingerprint" in stage_columns
    interview_columns = {column["name"] for column in inspect(engine).get_columns("interview_sessions")}
    assert "context_fingerprint" in interview_columns
    with engine.connect() as connection:
        stage = connection.execute(text("SELECT status,output_summary FROM onboarding_stages WHERE id=7")).one()
        interview = connection.execute(text("SELECT status,generation_method FROM interview_sessions WHERE id=8")).one()
    assert stage.status == "succeeded" and stage.output_summary == "old stage"
    assert interview.status == "active" and interview.generation_method == "deterministic-profile"
    engine.dispose()


def test_document_dates_migration_adds_columns_and_preserves_existing_document():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE companies (id INTEGER PRIMARY KEY, name VARCHAR(240) NOT NULL, website VARCHAR(1000) NOT NULL, created_at DATETIME NOT NULL, timezone VARCHAR(80) NOT NULL, brief_weekday INTEGER NOT NULL DEFAULT 0, brief_hour INTEGER NOT NULL DEFAULT 8, weekly_enabled BOOLEAN NOT NULL DEFAULT 1, monthly_audit_enabled BOOLEAN NOT NULL DEFAULT 1, source_refresh_hours INTEGER NOT NULL DEFAULT 24, active BOOLEAN NOT NULL)")
        connection.exec_driver_sql("INSERT INTO companies(id,name,website,created_at,timezone,active) VALUES (1,'Existing','https://existing.example',CURRENT_TIMESTAMP,'UTC',1)")
        connection.exec_driver_sql("CREATE TABLE documents (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, source_id INTEGER, canonical_url VARCHAR(2000) NOT NULL, title VARCHAR(1000) NOT NULL, published_at DATETIME, discovered_at DATETIME NOT NULL, content_hash VARCHAR(64) NOT NULL, content TEXT NOT NULL, category VARCHAR(32) NOT NULL, source_reliability FLOAT NOT NULL, version INTEGER NOT NULL, first_seen_at DATETIME NOT NULL, last_seen_at DATETIME NOT NULL)")
        connection.exec_driver_sql("INSERT INTO documents(id,company_id,canonical_url,title,discovered_at,content_hash,content,category,source_reliability,version,first_seen_at,last_seen_at) VALUES (44,1,'https://existing.example/story','Old story',CURRENT_TIMESTAMP,'hash','saved body','market',0.7,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        connection.exec_driver_sql("CREATE TABLE developments (id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,document_id INTEGER NOT NULL,what_happened TEXT NOT NULL,business_implication TEXT NOT NULL,signal_type VARCHAR(32) NOT NULL,verified BOOLEAN NOT NULL,verification_status VARCHAR(40) NOT NULL,created_at DATETIME NOT NULL)")
        connection.exec_driver_sql("INSERT INTO developments(id,company_id,document_id,what_happened,business_implication,signal_type,verified,verification_status,created_at) VALUES (55,1,44,'Old event','Old implication','neutral',0,'not-reviewed',CURRENT_TIMESTAMP)")
        connection.exec_driver_sql("CREATE TABLE competitors (id INTEGER PRIMARY KEY,company_id INTEGER NOT NULL,name VARCHAR(240) NOT NULL,website VARCHAR(1000),rationale TEXT NOT NULL,evidence_url VARCHAR(2000),status VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql("INSERT INTO competitors(id,company_id,name,rationale,status) VALUES (66,1,'Old Rival','Existing rationale','candidate')")
    init_db(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT title,content FROM documents WHERE id=44")).one()
        columns = {column["name"] for column in inspect(engine).get_columns("documents")}
        development_columns = {column["name"] for column in inspect(engine).get_columns("developments")}
        competitor_columns = {column["name"] for column in inspect(engine).get_columns("competitors")}
        version = connection.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar_one()
    assert row.title == "Old story" and row.content == "saved body"
    assert {"event_date", "modified_at", "is_article"} <= columns
    assert {"factual_claim", "analysis_confidence", "analysis_method", "signal_classification_method"} <= development_columns
    assert {"competitor_type", "evidence_snippet", "discovery_confidence"} <= competitor_columns
    assert {"products_services", "target_market", "customer_segments", "geographic_focus", "competitive_relationship", "supporting_source_urls_json", "discovered_at"} <= competitor_columns
    assert version == 17
    with engine.connect() as connection:
        old_development = connection.execute(text("SELECT what_happened,analysis_method,signal_classification_method FROM developments WHERE id=55")).one()
        old_competitor = connection.execute(text("SELECT name,rationale,competitor_type,products_services,supporting_source_urls_json,discovered_at FROM competitors WHERE id=66")).one()
    assert old_development.what_happened == "Old event" and old_development.analysis_method == "legacy-unassessed" and old_development.signal_classification_method == "legacy-unassessed"
    assert old_competitor.name == "Old Rival" and old_competitor.rationale == "Existing rationale" and old_competitor.competitor_type == "unclassified"
    assert old_competitor.products_services == "" and old_competitor.supporting_source_urls_json == "[]" and old_competitor.discovered_at is None
    assert "company_classifications" in inspect(engine).get_table_names()
    engine.dispose()

