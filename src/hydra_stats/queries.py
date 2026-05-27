"""Postgres queries + DSN plumbing + metric/point/summary types.

All queries run with ``autocommit=True`` (Hydra's schema is huge and a
long-lived implicit transaction against the production DB is risky).
Callers should pass ``statement_timeout_ms`` to ``connect`` to bound
scan time; CLI defaults to 60s.
"""

# pyright: strict

import logging
import os
import sqlite3
import statistics
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, NamedTuple

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row

from hydra_stats.formatting import human_duration, human_size
from hydra_stats.stores import closure_drvs

log = logging.getLogger(__name__)

Conn = psycopg.Connection[DictRow]


# ---- Metrics --------------------------------------------------------------


def _sec_csv(v: float | None) -> str:
    return "" if v is None else f"{v:.1f}"


def _bytes_csv(v: float | None) -> str:
    return "" if v is None else f"{int(v)}"


@dataclass(frozen=True)
class MetricSpec:
    name: str
    label: str
    unit: str  # CSV column suffix ("_s", "_bytes")
    sql_expr: sql.Composable | None  # None -> computed out-of-band
    format_text: Callable[[float | None], str]
    format_csv: Callable[[float | None], str]
    reports_completeness: bool = False  # closure-build-time only


def _sec_metric(
    name: str, label: str, expr: sql.Composable | None, *, comp: bool = False
) -> MetricSpec:
    return MetricSpec(name, label, "_s", expr, human_duration, _sec_csv, comp)


def _byte_metric(name: str, label: str, expr: sql.Composable) -> MetricSpec:
    return MetricSpec(name, label, "_bytes", expr, human_size, _bytes_csv)


# SQL expressions reference aliases fixed in ``_scan``: ``b`` = builds,
# ``bs`` = top buildsteps row.
METRICS: dict[str, MetricSpec] = {
    m.name: m
    for m in (
        _sec_metric("build-time", "build time", sql.SQL("(bs.stoptime - bs.starttime)")),
        _sec_metric("closure-build-time", "closure build time", None, comp=True),
        _byte_metric("closure-size", "closure size", sql.SQL("b.closuresize")),
        _byte_metric("output-size", "output size", sql.SQL("b.size")),
    )
}


# ---- MetricPoint / JobSummary --------------------------------------------


@dataclass
class MetricPoint:
    build_id: int
    timestamp: int
    value: float | None
    # Only populated for closure-build-time: fraction of the build's
    # dep .drvs for which we found a duration in buildsteps.
    completeness: float | None = None


class JobSummary(NamedTuple):
    samples: int
    min: float | None
    p25: float | None
    median: float | None
    mean: float | None
    p75: float | None
    p90: float | None
    p95: float | None
    max: float | None
    stdev: float | None
    first_timestamp: int | None
    last_timestamp: int | None
    # Mean closure-completeness (closure metric only).
    completeness: float | None = None


# fmt: off
NUMERIC_STATS: tuple[str, ...] = (
    "min", "p25", "median", "mean", "p75", "p90", "p95", "max", "stdev",
)
# fmt: on
_EMPTY_SUMMARY = JobSummary(0, *([None] * 12))


def summarise(points: list[MetricPoint]) -> JobSummary:
    """Five-number summary + mean/stdev + first/last timestamps."""
    values: list[float] = []
    timestamps: list[int] = []
    completenesses: list[float] = []
    for p in points:
        if p.value is not None:
            values.append(p.value)
            timestamps.append(p.timestamp)
            if p.completeness is not None:
                completenesses.append(p.completeness)
    if not values:
        return _EMPTY_SUMMARY
    values.sort()
    n = len(values)
    if n >= 2:
        q1, q2, q3 = statistics.quantiles(values, n=4, method="inclusive")
        dec = statistics.quantiles(values, n=20, method="inclusive")
        p90, p95 = dec[17], dec[18]  # 18/20 = 0.90, 19/20 = 0.95
        sd = statistics.stdev(values)
    else:
        q1 = q2 = q3 = p90 = p95 = values[0]
        sd = 0.0
    return JobSummary(
        n,
        values[0],
        q1,
        q2,
        statistics.fmean(values),
        q3,
        p90,
        p95,
        values[-1],
        sd,
        min(timestamps),
        max(timestamps),
        statistics.fmean(completenesses) if completenesses else None,
    )


