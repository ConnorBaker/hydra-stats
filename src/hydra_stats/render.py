"""Generic table rendering + per-report table builders.

Every report builds a ``Table`` and hands it to ``render()``. Renderers
don't know which report they came from: rows are opaque, cells are
pulled through ``Column`` callables. Text uses rich (ASCII box when
piped / ``--no-color``, heavy Unicode rules on a TTY); CSV/JSON use
the stdlib.
"""

# pyright: strict

import csv
import json
import operator
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

from rich import box
from rich.console import Console
from rich.table import Table as RichTable

from hydra_stats.formatting import human_duration, human_size, human_time
from hydra_stats.queries import NUMERIC_STATS, JobSummary, MetricSpec

# ---- Core types -----------------------------------------------------------


@dataclass(frozen=True)
class Column:
    """One column's text + CSV formatters plus sort key.

    Rows are heterogenous across reports, so callbacks take ``Any``.
    ``align`` is ``"l"`` or ``"r"``.
    """

    label: str
    csv_label: str
    text: Callable[[Any], str]
    csv: Callable[[Any], str]
    align: str = "r"
    sort_key: Callable[[Any], Any] = str


def _make_col(
    label: str,
    csv_label: str,
    extract: Callable[[Any], Any],
    *,
    text: Callable[[Any], str],
    csv: Callable[[Any], str],
    align: str,
    none_text: str = "-",
    none_csv: str = "",
) -> Column:
    def tfn(r: Any) -> str:
        v = extract(r)
        return none_text if v is None else text(v)

    def cfn(r: Any) -> str:
        v = extract(r)
        return none_csv if v is None else csv(v)

    def skey(r: Any) -> Any:
        v = extract(r)
        return (v is None, v)

    return Column(label, csv_label, tfn, cfn, align, skey)


def col(
    label: str,
    key: str,
    *,
    csv_label: str | None = None,
    text: Callable[[Any], str] = str,
    csv: Callable[[Any], str] | None = None,
    align: str = "r",
) -> Column:
    """Column extracting ``row[key]``, formatted through ``text`` (and
    ``csv``, defaulting to ``text``). ``None`` cells bypass formatters."""
    return _make_col(
        label,
        csv_label or key,
        operator.itemgetter(key),
        text=text,
        csv=csv or text,
        align=align,
    )


def _attr_col(
    attr: str,
    label: str,
    csv_label: str,
    *,
    text_fmt: Callable[[Any], str],
    csv_fmt: Callable[[Any], str] | None = None,
    align: str = "r",
) -> Column:
    """Column extracting ``row[1].<attr>`` (summary on a ``(name, summary)`` row)."""
    get = operator.attrgetter(attr)
    return _make_col(
        label,
        csv_label,
        lambda r: get(r[1]),
        text=text_fmt,
        csv=csv_fmt or text_fmt,
        align=align,
    )


@dataclass
class Table:
    """One rendered report."""

    columns: list[Column]
    rows: list[Any]
    title: str = ""
    scope: list[str] = field(default_factory=lambda: [])
    note: str | None = None
    empty_message: str = "(no data)"
    json_extra: dict[str, Any] = field(default_factory=lambda: {})
    json_row: Callable[[Any], dict[str, Any]] | None = None
    json_rows_key: str = "rows"


# ---- Renderers ------------------------------------------------------------


def _no_color(out: TextIO, flag: bool) -> bool:
    return flag or not (hasattr(out, "isatty") and out.isatty())


def render(table: Table, fmt: str, out: TextIO, *, no_color: bool = False) -> None:
    _RENDERERS[fmt](table, out, no_color)


def _render_text(table: Table, out: TextIO, no_color: bool) -> None:
    nc = _no_color(out, no_color)
    console = Console(file=out, highlight=False, no_color=nc)
    if table.title:
        console.print(table.title)
    for line in table.scope:
        console.print(f"[dim]{line}[/dim]")
    if not table.rows:
        console.print(f"[yellow]{table.empty_message}[/yellow]")
        return
    rt = RichTable(
        box=box.ASCII2 if nc else box.HEAVY_HEAD,
        show_header=True,
        header_style="" if nc else "bold",
        pad_edge=False,
        show_edge=False,
    )
    for c in table.columns:
        rt.add_column(c.label, justify="left" if c.align == "l" else "right")
    for row in table.rows:
        rt.add_row(*(c.text(row) for c in table.columns))
    console.print(rt)
    if table.note:
        console.print(f"\n[dim]{table.note}[/dim]")


def _render_csv(table: Table, out: TextIO, _nc: bool) -> None:
    w = csv.writer(out)
    w.writerow([c.csv_label for c in table.columns])
    for row in table.rows:
        w.writerow([c.csv(row) for c in table.columns])


def _render_json(table: Table, out: TextIO, _nc: bool) -> None:
    def default_row(r: Any) -> dict[str, Any]:
        return {c.csv_label: c.csv(r) for c in table.columns}

    make_row = table.json_row or default_row
    data: dict[str, Any] = {
        **table.json_extra,
        table.json_rows_key: [make_row(r) for r in table.rows],
    }
    json.dump(data, out, indent=2)
    out.write("\n")


