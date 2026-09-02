"""Live demo service: runs Assay nightly against a synthetic warehouse.

Deliberately self-contained. It holds no credential, reaches no real
warehouse, and exposes no way for a visitor to trigger a run — a public
"run it now" button is a free denial-of-wallet primitive.

The nightly job restates a closed month before each run, so the demo
continuously reproduces the one finding no other tool reports.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from assay.contracts.sources import YamlSource
from assay.engine.duckdb_adapter import DuckDBAdapter
from assay.engine.sql import Window
from assay.invariants.base import Status
from assay.run.history import History
from assay.run.runner import run
from demo import data, seed

DATA_DIR = Path(os.environ.get("ASSAY_DATA_DIR", "/data"))
WAREHOUSE = DATA_DIR / "demo.duckdb"
HISTORY = DATA_DIR / "history.db"
CONTRACTS = Path(__file__).resolve().parents[2] / "demo" / "contracts.yml"
DEMO_DAYS = 365          # enough months for every check, quick to seed
LOOKBACK_DAYS = 400

_lock = threading.Lock()


_ready = threading.Event()
_error: dict[str, str] = {}


def _clock() -> datetime:
    """Demo data is anchored to a fixed date, so 'now' tracks it, not the wall."""
    return data.AS_OF.replace(tzinfo=timezone.utc) + timedelta(days=_elapsed_days())


def _elapsed_days() -> int:
    return int(_state().get("cycles", 0))


def _state() -> dict[str, Any]:
    if not HISTORY.exists():
        return {}
    with sqlite3.connect(HISTORY) as c:
        c.execute("CREATE TABLE IF NOT EXISTS demo_state (k TEXT PRIMARY KEY, v TEXT)")
        return {k: v for k, v in c.execute("SELECT k, v FROM demo_state")}


def _bump_cycle() -> None:
    with sqlite3.connect(HISTORY) as c:
        c.execute("CREATE TABLE IF NOT EXISTS demo_state (k TEXT PRIMARY KEY, v TEXT)")
        c.execute(
            "INSERT INTO demo_state (k, v) VALUES ('cycles', '1') "
            "ON CONFLICT (k) DO UPDATE SET v = CAST(CAST(v AS INTEGER) + 1 AS TEXT)"
        )


def _is_complete(path: Path) -> bool:
    """Every expected table present and non-empty.

    A crash mid-seed leaves a readable DuckDB file with some tables missing or
    short, and `path.exists()` cannot tell that apart from a finished one. The
    checks would then run against a truncated warehouse and report defects that
    are artefacts of the seed rather than of the data — which is precisely the
    failure this project exists to catch, so it should not ship it.
    """
    if not path.exists():
        return False
    try:
        with duckdb.connect(str(path), read_only=True) as conn:
            return all(
                conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] > 0
                for table in data.TABLES
            )
    except duckdb.Error:
        return False


def _seed_atomically() -> None:
    """Build to a side path and rename, so a kill mid-seed leaves no half file."""
    building = WAREHOUSE.with_suffix(".building")
    building.unlink(missing_ok=True)
    seed.seed(building, days=DEMO_DAYS)
    building.replace(WAREHOUSE)


def execute_cycle(mutate: bool) -> dict[str, Any]:
    """One nightly cycle: optionally restate a closed month, then check."""
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not _is_complete(WAREHOUSE):
            _seed_atomically()
        elif mutate:
            seed.seed(WAREHOUSE, backfill=True)
            _bump_cycle()
        as_of = _clock()
        history = History(HISTORY)
        with DuckDBAdapter(str(WAREHOUSE), as_of=as_of) as adapter:
            summary = run(
                YamlSource(CONTRACTS).load(),
                adapter,
                Window(start=(as_of - timedelta(days=LOOKBACK_DAYS)).date()),
                history=history,
            )
        history.record_checks(summary.run_id, summary.results, summary.ran_at)
        history.close()
        return _serialise(summary)


def _serialise(summary) -> dict[str, Any]:
    return {
        "run_id": summary.run_id,
        "ran_at": summary.ran_at.isoformat(),
        "scans": summary.scans,
        "duration_s": round(summary.duration_s, 3),
        "counts": {
            "failed": len(summary.failures),
            "warned": len(summary.warnings),
            "passed": len(summary.by_status(Status.PASS)),
        },
        "findings": [
            {
                "invariant": r.invariant_id,
                "subject": r.subject,
                "status": r.status.value,
                "detail": r.detail,
            }
            for r in summary.results
            if r.violated
        ],
    }


def _warm_up() -> None:
    """Seeding a year of demo data takes tens of seconds.

    It must not run inside the ASGI lifespan: uvicorn would not bind until it
    finished, and the platform would see a dead port and cycle the container.
    """
    try:
        execute_cycle(mutate=False)
    except Exception as exc:  # noqa: BLE001 - surfaced through /health
        _error["warm_up"] = f"{type(exc).__name__}: {exc}"
    finally:
        _ready.set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_warm_up, daemon=True).start()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(lambda: execute_cycle(mutate=True), "cron", hour=3, minute=0)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Assay demo", lifespan=lifespan, docs_url="/docs")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"]
)


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "Assay live demo",
        "what": "Runs Assay nightly against a synthetic warehouse that restates "
                "a closed month each cycle. Read-only; holds no credential.",
        "ready": _ready.is_set(),
        "endpoints": ["/health", "/api/latest", "/api/runs", "/api/restatements", "/docs"],
        "source": "https://github.com/swathikchgithub/assay",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    """Ready as soon as the port is bound; `warming` while the demo seeds."""
    if _error:
        return {"status": "degraded", "error": _error.get("warm_up", "")}
    return {"status": "ok" if _ready.is_set() else "warming"}


@app.get("/api/latest")
def latest() -> dict[str, Any]:
    """Findings from the most recent run."""
    with sqlite3.connect(HISTORY) as c:
        row = c.execute("SELECT run_id, ran_at FROM check_run ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return {"findings": []}
        run_id, ran_at = row
        rows = c.execute(
            "SELECT invariant_id, subject, status, detail FROM check_run "
            "WHERE run_id = ? AND status IN ('fail','warn') ORDER BY status, invariant_id",
            (run_id,),
        ).fetchall()
    return {
        "run_id": run_id,
        "ran_at": ran_at,
        "findings": [
            {"invariant": i, "subject": s, "status": st, "detail": d} for i, s, st, d in rows
        ],
    }


@app.get("/api/runs")
def runs(limit: int = 30) -> dict[str, Any]:
    """One row per run: when, and how many checks landed in each status."""
    with sqlite3.connect(HISTORY) as c:
        rows = c.execute(
            "SELECT run_id, min(ran_at), "
            "sum(status='fail'), sum(status='warn'), sum(status='pass') "
            "FROM check_run GROUP BY run_id ORDER BY min(id) DESC LIMIT ?",
            (min(limit, 200),),
        ).fetchall()
    return {
        "runs": [
            {"run_id": r, "ran_at": t, "failed": f, "warned": w, "passed": p}
            for r, t, f, w, p in rows
        ]
    }


@app.get("/api/restatements")
def restatements() -> dict[str, Any]:
    """Every closed period Assay has seen move — the headline finding."""
    with sqlite3.connect(HISTORY) as c:
        rows = c.execute(
            "SELECT subject, detail, observed, expected, ran_at FROM check_run "
            "WHERE invariant_id = 'TMP-03' AND status = 'fail' ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return {
        "restatements": [
            {"metric": s, "detail": d, "now": o, "was": e, "detected_at": t}
            for s, d, o, e, t in rows
        ]
    }
