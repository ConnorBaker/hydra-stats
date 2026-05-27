"""Per-evaluation disk-usage accounting.

For each target eval, reports:

* ``total_bytes`` -- narSize over the eval's runtime closure.
* ``unique_bytes`` -- subset not reachable from any other eval on this
  Hydra. What deleting only this eval would actually reclaim.

The SQLite heavy lifting is in ``stores.compute_eval_disk_usage``.
"""

# pyright: strict

import logging
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hydra_stats.queries import Conn, query_evals, stream_all_eval_output_pairs
from hydra_stats.render import Column, Table, col, human_size, human_time
from hydra_stats.stores import compute_eval_disk_usage

log = logging.getLogger(__name__)

_BATCH_SIZE = 50_000
_BatchOrEnd = list[tuple[int, str]] | None


def _threaded_pg_stream(conn: Conn) -> Iterator[tuple[int, str]]:
    """Yield (eval_id, path) pairs while a side thread reads Postgres.

    psycopg releases the GIL during network reads and sqlite3 during
    C calls, so a one-thread producer overlaps Postgres I/O with
    SQLite insert time. The queue is bounded to cap peak memory.
    """
    q: queue.Queue[_BatchOrEnd] = queue.Queue(maxsize=16)
    err: list[BaseException] = []

    def producer() -> None:
        try:
            batch: list[tuple[int, str]] = []
            for pair in stream_all_eval_output_pairs(conn):
                batch.append(pair)
                if len(batch) >= _BATCH_SIZE:
                    q.put(batch)
                    batch = []
            if batch:
                q.put(batch)
        except BaseException as e:
            err.append(e)
        finally:
            q.put(None)

    t = threading.Thread(target=producer, name="hydra-stats-pg-stream", daemon=True)
    t.start()
    n, start, last = 0, time.monotonic(), time.monotonic()
    while True:
        batch = q.get()
        if batch is None:
            break
        yield from batch
        n += len(batch)
        now = time.monotonic()
        if now - last >= 5.0:
            last = now
            log.info("eval-sizes: streamed %d pairs (%.0fs)", n, now - start)
    t.join()
    if err:
        raise err[0]


@dataclass(frozen=True)
class EvalSizeReport:
    project: str | None
    jobset: str | None
    since_ts: int | None
    # Rows are dicts for renderer convenience, sorted by unique_bytes DESC.
    rows: list[dict[str, Any]]
    server_evals: int
    server_paths: int


def _opt(cast: type, v: Any) -> Any:
    return None if v is None else cast(v)


def compute_eval_sizes(
    conn: Conn,
    nix_db_path: Path,
    *,
    project: str | None = None,
    jobset: str | None = None,
    since_ts: int | None = None,
    max_samples: int | None = None,
) -> EvalSizeReport:
    """Run the per-eval disk-usage report."""
    meta = query_evals(
        conn,
        project=project,
        jobset=jobset,
        since_ts=since_ts,
        max_samples=max_samples,
    )
    target_ids = [int(r["eval_id"]) for r in meta]
    log.info(
        "eval-sizes: target filter matched %d evals (project=%s jobset=%s)",
        len(target_ids),
        project,
        jobset,
    )
    if not target_ids:
        return EvalSizeReport(project, jobset, since_ts, [], 0, 0)

    usage = compute_eval_disk_usage(nix_db_path, _threaded_pg_stream(conn), target_ids)
    log.info(
        "eval-sizes: server holds %d evals and %d distinct output paths",
        usage.server_evals,
        usage.server_paths,
    )

    def row(m: dict[str, Any]) -> dict[str, Any]:
        eid = int(m["eval_id"])
        total, unique, resolved, recorded = usage.per_eval.get(eid, (0, 0, 0, 0))
        return {
            "project": str(m["project"]),
            "jobset": str(m["jobset"]),
            "eval_id": eid,
            "timestamp": int(m["ts"]),
            "nr_builds": _opt(int, m["nr_builds"]),
            "nr_succeeded": _opt(int, m["nr_succeeded"]),
            "flake": _opt(str, m["flake"]),
            "total_bytes": total,
            "unique_bytes": unique,
            "paths_resolved": resolved,
            "paths_recorded": recorded,
        }

    rows = sorted((row(m) for m in meta), key=lambda r: int(r["unique_bytes"]), reverse=True)
    return EvalSizeReport(project, jobset, since_ts, rows, usage.server_evals, usage.server_paths)


_NOTE = (
    "total = narSize over the eval's full runtime closure. "
    "unique = subset not referenced by any other eval (the disk you'd "
    "reclaim by deleting just this eval). paths = resolved/recorded: "
    "how many of Hydra's recorded output paths are still in the local store."
)


def _scope_label(report: EvalSizeReport) -> str:
    if report.project and report.jobset:
        return f"{report.project}/{report.jobset}"
    if report.project:
        return f"{report.project}/* (all jobsets)"
    return "* (whole server)"


def eval_sizes_table(report: EvalSizeReport) -> Table:
    # Project/jobset columns only appear when the scope spans multiple
    # jobsets; otherwise the header covers it and per-row repetition is
    # noise.
    scoped = report.project is not None and report.jobset is not None
    cols: list[Column] = []
    if not scoped:
        cols += [col("project", "project", align="l"), col("jobset", "jobset", align="l")]
    cols += [
        col("eval", "eval_id", align="l"),
        col("timestamp", "timestamp", text=lambda v: human_time(int(v)), csv=str, align="l"),
        col("builds", "nr_builds", text=str),
        col("ok", "nr_succeeded", text=str),
        col("total", "total_bytes", text=lambda v: human_size(int(v)), csv=str),
        col("unique", "unique_bytes", text=lambda v: human_size(int(v)), csv=str),
        Column(
            "paths",
            "paths_resolved",
            text=lambda r: (
                f"{r['paths_resolved']}/{r['paths_recorded']}" if r["paths_recorded"] else "-"
            ),
            csv=lambda r: str(r["paths_resolved"]),
            sort_key=lambda r: r["paths_resolved"],
        ),
    ]

    scope_bits = [f"evals={len(report.rows)}"]
    if report.since_ts is not None:
        scope_bits.append(f"since={human_time(report.since_ts)}")

    return Table(
        columns=cols,
        rows=report.rows,
        title=f"[bold]Scope[/bold]: {_scope_label(report)}  [dim]report=eval-sizes[/dim]",
        scope=[
            f"Filter: {', '.join(scope_bits)}",
            f"Server: {report.server_evals} evals, "
            f"{report.server_paths} distinct recorded output paths",
        ],
        note=_NOTE,
        empty_message="(no evaluations in range)",
        json_extra={
            "project": report.project,
            "jobset": report.jobset,
            "report": "eval-sizes",
            "since_ts": report.since_ts,
            "total_evals_scanned": report.server_evals,
            "distinct_output_paths": report.server_paths,
        },
        # JSON wants raw ints, not formatted cells.
        json_row=lambda r: dict(r),  # noqa: PLW0108
        json_rows_key="evals",
    )
