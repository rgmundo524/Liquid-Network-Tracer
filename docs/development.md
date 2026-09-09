# Local development and API secrets

The project uses **one main `devenv.nix`** to define Python, the Textual terminal interface, commands, and nonsecret environment defaults. `secretspec.toml` declares credential names. Proton Pass stores their values. Investigation names, board IDs, run limits, and the latest-run reference belong in the saved investigation files.

Entering the environment and opening the interface do not access a secret provider. Selecting a live output lookup, trace, Miro board creation, or Miro sync retrieves credentials through SecretSpec for that action. The offline demo trace, navigation, and local previews need no credentials.

## Start the environment and interface

From the repository directory:

```bash
devenv shell
liquid-trace
```

In an interactive terminal, the command opens the Textual interface. Use the keyboard or mouse to select **New investigation**, **Continue investigation**, **Settings**, or **Exit**. The explicit launcher is:

```bash
liquid-trace menu
```

Creating an investigation asks for a name, live or synthetic-demo source, starting outputs, an optional existing Miro board URL or ID, and numeric run limits. Enter known outputs as `HASH:NUMBER` in the multiline field, separated by whitespace or commas. `vout` is the numeric output index, not a word to enter: `:0` selects the first output and `:1` the second. If you only have a transaction hash, enter it in **Transaction hash**, choose **Load outputs**, toggle the relevant rows with Enter, and choose **Use selected outputs**. Nothing is preselected; applying a selection replaces the starting-output field. The offline demo uses its original sample seeds when this field is empty.

Lookup retrieves one transaction with a five-attempt and 30-second API budget, including authentication and retries. It loads live credentials only after selecting **Load outputs**. The terminal remains available for provider prompts. It follows no spends, saves no case, and uses a temporary report removed after loading the picker. Fees, peg-outs, and unspendable outputs cannot be selected. Confidential quantities remain unknown. Creating the investigation saves the selected references in a unique case directory; the later trace fetches and archives its own evidence. Within that investigation, start or continue a bounded run, review saved information, preview or sync Miro, or change its settings.

A case normally keeps the same board as its graph grows. If no board is linked, choose **Create Miro board** from the investigation menu. Review the name, visibility (Private by default), and optional team ID, then create it. SecretSpec supplies the Miro access token only after confirmation. The returned board ID is saved with the case, and its URL appears in the menu. **Preview Miro** and **Sync to Miro** use that selection and the saved run; there is no need to repeat tracing. You can also link an existing board through **Investigation settings**.

Board selection and numeric run defaults are saved with the case, so you do not need a board environment variable or a separate Nix file. The top-level Settings screen changes defaults for future investigations. Existing cases retain their saved settings.

For a command without entering an interactive shell:

```bash
devenv shell -- liquid-trace menu
devenv test
```

Use a current devenv release. The project supplies its own pinned SecretSpec and Proton Pass CLI packages instead of relying on an inherited system or bundled SecretSpec. `devenv.yaml` fixes the nixpkgs input to an explicit revision. The first build generates `devenv.lock`; commit it after a successful build. To upgrade the pinned package input, deliberately change its revision and run `devenv update`, then `devenv test`.

| Command | Behavior |
| --- | --- |
| `liquid-trace` or `liquid-trace menu` | Opens the investigation interface. Retrieves credentials only for a selected live action. |
| `liquid-trace SUBCOMMAND ...` | Runs an explicit command using the existing process environment. |
| `liquid-live SUBCOMMAND ...` | Resolves project credentials through SecretSpec, then runs an explicit command. |
| `liquid-live inspect-tx --txid HASH` | Looks up one live transaction and prints its output numbers, addresses, and available public quantities as JSON. Does not trace spends or create a case. |
| `liquid-live miro-create-board --case CASE` | Creates an empty Miro board and saves it with the investigation. Uses the case name and private visibility by default. |
| `liquid-secrets-setup [all\|blockstream\|miro]` | Prompts for selected credentials and stores them in the configured provider; defaults to all three. |
| `liquid-toolchain-check` | Reports the pinned SecretSpec/Proton CLI versions and checks support for `info`, without accessing a vault. |
| `liquid-secrets-check [--service blockstream\|miro\|all]` | Loads project secrets and reports presence only; defaults to Blockstream. No Blockstream or Miro calls. |
| `liquid-demo` | Creates a fresh synthetic one-hop run under `LIQUID_DEMO_CASE_DIR`; no paid requests or credentials. |
| `liquid-demo-preview [--board URL_OR_ID]` | Locally previews the latest demo run for the selected or saved board. |
| `liquid-demo-sync [--board URL_OR_ID]` | Loads credentials through SecretSpec and syncs the latest demo run to the selected or saved board. |
| `liquid-test` | Runs the offline test suite. |

