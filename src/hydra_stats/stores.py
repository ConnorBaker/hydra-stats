"""Read-only access to the Nix store's SQLite database.

Located at ``/nix/var/nix/db/db.sqlite`` (overridable via
``NIX_STATE_DIR``). Opened ``mode=ro`` so we coexist with a live
nix-daemon.

Schema (stable for years):

    ValidPaths(id INTEGER PK, path TEXT UNIQUE, narSize INTEGER, ...)
    Refs(referrer INTEGER, reference INTEGER, PRIMARY KEY(referrer, reference))

Following ``referrer -> reference`` from a .drv id yields its
build-time closure; from an output id, its runtime closure.
"""

# pyright: strict

import contextlib
import logging
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def default_nix_db_path() -> Path:
    """Resolve the Nix DB path, honouring ``NIX_STATE_DIR``."""
    if state := os.environ.get("NIX_STATE_DIR"):
        return Path(state) / "db" / "db.sqlite"
    return Path("/nix/var/nix/db/db.sqlite")


def _require_tables(conn: sqlite3.Connection, schema: str, path: Path) -> None:
    rows = conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table'").fetchall()
    missing = {"ValidPaths", "Refs"} - {str(r["name"]) for r in rows}
    if missing:
        raise RuntimeError(f"Nix DB at {path} missing tables {sorted(missing)}")


# Big-cache pragmas. Connection-local, safe on a RO handle, safe on a
# small host (SQLite sizes its actual cache on demand).
_BIG_CACHE = (
    "PRAGMA cache_size = -67108864",  # 64 GiB page cache
    "PRAGMA mmap_size = 137438953472",  # 128 GiB mmap ceiling
    "PRAGMA temp_store = MEMORY",
)


def _tune(conn: sqlite3.Connection, extra: tuple[str, ...] = ()) -> None:
    for pragma in (*_BIG_CACHE, *extra):
        conn.execute(pragma)


