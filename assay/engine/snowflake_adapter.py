"""Snowflake adapter.

Three things differ from DuckDB in ways that silently produce wrong answers
rather than errors, so each is handled explicitly below: identifier case
folding, parameter binding style, and the absence of a read-only connection
flag.

Credentials are read from the environment only. Assay never accepts a
password or key as a command-line argument — that puts it in shell history
and in `ps` output for every other user on the host.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from assay.engine.adapter import Query

_SELECT_ONLY = re.compile(r"^\s*(?:with\b|select\b)", re.IGNORECASE)


class SnowflakeDialect:
    """Quoting and date arithmetic for Snowflake.

    Snowflake folds unquoted identifiers to upper case at parse time, so a
    table created as `orders` is stored as `ORDERS`. Emitting `"orders"`
    quotes it exactly and resolves to a *different*, usually non-existent
    object. Folding to upper before quoting reproduces what an unquoted
    reference would have done, which is what a contract naming `orders`
    means on a conventional project.

    Projects that deliberately created lower-case quoted identifiers need
    `case_policy="exact"` instead — there is no way to satisfy both, so it is
    a setting rather than a guess.
    """

    def __init__(self, case_policy: str = "upper") -> None:
        if case_policy not in ("upper", "exact"):
            raise ValueError(f"unknown case policy: {case_policy!r}")
        self._policy = case_policy

    def quote(self, identifier: str) -> str:
        folded = identifier.upper() if self._policy == "upper" else identifier
        return '"' + folded.replace('"', '""') + '"'

    def date_trunc(self, grain: str, expression: str) -> str:
        return f"DATE_TRUNC('{grain.upper()}', {expression})"


@dataclass(frozen=True)
class SnowflakeConfig:
    """Connection settings, all sourced from the environment."""

    account: str
    user: str
    warehouse: str
    database: str
    schema: str
    role: Optional[str] = None
    authenticator: str = "externalbrowser"
    private_key_file: Optional[str] = None

    @classmethod
    def from_env(cls, environ: Optional[dict[str, str]] = None) -> "SnowflakeConfig":
        env = dict(environ if environ is not None else os.environ)
        if "SNOWFLAKE_PASSWORD" in env:
            raise ValueError(
                "SNOWFLAKE_PASSWORD is not supported. Use key-pair auth "
                "(SNOWFLAKE_PRIVATE_KEY_FILE) or SSO (the default "
                "externalbrowser authenticator)."
            )
        missing = [key for key in cls._REQUIRED if not env.get(key)]
        if missing:
            raise ValueError(f"missing environment variables: {', '.join(missing)}")
        return cls(
            account=env["SNOWFLAKE_ACCOUNT"],
            user=env["SNOWFLAKE_USER"],
            warehouse=env["SNOWFLAKE_WAREHOUSE"],
            database=env["SNOWFLAKE_DATABASE"],
            schema=env["SNOWFLAKE_SCHEMA"],
            role=env.get("SNOWFLAKE_ROLE"),
            authenticator=env.get("SNOWFLAKE_AUTHENTICATOR", "externalbrowser"),
            private_key_file=env.get("SNOWFLAKE_PRIVATE_KEY_FILE"),
        )

    _REQUIRED = (
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_USER",
        "SNOWFLAKE_WAREHOUSE",
        "SNOWFLAKE_DATABASE",
        "SNOWFLAKE_SCHEMA",
    )

    def connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "account": self.account,
            "user": self.user,
            "warehouse": self.warehouse,
            "database": self.database,
            "schema": self.schema,
            "authenticator": self.authenticator,
            "client_session_keep_alive": False,
        }
        if self.role:
            kwargs["role"] = self.role
        if self.private_key_file:
            kwargs["private_key_file"] = self.private_key_file
            kwargs["authenticator"] = "snowflake"
        return kwargs


class SnowflakeAdapter:
    """Read-only Snowflake access.

    Snowflake has no read-only connection flag, so the guarantee is enforced
    twice: point this at a role granted only SELECT, and every statement is
    checked here before it is sent. Assay generates all of its own SQL, so a
    statement that is not a query means something has gone wrong upstream.
    """

    def __init__(
        self,
        config: Optional[SnowflakeConfig] = None,
        as_of: Optional[datetime] = None,
        case_policy: str = "upper",
        connector: Any = None,
    ) -> None:
        self.dialect = SnowflakeDialect(case_policy)
        self._as_of = as_of
        self._config = config or SnowflakeConfig.from_env()
        self._connector = connector or _import_connector()
        # The connector reads its bind style from a module-level global. Assay
        # builds every statement it runs with `?` placeholders, so this is set
        # once here rather than translating placeholders per statement.
        self._connector.paramstyle = "qmark"
        self._conn = self._connector.connect(**self._config.connect_kwargs())

    def fetch(self, query: Query) -> list[tuple[Any, ...]]:
        _assert_read_only(query.sql)
        cursor = self._conn.cursor()
        try:
            cursor.execute(query.sql, list(query.params))
            return [tuple(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def now(self) -> datetime:
        """Assay's clock, not the session's — a warehouse timezone setting
        should not change whether a freshness check passes."""
        return self._as_of or datetime.now(timezone.utc)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SnowflakeAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _assert_read_only(sql: str) -> None:
    if not _SELECT_ONLY.match(sql):
        raise PermissionError(f"refusing to run a non-query statement: {sql[:80]!r}")


def _import_connector() -> Any:
    try:
        import snowflake.connector as connector
    except ImportError as exc:  # pragma: no cover - exercised by the error path
        raise ImportError(
            "the Snowflake target needs `pip install 'assay[snowflake]'`"
        ) from exc
    return connector
