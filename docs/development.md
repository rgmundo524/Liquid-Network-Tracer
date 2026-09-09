# Local development and API secrets

`devenv.nix` defines the Python version, commands and public environment settings. `secretspec.toml` declares the names of credentials. Their values are stored separately and loaded by `liquid-live` when the program starts. Entering the shell, running tests and trying the demo do not access a secret provider.

## Start the environment

From the repository directory:

```bash
devenv shell
liquid-trace --help
liquid-demo
liquid-test
```

For a single command without an interactive shell:

```bash
devenv shell -- liquid-demo
devenv test
```

Use a current devenv release with its bundled `secretspec` command. `devenv.yaml` fixes the nixpkgs input to an explicit revision, and the Python package is `pkgs.python312`. The first build generates `devenv.lock`; commit it after a successful build. No lockfile has been fabricated without a Nix evaluation. To upgrade the pinned package input, deliberately change its revision and run `devenv update`, then `devenv test`.

| Command | Behavior |
| --- | --- |
| `liquid-trace ...` | Calls the CLI using the existing process environment. Does not access a secret provider. |
| `liquid-live ...` | Resolves project credentials through SecretSpec, then calls the same CLI. |
| `liquid-secrets-setup [all\|blockstream\|miro]` | Prompts for selected credentials and stores them in the configured provider; defaults to all three. |
| `liquid-demo` | Creates a fresh synthetic one-hop run under `LIQUID_DEMO_CASE_DIR`; no paid requests or credentials. |
| `liquid-demo-preview` | Previews the latest demo run on `LIQUID_DEMO_MIRO_BOARD`; no network requests or credentials. |
| `liquid-demo-sync` | Loads credentials through SecretSpec and syncs the latest demo run to its configured Miro board. |
| `liquid-test` | Runs the offline test suite. |

The launchers locate the source and secret declaration using `LIQUID_TRACER_ROOT`, preserving your working directory for relative case paths.

## Change public settings through devenv

The committed defaults are:

```nix
env = {
  LIQUID_TRACER_ROOT = config.devenv.root;
  LIQUID_CASE_DIR = "${config.devenv.root}/cases/current";
  LIQUID_DEMO_CASE_DIR = "${config.devenv.root}/demo-case";
  LIQUID_MIRO_BOARD = "";
  LIQUID_DEMO_MIRO_BOARD = "";
  LIQUID_SECRET_PROVIDER = "protonpass";
  LIQUID_SECRET_PROFILE = "development";
};
```

Keep personal settings in the ignored `devenv.local.nix` at the repository root. Set the case directory and board URLs once:

```nix
{ lib, config, ... }:
{
  env = {
    LIQUID_CASE_DIR = lib.mkForce "${config.devenv.root}/cases/my-case";
    LIQUID_MIRO_BOARD = lib.mkForce "https://miro.com/app/board/YOUR_CASE_BOARD_ID/";
    LIQUID_DEMO_MIRO_BOARD = lib.mkForce "https://miro.com/app/board/YOUR_DEMO_BOARD_ID/";
  };
}
```

Re-enter `devenv shell` after editing this file. Its settings apply on subsequent shell entries without manually setting variables. Keep demo and case boards separate. Board URLs and case paths are configuration; API credentials stay in Proton Pass. Ignoring a Nix file in Git does not prevent evaluated secret strings from entering generated Nix artifacts.

For a temporary override, your existing `-O` workflow still works:

```bash
devenv -O env.LIQUID_CASE_DIR:string /absolute/path/to/my-case shell
```

`trace`, `export`, and `miro-sync` use `LIQUID_CASE_DIR` when `--case` is omitted. `miro-sync` uses `LIQUID_MIRO_BOARD` when `--board` is omitted and selects `--run latest` by default. Explicit CLI arguments take precedence. Outside an environment that defines these settings, supply the case path and board explicitly. A board setting does not make `trace` publish automatically; publication during tracing requires an explicit `--miro-board` argument.

## Repeat the same workflow

The first demo run and Miro trial use these commands inside the configured shell:

```bash
liquid-demo
liquid-demo-preview
liquid-demo-sync
```

Only `liquid-demo-sync` contacts Miro and retrieves credentials. To extend that demo graph, resume it instead of starting another fresh demo:

```bash
liquid-trace trace \
  --case "$LIQUID_DEMO_CASE_DIR" \
  --fixture "$LIQUID_TRACER_ROOT/examples/demo-api.json" \
  --resume latest --additional-hops 1
liquid-demo-preview
liquid-demo-sync
```

Running `liquid-demo` again creates an independent root and updates the demo case's latest pointer. An already published board requires continuation of its existing lineage, so a fresh root cannot replace it. Use an explicit prior run ID if you need to return to that lineage.

