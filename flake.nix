{
  description = "partslib: pinned development environment (KiCad 10, Python, git LFS)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "x86_64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.python313
            pkgs.git-lfs
          ]
          # nixpkgs marks KiCad as broken on Darwin, so macOS uses the installed app.
          # This means the KiCad version is pinned on Linux only.
          ++ pkgs.lib.optional (!pkgs.stdenv.hostPlatform.isDarwin) pkgs.kicad;

          shellHook = pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isDarwin ''
            export PATH="/Applications/KiCad/KiCad.app/Contents/MacOS:$PATH"
          '';
        };
      });
    };
}
