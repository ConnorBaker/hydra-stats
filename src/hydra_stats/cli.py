"""``hydra-stats`` CLI.

Exit codes: 0 ok; 1 config; 2 argparse; 3 no data; 4 DB; 5 auth; 130 SIGINT.
"""

# pyright: strict

import argparse
import contextlib
import logging
import re
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import TextIO

import psycopg

from hydra_stats import __version__
from hydra_stats.eval_sizes import compute_eval_sizes, eval_sizes_table
from hydra_stats.formatting import parse_duration
from hydra_stats.queries import (
    METRICS,
    SORT_KEYS,
    Conn,
    QueryParams,
    connect,
    query_closure_build_time,
    query_metric,
    query_queued_builds,
    resolve_dsn,
    summarise,
)
from hydra_stats.render import Table, per_build_table, queued_table, render
from hydra_stats.stores import default_nix_db_path, open_nix_db

log = logging.getLogger("hydra_stats")


class ConfigError(Exception):
    """Resolvable-but-missing configuration. Caught in ``main`` -> exit 1."""


_DESCRIPTION = """\
Report per-job build statistics from a Hydra Postgres database.

Metrics: build-time, closure-build-time (sum of durations of every .drv
in the build's dep closure), closure-size, output-size.

Connection: --dsn / HYDRA_SERVER_DSN / HYDRA_DBI (in that order). On a
Hydra head node the ``hydra`` OS user has peer-auth, so
``sudo -u hydra hydra-stats --dsn 'dbname=hydra' ...`` just works.
"""


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="hydra-stats",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add = ap.add_argument
    add("--dsn", help="libpq connection string. Falls back to HYDRA_SERVER_DSN / HYDRA_DBI.")
    add("--project", help="Project name")
    add("--jobset", help="Jobset name")
    add(
        "--metric",
        choices=tuple(METRICS),
        default="build-time",
        help="Per-build quantity. Default: %(default)s.",
    )
    add(
        "--nix-db",
        type=Path,
        metavar="PATH",
        help="Nix DB path (default: $NIX_STATE_DIR/db/db.sqlite or /nix/var/nix/db/db.sqlite).",
    )
    add(
        "--status",
        choices=("success", "failed", "all"),
        default="success",
        help="Default: %(default)s.",
    )
    add("--system", help="Restrict to a Nix system (e.g. 'aarch64-linux').")
    add(
        "--group-by",
        choices=("job", "machine"),
        default="job",
        help="'machine' is only valid with --metric build-time.",
    )
    add(
        "--samples-since",
        type=parse_duration,
        metavar="DURATION",
        help="Only include builds newer than DURATION (e.g. '30d').",
    )
    add(
        "--max-samples",
        type=int,
        metavar="N",
        help="Keep only the N most recent builds per job/machine.",
    )
    add(
        "--timeout",
        type=parse_duration,
        default=60,
        metavar="DURATION",
        help="Statement timeout. 0 disables. Default: 60s.",
    )
    add("--format", choices=("text", "json", "csv"), default="text")
    add("--sort-by", choices=SORT_KEYS, default="group", help="Default: %(default)s.")
    add(
        "--sort-order",
        choices=("asc", "desc"),
        help="Default: 'asc' for 'group', 'desc' otherwise.",
    )
    add("--no-color", action="store_true", help="Disable coloured output even on a TTY.")
    add("--queued", action="store_true", help="Print currently-building jobs, then exit.")
    add(
        "--eval-sizes",
        action="store_true",
        help="Report total + unique on-disk size per eval. Requires --nix-db.",
    )
    add("--output", default="-", help="Output path or '-'. Default: %(default)s.")
    v = ap.add_mutually_exclusive_group()
    v.add_argument("-v", "--verbose", action="store_true", help="Log INFO to stderr.")
    v.add_argument("-q", "--quiet", action="store_true", help="Log only errors.")
    add("--version", action="version", version=__version__)
    return ap


# Alnum + the punctuation Hydra actually uses. Must start with alnum so a
# hyphen-prefixed string can't trip downstream tools as a flag.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+\-/@]*$")


def _validate_args(ap: argparse.ArgumentParser, a: argparse.Namespace) -> None:
    if a.group_by == "machine" and a.metric != "build-time":
        ap.error("--group-by machine is only valid with --metric build-time")
    if a.max_samples is not None and a.max_samples <= 0:
        ap.error("--max-samples must be positive")
    if a.queued and a.eval_sizes:
        ap.error("--queued and --eval-sizes are mutually exclusive")
    if not a.eval_sizes:
        missing = [f"--{k}" for k, v in (("project", a.project), ("jobset", a.jobset)) if not v]
        if missing:
            ap.error(f"required: {', '.join(missing)}")
    if a.jobset and not a.project:
        ap.error("--jobset requires --project")
    for kind, val in (("project", a.project), ("jobset", a.jobset), ("system", a.system)):
        if val is not None and not _NAME_RE.match(val):
            ap.error(f"{kind} {val!r}: bad chars or leading non-alnum")