For a live case, start with an exact Liquid transaction output and small budgets:

```bash
liquid-live trace \
  --seed 'LIQUID_TXID:OUTPUT_INDEX' --hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60
```

After reviewing the run's exported files, preview and update the configured case board:

```bash
liquid-trace miro-sync --dry-run
liquid-live miro-sync
```

For each subsequent run, continue one more hop with the same per-run budgets:

```bash
liquid-live trace \
  --resume latest --additional-hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60
liquid-trace miro-sync --dry-run
liquid-live miro-sync
```

`latest` reads the `latest_run` pointer in the configured case's `case.json`. Each new run updates that pointer after its exports finish successfully, including saved runs paused by a budget or an error. It does not depend on shell history or directory timestamps. The evidence retains the actual run ID and parent. Existing cases without a pointer require an explicit run ID; the program does not guess. Use `--run RUN_ID` for sync or export, or `--resume RUN_ID` for tracing, when you need to select a particular snapshot. `export --run latest --out NEW_DIRECTORY` also accepts the pointer.

## Store secrets in Proton Pass

Install the official Proton Pass CLI (`pass-cli`) and sign in with `pass-cli login` if you have not already done so. The project's Python environment does not install or authenticate that CLI. With the default provider, SecretSpec uses note items in the `secretspec` vault; ensure that vault exists in Proton Pass. See the [Proton Pass provider setup](https://secretspec.dev/providers/protonpass/).

Check compatibility when updating the tools: `pass-cli` 2.2.4 removed a command used by older SecretSpec releases, so that CLI version and later need SecretSpec 0.19 or newer for the session-check fix. Keep a tested pair of versions; see the provider's [compatibility notes](https://secretspec.dev/providers/protonpass/#pass-cli-compatibility).

Inside the project shell, run the setup helper and enter each credential at the prompt:

```bash
liquid-secrets-setup
```

Use `liquid-secrets-setup blockstream` or `liquid-secrets-setup miro` to configure one service. Both the setup helper and `liquid-live` explicitly select this project's manifest, `LIQUID_SECRET_PROVIDER`, and `LIQUID_SECRET_PROFILE`, even if your global SecretSpec defaults differ. Setting an existing entry replaces its value; rerun the relevant command when rotating a credential. Setup contacts the secret provider, but makes no Blockstream or Miro requests.

If you already stored the credentials under this project's `development` profile in Proton Pass, skip setup. Selecting a provider and profile in SecretSpec's global configuration does not itself create the credentials. Changing providers or profiles does not migrate existing values; the manifest retains the `default` profile for access to earlier entries.

The equivalent individual commands from the project directory, also useful when updating just one value, are:

```bash
secretspec set BLOCKSTREAM_CLIENT_ID --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE"
secretspec set BLOCKSTREAM_CLIENT_SECRET --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE"
secretspec set MIRO_ACCESS_TOKEN --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE"
```

For a report that does not print the stored values:

```bash
secretspec --file "$LIQUID_TRACER_ROOT/secretspec.toml" check \
  --provider "$LIQUID_SECRET_PROVIDER" --profile "$LIQUID_SECRET_PROFILE"
```

Confirm that each credential you intend to use is resolved. An optional missing entry does not cause this check to fail. This checks secret resolution, not whether Blockstream or Miro accepts the credential.

```bash
liquid-live trace --seed 'LIQUID_TXID:0' --hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60
liquid-live miro-sync
```

The manifest marks credentials optional because different commands need different services. The existing application checks Blockstream credentials for authenticated tracing and the Miro token for live sync. Set only the services you use. Secret values are provided to the child process environment and are not added to the interactive parent shell. They remain readable by the process that needs them; environment variables are a delivery mechanism, not encrypted storage.

## Optional desktop keyring storage

To use existing keyring entries in the earlier `default` profile, override both settings when entering the shell:

```bash
devenv -O env.LIQUID_SECRET_PROVIDER:string keyring \
  -O env.LIQUID_SECRET_PROFILE:string default shell
```

Use `liquid-live` normally in that shell. To store new keyring entries in `development` instead, override only the provider and run `liquid-secrets-setup`.

