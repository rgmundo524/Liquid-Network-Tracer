{ config, lib, pkgs, ... }:

assert lib.assertMsg (lib.versionAtLeast pkgs.secretspec.version "0.19")
  "The Proton Pass provider requires SecretSpec 0.19 or newer with current pass-cli releases.";

{
  languages.python = {
    enable = true;
    package = pkgs.python312.withPackages (python: [ python.textual ]);
  };

  # Both tools come from the same pinned input; do not inherit an older
  # SecretSpec from the host or devenv's own bundled commands.
  packages = [ pkgs.git pkgs.secretspec pkgs.proton-pass-cli ];

  # Only public configuration belongs in env. Secret values are resolved by
  # liquid-live at process startup, never interpolated into a Nix expression.
  env = {
    LIQUID_TRACER_ROOT = config.devenv.root;
    LIQUID_INVESTIGATIONS_DIR = "${config.devenv.root}/cases";
    LIQUID_CASE_DIR = "${config.devenv.root}/cases/current";
    LIQUID_DEMO_CASE_DIR = "${config.devenv.root}/demo-case";
    LIQUID_SECRET_PROVIDER = "protonpass";
    LIQUID_SECRET_PROFILE = "development";
    LIQUID_SECRETSPEC_BIN = "${pkgs.secretspec}/bin/secretspec";
    SECRETSPEC_PROTONPASS_CLI_PATH = "${pkgs.proton-pass-cli}/bin/pass-cli";
  };

  # SecretSpec's runtime dotenv provider is supported as an alternative to
  # Proton Pass. Do not enable devenv's evaluation-time dotenv integration.
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
      exec "$LIQUID_SECRETSPEC_BIN" --file "$LIQUID_TRACER_ROOT/secretspec.toml" run \
        --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE" \
        -- liquid-trace "$@"
    '';
  };

  scripts.liquid-secrets-setup = {
    description = "Prompt locally for Blockstream, Miro, or all API credentials";
    exec = ''
      set -euo pipefail
      if [ "$#" -gt 1 ]; then
        printf '%s\n' 'Usage: liquid-secrets-setup [all|blockstream|miro]' >&2
        exit 2
      fi
      case "''${1:-all}" in
        all) credential_names=(BLOCKSTREAM_CLIENT_ID BLOCKSTREAM_CLIENT_SECRET MIRO_ACCESS_TOKEN) ;;
        blockstream) credential_names=(BLOCKSTREAM_CLIENT_ID BLOCKSTREAM_CLIENT_SECRET) ;;
        miro) credential_names=(MIRO_ACCESS_TOKEN) ;;
        -h|--help)
          printf '%s\n' 'Usage: liquid-secrets-setup [all|blockstream|miro]' \
            'Prompts for values using the configured provider and profile.' \
            'Setting an existing entry replaces its value. No Blockstream or Miro requests are made.'
          exit 0
          ;;
        *)
          printf '%s\n' 'Usage: liquid-secrets-setup [all|blockstream|miro]' >&2
          exit 2
          ;;
      esac
      printf 'Provider: %s; profile: %s\n' "$LIQUID_SECRET_PROVIDER" "$LIQUID_SECRET_PROFILE"
      for credential_name in "''${credential_names[@]}"; do
        "$LIQUID_SECRETSPEC_BIN" --file "$LIQUID_TRACER_ROOT/secretspec.toml" set "$credential_name" \
          --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE"
      done
      printf '%s\n' 'Credentials saved. Use liquid-live for authenticated commands.'
    '';
  };

  scripts.liquid-secrets-check = {
    description = "Check credential delivery through SecretSpec without displaying values or calling an explorer";
    exec = ''
      exec liquid-live credentials-check "$@"
    '';
  };

  scripts.liquid-toolchain-check = {
    description = "Check the pinned secret tools without accessing a vault or network service";
    exec = ''
      set -euo pipefail
      "$LIQUID_SECRETSPEC_BIN" --version
      "$SECRETSPEC_PROTONPASS_CLI_PATH" --version
      "$SECRETSPEC_PROTONPASS_CLI_PATH" info --help > /dev/null
    '';
  };

  scripts.liquid-demo = {
    description = "Run a synthetic one-hop trace without loading credentials";
    exec = ''
      exec liquid-trace trace \
        --case "$LIQUID_DEMO_CASE_DIR" \
        --fixture "$LIQUID_TRACER_ROOT/examples/demo-api.json" \
        --seeds-file "$LIQUID_TRACER_ROOT/examples/demo-seeds.txt" \
        --hops 1 "$@"
    '';
  };

  scripts.liquid-demo-preview = {
    description = "Preview the latest saved demo run for the configured test board";
    exec = ''
      exec liquid-trace miro-sync \
        --case "$LIQUID_DEMO_CASE_DIR" --run latest \
        --dry-run "$@"
    '';
  };

  scripts.liquid-demo-sync = {
    description = "Sync the latest saved demo run to the configured test board";
    exec = ''
      exec liquid-live miro-sync \
        --case "$LIQUID_DEMO_CASE_DIR" --run latest "$@"
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
    liquid-toolchain-check
    liquid-test
  '';
}
