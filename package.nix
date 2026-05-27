{
  lib,
  python3,
  pyright,
  ruff,
  runCommand,
}:

let
  # Single-source the version from pyproject.toml rather than duplicating it
  # here. builtins.fromTOML is pure, so it lives in lib scope cleanly.
  pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);

  # Build a python env that actually has our runtime deps so the in-derivation
  # pyright invocation can see psycopg's stubs. A bare ${python3}/bin/python3
  # would leave the stubs invisible and strict mode would silently miss them.
  pythonForCheck = python3.withPackages (ps: [
    ps.psycopg
    ps.rich
  ]);

  # Source filter: drop local caches so they can't pollute the hash or bloat
  # the Nix store. lib.cleanSource already excludes .git*; this adds tool
  # caches that do leak through.
  cleanedSrc = lib.cleanSourceWith {
    src = lib.cleanSource ./.;
    filter =
      name: _type:
      let
        baseName = baseNameOf name;
      in
      !(baseName == ".ruff_cache" || baseName == "__pycache__" || baseName == ".mypy_cache");
  };

  pkg = python3.pkgs.buildPythonApplication {
    pname = "hydra-stats";
    version = pyproject.project.version;
    pyproject = true;

    src = cleanedSrc;

    build-system = [ python3.pkgs.hatchling ];

    dependencies = [
      python3.pkgs.psycopg
      python3.pkgs.rich
    ];

    nativeCheckInputs = [
      pyright
      ruff
    ];

    # Runs ruff + pyright at build time so dep bumps see breakage
    # immediately, not at first CLI invocation.
    doCheck = true;
    checkPhase = ''
      runHook preCheck

      echo "-- ruff check --"
      ruff check src

      echo "-- ruff format --check --"
      ruff format --check src

      echo "-- pyright --"
      pyright --pythonpath ${pythonForCheck}/bin/python3 src/hydra_stats

      runHook postCheck
    '';

    meta = {
      description = "Per-job build statistics direct from the Hydra Postgres database";
      mainProgram = "hydra-stats";
    };
  };
in
pkg.overrideAttrs (prev: {
  passthru = (prev.passthru or { }) // {
    tests = {
      # Smoke-test: the installed binary must run and print its --help.
      # Catches broken wheel packaging, missing entry point, import errors,
      # and any runtime-only breakage that pyright wouldn't have flagged.
      help = runCommand "hydra-stats-help-test" { } ''
        ${pkg}/bin/hydra-stats --help > $out
      '';
      version = runCommand "hydra-stats-version-test" { } ''
        test "$(${pkg}/bin/hydra-stats --version)" = "${pyproject.project.version}"
        touch $out
      '';
    };
  };
})