@contextlib.contextmanager
def _open_output(path: str) -> Generator[TextIO]:
    if path == "-":
        yield sys.stdout
        return
    with open(path, "w", encoding="utf-8") as f:
        yield f


def _nix_db_path(arg: Path | None) -> Path:
    p = arg or default_nix_db_path()
    if not p.exists():
        raise ConfigError(
            f"Nix DB not found at {p}\n"
            "hint: pass --nix-db PATH or set NIX_STATE_DIR. "
            "On a Hydra head node it's usually /nix/var/nix/db/db.sqlite."
        )
    return p


def _since(samples_since: int | None) -> int | None:
    return None if samples_since is None else int(time.time()) - samples_since


# ---- Modes ----------------------------------------------------------------


def _build_queued(conn: Conn, a: argparse.Namespace) -> Table | None:
    rows = query_queued_builds(conn, a.project, a.jobset)
    if not rows:
        log.warning("no queued builds in %s/%s", a.project, a.jobset)
        return None
    return queued_table(rows)


def _build_eval_sizes(conn: Conn, a: argparse.Namespace) -> Table | None:
    report = compute_eval_sizes(
        conn,
        _nix_db_path(a.nix_db),
        project=a.project,
        jobset=a.jobset,
        since_ts=_since(a.samples_since),
        max_samples=a.max_samples,
    )
    if not report.rows:
        log.warning("no evaluations matched (project=%s jobset=%s)", a.project, a.jobset)
        return None
    return eval_sizes_table(report)


def _build_per_build(conn: Conn, a: argparse.Namespace) -> Table | None:
    metric = METRICS[a.metric]
    since_ts = _since(a.samples_since)
    params = QueryParams(
        project=a.project,
        jobset=a.jobset,
        metric=metric,
        status=a.status,
        system=a.system,
        since_ts=since_ts,
        max_samples=a.max_samples,
    )
    log.info("scan: metric=%s group_by=%s", metric.name, a.group_by)
    if metric.name == "closure-build-time":
        with contextlib.closing(open_nix_db(_nix_db_path(a.nix_db))) as nix_db:
            points = query_closure_build_time(conn, nix_db, params)
    else:
        points = query_metric(conn, params, group_by=a.group_by)
    log.info(
        "scan complete: %d group(s), %d point(s)",
        len(points),
        sum(len(v) for v in points.values()),
    )
    if not points:
        log.warning(
            "jobset %s/%s: no builds matching status=%s (Hydra is case-sensitive)",
            a.project,
            a.jobset,
            a.status,
        )
        return None
    summaries = {name: summarise(pts) for name, pts in points.items()}
    sort_order = a.sort_order or ("asc" if a.sort_by == "group" else "desc")
    return per_build_table(
        project=a.project,
        jobset=a.jobset,
        metric=metric,
        group_by=a.group_by,
        status=a.status,
        system=a.system,
        since_ts=since_ts,
        sort_by=a.sort_by,
        sort_order=sort_order,
        summaries=summaries,
    )


def _main_inner(a: argparse.Namespace, conn: Conn, out: TextIO) -> int:
    if a.queued:
        t = _build_queued(conn, a)
    elif a.eval_sizes:
        t = _build_eval_sizes(conn, a)
    else:
        t = _build_per_build(conn, a)
    if t is None:
        return 3
    render(t, a.format, out, no_color=a.no_color)
    return 0


def _map_db_error(e: psycopg.Error) -> int:
    if isinstance(e, psycopg.errors.QueryCanceled):
        log.error("query timed out: %s (raise --timeout or narrow the scope)", e)
        return 4
    if isinstance(e, psycopg.OperationalError) and (
        getattr(e.diag, "sqlstate", None) or ""
    ).startswith("28"):
        log.error("auth failed: %s", e)
        log.error("hint: run as 'hydra' (peer-auth) or pass user= in --dsn.")
        return 5
    log.error("database error: %s", e)
    return 4


def main(argv: list[str] | None = None) -> int:
    ap = build_argparser()
    a = ap.parse_args(argv)
    level = logging.INFO if a.verbose else logging.ERROR if a.quiet else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr)
    _validate_args(ap, a)
    dsn = resolve_dsn(a.dsn)
    if not dsn:
        log.error("--dsn is required (or set HYDRA_SERVER_DSN / HYDRA_DBI)")
        return 1
    try:
        with (
            _open_output(a.output) as out,
            connect(dsn, statement_timeout_ms=a.timeout * 1000) as conn,
        ):
            return _main_inner(a, conn, out)
    except ConfigError as e:
        log.error("%s", e)
        return 1
    except psycopg.Error as e:
        return _map_db_error(e)
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
