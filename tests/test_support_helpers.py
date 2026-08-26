"""The test harness itself, where it makes backend-specific assumptions.

CI broke once because a test injected a SQLite error to exercise the retry
path, and the Postgres backend quite correctly refused to retry it. The fix
was `retryable_error()`; this is what stops it regressing, including on the
Postgres branch that cannot be executed on a machine without psycopg.
"""

from __future__ import annotations

import sqlite3
import sys
import types
import unittest

from app.db import POSTGRES
from tests.support import make_db, retryable_error


class _FakeSerializationFailure(Exception):
    pass


class _FakeDeadlockDetected(Exception):
    pass


def _fake_psycopg() -> types.ModuleType:
    """Just enough psycopg for PostgresDatabase to be constructed.

    ``PostgresDatabase.__init__`` only imports the module and keeps a
    reference; it does not connect until asked, so the retry predicate can be
    exercised without a server.
    """
    module = types.ModuleType("psycopg")
    errors = types.ModuleType("psycopg.errors")
    errors.SerializationFailure = _FakeSerializationFailure
    errors.DeadlockDetected = _FakeDeadlockDetected
    module.errors = errors
    module.connect = lambda *a, **k: None
    return module


class RetryableErrorTests(unittest.TestCase):
    def test_the_sqlite_branch_produces_something_sqlite_retries(self):
        db = make_db()
        self.addCleanup(db.close)
        if db.dialect == POSTGRES:
            self.skipTest("suite is running against Postgres")

        error = retryable_error(db)
        self.assertIsInstance(error, sqlite3.OperationalError)
        self.assertTrue(db._is_retryable(error))

    def test_the_postgres_branch_produces_something_postgres_retries(self):
        """Exercised with a stubbed driver, so it runs anywhere."""
        from app.db.base import PostgresDatabase

        saved = sys.modules.get("psycopg")
        sys.modules["psycopg"] = _fake_psycopg()
        self.addCleanup(
            lambda: sys.modules.__setitem__("psycopg", saved)
            if saved is not None
            else sys.modules.pop("psycopg", None)
        )

        db = PostgresDatabase("postgresql://example/db")
        error = retryable_error(db)

        self.assertIsInstance(error, _FakeSerializationFailure)
        self.assertTrue(
            db._is_retryable(error),
            "the error the harness injects is not one this backend retries",
        )

    def test_a_foreign_error_is_not_retried_by_either_backend(self):
        """The guard has to be selective, or a real bug would be retried away."""
        from app.db.base import PostgresDatabase

        sqlite_db = make_db()
        self.addCleanup(sqlite_db.close)
        self.assertFalse(sqlite_db._is_retryable(ValueError("nope")))
        # A SQLite lock error is exactly what used to leak into the Postgres
        # run; it must not be treated as retryable there.
        saved = sys.modules.get("psycopg")
        sys.modules["psycopg"] = _fake_psycopg()
        self.addCleanup(
            lambda: sys.modules.__setitem__("psycopg", saved)
            if saved is not None
            else sys.modules.pop("psycopg", None)
        )
        pg = PostgresDatabase("postgresql://example/db")
        self.assertFalse(pg._is_retryable(sqlite3.OperationalError("database is locked")))
