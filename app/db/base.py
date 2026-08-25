"""Database access layer.

Two backends sit behind one interface:

* **SQLite** (:mod:`sqlite3`, standard library) for local development and the
  test suite. Zero dependencies, so the suite runs anywhere Python does.
* **Postgres** (``psycopg`` v3) for production on Neon, whose free tier keeps
  data indefinitely as spec C6 requires.

Both are given the same write-serialisation guarantee, which the preemption
engine (spec §7) depends on for its "exactly one winner" property:

* Postgres opens ``SERIALIZABLE`` transactions and takes ``SELECT … FOR
  UPDATE`` row locks on the overlap set. Serialisation failures are retried.
* SQLite opens ``BEGIN IMMEDIATE``, which takes the database write lock for
  the whole transaction and therefore serialises writers outright. ``FOR
  UPDATE`` is a no-op there because the coarser lock already subsumes it.

SQL is written once using ``?`` placeholders and translated for Postgres.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator, Sequence

from app.timeutil import ensure_utc, isoformat_utc, parse_utc

SQLITE = "sqlite"
POSTGRES = "postgres"

#: How many times a transaction is retried after a serialisation failure.
MAX_RETRIES = 6


class DatabaseError(RuntimeError):
    """Raised for backend problems that are not business-rule failures."""


# --- SQLite type mapping ----------------------------------------------------
#
# Columns are declared as TIMESTAMPTZ / BOOLEAN in the SQLite DDL and decoded
# via PARSE_DECLTYPES. Using the declared type rather than sniffing values
# means a booking title that happens to look like a timestamp is never
# silently converted.

sqlite3.register_adapter(datetime, isoformat_utc)
sqlite3.register_converter("TIMESTAMPTZ", lambda raw: parse_utc(raw.decode()))
sqlite3.register_converter("BOOLEAN", lambda raw: raw not in (b"0", b"", b"false"))


def _to_postgres(sql: str) -> str:
    """Translate ``?`` placeholders to psycopg's ``%s`` form.

    Literal percent signs are doubled first so that patterns such as
    ``LIKE '%x%'`` survive psycopg's own parameter interpolation.
    """
    return sql.replace("%", "%%").replace("?", "%s")


class Connection:
    """A thin, dialect-aware wrapper returning plain dict rows."""

    def __init__(self, raw: Any, dialect: str) -> None:
        self._raw = raw
        self.dialect = dialect

    @property
    def raw(self) -> Any:
        return self._raw

    def _prepare(self, sql: str) -> str:
        return sql if self.dialect == SQLITE else _to_postgres(sql)

    def _bind(self, params: Sequence[Any] | None) -> Sequence[Any]:
        if not params:
            return ()
        if self.dialect == SQLITE:
            return tuple(params)
        # psycopg adapts datetimes natively but naive values would be stored
        # without an offset, so normalise them here instead.
        return tuple(
            ensure_utc(value) if isinstance(value, datetime) else value
            for value in params
        )

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        cursor = self._raw.cursor()
        cursor.execute(self._prepare(sql), self._bind(params))
        return cursor

    def query_all(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[dict[str, Any]]:
        cursor = self.execute(sql, params)
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def query_one(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> dict[str, Any] | None:
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    def query_value(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.execute(sql, params).fetchone()
        return row[0] if row else None

    def for_update(self) -> str:
        """Row-lock clause for the current dialect.

        Empty on SQLite, where ``BEGIN IMMEDIATE`` already holds the write
        lock for the whole transaction.
        """
        return " FOR UPDATE" if self.dialect == POSTGRES else ""


class Database:
    """Backend-agnostic entry point. Create one per process."""

    dialect: str = ""

    def connect(self) -> Connection:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        pass

    def _is_retryable(self, exc: BaseException) -> bool:  # pragma: no cover
        return False

    @contextmanager
    def _begin(self, conn: Connection) -> Iterator[Connection]:  # pragma: no cover
        raise NotImplementedError

    def run_in_transaction(
        self, work: Callable[[Connection], Any], *, retries: int = MAX_RETRIES
    ) -> Any:
        """Run ``work`` inside a serialisable transaction, retrying conflicts.

        ``work`` must be idempotent with respect to retries: it may be invoked
        more than once, so side effects that escape the database (sending
        email above all) must happen *after* this returns. Spec §7.3 is
        explicit that a rollback must never produce a cancellation email for a
        booking that still exists.
        """
        last_error: BaseException | None = None
        for _ in range(retries):
            conn = self.connect()
            try:
                with self._begin(conn) as tx:
                    return work(tx)
            except Exception as exc:  # noqa: BLE001 - re-raised below
                if not self._is_retryable(exc):
                    raise
                last_error = exc
            finally:
                self._release(conn)
        raise DatabaseError(
            f"transaction failed after {retries} serialisation conflicts"
        ) from last_error

    def _release(self, conn: Connection) -> None:
        pass


class SqliteDatabase(Database):
    """SQLite backend for development and tests.

    Connections are per-thread because :mod:`sqlite3` objects cannot be shared
    across threads. The concurrency tests rely on this: each worker thread gets
    a real, independent connection contending for the same write lock.
    """

    dialect = SQLITE

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._local = threading.local()
        # An in-memory database lives only as long as its connection, so a
        # shared cache URI is used to give every thread the same database.
        self._uri = path.startswith("file:")
        if path == ":memory:":
            # A unique name per instance keeps tests isolated from each other
            # while still letting every thread of one instance share the
            # database, which the concurrency tests need.
            self.path = f"file:booking_memdb_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            # Hold one connection open for the process lifetime, otherwise the
            # shared in-memory database is destroyed between transactions.
            self._keepalive = self._new_connection()

    def _new_connection(self) -> sqlite3.Connection:
        raw = sqlite3.connect(
            self.path,
            uri=self._uri,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # explicit transaction control
            timeout=30.0,
            check_same_thread=False,
        )
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA busy_timeout = 30000")
        if not self._uri:
            raw.execute("PRAGMA journal_mode = WAL")
        return raw

    def connect(self) -> Connection:
        raw = getattr(self._local, "raw", None)
        if raw is None:
            raw = self._new_connection()
            self._local.raw = raw
        return Connection(raw, SQLITE)

    def _is_retryable(self, exc: BaseException) -> bool:
        return isinstance(exc, sqlite3.OperationalError) and (
            "locked" in str(exc).lower() or "busy" in str(exc).lower()
        )

    @contextmanager
    def _begin(self, conn: Connection) -> Iterator[Connection]:
        # IMMEDIATE acquires the write lock up front, so two concurrent
        # preemption attempts cannot both read the same overlap set and win.
        conn.raw.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.raw.execute("ROLLBACK")
            raise
        else:
            conn.raw.execute("COMMIT")

    def close(self) -> None:
        raw = getattr(self._local, "raw", None)
        if raw is not None:
            raw.close()
            self._local.raw = None


class PostgresDatabase(Database):
    """Production backend (Neon).

    Not exercised by the local test suite, which has no Postgres available;
    the CI workflow runs the same suite against a real Postgres service
    container so this path is covered before any deploy.
    """

    dialect = POSTGRES

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        try:
            import psycopg  # noqa: PLC0415 - optional production dependency
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise DatabaseError(
                "psycopg is required for the Postgres backend; "
                "install it with `pip install -r requirements.txt`"
            ) from exc
        self._psycopg = psycopg

    def connect(self) -> Connection:
        raw = self._psycopg.connect(self.dsn, autocommit=False)
        with raw.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")
        return Connection(raw, POSTGRES)

    def _is_retryable(self, exc: BaseException) -> bool:
        errors = self._psycopg.errors
        return isinstance(exc, (errors.SerializationFailure, errors.DeadlockDetected))

    @contextmanager
    def _begin(self, conn: Connection) -> Iterator[Connection]:
        conn.raw.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
        try:
            yield conn
        except BaseException:
            conn.raw.rollback()
            raise
        else:
            conn.raw.commit()

    def _release(self, conn: Connection) -> None:
        conn.raw.close()


_STATEMENT_SPLIT = re.compile(r";\s*(?:\n|$)")


def execute_script(conn: Connection, script: str) -> None:
    """Run a multi-statement DDL script one statement at a time.

    Kept dialect-neutral rather than using ``sqlite3.executescript``, which
    would commit the surrounding transaction.

    Whole-line ``--`` comments are stripped before splitting. Skipping a
    comment *chunk* instead would silently drop the statement that follows it,
    which is exactly the kind of half-applied schema that is painful to debug
    in production.
    """
    without_comments = "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("--")
    )
    for statement in _STATEMENT_SPLIT.split(without_comments):
        text = statement.strip()
        if text:
            conn.execute(text)
