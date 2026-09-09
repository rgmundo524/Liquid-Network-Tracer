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
| `liquid-demo` | Creates a synthetic one-hop run under `demo-case/`; no paid requests or credentials. |
| `liquid-test` | Runs the offline test suite. |

The launchers locate the source and secret declaration using `LIQUID_TRACER_ROOT`, preserving your working directory for relative case paths.

## Change public settings through devenv

The committed defaults are:

```nix
env = {
  LIQUID_TRACER_ROOT = config.devenv.root;
  LIQUID_CASE_DIR = "${config.devenv.root}/cases/current";
  LIQUID_SECRET_PROVIDER = "keyring";
};
```

Override the case directory for a shell, using your existing `-O` workflow:

```bash
devenv -O env.LIQUID_CASE_DIR:string /absolute/path/to/my-case shell
```

Then, inside that shell:

```bash
liquid-live trace --case "$LIQUID_CASE_DIR" --seed 'LIQUID_TXID:0' --hops 3
```

`LIQUID_CASE_DIR` is a convenience variable; pass it to `--case` as shown. It does not silently change explicit CLI arguments. A persistent personal override can go in ignored `devenv.local.nix`:

```nix
{ lib, ... }:
{
  env.LIQUID_CASE_DIR = lib.mkForce "/absolute/path/to/my-case";
}
```

Only use that file for nonsecret settings. Ignoring a Nix file in Git does not prevent evaluated secret strings from entering generated Nix artifacts.

## Store secrets in your keyring

Inside the project shell, run the setup helper and enter each credential at the prompt:

```bash
liquid-secrets-setup
```

Use `liquid-secrets-setup blockstream` or `liquid-secrets-setup miro` to configure one service. It always selects this project's manifest, configured provider, and `default` profile. Setting an existing entry replaces its value; rerun the relevant command when rotating a credential. No API requests are made by setup.

The equivalent individual commands, also useful when updating just one value, are:

```bash
secretspec set BLOCKSTREAM_CLIENT_ID --provider keyring --profile default
secretspec set BLOCKSTREAM_CLIENT_SECRET --provider keyring --profile default
secretspec set MIRO_ACCESS_TOKEN --provider keyring --profile default
```

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

Return to the project and provision the credentials:

```bash
git pull
devenv shell
liquid-secrets-setup
```

For a report that does not print the stored values:

```bash
secretspec --file "$LIQUID_TRACER_ROOT/secretspec.toml" check \
  --provider "$LIQUID_SECRET_PROVIDER" --profile default --explain
```

Confirm that each credential you intend to use is resolved. An optional missing entry does not cause this check to fail. This checks local secret resolution, not whether Blockstream or Miro accepts the credential.

```bash
liquid-live trace --case "$LIQUID_CASE_DIR" --seed 'LIQUID_TXID:0' --hops 3
liquid-live miro-sync --case "$LIQUID_CASE_DIR" --run RUN_ID --board 'MIRO_BOARD_URL'
```

The manifest marks credentials optional because different commands need different services. The existing application checks Blockstream credentials for authenticated tracing and the Miro token for live sync. Set only the services you use. Secret values are provided to the child process environment and are not added to the interactive parent shell. They remain readable by the process that needs them; environment variables are a delivery mechanism, not encrypted storage.

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
| OS keyring or password manager | No secret values in Git | Local secret storage; can also support a team's sharing workflow. |
| Local `.env` | No | Simple plaintext storage for one development machine. |
| CI/deployment secret store | No secret values in code | Supplies credentials to the job or service that needs them. |

For CI, GitHub Actions secrets can supply environment variables directly and run the Python CLI; `liquid-trace` also accepts them inside devenv. The offline test suite needs no GitHub secrets. See [GitHub's secrets documentation](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions).

Other SecretSpec providers can replace keyring by setting the nonsecret `LIQUID_SECRET_PROVIDER` value. Avoid placing values in `env.MIRO_ACCESS_TOKEN`, reading secret files with `builtins.readFile`, or passing credentials as `devenv -O` arguments. The supported pattern follows [devenv's runtime SecretSpec guidance](https://devenv.sh/integrations/secretspec/).

`.gitignore` prevents ordinary additions of matching untracked files; it is not encryption and does not untrack files already committed. If a credential is ever committed, revoke/rotate it. Removing the current file alone does not remove it from Git history.
