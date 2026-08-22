{
  description = "gel-registry development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];

      # Terraform is BUSL-licensed and marked unfree in nixpkgs. Pinning the
      # CLI here is the point of the shell: CI and every maintainer run the
      # same binary, so no one silently upgrades the state format.
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };

      forAllSystems = fn: nixpkgs.lib.genAttrs systems (system: fn (pkgsFor system));
    in
    {
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.terraform
            pkgs.tflint
            pkgs.uv
            pkgs.jq
            pkgs.curl
            pkgs.gh
          ];

          # Write to stderr. `nix develop --command` is used in CI and in
          # scripts that capture stdout; a banner on stdout corrupts them.
          shellHook = ''
            if [ -t 2 ]; then
              echo "gel-registry devshell: terraform $(terraform version -json | ${pkgs.jq}/bin/jq -r .terraform_version)" >&2
            fi
          '';
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-rfc-style);
    };
}