SORT_KEYS: tuple[str, ...] = ("group", *JobSummary._fields)


# ---- DSN plumbing ---------------------------------------------------------


def parse_hydra_dbi(dbi: str) -> str:
    """Convert Hydra's ``dbi:Pg:dbname=hydra;user=hydra;`` to libpq kv syntax.

    Naive split: values containing ``;`` would be mis-parsed, but Hydra
    has never used quoted values in the wild. Pass a URL-form DSN via
    ``--dsn`` if you need quoting.
    """
    s = dbi.strip()
    if s.startswith(("postgresql://", "postgres://")):
        return s
    if not s.lower().startswith("dbi:pg:"):
        return s
    return s[len("dbi:pg:") :].replace(";", " ").strip()


def resolve_dsn(arg: str | None) -> str | None:
    """Pick a DSN from --dsn, HYDRA_SERVER_DSN, or HYDRA_DBI (in that order)."""
    if arg:
        return arg
    if env := os.environ.get("HYDRA_SERVER_DSN"):
        return env
    if dbi := os.environ.get("HYDRA_DBI"):
        return parse_hydra_dbi(dbi)
    return None


def connect(dsn: str, *, statement_timeout_ms: int = 60_000) -> Conn:
    """Open an autocommit, dict-row, read-only psycopg connection.

    Read-only is enforced via session-level
    ``default_transaction_read_only = on``: any mutation fails with
    SQLSTATE 25006 before touching disk.
    """
    conn = psycopg.Connection[DictRow].connect(dsn, autocommit=True, row_factory=dict_row)
    conn.execute(sql.SQL("SET default_transaction_read_only = on"))
    conn.execute(sql.SQL("SET transaction_read_only = on"))
    # Bigger work_mem helps the eval-output-pair stream avoid spilling
    # sorts/hashes to disk. Session-local only.
    conn.execute(sql.SQL("SET work_mem = '256MB'"))
    if statement_timeout_ms > 0:
        conn.execute(
            sql.SQL("SET statement_timeout = {ms}").format(ms=sql.Literal(statement_timeout_ms))
        )
    return conn


# ---- Per-build scan -------------------------------------------------------


@dataclass(frozen=True)
class QueryParams:
    project: str
    jobset: str
    metric: MetricSpec
    status: str = "success"  # "success" | "failed" | "all"
    system: str | None = None
    since_ts: int | None = None
    # Per-group cap on most-recent samples; applied server-side via ROW_NUMBER().
    max_samples: int | None = None


_STATUS_PRED: dict[str, sql.Composable] = {
    "success": sql.SQL("b.buildstatus = 0"),
    "failed": sql.SQL("b.buildstatus <> 0 AND b.buildstatus IS NOT NULL"),
    "all": sql.SQL("TRUE"),
}


def _compose_where(
    fixed: list[sql.Composable],
    fixed_args: list[Any],
    optional: list[tuple[Any | None, sql.Composable]],
) -> tuple[sql.Composed, list[Any]]:
    """Join WHERE clauses whose optional ``(value, clause)`` pairs drop
    out when ``value is None``. ``clause`` is a ``sql.SQL(...)`` value
    with one ``%s`` placeholder."""
    parts = list(fixed)
    args = list(fixed_args)
    for val, clause in optional:
        if val is not None:
            parts.append(clause)
            args.append(val)
    return sql.SQL(" AND ").join(parts), args


_GROUP_EXPR: dict[str, sql.Composable] = {
    "job": sql.SQL("b.job"),
    "machine": sql.SQL("COALESCE(NULLIF(bs.machine, ''), '(unknown)')"),
}