The launchers locate the source and secret declaration using `LIQUID_TRACER_ROOT`, preserving your working directory for relative case paths. The ordinary command-line tracing engine uses Python's standard library. The interactive interface uses Textual, provided by devenv; outside devenv, install it with `python3 -m pip install -e '.[tui]'`.

## Saved investigations and settings

The main environment defines `LIQUID_INVESTIGATIONS_DIR` as `${config.devenv.root}/cases`. The interface stores numeric defaults for future investigations in `cases/settings.json`. Each newly named investigation receives its own unique subdirectory.

| Path within an investigation | Purpose |
| --- | --- |
| `case.json` | Name, seeds, source, chosen board, run defaults, and latest exported run ID. |
| `runs/<run-id>/` | Evidence, CSVs, tracing checkpoint, graph plan, and checksums for one run. |
| `runs/<run-id>/investigation.json` | Name and board selection as known when that run was traced. |
| `miro/` | Per-board mapping that keeps graph items stable across continuations. |
| `miro/board-creation.json` | Board creation request, status, and returned ID/URL for reuse and recovery. |
| `miro/reports/` | Publication reports recording which run was synced to which board. |

Run IDs are saved references, not variables you must remember or re-enter. The latest pointer advances after the exports finish successfully, including saved runs paused by a budget or an error. It does not depend on shell history or directory timestamps. A continuation preserves its parent and records a new snapshot. Settings or a new board chosen later do not rewrite completed run exports.

The default investigation root is ignored by Git. Keep the whole case directory together when moving or backing it up. Use a different investigation root with an explicit launch argument if needed:

```bash
liquid-trace menu --investigations-dir /absolute/path/to/investigations
```

## Direct commands for scripts

The interface handles investigation selection, saved defaults, and runtime credentials. Direct subcommands remain available when you want explicit flags or automation. The examples below use a case path chosen once for the script; substitute the directory printed by the interface.

For an initial live trace:

```bash
liquid-live trace --case cases/theft-liquid \
  --seed 'LIQUID_TXID:OUTPUT_INDEX' --hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60
```

Create and save a board, then preview and sync the latest run:

```bash
liquid-live miro-create-board --case cases/theft-liquid
liquid-trace miro-sync --case cases/theft-liquid --dry-run
liquid-live miro-sync --case cases/theft-liquid
```

Optional creation flags are `--name TEXT` (1–60 characters), `--visibility private|team`, and `--team-id ID`. Private visibility restricts team, organization, and public-link access. Team visibility enables team editing while keeping organization and public-link access private. Your Miro plan and permissions must support the chosen setting. Failed requests never automatically broaden visibility.

Creation records a pending request before contacting Miro, then saves the returned board ID before updating `case.json`. A retry reuses an acknowledged board. An uncertain outcome (such as a connection failure after sending the request) blocks a second creation request: inspect Miro and link the board through **Investigation settings**. If none was created, create one in Miro and link it. This avoids duplicate boards and preserves completed run evidence.

Alternatively, supply an existing board with `--board`:

```bash
liquid-trace miro-sync --case cases/theft-liquid --dry-run \
  --board 'https://miro.com/app/board/YOUR_CASE_BOARD_ID/'
liquid-live miro-sync --case cases/theft-liquid \
  --board 'https://miro.com/app/board/YOUR_CASE_BOARD_ID/'
```

For each subsequent run, continue one more hop, review, and update the saved board:

```bash
liquid-live trace --case cases/theft-liquid \
  --resume latest --additional-hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60
liquid-trace miro-sync --case cases/theft-liquid --dry-run
liquid-live miro-sync --case cases/theft-liquid
```

For direct sync commands, board selection follows **explicit `--board` → saved case board → legacy `LIQUID_MIRO_BOARD` → interactive prompt**. Noninteractive commands fail clearly if no board is available. Local preview does not persist a new board selection. Live sync saves the selection after local validation and before a network request, so retries can reuse it even if publication fails. A saved board does not trigger publication during tracing; that requires an explicit `--miro-board` flag or a separate sync action.

`--run` defaults to `latest` for sync. Use `--run RUN_ID` for sync or export, or `--resume RUN_ID` for tracing, to select a particular historical snapshot. `export --case cases/theft-liquid --run latest --out NEW_DIRECTORY` also accepts the pointer. Existing cases without a latest pointer need an explicit run ID; the program does not guess.

