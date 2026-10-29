# Configuring a repository

1. Add `configs/github.com/<owner>/<repo>/config.yaml` and an `OWNERS` file beside it with `approvers` set to trusted GitHub logins, inside the relevant policy root: `deploy/policy/` for production, `examples/` for local trials.
2. Set `enabled: true`, name at least one expected CI check, and choose static or LLM rules. Add reusable profiles within `rules` using `- profile: <name>`. LLM models must appear in `config/global.yaml:allowed_models` for that root.
3. Run `uv run auto-merge-agent --config-root deploy/policy validate-config` (or `--config-root examples`). Keep the service in shadow mode until its logged decisions match the intended policy.
4. For `deploy/policy/`, add each new policy file to the `configMapGenerator` files list in `deploy/policy/kustomization.yaml` and to the volume `items` in `deploy/policy/volume.yaml`; the deployment tests fail until the mounted policy matches the tree exactly.

See [the README](README.md) for examples and [configuration](docs/configuration.md) for the full schema and inheritance rules.