The [keyring provider](https://secretspec.dev/providers/keyring/) uses the operating system's credential store. Linux needs a running, unlocked Secret Service such as GNOME Keyring or KWallet. This repository does not modify your NixOS login or keyring services.

### NixOS with Hyprland

GNOME Keyring supplies the background Secret Service. **Seahorse**, shown as **Passwords and Keys**, is an optional graphical manager for that keyring. It can list entries, unlock collections and remove old credentials. SecretSpec stores and retrieves values through the service, so you do not need to enter the same values manually in Seahorse. You can use these GNOME components while keeping Hyprland as your desktop.

If KWallet, KeePassXC, or another application already supplies your Secret Service, use that existing provider. To inspect an active provider without reading stored values:

```bash
busctl --user status org.freedesktop.secrets
```

A missing active owner can also mean that an installed service has not started yet. Avoid starting competing Secret Service providers for the same session.

For a GNOME Keyring setup, add these settings to your **NixOS system configuration**, where `pkgs` is available:

```nix
services.gnome.gnome-keyring.enable = true;
environment.systemPackages = [ pkgs.seahorse ];
```

Merge the package into your existing package list rather than defining that attribute twice in one file. Apply your normal flake-based `nixos-rebuild switch` command, then log completely out and back in using your password. This system configuration belongs outside the project's `devenv.nix`: the keyring service and login integration need to persist beyond a project shell.

On NixOS 26.05, the GNOME Keyring module configures login PAM; greetd also enables its keyring PAM integration by default when the service is enabled, and SDDM uses the login PAM stack. Do not add guessed PAM entries for a display manager you do not use. Custom login configuration, autologin, fingerprint-only login, and mismatched keyring/login passwords can require additional setup or manual unlocking. Sources: [GNOME Keyring module](https://github.com/NixOS/nixpkgs/blob/nixos-26.05/nixos/modules/services/desktops/gnome/gnome-keyring.nix), [greetd module](https://github.com/NixOS/nixpkgs/blob/nixos-26.05/nixos/modules/services/display-managers/greetd.nix), [SDDM module](https://github.com/NixOS/nixpkgs/blob/nixos-26.05/nixos/modules/services/display-managers/sddm.nix), [GNOME PAM behavior](https://wiki.gnome.org/Projects%282f%29GnomeKeyring%282f%29Pam.html).

Open Seahorse with `seahorse`. Unlock the **Login** keyring. If no password keyring exists, create a password-protected one and set it as the default. For automatic login unlocking, use a Login keyring whose password matches your login password. See GNOME's [keyring creation](https://help.gnome.org/seahorse/keyring-create.html) and [unlocking](https://help.gnome.org/seahorse/keyring-unlock.html) instructions.

Return to the project and enter a shell with the keyring provider to provision credentials:

```bash
git pull
devenv -O env.LIQUID_SECRET_PROVIDER:string keyring shell
liquid-secrets-setup
```

## Optional local `.env` storage

If you prefer a local file, create it without overwriting an existing one:

```bash
umask 077
cp -n .env.example .env
chmod 600 .env
```

Edit `.env` locally and fill in only the keys you use. It is plaintext on disk and ignored by Git. `.env.example` is the empty, public template. Select SecretSpec's dotenv provider for a shell:

```bash
devenv -O env.LIQUID_SECRET_PROVIDER:string dotenv shell
```

Then use `liquid-live` normally. [SecretSpec's dotenv provider](https://secretspec.dev/providers/dotenv/) reads the `.env` beside the selected `secretspec.toml`. It reads the file at process startup. We intentionally do **not** enable `dotenv.enable`: [devenv's documented dotenv integration](https://devenv.sh/integrations/dotenv/) warns that it can copy secret values into the readable Nix store.

## How projects usually separate configuration and secrets

| Location | Commit to a public repository? | Purpose |
| --- | --- | --- |
| `devenv.nix`, `devenv.yaml`, `devenv.lock` | Yes | Environment setup, package pins and nonsecret defaults. |
| `secretspec.toml`, `.env.example` | Yes, with no secret values | Names, descriptions and setup templates. |
| OS keyring or password manager | No secret values in Git | Separate secret storage, local or cloud depending on provider. |
| Local `.env` | No | Simple plaintext storage for one development machine. |
| CI/deployment secret store | No secret values in code | Supplies credentials to the job or service that needs them. |

For CI, GitHub Actions secrets can supply environment variables directly and run the Python CLI; `liquid-trace` also accepts them inside devenv. The offline test suite needs no GitHub secrets. See [GitHub's secrets documentation](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions).

Other SecretSpec providers can replace Proton Pass by setting the nonsecret `LIQUID_SECRET_PROVIDER` value. Set `LIQUID_SECRET_PROFILE` to select another declared profile. Avoid placing values in `env.MIRO_ACCESS_TOKEN`, reading secret files with `builtins.readFile`, or passing credentials as `devenv -O` arguments. The supported pattern follows [devenv's runtime SecretSpec guidance](https://devenv.sh/integrations/secretspec/).

`.gitignore` prevents ordinary additions of matching untracked files; it is not encryption and does not untrack files already committed. If a credential is ever committed, revoke/rotate it. Removing the current file alone does not remove it from Git history.
