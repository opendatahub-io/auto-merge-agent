# Deploy on OpenShift

The deployment runs the GitHub App webhook service, not token-based polling.
It uses one replica and a `Recreate` update strategy because queues and duplicate
suppression are process-local. Updates briefly interrupt webhook delivery; the
startup and 15-minute scans recover open PRs. Do not add replicas or additional
Uvicorn workers. `Recreate` avoids overlap during normal deployment updates; it
does not provide distributed fencing during node failures or manual pod deletion.

## Build and publish an image

The `Containerfile` installs dependencies from `uv.lock` in a build stage. The
runtime contains the installed application and dependencies, with no policy
checkout, credentials, Git CLI, or runtime dependency installation. It supports
an arbitrary non-root UID with a read-only root filesystem and writable `/tmp`.

The production repository is `quay.io/opendatahub/auto-merge-agent`. The
OpenShift overlay pins the image to the commit that was built and published.
Build and push with Podman (Docker works too):

```bash
export AGENT_IMAGE=quay.io/opendatahub/auto-merge-agent:sha-$(git rev-parse HEAD)
podman build -f Containerfile -t "$AGENT_IMAGE" .
podman login quay.io
podman push "$AGENT_IMAGE"
```

The **Publish image** workflow uses GitHub OIDC federation, not a stored Quay
password or a GitHub Actions secret. Before its first use, an `opendatahub` Quay
organization administrator must create the robot account
`auto-merge-agent-publisher`, grant it **Write** permission only on
`opendatahub/auto-merge-agent`, and configure its federation entry as follows:

| Field | Value |
| --- | --- |
| Robot username | `opendatahub+auto-merge-agent-publisher` |
| Issuer | `https://token.actions.githubusercontent.com` |
| Audience | `quay` |
| Subject | `repo:opendatahub-io@57720972/auto-merge-agent@1412421827:ref:refs/heads/main` |

Use the immutable OIDC subject shown in this repository's **Settings → Actions →
OIDC** page as the source of truth; the value above is the expected immutable
subject for this repository. Restricting it to `main` means pull-request runs and
other branches cannot obtain a Quay push token. The workflow requests the `quay`
audience and exchanges the GitHub OIDC token for Quay's short-lived robot token
only while publishing.

When a push to `main` changes a runtime image input (`Containerfile`,
`.dockerignore`, `pyproject.toml`, `uv.lock`, or `src/`), **Publish image** runs
the entire CI workflow and then pushes the exact tested linux/amd64 image as
`sha-<full-commit-sha>`. It does not publish from PRs or deploy to a cluster.
You can also run it manually on `main` to deliberately republish a commit.
Ordinary CI needs no Quay credentials. For other CPU architectures, build and
test separately. Prefer a digest reference for deployments; tags can be
overwritten in a registry.

## Render and apply the checked-in overlays

Install Python 3.11+, uv, and Kustomize (CI uses 5.6.0). Add and review policies
using the [configuration guide](configuration.md), then validate and render:

```bash
uv sync --locked
uv run auto-merge-agent --config-root deploy/policy validate-config
mkdir -p build
kustomize build deploy/overlays/openshift > build/openshift.yaml
```

The checked-in overlays are the deployment source of truth; there is no separate
generation step. Use a reviewed, protected revision of this repository, not the
PR being evaluated. GitOps users can point Argo CD (or similar) at
`deploy/overlays/openshift` directly.

The `deploy/policy` Kustomize component packages the production policy tree —
root `OWNERS`, `config/global.yaml`, owner defaults, repository policies and
their `OWNERS`, profiles, and optional `context.md` files — into the
`auto-merge-agent-policy` ConfigMap. Volume `items` mappings restore the original
directory structure under `/etc/auto-merge-agent`. Both containers use the same
policy mount and image. The init container runs `validate-config` before the
webhook service can start.

**Adding a policy file.** A new file under `deploy/policy/` must be added to two
places in the component: an entry in the `configMapGenerator` files list, for
example
`opendatahub-io-my-repo-config.yaml=configs/github.com/opendatahub-io/my-repo/config.yaml`,
and a matching `key`/`path` item in `deploy/policy/volume.yaml`. CI renders the
overlay, reconstructs the tree as the container mounts it, and fails if any
policy file is missing, extra, misdirected, or altered — so a forgotten mapping
cannot deploy a policy that differs from the validated source. The same check
enforces a 200 KB ConfigMap budget (the client-side apply annotation limit) and
validates the reconstructed policy with `validate-config` logic.

