{ config, pkgs, ... }:

{
  languages.python = {
    enable = true;
    package = pkgs.python312;
  };

  packages = [ pkgs.git ];

  # Only public configuration belongs in env. Secret values are resolved by
  # liquid-live at process startup, never interpolated into a Nix expression.
  env = {
    LIQUID_TRACER_ROOT = config.devenv.root;
    LIQUID_CASE_DIR = "${config.devenv.root}/cases/current";
    LIQUID_SECRET_PROVIDER = "keyring";
  };

  # SecretSpec's runtime dotenv provider is supported as an alternative to
  # keyring. Do not enable devenv's evaluation-time dotenv integration.
  dotenv.enable = false;
  dotenv.disableHint = true;

  scripts.liquid-trace = {
    description = "Run the tracer with the existing process environment";
    exec = ''
      export PYTHONPATH="$LIQUID_TRACER_ROOT''${PYTHONPATH:+:$PYTHONPATH}"
      exec ${config.languages.python.package}/bin/python3 -m liquid_tracer "$@"
    '';
  };

  scripts.liquid-live = {
    description = "Load API credentials at runtime, then run liquid-trace";
    exec = ''
      exec secretspec --file "$LIQUID_TRACER_ROOT/secretspec.toml" run \
        --provider "$LIQUID_SECRET_PROVIDER" --profile default \
        -- liquid-trace "$@"
    '';
  };

  scripts.liquid-demo = {
    description = "Run a synthetic one-hop trace without loading credentials";
    exec = ''
      exec liquid-trace trace \
        --case "$LIQUID_TRACER_ROOT/demo-case" \
        --fixture "$LIQUID_TRACER_ROOT/examples/demo-api.json" \
        --seeds-file "$LIQUID_TRACER_ROOT/examples/demo-seeds.txt" \
        --hops 1 "$@"
    '';
  };

  scripts.liquid-test = {
    description = "Run the offline test suite without loading credentials";
    exec = ''
      cd "$LIQUID_TRACER_ROOT"
      exec ${config.languages.python.package}/bin/python3 -m unittest discover -v "$@"
    '';
  };

  enterTest = ''
    liquid-test
  '';
}