The demo helpers remain useful for scripts. `liquid-demo` starts an independent root each time. After syncing it, resume with `--case "$LIQUID_DEMO_CASE_DIR" --fixture "$LIQUID_TRACER_ROOT/examples/demo-api.json" --resume latest --additional-hops 1` to extend its lineage. An independent new root cannot replace an already published lineage. The interactive interface offers continuation for a selected demo investigation automatically.

## Optional environment overrides

The normal setup needs no `devenv.local.nix`. The single committed environment supplies the investigation root, demo helper path, and SecretSpec provider/profile. Personal board IDs belong to case settings, and credentials belong to Proton Pass.

For advanced machine-specific settings, an ignored `devenv.local.nix` can override the main environment; it contributes to the same environment rather than creating another one. For example, to keep investigations outside the source directory:

```nix
{ lib, ... }:
{
  env.LIQUID_INVESTIGATIONS_DIR = lib.mkForce "/absolute/path/to/investigations";
}
```

Re-enter the shell after changing environment configuration. For a temporary override, the existing `-O` workflow also works:

```bash
devenv -O env.LIQUID_INVESTIGATIONS_DIR:string /absolute/path/to/investigations shell
```

Direct `trace`, `export`, and `miro-sync` commands still accept an existing `LIQUID_CASE_DIR` setting when `--case` is omitted. Explicit arguments take precedence. That compatibility setting does not choose the investigation in the interface. Keep secret values out of all Nix expressions: ignoring a Nix file in Git does not prevent evaluated strings from entering generated Nix artifacts.

## Store secrets in Proton Pass

Devenv provides SecretSpec **0.19.1** and Proton Pass CLI **2.3.2** from the project's pinned nixpkgs input. `LIQUID_SECRETSPEC_BIN` points the project launchers and TUI at that exact SecretSpec binary. `SECRETSPEC_PROTONPASS_CLI_PATH` tells the provider to use the matching pinned `pass-cli`. Both settings are declared in the main `devenv.nix`; no shell assignments or Home Manager changes are required for this project. Outside devenv, install compatible versions yourself. Sign in locally with `pass-cli login` if you have not already done so. With the default provider, SecretSpec uses note items in the `secretspec` vault; ensure that vault exists in Proton Pass. See the [Proton Pass provider setup](https://secretspec.dev/providers/protonpass/).

`pass-cli` 2.2.4 removed `test`; SecretSpec 0.18 and earlier still invoke it. SecretSpec 0.19 and later try `info` first, fixing the reported `unrecognized subcommand 'test'` failure. The environment rejects SecretSpec versions older than 0.19. See the provider's [compatibility notes](https://secretspec.dev/providers/protonpass/#pass-cli-compatibility).

After pulling environment changes, leave the old shell and re-enter `devenv shell`. Check the tools, then credential delivery:

```bash
liquid-toolchain-check
liquid-secrets-check
```

The first command needs no login. The second contacts the secret provider and reports `present` or `MISSING` for each requested value, without printing values. It defaults to the two Blockstream credentials; `--service miro` checks the Miro token, and `--service all` checks all three. A missing value causes exit status 1. This verifies delivery to the application, not acceptance by Blockstream or Miro. It also avoids the indentation problems of pasting a multiline Python probe into a shell.

`devenv test` runs the toolchain check and offline suite without vault access. GitHub Actions runs this through the actual Nix environment as well as running the Python/TUI suite separately. Neither job uses real credentials.

Inside the project shell, run the setup helper and enter each credential at the prompt:

```bash
liquid-secrets-setup
```

Use `liquid-secrets-setup blockstream` or `liquid-secrets-setup miro` to configure one service. The setup helper, menu live actions, and `liquid-live` explicitly select this project's manifest, `LIQUID_SECRET_PROVIDER`, and `LIQUID_SECRET_PROFILE`, even if your global SecretSpec defaults differ. Setting an existing entry replaces its value; rerun the relevant command when rotating a credential. Setup contacts the secret provider, but makes no Blockstream or Miro requests.

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
liquid-live trace --case cases/theft-liquid --seed 'LIQUID_TXID:0' --hops 1 \
  --max-transactions 20 --max-outpoints 100 --max-requests 30 --max-seconds 60
liquid-live miro-sync --case cases/theft-liquid
```

The manifest marks credentials optional because different commands need different services. The application checks Blockstream credentials for authenticated tracing and the Miro token for board creation and live sync. Set only the services you use. Secret values are provided to the child process environment and are not added to the interactive parent shell or saved case files. They remain readable by the process that needs them; environment variables are a delivery mechanism, not encrypted storage.

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
