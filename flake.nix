{
  description = "Per-job build statistics direct from the Hydra Postgres database";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11-small";

  inputs.treefmt-nix = {
    url = "github:numtide/treefmt-nix";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      treefmt-nix,
      ...
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forEachSystem = nixpkgs.lib.genAttrs systems;

      treefmtConfig =
        { ... }:
        {
          projectRootFile = "flake.lock";
          programs.nixfmt.enable = true;
          programs.taplo.enable = true;
          programs.ruff-check.enable = true;
          programs.ruff-format.enable = true;
        };

      treefmtEval = system: treefmt-nix.lib.evalModule nixpkgs.legacyPackages.${system} treefmtConfig;
    in
    {
      overlays.default = final: _prev: {
        hydra-stats = final.callPackage ./package.nix { };
      };

      packages = forEachSystem (system: {
        hydra-stats = nixpkgs.legacyPackages.${system}.callPackage ./package.nix { };
        default = self.packages.${system}.hydra-stats;
      });

      checks = forEachSystem (system: {
        build = self.packages.${system}.hydra-stats;
        inherit (self.packages.${system}.hydra-stats.passthru.tests) help version;
        formatter = (treefmtEval system).config.build.check self;
      });

      devShells = forEachSystem (system: {
        default =
          let
            pkgs = nixpkgs.legacyPackages.${system};
          in
          pkgs.mkShell {
            inputsFrom = [ self.packages.${system}.hydra-stats ];
            packages = [
              pkgs.pyright
              pkgs.ruff
            ];
          };
      });

      formatter = forEachSystem (system: (treefmtEval system).config.build.wrapper);
    };
}
