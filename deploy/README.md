# Deployment sources

These Kustomize sources are applied directly; there is no generation step.
Render them with `kustomize build` or apply them with `kubectl/oc apply -k`.

See [the deployment guide](../docs/openshift.md) for the complete workflow.

- `base/`: platform-neutral GitHub App service (one replica, shadow mode).
- `overlays/openshift/`: an edge TLS Route for `/webhook` and the production
  image pin.
- `overlays/kind/`: no public endpoint; a restricted namespace and arbitrary UID
  for CI. It is not a deployment template: for another cluster, compose your own
  overlay and restrict the service port with a NetworkPolicy, using
  `overlays/openshift/` as the pattern.
- `policy/`: a Kustomize component that packages the production policy tree into
  the `auto-merge-agent-policy` ConfigMap and restores its directory layout
  under `/etc/auto-merge-agent` through volume `items` mappings.

When adding a policy file under `policy/`, add it to both the
`configMapGenerator` files list in `policy/kustomization.yaml` and the `items`
mapping in `policy/volume.yaml`; CI fails the completeness check in
`tests/test_deployment.py` otherwise. Keep Kustomize's generated name hash
enabled: policy changes produce a new ConfigMap name that updates the
Deployment's volume reference and restarts the service. Do not use
`--load-restrictor LoadRestrictionsNone` or commit credentials into this tree.
