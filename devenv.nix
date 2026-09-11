{ config, lib, pkgs, ... }:

{
  languages.python = {
    enable = true;
    package = pkgs.python312.withPackages (python: [ python.textual ]);
  };

  # Both tools come from the same pinned input; do not inherit an older
  # SecretSpec from the host or devenv's own bundled commands.
  packages =
    assert lib.assertMsg (lib.versionAtLeast pkgs.secretspec.version "0.19")
      "The Proton Pass provider requires SecretSpec 0.19 or newer with current pass-cli releases.";
    [ pkgs.git pkgs.secretspec pkgs.proton-pass-cli pkgs.mermaid-cli pkgs.nodejs_24 ];

  # Only public configuration belongs in env. Secret values are resolved by
  # liquid-live at process startup, never interpolated into a Nix expression.
  env = {
    LIQUID_TRACER_ROOT = config.devenv.root;
    ASTRO_TELEMETRY_DISABLED = "1";
    LIQUID_INVESTIGATIONS_DIR = "${config.devenv.root}/cases";
    LIQUID_CASE_DIR = "${config.devenv.root}/cases/current";
    LIQUID_DEMO_CASE_DIR = "${config.devenv.root}/demo-case";
    LIQUID_SECRET_PROVIDER = "protonpass";
    LIQUID_SECRET_PROFILE = "development";
    LIQUID_SECRETSPEC_BIN = "${pkgs.secretspec}/bin/secretspec";
    SECRETSPEC_PROTONPASS_CLI_PATH = "${pkgs.proton-pass-cli}/bin/pass-cli";
    # Mermaid's Nix wrapper also supplies Chromium on Linux. Rendering uses
    # local files and never needs API credentials or a separate server.
    LIQUID_MERMAID_BIN = "${pkgs.mermaid-cli}/bin/mmdc";
    LIQUID_NODE_BIN = "${pkgs.nodejs_24}/bin/node";
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

  scripts.liquid-layout-setup = {
    description = "Install the locked local ELK layout dependency when needed";
    exec = ''
      set -euo pipefail
      cd "$LIQUID_TRACER_ROOT/layout"
      layout_lock_hash="$(sha256sum package.json package-lock.json | sha256sum | cut -d ' ' -f 1)"
      if [ ! -f node_modules/.liquid-lock ] || [ "$(cat node_modules/.liquid-lock)" != "$layout_lock_hash" ]; then
        ${pkgs.nodejs_24}/bin/npm ci --ignore-scripts --no-audit --no-fund
        printf '%s\n' "$layout_lock_hash" > node_modules/.liquid-lock
      fi
    '';
  };

  scripts.liquid-web-build = {
    description = "Install locked Astro dependencies when needed and build the local UI";
    exec = ''
      set -euo pipefail
      liquid-layout-setup
      cd "$LIQUID_TRACER_ROOT/web"
      web_lock_hash="$(sha256sum package.json package-lock.json | sha256sum | cut -d ' ' -f 1)"
      if [ ! -f node_modules/.liquid-lock ] || [ "$(cat node_modules/.liquid-lock)" != "$web_lock_hash" ]; then
        ${pkgs.nodejs_24}/bin/npm ci --no-audit --no-fund
        printf '%s\n' "$web_lock_hash" > node_modules/.liquid-lock
      fi
      exec ${pkgs.nodejs_24}/bin/npm run build
    '';
  };

  scripts.liquid-web = {
    description = "Open the local Astro investigation interface on 127.0.0.1";
    exec = ''
      set -euo pipefail
      export PYTHONPATH="$LIQUID_TRACER_ROOT''${PYTHONPATH:+:$PYTHONPATH}"
      case "''${1:-}" in
        -h|--help) ;;
        *) liquid-web-build ;;
      esac
      exec ${config.languages.python.package}/bin/python3 -m liquid_tracer.web "$@"
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
    "$LIQUID_MERMAID_BIN" --version
    liquid-web-build
    ${pkgs.nodejs_24}/bin/npm --prefix "$LIQUID_TRACER_ROOT/web" run check
    liquid-test
  '';

  # Install before credential-bearing commands. ELK runs entirely offline.
  enterShell = ''
    liquid-layout-setup
  '';
}