**Setting the image.** `deploy/overlays/openshift/kustomization.yaml` pins the
deployed image for both the service and its validation init container. Update
`newTag` to the published `sha-<full-commit-sha>` tag, or pin immutably with a
digest instead of a tag:

```yaml
images:
  - name: auto-merge-agent
    newName: quay.io/opendatahub/auto-merge-agent
    newTag: sha-<full-commit-sha>
    # or, preferably: digest: sha256:<digest>
```

Keep Kustomize's generated name hash enabled: policy changes produce a new
ConfigMap name that updates the Deployment's volume reference and restarts the
service, refreshing the global model allowlist.

## Provision credentials

Register/install the App using [the deployment guide](deployment.md#register-and-install-the-app).
The commands below explicitly select `auto-merge-agent` so credentials and workloads
use the same namespace regardless of your current project. Create that namespace,
then provide the required Secret through your
secret manager or a local file that stays outside Git. GitOps users can let Argo CD
create it with `CreateNamespace=true`:

```bash
oc create namespace auto-merge-agent
# /secure/github-app.env contains app-id=... and webhook-secret=... lines.
oc -n auto-merge-agent create secret generic auto-merge-agent-github \
  --from-env-file=/secure/github-app.env \
  --from-file=private-key.pem=/secure/app-private-key.pem
```

The exact required keys are `app-id`, `webhook-secret`, and `private-key.pem`.
Only the private key is mounted as a file; the other values use explicit Secret
environment references. The validation init container receives no credentials.
The key file is read-only and readable by the arbitrary container UID; its volume
is accessible only to the agent container. Do not supply `GITHUB_TOKEN` to this
deployment: the webhook service rejects token authentication.

For OpenAI-backed rules, optionally create `auto-merge-agent-openai` with key
`api-key`, for example `--from-file=api-key=/secure/openai-api-key`. Static-only
policies require no OpenAI Secret. For a private Quay repository, create a registry
pull Secret and link it to the service account after creating it:

```bash
oc -n auto-merge-agent apply -f deploy/base/serviceaccount.yaml
oc -n auto-merge-agent create secret generic quay-pull \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=/secure/quay-auth.json
oc -n auto-merge-agent secrets link auto-merge-agent quay-pull --for=pull
```

No Kubernetes API permissions are needed. Service-account token mounting is
disabled. The base does not request a fixed UID or privileged SCC; OpenShift can
assign its normal namespace UID. Default resources are starting values, not
capacity guarantees; tune them against actual policy/model workloads.

## Deploy and verify shadow mode

```bash
oc -n auto-merge-agent apply -k deploy/overlays/openshift
oc -n auto-merge-agent rollout status deployment/auto-merge-agent
oc -n auto-merge-agent logs deployment/auto-merge-agent -c validate-config
oc -n auto-merge-agent logs deployment/auto-merge-agent -c agent
oc -n auto-merge-agent get route auto-merge-agent -o jsonpath='{.spec.host}'
```

Use `https://<route-host>/webhook` as the GitHub App's webhook URL. The Route uses
the cluster's default ingress certificate, redirects HTTP to HTTPS, and exposes
the `/webhook` path. Supply a custom host/certificate through your deployment
overlay if the cluster default is not publicly trusted or reachable by GitHub.
TLS terminates at the router; traffic from router to pod is HTTP. Clusters requiring
encrypted backend connections need additional server-side TLS support.

The overlay ships a `NetworkPolicy` that restricts ingress on port 8000 to the
cluster's router namespaces (selected by the `policy-group.network.openshift.io/ingress`
label OpenShift sets on them). Without it, any pod in the cluster could reach
`/health` and the other service paths directly, bypassing the Route. If your
cluster's router namespaces use a different label, or other components need to
call the service, adjust the policy in your deployment overlay before applying.

The service needs outbound access to GitHub and, when configured, the model
provider and MLflow server. There is no persistent volume or database requirement.
An optional MLflow integration uses `MLFLOW_TRACKING_URI` and `MLFLOW_EXPERIMENT`
environment entries on the agent container; supply any credentials through Secrets.

