from ncte.memory import db


def test_migrations_idempotent(tmp_path):
    c = db.connect(tmp_path / "a.db")
    v = db.current_version(c)
    assert v == max(m[0] for m in db.MIGRATIONS)
    assert db.migrate(c) == v
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()


def test_new_migration_keeps_data(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "a.db")
    c.execute("INSERT INTO model_snapshots(fetched_at, source, models_json) VALUES ('t','s','[]')")
    c.close()
    extra = (999, "test add column", "ALTER TABLE model_snapshots ADD COLUMN note TEXT;")
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, extra])
    c = db.connect(tmp_path / "a.db")
    assert db.current_version(c) == 999
    assert c.execute("SELECT COUNT(*) FROM model_snapshots").fetchone()[0] == 1


def test_split_sql_handles_triggers():
    sql = "CREATE TABLE a(x);\nCREATE TRIGGER t AFTER INSERT ON a BEGIN\n  SELECT 1;\n  SELECT 2;\nEND;\nCREATE TABLE b(y);"
    parts = db._split_sql(sql)
    assert len(parts) == 3 and "SELECT 2" in parts[1]


def test_an_older_workspace_gains_finding_versions_without_losing_findings(tmp_path, monkeypatch):
    """Migration 22 only adds a column: findings made before it keep their status and read as 'any version'."""
    monkeypatch.setattr(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] < 22])
    c = db.connect(tmp_path / "old.db")
    c.execute("INSERT INTO qa_findings(book_id, unit_id, type, severity, evidence, status, created_at) "
              "VALUES ('b','u','review:omission','high','x','open','t')")
    c.close()
    monkeypatch.undo()
    c = db.connect(tmp_path / "old.db")
    assert db.current_version(c) == max(m[0] for m in db.MIGRATIONS)
    row = c.execute("SELECT status, translation_id FROM qa_findings").fetchone()
    assert (row["status"], row["translation_id"]) == ("open", None)
    c.close()