def open_nix_db(path: Path | str) -> sqlite3.Connection:
    """Open the Nix DB read-only, tuned for a large store."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Nix DB not found at {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    _tune(conn)
    _require_tables(conn, "main", p)
    return conn


def closure_drvs(conn: sqlite3.Connection, top_drv: str) -> set[str]:
    """The .drv paths in the build-time closure of ``top_drv``.

    Empty set means ``top_drv`` isn't in ValidPaths (GC'd / never
    imported) -- callers treat as "closure unknown".
    """
    row = conn.execute("SELECT id FROM ValidPaths WHERE path = ?", (top_drv,)).fetchone()
    if row is None:
        return set()
    rows = conn.execute(
        """
        WITH RECURSIVE closure(id) AS (
            VALUES (?) UNION
            SELECT r.reference FROM Refs r JOIN closure c ON c.id = r.referrer
        )
        SELECT v.path FROM ValidPaths v JOIN closure c ON c.id = v.id
        WHERE  v.path LIKE '%.drv'
        """,
        (int(row["id"]),),
    ).fetchall()
    return {str(r["path"]) for r in rows}


@contextlib.contextmanager
def _heartbeat(conn: sqlite3.Connection, label: str) -> Iterator[None]:
    """Log ``label`` every 5s while a long SQLite query runs.

    Uses SQLite's progress handler (every 1M VDBE ops). Must return 0
    -- non-zero aborts the query.
    """
    start = time.monotonic()
    state = {"last": start}

    def tick() -> int:
        now = time.monotonic()
        if now - state["last"] >= 5.0:
            state["last"] = now
            log.info("%s: still running (%.0fs elapsed)", label, now - start)
        return 0

    conn.set_progress_handler(tick, 1_000_000)
    try:
        yield
    finally:
        conn.set_progress_handler(None, 0)


@contextlib.contextmanager
def _timed(label: str) -> Iterator[None]:
    log.info("eval-sizes: %s", label)
    t0 = time.monotonic()
    yield
    log.info("eval-sizes: %s done in %.0fs", label, time.monotonic() - t0)


# ---- Eval disk-usage computation ------------------------------------------


@dataclass(frozen=True)
class DiskUsage:
    # {eval_id: (total_bytes, unique_bytes, paths_resolved, paths_recorded)}
    per_eval: dict[int, tuple[int, int, int, int]]
    server_evals: int
    server_paths: int


def compute_eval_disk_usage(
    nix_db_path: Path,
    seed_stream: Iterable[tuple[int, str]],
    target_eval_ids: Iterable[int],
) -> DiskUsage:
    """Per-target-eval disk usage in one SQLite session.

    1. Bulk-load ``(eval_id, path)`` pairs into scratch; resolve to
       ValidPaths.id in SQL.
    2. Walk Refs once from every non-target seed into a one-column
       ``nontarget_reach(path_id)`` set (dedup bounded by |distinct
       reachable paths|, ~20M on a big Hydra).
    3. Walk Refs once from every target seed into a two-column
       ``target_closure(eval_id, path_id)``.
    4. Aggregate: for each target path, ``unique`` = narSize iff no
       non-target reaches it AND no *other* target reaches it.

    Evals whose outputs are all GC'd surface as ``(0, 0, 0, recorded)``.
    """
    if not nix_db_path.exists():
        raise FileNotFoundError(f"Nix DB not found at {nix_db_path}")
    target_ids = list(target_eval_ids)
    # RW scratch in memory, with the Nix DB attached read-only so the
    # planner can join across both halves while the Nix DB stays RO.
    # ``uri=True`` is required for ATTACH to interpret ``file:?mode=ro``
    # as a URI rather than a literal filename.
    conn = sqlite3.connect(":memory:", uri=True)
    conn.row_factory = sqlite3.Row
    _tune(conn, extra=("PRAGMA journal_mode = OFF", "PRAGMA synchronous = OFF"))
    conn.execute("ATTACH DATABASE ? AS nix", (f"file:{nix_db_path}?mode=ro",))
    _require_tables(conn, "nix", nix_db_path)
    try:
        return _run_disk_usage(conn, seed_stream, target_ids)
    finally:
        conn.close()


_BATCH = 50_000


def _bulk_load(conn: sqlite3.Connection, stream: Iterable[tuple[int, str]]) -> int:
    """Drain ``stream`` into ``seed_stream`` via batched executemany."""
    batch: list[tuple[int, str]] = []
    n = 0
    for pair in stream:
        batch.append(pair)
        if len(batch) >= _BATCH:
            conn.executemany("INSERT INTO seed_stream VALUES (?, ?)", batch)
            n += len(batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO seed_stream VALUES (?, ?)", batch)
        n += len(batch)
    return n


def _count_by_eval(conn: sqlite3.Connection, query: str) -> dict[int, int]:
    return {int(r["eval_id"]): int(r["n"]) for r in conn.execute(query)}


def _walk(conn: sqlite3.Connection, label: str, create: str, body: str) -> None:
    """Create a destination table, then populate it from a recursive
    ``WITH RECURSIVE walk(...)`` CTE whose body is ``body``."""
    with _timed(label), _heartbeat(conn, f"eval-sizes: {label}"):
        conn.execute(create)
        conn.execute(body)


def _run_disk_usage(
    conn: sqlite3.Connection,
    seed_stream: Iterable[tuple[int, str]],
    target_eval_ids: list[int],
) -> DiskUsage:
    conn.execute("CREATE TEMP TABLE target_evals(eval_id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO target_evals VALUES (?)", ((int(e),) for e in target_eval_ids))
    conn.execute("CREATE TEMP TABLE seed_stream(eval_id INTEGER, path TEXT)")

    with _timed("bulk-loading Postgres stream into SQLite"):
        n_seen = _bulk_load(conn, seed_stream)
    log.info("eval-sizes: loaded %d pairs", n_seen)

    with _timed("resolving paths -> ValidPaths.id"):
        conn.execute("CREATE TEMP TABLE seeds(eval_id INTEGER, path_id INTEGER, is_target INTEGER)")
        conn.execute(
            """
            INSERT INTO seeds
            SELECT s.eval_id, v.id, (t.eval_id IS NOT NULL)
            FROM   seed_stream s
            JOIN   nix.ValidPaths v ON v.path = s.path
            LEFT   JOIN target_evals t ON t.eval_id = s.eval_id
            """
        )
        conn.execute("CREATE INDEX seeds_t ON seeds(is_target, path_id)")

    paths_recorded = _count_by_eval(
        conn,
        "SELECT s.eval_id, COUNT(*) AS n FROM seed_stream s "
        "JOIN target_evals t ON t.eval_id = s.eval_id GROUP BY s.eval_id",
    )
    paths_resolved = _count_by_eval(
        conn,
        "SELECT eval_id, COUNT(DISTINCT path_id) AS n FROM seeds "
        "WHERE is_target = 1 GROUP BY eval_id",
    )
    srv = conn.execute(
        "SELECT COUNT(DISTINCT eval_id) AS e, COUNT(DISTINCT path) AS p FROM seed_stream"
    ).fetchone()
    server_evals = int(srv["e"] or 0) if srv else 0
    server_paths = int(srv["p"] or 0) if srv else 0
    conn.execute("DROP TABLE seed_stream")

    _walk(
        conn,
        "nontarget walk",
        "CREATE TEMP TABLE nontarget_reach(path_id INTEGER PRIMARY KEY)",
        """
        WITH RECURSIVE walk(pid) AS (
            SELECT DISTINCT path_id FROM seeds WHERE is_target = 0 UNION
            SELECT r.reference FROM nix.Refs r JOIN walk w ON w.pid = r.referrer
        )
        INSERT INTO nontarget_reach SELECT pid FROM walk
        """,
    )
    _walk(
        conn,
        "target walk",
        "CREATE TEMP TABLE target_closure(eval_id INTEGER, path_id INTEGER, "
        "PRIMARY KEY(eval_id, path_id)) WITHOUT ROWID",
        """
        WITH RECURSIVE walk(eval_id, pid) AS (
            SELECT eval_id, path_id FROM seeds WHERE is_target = 1 UNION
            SELECT w.eval_id, r.reference FROM nix.Refs r JOIN walk w ON w.pid = r.referrer
        )
        INSERT INTO target_closure SELECT eval_id, pid FROM walk
        """,
    )
    conn.execute("CREATE INDEX tc_pid ON target_closure(path_id)")

    out: dict[int, tuple[int, int, int, int]] = {}
    with _timed("aggregation"), _heartbeat(conn, "eval-sizes: aggregation"):
        rows = conn.execute(
            """
            WITH target_owners(path_id, n_targets) AS (
                SELECT path_id, COUNT(*) FROM target_closure GROUP BY path_id
            )
            SELECT tc.eval_id                  AS eval_id,
                   COALESCE(SUM(v.narSize), 0) AS total,
                   COALESCE(SUM(CASE
                       WHEN nr.path_id IS NULL AND own.n_targets = 1 THEN v.narSize
                       ELSE 0 END), 0)         AS unique_
            FROM   target_closure tc
            JOIN   nix.ValidPaths v  ON v.id       = tc.path_id
            JOIN   target_owners own ON own.path_id = tc.path_id
            LEFT   JOIN nontarget_reach nr ON nr.path_id = tc.path_id
            GROUP  BY tc.eval_id
            """
        )
        for r in rows:
            eid = int(r["eval_id"])
            out[eid] = (
                int(r["total"] or 0),
                int(r["unique_"] or 0),
                paths_resolved.get(eid, 0),
                paths_recorded.get(eid, 0),
            )
    for eid in target_eval_ids:
        out.setdefault(eid, (0, 0, 0, paths_recorded.get(eid, 0)))
    return DiskUsage(out, server_evals, server_paths)