The startup/readiness/liveness probes all use `/health`. They establish HTTP process
availability, not GitHub authentication, worker progress, or downstream health.
Check the App's recent deliveries and the service's decision/error logs. `/health`
is available inside the cluster, or through `oc -n auto-merge-agent port-forward
service/auto-merge-agent 8000:8000`; it is not deliberately exposed by the public Route.

Shadow mode is the checked-in default (`AUTOMERGE_ACTIVE=false`). Once decisions
match the intended policies, add this strategic-merge patch through `patches` in
your deployment overlay's `kustomization.yaml` and reapply:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: auto-merge-agent
spec:
  template:
    spec:
      containers:
        - name: agent
          env:
            - name: AUTOMERGE_ACTIVE
              value: "true"
```

Save it as `active.yaml` beside the overlay's `kustomization.yaml`, then append
`patches: [{path: active.yaml}]` to that file (extend existing patches if present).
Keep environment-specific customizations in your protected deployment repository
or reproducible automation. Activation may merge existing eligible PRs in every
enabled selected repository.

## Updates and rollback

Deploy updates by applying a new reviewed revision of this repository: policy
edits and the image pin are committed changes to the checked-in overlays, so
render/review the new revision and apply it. Keep the previous rendered manifest
for rollback. To restore it, apply the saved manifest with
`oc -n auto-merge-agent apply -f <saved-manifest.yaml>` and wait
for the rollout. Rollback cannot undo merges already performed.

Old hashed ConfigMaps are not automatically deleted by `oc apply`; retain those
needed by rollback revisions, then remove unreferenced ones deliberately or use
your GitOps tool's retention/pruning policy. Never delete a ConfigMap referenced
by a running pod or a retained rollback manifest.

After rotating credentials, restart the deployment with
`oc -n auto-merge-agent rollout restart deployment/auto-merge-agent`.
Environment values and the App private key are captured at startup, so updating
a Secret alone is insufficient. The optional OpenAI key also needs a restart.

## CI and local kind testing

Every push and pull request runs:

| Check | Coverage |
| --- | --- |
| Python 3.12 | Existing application tests, deployment packaging tests, schema generation, policy validation, rule ordering |
| Manifests | Both overlays render; native resources and the OpenShift Route schema validate; the mounted policy tree is checked for completeness against `deploy/policy`, and hash-triggered pod changes are verified |
| Workflow lint | Actionlint validates GitHub Actions syntax and expressions |
| Image | Locked dependency build; entrypoint under arbitrary UID/read-only filesystem; policy/source-checkout isolation |
| kind 1.35.0 | Restricted Pod Security admission, server-side dry-run, init validation, readiness, Service connectivity, signed/tampered webhooks, shadow mode, arbitrary UID, read-only mounts, policy replacement, invalid-config startup rejection, rollback |

Actions and kind node images are pinned by commit/digest. The Route schema comes
from a pinned `openshift/api` revision on release-4.18. Schema validation does not
exercise an OpenShift router, SCC admission, or TLS certificates: verify those in
the actual OpenShift cluster before enabling writes. kind uses disabled policies
and ephemeral credentials; it tests deployment behavior without making real GitHub
or model calls. Application unit tests cover evaluation and merge behavior.

`CI complete` is the aggregate check suitable for branch protection. Failing jobs
retain test reports, rendered manifests, or cluster logs/events; Secret values
are excluded. The image artifact is retained for one day. Publishing is automatic
for runtime-input changes on `main` (and may be run manually there), restricted
to `main`, and gated on the same full test suite.

To reproduce the cluster smoke test locally with Docker, kind 0.31.0,
Kustomize 5.6.0, kubectl, and uv installed:

```bash
docker build -f Containerfile -t auto-merge-agent:ci .
kind create cluster --name automerge
kind load docker-image auto-merge-agent:ci --name automerge
uv sync --locked
uv run python scripts/smoke_deployment.py auto-merge-agent:ci
kind delete cluster --name automerge
```

Use a disposable kind cluster: the script creates resources and deliberately
deploys an invalid policy before testing rollback. It uses port 18000 locally.
