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
    LIQUID_SECRET_PROVIDER = "protonpass";
    LIQUID_SECRET_PROFILE = "development";
    # Share the desktop Secret Service keyring across terminal sessions.
    PROTON_PASS_KEY_PROVIDER = "keyring";
    PROTON_PASS_LINUX_KEYRING = "dbus";
    # Discover throughput from successful responses and provider backoff,
    # shared across local workers and instances. A number selects a fixed rate.
    LIQUID_BLOCKSTREAM_ENTERPRISE_RPS = "auto";
    # Trace fetches adapt to latency and available resources, up to 64 workers.
    # Set 1 for serial fetching or 1..64 as an explicit concurrency ceiling.
    LIQUID_TRACE_WORKERS = "auto";
    # A verified allowance in LIQUID_BLOCKSTREAM_API_RPS overrides auto mode
    # and applies 5% headroom to that allowance instead.
    LIQUID_SECRETSPEC_BIN = "${pkgs.secretspec}/bin/secretspec";
    SECRETSPEC_PROTONPASS_CLI_PATH = "${pkgs.proton-pass-cli}/bin/pass-cli";
    # Mermaid's Nix wrapper also supplies Chromium on Linux. Rendering uses
    # local files and never needs API credentials or a separate server.
    LIQUID_MERMAID_BIN = "${pkgs.mermaid-cli}/bin/mmdc";
    LIQUID_NODE_BIN = "${pkgs.nodejs_24}/bin/node";
    # Per-layout V8 old-space ceiling within the shared ELK resource pool. Mermaid
    # uses the allowance for one renderer. Auto uses 90% of available RAM,
    # accounting for Linux cgroup limits; a number sets total MiB explicitly.
    LIQUID_RENDER_HEAP_MB = "auto";
    # Auto measures the first ELK attempt, then sizes parallel batches from
    # peak RAM with 2x headroom and CPU/memory shares across active instances.
    # Set 1 for serial execution or 1..64 as an explicit concurrency ceiling.
    LIQUID_ELK_WORKERS = "auto";
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
      export PYTHONPATH="$LIQUID_TRACER_ROOT''${PYTHONPATH:+:$PYTHONPATH}"
      exec ${config.languages.python.package}/bin/python3 -m liquid_tracer.web_build layout \
        --root "$LIQUID_TRACER_ROOT" --npm "${pkgs.nodejs_24}/bin/npm"
    '';
  };

  scripts.liquid-web-build = {
    description = "Install locked Astro dependencies when needed and build the local UI";
    exec = ''
      set -euo pipefail
      export PYTHONPATH="$LIQUID_TRACER_ROOT''${PYTHONPATH:+:$PYTHONPATH}"
      exec ${config.languages.python.package}/bin/python3 -m liquid_tracer.web_build web \
        --root "$LIQUID_TRACER_ROOT" --npm "${pkgs.nodejs_24}/bin/npm"
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
      secret_access_reason="''${SECRETSPEC_REASON:-}"
      case "$secret_access_reason" in
        *[![:space:]]*) ;;
        *) secret_access_reason="Authenticate the user-selected Liquid Tracer action with Blockstream or Miro." ;;
      esac
      exec "$LIQUID_SECRETSPEC_BIN" --file "$LIQUID_TRACER_ROOT/secretspec.toml" run \
        --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE" \
        --reason "$secret_access_reason" \
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
      secret_access_reason="''${SECRETSPEC_REASON:-}"
      case "$secret_access_reason" in
        *[![:space:]]*) ;;
        *) secret_access_reason="Store API credentials for Liquid Tracer's Blockstream and Miro operations." ;;
      esac
      printf 'Provider: %s; profile: %s\n' "$LIQUID_SECRET_PROVIDER" "$LIQUID_SECRET_PROFILE"
      for credential_name in "''${credential_names[@]}"; do
        "$LIQUID_SECRETSPEC_BIN" --file "$LIQUID_TRACER_ROOT/secretspec.toml" set "$credential_name" \
          --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE" \
          --reason "$secret_access_reason"
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
    ${pkgs.nodejs_24}/bin/npm --prefix "$LIQUID_TRACER_ROOT/web" test
    liquid-test
  '';

  # Install before credential-bearing commands. ELK runs entirely offline.
  enterShell = ''
    liquid-layout-setup
  '';
}
