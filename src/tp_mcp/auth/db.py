"""Shared SQLite connection for the multi-user credential + OAuth stores.

A single database file holds both the per-user TrainingPeaks cookies
(:mod:`tp_mcp.auth.multiuser_store`) and the OAuth clients/tokens/codes
(:mod:`tp_mcp.oauth.store`). Each module creates its own tables with
``CREATE TABLE IF NOT EXISTS`` so initialization order does not matter.
"""

import contextlib
import os
import sqlite3
import stat
from pathlib import Path

from tp_mcp.auth.encrypted import CONFIG_DIR

DEFAULT_DB_PATH = CONFIG_DIR / "tp_mcp.db"


def get_db_path() -> Path:
    """Resolve the database path.

    Honors ``TP_MCP_DB_PATH`` (also accepts the ``TP_MCP_TOKEN_STORE_PATH`` alias
    used in the deploy docs); defaults to a file in the config dir.
    """
    override = os.environ.get("TP_MCP_DB_PATH") or os.environ.get("TP_MCP_TOKEN_STORE_PATH")
    return Path(override) if override else DEFAULT_DB_PATH


def connect() -> sqlite3.Connection:
    """Open the shared database with sane defaults and secure permissions."""
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, stat.S_IRWXU)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return conn