_RENDERERS: dict[str, Callable[[Table, TextIO, bool], None]] = {
    "text": _render_text,
    "csv": _render_csv,
    "json": _render_json,
}


# ---- Per-build table ------------------------------------------------------


def per_build_table(
    *,
    project: str,
    jobset: str,
    metric: MetricSpec,
    group_by: str,
    status: str,
    system: str | None,
    since_ts: int | None,
    sort_by: str,
    sort_order: str,
    summaries: dict[str, JobSummary],
) -> Table:
    """Summary table of ``{name: JobSummary}`` for one metric."""
    group_label = "job" if group_by == "job" else "machine"
    cols: list[Column] = [
        Column(
            group_label,
            group_label,
            lambda r: r[0],
            lambda r: r[0],
            align="l",
            sort_key=lambda r: r[0],
        ),
        _attr_col("samples", "samples", "samples", text_fmt=str),
        *(
            _attr_col(
                s, s, f"{s}{metric.unit}", text_fmt=metric.format_text, csv_fmt=metric.format_csv
            )
            for s in NUMERIC_STATS
        ),
    ]
    if metric.reports_completeness:
        cols.append(
            _attr_col(
                "completeness",
                "closure %",
                "closure_completeness",
                text_fmt=lambda v: f"{v * 100:.0f}%",
                csv_fmt=lambda v: f"{v:.4f}",
            )
        )
    for attr, label, csv_label in (
        ("first_timestamp", "first build", "first_build_timestamp"),
        ("last_timestamp", "last build", "last_build_timestamp"),
    ):
        cols.append(_attr_col(attr, label, csv_label, text_fmt=human_time, csv_fmt=str, align="l"))

    # Sort: "group" is name-based; else try column label/csv_label;
    # else fall back to a JobSummary attr of the same name.
    items: list[tuple[str, JobSummary]] = list(summaries.items())
    desc = sort_order == "desc"
    if sort_by == "group":
        items.sort(key=lambda r: r[0], reverse=desc)
    elif (match := next((c for c in cols if sort_by in (c.label, c.csv_label)), None)) is not None:
        items.sort(key=match.sort_key, reverse=desc)
    else:
        get = operator.attrgetter(sort_by)
        items.sort(key=lambda r: (get(r[1]) is None, get(r[1]), r[0]), reverse=desc)

    scope_bits = [f"status={status}", f"group-by={group_by}"]
    if system:
        scope_bits.append(f"system={system}")
    if since_ts is not None:
        scope_bits.append(f"since={human_time(since_ts)}")

    note = None
    if metric.name == "closure-build-time":
        note = (
            "closure-build-time sums durations of every .drv in the build's dep "
            "closure, joined against the MAX duration across any Hydra-recorded "
            "build of that .drv. 'closure %' is the fraction we could price."
        )

    return Table(
        columns=cols,
        rows=items,
        title=f"[bold]Jobset[/bold]: {project}/{jobset}  [dim]metric={metric.label}[/dim]",
        scope=[
            f"Scope : {', '.join(scope_bits)}",
            f"Unique {group_label}s: {len(summaries)} (sorted by {sort_by} {sort_order})",
        ],
        note=note,
        json_extra={
            "project": project,
            "jobset": jobset,
            "metric": metric.name,
            "group_by": group_by,
            "status": status,
            "system": system,
            "since_ts": since_ts,
            "sort_by": sort_by,
            "sort_order": sort_order,
        },
        json_row=lambda r: {"group": r[0], "summary": r[1]._asdict()},
        json_rows_key="groups",
    )


# ---- Queued table ---------------------------------------------------------


def queued_table(rows: list[Any]) -> Table:
    """Currently-building jobs + their elapsed wall time."""
    now = int(time.time())

    def ts(v: Any) -> str:
        return human_time(int(v))

    def since(v: Any) -> str:
        return human_duration(now - int(v))

    return Table(
        columns=[
            col("build", "id", align="l"),
            col("job", "job", align="l"),
            col("queued", "timestamp", text=ts, align="l"),
            col("queued-for", "timestamp", csv_label="queued_for", text=since, align="l"),
            col("started", "starttime", text=ts, align="l"),
            col("running-for", "starttime", csv_label="running_for", text=since, align="l"),
            col("machine", "machine", text=lambda v: str(v or ""), align="l"),
        ],
        rows=rows,
        empty_message="(no queued builds)",
        json_extra={"report": "queued"},
        json_rows_key="builds",
    )


# NOTE: ``eval_sizes_table`` lives in ``eval_sizes.py`` next to the
# ``EvalSizeReport`` type it consumes -- keeping them together avoids a
# circular import.


# Keep human_size/human_duration re-exports so eval_sizes.py only needs
# one `from hydra_stats.render import ...` line.
__all__ = [
    "Column",
    "Table",
    "col",
    "human_duration",
    "human_size",
    "human_time",
    "per_build_table",
    "queued_table",
    "render",
]
