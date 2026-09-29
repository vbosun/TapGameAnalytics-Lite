from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
except ModuleNotFoundError:  # PostgreSQL is an optional lightweight-edition extra.
    psycopg = None
    sql = None
    dict_row = None


ROOT = Path(__file__).resolve().parents[1]
SQLITE_MIGRATION = Path(__file__).with_name("sqlite_owner_schema.sql")
POSTGRES_MIGRATION = Path(__file__).with_name("postgres_owner_schema.sql")


def is_sqlite_url(database_url: str) -> bool:
    return database_url.startswith("sqlite:///") or database_url == "sqlite://"


def default_database_url() -> str:
    return f"sqlite:///{(ROOT / 'data' / 'tapgame.db').as_posix()}"


def _sqlite_path(database_url: str) -> Path:
    if database_url == "sqlite://":
        return Path(":memory:")
    raw = database_url.removeprefix("sqlite:///")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _adapt(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    # psycopg Jsonb exposes the original object as ``obj``.
    if value.__class__.__name__ == "Jsonb" and hasattr(value, "obj"):
        return json.dumps(value.obj, ensure_ascii=False)
    return value


class SQLiteCursor:
    def __init__(self, connection: "SQLiteConnection") -> None:
        self.connection = connection
        self.cursor = connection.raw.cursor()

    def execute(self, statement: str, params=()) -> "SQLiteCursor":
        self.cursor.execute(statement.replace("%s", "?"), tuple(_adapt(v) for v in params))
        return self

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)

    def __enter__(self) -> "SQLiteCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cursor.close()


class SQLiteConnection:
    dialect = "sqlite"

    def __init__(self, path: Path) -> None:
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.raw = sqlite3.connect(str(path), timeout=30)
        self.raw.row_factory = sqlite3.Row
        self.raw.execute("PRAGMA foreign_keys=ON")
        self.raw.execute("PRAGMA journal_mode=WAL")
        self.raw.create_function("octet_length", 1, lambda value: len(value) if value else None)

    def execute(self, statement: str, params=()) -> SQLiteCursor:
        return SQLiteCursor(self).execute(statement, params)

    def cursor(self) -> SQLiteCursor:
        return SQLiteCursor(self)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self) -> "SQLiteConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


class OwnerStore:
    """Small storage facade shared by the lite SQLite and PostgreSQL editions."""

    def __init__(self, database_url: str, schema: str = "tap_game") -> None:
        self.database_url = database_url
        self.schema = schema
        if is_sqlite_url(database_url):
            self.conn = SQLiteConnection(_sqlite_path(database_url))
            self.conn.raw.executescript(SQLITE_MIGRATION.read_text(encoding="utf-8"))
            self.conn.commit()
        else:
            if psycopg is None or sql is None:
                raise RuntimeError(
                    "PostgreSQL 支持未安装；请执行 pip install 'tap-game-analytics-lite[postgres]'"
                )
            self.conn = psycopg.connect(database_url, row_factory=dict_row)
            namespace = self.conn.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)
            ).fetchone()
            if not namespace:
                self.conn.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
                )
            self.conn.execute(
                sql.SQL("SET search_path TO {},public").format(sql.Identifier(schema))
            )
            owner_table = self.conn.execute(
                """SELECT 1 FROM information_schema.tables
                   WHERE table_schema=%s AND table_name='owner_game'""",
                (schema,),
            ).fetchone()
            if not owner_table:
                self.conn.execute(POSTGRES_MIGRATION.read_text(encoding="utf-8"))
                self.conn.commit()

    @property
    def dialect(self) -> str:
        return getattr(self.conn, "dialect", "postgres")

    def start_run(self, target: str) -> int:
        if self.dialect == "sqlite":
            cursor = self.conn.execute(
                "INSERT INTO collect_run(started_at,target) VALUES(CURRENT_TIMESTAMP,%s)",
                (target,),
            )
            run_id = int(cursor.cursor.lastrowid)
        else:
            run_id = int(self.conn.execute(
                "INSERT INTO collect_run(started_at,target) VALUES(CURRENT_TIMESTAMP,%s) RETURNING run_id",
                (target,),
            ).fetchone()["run_id"])
        self.conn.commit()
        return run_id

    def finish_run(
        self, run_id: int, total: int, ok: int, failed: int, note: str = ""
    ) -> None:
        self.conn.execute(
            """UPDATE collect_run SET finished_at=CURRENT_TIMESTAMP,req_total=%s,
               req_ok=%s,req_failed=%s,note=%s WHERE run_id=%s""",
            (total, ok, failed, note, run_id),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


def open_owner_store(database_url: str, schema: str = "tap_game") -> OwnerStore:
    return OwnerStore(database_url or default_database_url(), schema)
