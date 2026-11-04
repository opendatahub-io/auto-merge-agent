"""Exercise a loaded image in an existing kind cluster without real credentials.

The cluster receives the checked-in disabled fixture policies from
``tests/fixtures/deployment``, never the production policy tree, plus
ephemeral credentials generated per run.

Cluster operations use the Kubernetes Python client wherever it offers an
equivalent: listing pods, waiting for rollout, patching the Deployment,
applying the Secret, exec, and port-forward. kubectl remains only for
server-side apply of rendered manifests, which the client library does not
support, matching how an operator deploys the service.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import yaml
from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import portforward, stream

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_COMPONENT = REPO_ROOT / "tests/fixtures/deployment"
NAMESPACE = "auto-merge-agent"
# Ephemeral per-run secret; generated, never stored in the repository.
WEBHOOK_SECRET = secrets.token_urlsafe(32)
core: k8s.CoreV1Api
apps: k8s.AppsV1Api


def run(*args: str, input: str | None = None, timeout: int = 180) -> str:
    return subprocess.check_output(args, input=input, text=True, timeout=timeout)


def kubectl(*args: str, input: str | None = None) -> str:
    return run("kubectl", "-n", NAMESPACE, *args, input=input)


def wait_for(check, description: str, timeout: int = 90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(1)
    raise AssertionError(f"Timed out: {description}")


def connect() -> None:
    contexts, current = k8s_config.list_kube_config_contexts()
    if not current["name"].startswith("kind-"):
        raise SystemExit(f"Refusing to run against non-kind context: {current['name']}")
    k8s_config.load_kube_config(context=current["name"])
    global core, apps
    core = k8s.CoreV1Api()
    apps = k8s.AppsV1Api()


def pods() -> list[k8s.V1Pod]:
    return core.list_namespaced_pod(
        NAMESPACE, label_selector="app.kubernetes.io/name=auto-merge-agent"
    ).items


def current_pod() -> k8s.V1Pod:
    candidates = [p for p in pods() if not p.metadata.deletion_timestamp]
    assert len(candidates) == 1, candidates
    return candidates[0]


def rollout() -> k8s.V1Pod:
    def available():
        deployment = apps.read_namespaced_deployment_status("auto-merge-agent", NAMESPACE)
        status = deployment.status
        if not status.observed_generation or status.observed_generation < deployment.metadata.generation:
            return None
        if status.available_replicas == 1 and status.unavailable_replicas in (None, 0):
            return deployment
        return None

    wait_for(available, "deployment to become available", timeout=180)
    pod = current_pod()
    init = next(s for s in pod.status.init_container_statuses if s.name == "validate-config")
    assert init.state.terminated.exit_code == 0
    return pod


def apply_secret(string_data: dict) -> None:
    secret = k8s.V1Secret(
        metadata=k8s.V1ObjectMeta(name="auto-merge-agent-github"),
        string_data=string_data,
    )
    try:
        core.read_namespaced_secret(secret.metadata.name, NAMESPACE)
    except ApiException:
        core.create_namespaced_secret(NAMESPACE, secret)
    else:
        core.replace_namespaced_secret(secret.metadata.name, NAMESPACE, secret)


def exec_in_agent(pod_name: str, command: list[str]) -> str:
    response = stream(
        core.connect_get_namespaced_pod_exec, pod_name, NAMESPACE,
        command=command, container="agent",
        stdout=True, stderr=True, tty=False,
        _preload_content=False,
    )
    response.run_forever(timeout=60)
    # read_all() clears every channel buffer, so the exit status must be read first.
    code = response.returncode
    output = "".join(response.read_all())
    assert code == 0, output
    return output


def write_overlay(directory: Path, component: Path, image: str) -> Path:
    """Compose a throwaway kind overlay from the base and a policy component.

    Platform files are copied beside the overlay because Kustomize's default
    load restrictions keep plain resource files within the build root.
    """
    directory.mkdir(parents=True)
    if "@sha256:" in image:
        name, digest = image.split("@", 1)
        transform = {"name": "auto-merge-agent", "newName": name, "digest": digest}
    else:
        name, tag = image.rsplit(":", 1)
        transform = {"name": "auto-merge-agent", "newName": name, "newTag": tag}
    relative = lambda target: os.path.relpath(target, directory.resolve())
    manifest = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "namespace": NAMESPACE,
        "resources": [relative(REPO_ROOT / "deploy" / "base"), "namespace.yaml"],
        "components": [relative(component)],
        "patches": [{"path": "security.yaml"}],
        "images": [transform],
    }
    (directory / "kustomization.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    for filename in ["namespace.yaml", "security.yaml"]:
        shutil.copyfile(REPO_ROOT / "deploy" / "overlays" / "kind" / filename, directory / filename)
    return directory


def apply_rendered(overlay: Path) -> str:
    rendered = run("kustomize", "build", str(overlay))
    kubectl("apply", "--dry-run=server", "-f", "-", input=rendered)
    kubectl("apply", "-f", "-", input=rendered)
    return rendered


def pod_http(pod_name: str, method: str, path: str, body: bytes | None = None,
             signature: str | None = None) -> tuple[int, dict]:
    """One HTTP request to the pod over a websocket port-forward."""
    forward = portforward(
        core.connect_get_namespaced_pod_portforward, pod_name, NAMESPACE, ports="8000",
    )
    connection = http.client.HTTPConnection("auto-merge-agent")
    try:
        connection.sock = forward.socket(8000)
        connection.sock.settimeout(5)
        headers = {"X-GitHub-Event": "pull_request", "Content-Type": "application/json"}
        if signature:
            headers["X-Hub-Signature-256"] = signature
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()
        forward.close()


def check_http():
    pod = current_pod().metadata.name

    def request(path, body=None, signature=None):
        return pod_http(pod, "POST" if body is not None else "GET", path, body, signature)

    def healthy():
        try:
            return request("/health") == (200, {"ok": True, "active": False})
        except (OSError, http.client.HTTPException, ApiException):
            return False

    wait_for(healthy, "service health via port-forward")
    body = json.dumps({
        "action": "opened", "number": 1,
        "repository": {"owner": {"login": "ci-owner"}, "name": "fixture"},
    }).encode()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert request("/webhook", body)[0] == 401
    assert request("/webhook", body, "sha256=invalid")[0] == 401
    assert request("/webhook", body + b" ", signature)[0] == 401
    # A valid signature is accepted, but disabled policies never enqueue work.
    assert request("/webhook", body, signature) == (200, {"queued": 0})


def main():
    connect()
    image = sys.argv[1] if len(sys.argv) > 1 else "auto-merge-agent:ci"
    with tempfile.TemporaryDirectory(prefix="automerge-smoke-") as tmp:
        root = Path(tmp)
        first = write_overlay(root / "first", FIXTURE_COMPONENT, image)
        kubectl("apply", "-f", str(REPO_ROOT / "deploy/overlays/kind/namespace.yaml"))
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        # Ephemeral test key, never uploaded as an artifact or logged.
        apply_secret({
            "app-id": "12345", "webhook-secret": WEBHOOK_SECRET,
            "private-key.pem": key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
        })
        original = apply_rendered(first)
        # GITHUB_WEBHOOK_SECRET is env-injected, so a rerun against an existing
        # cluster must restart the pod to pick up the newly generated secret.
        # This is the same annotation kubectl rollout restart sets.
        apps.patch_namespaced_deployment(
            "auto-merge-agent", NAMESPACE,
            body={"spec": {"template": {"metadata": {"annotations": {
                "kubectl.kubernetes.io/restarted-at": datetime.now(timezone.utc).isoformat(),
            }}}}},
        )
        pod = rollout()
        first_uid = pod.metadata.uid
        print("PASS: init validation, probes, Service, and restricted pod admission", flush=True)
        check_http()
        print("PASS: shadow health, signed webhook, missing/invalid/tampered signatures", flush=True)
        selfcheck = (REPO_ROOT / "scripts" / "container_selfcheck.py").read_text(encoding="utf-8")
        exec_in_agent(pod.metadata.name, ["python", "-c", selfcheck])
        print("PASS: arbitrary UID, read-only root/policies, writable tmp, no API token", flush=True)

        # Policy-triggered rollout: render the same fixture component with a
        # changed policy file, as a real policy revision would.
        revised = root / "revised-policy"
        shutil.copytree(FIXTURE_COMPONENT, revised)
        with (revised / "config/global.yaml").open("a") as f:
            f.write("\n# Exercise policy-triggered rollout\n")
        second = write_overlay(root / "second", revised, image)
        updated = apply_rendered(second)
        assert rollout().metadata.uid != first_uid
        assert first_uid not in {p.metadata.uid for p in pods()}
        check_http()
        print("PASS: policy hash triggers replacement and health recovers", flush=True)

        # Bypass the packaging checks to test the in-cluster init gate itself.
        manifests = list(yaml.safe_load_all(updated))
        cm = next(d for d in manifests if d["kind"] == "ConfigMap")
        deployment = next(d for d in manifests if d["kind"] == "Deployment")
        volume = next(v for v in deployment["spec"]["template"]["spec"]["volumes"] if v["name"] == "policy")["configMap"]
        cm["metadata"]["name"] += "-invalid"
        volume["name"] = cm["metadata"]["name"]
        key = next(item["key"] for item in volume["items"] if item["path"] == "config/global.yaml")
        cm["data"][key] = "not: [valid YAML"
        kubectl("apply", "-f", "-", input=yaml.safe_dump_all(manifests))

        def rejected():
            for candidate in pods():
                if candidate.metadata.deletion_timestamp:
                    continue
                volumes = candidate.spec.volumes or []
                if not any(v.config_map and v.config_map.name == cm["metadata"]["name"] for v in volumes):
                    continue
                for status in candidate.status.init_container_statuses or []:
                    for state in (status.state, status.last_state):
                        terminated = state.terminated if state else None
                        if terminated and terminated.exit_code:
                            return candidate
            return None

        bad = wait_for(rejected, "invalid policy rejected by init container")
        assert not any(s.state and s.state.running for s in bad.status.container_statuses or [])
        print("PASS: invalid policy prevents the service from starting", flush=True)
        kubectl("apply", "-f", "-", input=original)
        rollout()
        check_http()
        print("PASS: rollback to previous policy restores service", flush=True)


if __name__ == "__main__":
    main()
