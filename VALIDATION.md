# Validation performed

- Version 0.2.0; Python 3.12.14; 49 automated tests passed.
- CLI synthetic run: hop ceiling 1, 2 transactions, 2 frontier outputs, 3 simulated requests.
- CLI continuation: ceiling extended to 3, 4 cumulative transactions, 4 additional simulated requests; branches terminate in fees, an unspendable output, and a peg-out request.
- Original run preserved.
- SVG graph parsed as XML and rendered for visual inspection.
- Miro payloads checked against official schema/model documentation. Mock transports cover incremental updates, persistent object IDs, repeated-run idempotence, analyst moves/resizing/annotations, field conflicts, missing items, stale-branch protection, interrupted publication, rate limits and uncertain-response recovery.
- CLI continuation preserves namespace and stable graph IDs. CSV relationships resolve to saved nodes, parent exports retain their original bytes, and checksum mismatches stop continuation before API access.
- Local Miro preview requires no credentials, makes no network calls, and changes no files. A failed automatic sync preserves the saved run, and its separate retry does not refetch Blockstream data.
- OAuth client credentials, bearer refresh after a 401, caching, budget enforcement and absence of saved credentials exercised with mock transports.

No paid Blockstream requests, case transactions, service swaps, or live Miro board writes were performed. Live API integration remains to be validated using local credentials and real case seeds.

Known Miro limits: shapes with frame/group-relative coordinates must be moved to the canvas before sync; avoid concurrent content/style editing during preflight and updates. Existing geometry is never patched. New-batch placement accounts for mapped shapes, not unrelated board items.

The devenv configuration declares Python 3.12, an explicit nixpkgs revision, and runtime SecretSpec launchers. YAML/TOML parsing, empty credential templates, extracted launcher shell syntax, argument quoting, working-directory preservation, offline CLI execution, and Git ignore rules were checked locally. The Python suite still passes all 49 tests. Nix/devenv are not installed in the implementation environment, so a full `devenv shell` build and real SecretSpec/keyring access have not been executed here. Run `devenv test` on a Nix machine to validate shell evaluation and the test suite together.

The user subsequently supplied successful `devenv test` output from their NixOS machine for the initial environment configuration. The log ends with `Running tests` successful; it does not expose individual test results. Real SecretSpec/keyring provisioning and live API authentication are still to be performed locally. The later `liquid-secrets-setup` helper prompts through SecretSpec and makes no API calls. Its shell syntax, service selection, explicit manifest/profile, absence of value arguments, help/invalid-input behavior, and stop-on-provider-failure were checked with a stub provider command.