def _scan(
    conn: Conn,
    params: QueryParams,
    *,
    group_expr: sql.Composable,
    extra_select: sql.Composable,
    needs_buildsteps: bool,
) -> list[DictRow]:
    """Per-build scan. ``extra_select`` goes after the fixed columns;
    ``needs_buildsteps`` toggles the LEFT JOIN to ``buildsteps``;
    ``max_samples`` caps rows per group via ROW_NUMBER."""
    where, where_args = _compose_where(
        fixed=[
            sql.SQL("p.name = %s"),
            sql.SQL("js.name = %s"),
            sql.SQL("b.finished = 1"),
            _STATUS_PRED[params.status],
        ],
        fixed_args=[params.project, params.jobset],
        optional=[
            (params.system, sql.SQL("b.system = %s")),
            (params.since_ts, sql.SQL("b.timestamp >= %s")),
        ],
    )
    bs_join = (
        sql.SQL("LEFT JOIN buildsteps bs ON bs.build = b.id AND bs.drvpath = b.drvpath")
        if needs_buildsteps
        else sql.SQL("")
    )
    base = sql.SQL(
        """
        SELECT {group_expr} AS group_key, b.id AS build_id,
               b.timestamp AS timestamp, {extra_select}
        FROM   builds b
        JOIN   jobsets  js ON js.id   = b.jobset_id
        JOIN   projects p  ON p.name  = js.project
        {bs_join}
        WHERE  {where_clause}
        """
    ).format(group_expr=group_expr, extra_select=extra_select, bs_join=bs_join, where_clause=where)

    cap = params.max_samples
    if cap is not None and cap > 0:
        stmt = sql.SQL(
            """
            SELECT * FROM (
              SELECT s.*, ROW_NUMBER() OVER (PARTITION BY group_key
                          ORDER BY timestamp DESC, build_id DESC) AS rn
              FROM ({base}) s
            ) capped
            WHERE rn <= %s
            ORDER BY group_key, timestamp, build_id
            """
        ).format(base=base)
        args = [*where_args, cap]
    else:
        stmt = sql.SQL("SELECT * FROM ({base}) s ORDER BY group_key, timestamp, build_id").format(
            base=base
        )
        args = where_args

    with conn.cursor() as cur:
        cur.execute(stmt, args)
        return list(cur)


def _bucket(
    rows: list[DictRow],
    make_point: Callable[[DictRow], MetricPoint],
) -> dict[str, list[MetricPoint]]:
    """Group ``rows`` by their ``group_key`` column, projecting each
    through ``make_point``."""
    out: dict[str, list[MetricPoint]] = {}
    for row in rows:
        out.setdefault(str(row["group_key"]), []).append(make_point(row))
    return out


def query_metric(conn: Conn, params: QueryParams, *, group_by: str) -> dict[str, list[MetricPoint]]:
    """Scan builds for a SQL-expressible metric, grouped by job or machine."""
    if params.metric.sql_expr is None:
        raise ValueError(f"metric {params.metric.name!r} has no SQL expression")
    rows = _scan(
        conn,
        params,
        group_expr=_GROUP_EXPR[group_by],
        extra_select=sql.SQL("{e} AS value").format(e=params.metric.sql_expr),
        needs_buildsteps=True,
    )

    def point(row: DictRow) -> MetricPoint:
        v = row["value"]
        return MetricPoint(
            build_id=int(row["build_id"]),
            timestamp=int(row["timestamp"]),
            value=None if v is None else float(v),
        )

    return _bucket(rows, point)


def _drvpath_durations(conn: Conn) -> dict[str, float]:
    """Global ``{drvpath: max_seconds}`` map across every jobset.

    ``.drv`` paths are content-addressed, so any successful build's
    duration is a usable estimate; MAX is conservative over flaky retries.
    """
    stmt = sql.SQL(
        """
        SELECT drvpath, MAX(stoptime - starttime) AS secs
        FROM   buildsteps
        WHERE  status = 0 AND drvpath IS NOT NULL
          AND  starttime IS NOT NULL AND stoptime IS NOT NULL
          AND  stoptime >= starttime
        GROUP  BY drvpath
        """
    )
    with conn.cursor() as cur:
        cur.execute(stmt)
        return {str(r["drvpath"]): float(r["secs"]) for r in cur}


