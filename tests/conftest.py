"""Force tests onto temp SQLite paths — never touch production data/ DBs."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_tmpdir: Path | None = None


@pytest.fixture(scope="session", autouse=True)
def _isolate_db_env():
    global _tmpdir
    _tmpdir = Path(tempfile.mkdtemp(prefix="unify-test-"))
    os.environ["UNIFY_USERS_DB"] = str(_tmpdir / "users.db")
    os.environ["UNIFY_STATS_DB"] = str(_tmpdir / "stats.db")
    yield
    os.environ.pop("UNIFY_USERS_DB", None)
    os.environ.pop("UNIFY_STATS_DB", None)
