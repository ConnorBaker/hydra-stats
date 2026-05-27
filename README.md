# hydra-stats

> [!CAUTION]
> Entirely vibe-coded. Use at your own risk.

Per-job build statistics straight out of a Hydra Postgres database.
Three reports: per-build metric summaries, currently-queued builds, and
per-evaluation disk-usage accounting against the local Nix store.

Read-only by construction — the Postgres session is forced into
`default_transaction_read_only = on`, so any mutation fails with
SQLSTATE 25006 before touching disk.

## Install

The flake exposes the package, an overlay, and a dev shell:

```sh
# Run without installing
nix run github:ConnorBaker/hydra-stats -- --help

# Install into a profile
nix profile install github:ConnorBaker/hydra-stats

# Or use the overlay
{ inputs.hydra-stats.url = "github:ConnorBaker/hydra-stats"; }
# nixpkgs.overlays = [ inputs.hydra-stats.overlays.default ];
```

A `devShell` with `pyright` and `ruff` is available via `nix develop`.

## Quick start

`hydra-stats` needs a Postgres DSN. It checks `--dsn`, then
`HYDRA_SERVER_DSN`, then `HYDRA_DBI` (Hydra's `dbi:Pg:dbname=hydra;user=hydra;`
form is auto-converted to libpq kv syntax). On a Hydra head node,
peer-auth as the `hydra` user gets you in:

```sh
sudo -u hydra hydra-stats \
  --dsn 'dbname=hydra' \
  --project myproj --jobset main
```

## Reports

### Per-build metrics (default)

Summarises one metric per job (or per build machine) across the jobset.
Stats reported: `samples`, `min`, `p25`, `median`, `mean`, `p75`, `p90`,
`p95`, `max`, `stdev`, `first_timestamp`, `last_timestamp`.

```sh
# Build-time per job, last 30 days
hydra-stats --project myproj --jobset main --samples-since 30d

# Output size per job
hydra-stats --project myproj --jobset main --metric output-size

# Build-time per builder machine
hydra-stats --project myproj --jobset main --group-by machine

# Top 20 jobs by p95 build-time
hydra-stats --project myproj --jobset main --sort-by p95 --sort-order desc
```

Available metrics:

| Metric                | Source                                                            |
| --------------------- | ----------------------------------------------------------------- |
| `build-time`          | `buildsteps.stoptime - buildsteps.starttime` for each build's top step |
| `closure-build-time`  | Sum of build-times of every `.drv` in the build's dependency closure (see note below) |
| `closure-size`        | `builds.closuresize`                                              |
| `output-size`         | `builds.size`                                                     |

`closure-build-time` walks the Nix store's `Refs` table from the
build's top `.drv` and sums per-`.drv` durations from the cross-jobset
`{drvpath: max(stoptime - starttime)}` map. Missing `.drv`s (GC'd or
never built on this Hydra) contribute zero; the report includes a
`closure %` column with the fraction that could be priced. Requires
`--nix-db` (or a discoverable `NIX_STATE_DIR` / `/nix/var/nix/db/db.sqlite`).

### Queued builds (`--queued`)

Currently-building jobs in the jobset, with wait time and run time:

```sh
hydra-stats --project myproj --jobset main --queued
```

### Eval disk usage (`--eval-sizes`)

For each evaluation, reports:

* **total** — `narSize` over the runtime closure of every output the
  eval recorded.
* **unique** — the subset not reachable from any other eval on this
  Hydra. What deleting only this eval would actually reclaim.
* **paths** — `resolved/recorded`: how many of the eval's recorded
  output paths still exist as `ValidPaths` rows (i.e. haven't been GC'd).

```sh
# All evals, server-wide
hydra-stats --eval-sizes

# Scoped to a project / jobset / time window
hydra-stats --eval-sizes --project myproj --jobset main --samples-since 14d
```

Reads from the Postgres DB and the Nix store SQLite DB simultaneously;
the server-side stream is unbounded by default so memory scales with
`distinct (eval_id, output_path)` pairs.

## Filters & options

| Flag              | Meaning                                                              |
| ----------------- | -------------------------------------------------------------------- |
| `--system SYS`    | Restrict to a Nix system (e.g. `aarch64-linux`).                     |
| `--status`        | `success` (default), `failed`, or `all`.                             |
| `--samples-since` | Only builds newer than the duration. Accepts `30d`, `12h`, `1w`, `3600s`, or a bare number of seconds. |
| `--max-samples N` | Per-group cap on most-recent samples (server-side `ROW_NUMBER`).     |
| `--timeout`       | Postgres `statement_timeout`. Default 60s; `0` disables.             |
| `--format`        | `text` (default), `json`, `csv`.                                     |
| `--sort-by`       | `group` or any stat column (`p95`, `mean`, …).                       |
| `--sort-order`    | `asc` / `desc`. Default: `asc` for `group`, `desc` otherwise.        |
| `--no-color`      | Disable colour even on a TTY (also auto-off when piped).             |
| `--output PATH`   | Write to a file instead of stdout.                                   |
| `-v` / `-q`       | Log INFO to stderr / log only errors.                                |

## Exit codes

| Code | Meaning                                                                |
| ---- | ---------------------------------------------------------------------- |
| 0    | OK                                                                     |
| 1    | Config error (missing DSN, missing Nix DB, etc.)                       |
| 2    | argparse error                                                         |
| 3    | No data matched (empty result is treated as a soft failure)            |
| 4    | Database error (incl. statement timeout — raise `--timeout` or narrow scope) |
| 5    | Postgres auth failure                                                  |
| 130  | SIGINT                                                                 |

## Requirements

* Python ≥ 3.13 with `psycopg` ≥ 3.2 and `rich` ≥ 13 (handled by the
  flake; if you're installing some other way these are in `pyproject.toml`).
* Read access to the Hydra Postgres DB.
* For closure-build-time and `--eval-sizes`: read access to the Nix
  store SQLite DB (`/nix/var/nix/db/db.sqlite`, or `$NIX_STATE_DIR/db/db.sqlite`).

## Development

```sh
nix develop          # python with deps + pyright + ruff
nix flake check      # build, smoke tests, formatter
nix fmt              # treefmt: nixfmt + taplo + ruff
```