def query_closure_build_time(
    conn: Conn,
    nix_db: sqlite3.Connection,
    params: QueryParams,
) -> dict[str, list[MetricPoint]]:
    """Per-job ``MetricPoint`` lists for the closure-build-time metric.

    Two stages: (1) cross-jobset ``{drvpath: max_seconds}`` map; (2)
    scan this jobset's matching builds, expand each build's top .drv
    to its build-time closure via ``Refs``, and sum durations from the
    map. Missing .drvs (GC'd / never built on this Hydra) contribute
    zero; ``completeness`` is the fraction we could price.
    """
    log.info("closure-build-time: fetching drvpath->duration map")
    durations = _drvpath_durations(conn)
    log.info("closure-build-time: %d distinct drvpaths", len(durations))

    rows = _scan(
        conn,
        params,
        group_expr=sql.SQL("b.job"),
        extra_select=sql.SQL("b.drvpath AS drvpath"),
        needs_buildsteps=False,
    )
    log.info("closure-build-time: expanding closures for %d builds", len(rows))

    cache: dict[str, set[str]] = {}

    def price(drv: str | None) -> tuple[float | None, float | None]:
        if not drv:
            return (None, None)
        cl = cache.get(drv)
        if cl is None:
            cl = closure_drvs(nix_db, drv)
            cache[drv] = cl
        if not cl:
            return (None, 0.0)
        total, found = 0.0, 0
        for d in cl:
            secs = durations.get(d)
            if secs is not None:
                total += secs
                found += 1
        return (total, found / len(cl))

    def point(row: DictRow) -> MetricPoint:
        drv = row.get("drvpath")
        value, completeness = price(str(drv) if drv else None)
        return MetricPoint(
            build_id=int(row["build_id"]),
            timestamp=int(row["timestamp"]),
            value=value,
            completeness=completeness,
        )

    return _bucket(rows, point)


# ---- Ad-hoc queries -------------------------------------------------------


def query_queued_builds(conn: Conn, project: str, jobset: str) -> list[DictRow]:
    """Currently-building jobs in this jobset and their elapsed wall time."""
    stmt = sql.SQL(
        """
        SELECT b.id, b.job, b.timestamp, b.starttime,
               COALESCE(bs.machine, '') AS machine
        FROM   builds b
        JOIN   jobsets  js ON js.id   = b.jobset_id
        JOIN   projects p  ON p.name  = js.project
        LEFT   JOIN buildsteps bs ON bs.build = b.id AND bs.busy > 0
        WHERE  p.name = %s AND js.name = %s AND b.finished = 0
        ORDER  BY b.timestamp
        """
    )
    with conn.cursor() as cur:
        cur.execute(stmt, (project, jobset))
        return list(cur)


def query_evals(
    conn: Conn,
    *,
    project: str | None = None,
    jobset: str | None = None,
    since_ts: int | None = None,
    max_samples: int | None = None,
) -> list[DictRow]:
    """Evaluation metadata. Excludes ``hasNewBuilds = 0`` (no disk footprint)."""
    where, args = _compose_where(
        fixed=[sql.SQL("je.hasNewBuilds = 1")],
        fixed_args=[],
        optional=[
            (project, sql.SQL("p.name = %s")),
            (jobset, sql.SQL("js.name = %s")),
            (since_ts, sql.SQL("je.timestamp >= %s")),
        ],
    )
    stmt = sql.SQL(
        """
        SELECT je.id AS eval_id, je.timestamp AS ts,
               je.nrbuilds AS nr_builds, je.nrsucceeded AS nr_succeeded,
               je.flake AS flake, p.name AS project, js.name AS jobset
        FROM   jobsetevals je
        JOIN   jobsets  js ON js.id  = je.jobset_id
        JOIN   projects p  ON p.name = js.project
        WHERE  {where_clause}
        ORDER  BY je.id DESC
        """
    ).format(where_clause=where)
    if max_samples is not None and max_samples > 0:
        stmt = sql.SQL("{base} LIMIT %s").format(base=stmt)
        args = [*args, max_samples]
    with conn.cursor() as cur:
        cur.execute(stmt, args)
        return list(cur)


_PG_STREAM_BATCH = 50_000


def stream_all_eval_output_pairs(conn: Conn) -> Iterator[tuple[int, str]]:
    """Stream ``(eval_id, output_path)`` pairs on the Hydra server.

    Named cursor + bounded ``itersize`` keeps libpq's buffer sane on
    43M-row result sets. Read-only invariant is preserved via the
    session-level ``default_transaction_read_only`` carried into the
    transaction. The cursor auto-closes when the transaction exits.
    """
    stmt = sql.SQL(
        """
        SELECT jem.eval AS eval_id, bo.path AS path
        FROM   jobsetevalmembers jem
        JOIN   jobsetevals je ON je.id = jem.eval
        JOIN   buildoutputs bo ON bo.build = jem.build
        WHERE  je.hasNewBuilds = 1 AND bo.path IS NOT NULL
        """
    )
    with conn.transaction(), conn.cursor(name="hydra_stats_eval_pairs") as cur:
        cur.itersize = _PG_STREAM_BATCH
        cur.execute(stmt)
        for row in cur:
            yield (int(row["eval_id"]), str(row["path"]))
