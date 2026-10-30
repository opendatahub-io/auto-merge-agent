"""Verify the checked-in Kustomize overlays deploy the complete policy tree.

The overlays are the deployment source of truth: ``deploy/policy/`` feeds a
configMapGenerator whose keys the Deployment volume maps back to their original
paths. These tests render the real overlays and prove that the policy mounted
into the container matches the policy root exactly, so a policy file that is
added without updating the packaging fails CI instead of silently changing
what the deployed service enforces.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from auto_merge_agent.config import ConfigStore

ROOT = Path(__file__).resolve().parents[1]
POLICY_ROOT = ROOT / "deploy" / "policy"
# Kustomize infrastructure inside a policy root; every other file is policy.
POLICY_INFRASTRUCTURE = {"kustomization.yaml", "volume.yaml"}
# `kubectl apply` copies the whole ConfigMap into its last-applied annotation;
# stay below that limit with room for JSON-escaping overhead.
MAX_POLICY_BYTES = 200_000
KUSTOMIZE = shutil.which("kustomize")

requires_kustomize = pytest.mark.skipif(KUSTOMIZE is None, reason="kustomize required (installed in deployment CI)")


def render(overlay: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(subprocess.check_output(
        [KUSTOMIZE, "build", str(overlay)], text=True,
    )) if d]


def policy_inputs(root: Path) -> dict[str, str]:
    """Every checked-in policy file, keyed by its path relative to the root."""
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() not in POLICY_INFRASTRUCTURE
    }


def policy_volume(docs: list[dict]) -> tuple[dict, dict]:
    """Locate the policy ConfigMap through the Deployment's volume reference."""
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    volumes = deployment["spec"]["template"]["spec"]["volumes"]
    volume = next(v for v in volumes if v["name"] == "policy")["configMap"]
    config_map = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == volume["name"])
    return volume, config_map


def mounted_files(volume: dict, config_map: dict) -> dict[str, str]:
    return {item["path"]: config_map["data"][item["key"]] for item in volume["items"]}


def reconstruct(mounted: dict[str, str], target: Path) -> Path:
    """Rebuild the policy tree as the container mounts it."""
    for path, content in mounted.items():
        file = target / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
    return target


def assert_packaging_complete(policy_root: Path, overlay: Path, description: str) -> None:
    """Fail unless the overlay mounts the policy root exactly as checked in.

    Missing, extra, misdirected, or altered policy files, ConfigMap keys the
    volume never mounts, an oversized bundle, and a mounted tree that does not
    validate all fail here.
    """
    volume, config_map = policy_volume(render(overlay))
    items = volume["items"]
    assert items, f"{description}: policy volume mounts no ConfigMap items"
    unmounted_keys = sorted(set(config_map["data"]) - {item["key"] for item in items})
    assert not unmounted_keys, f"{description}: ConfigMap keys absent from volume items: {unmounted_keys}"
    mounted = mounted_files(volume, config_map)
    assert len(mounted) == len(items), f"{description}: duplicate path in volume items"
    expected = policy_inputs(policy_root)
    missing = sorted(set(expected) - set(mounted))
    extra = sorted(set(mounted) - set(expected))
    assert not missing, f"{description}: policy files not mounted: {missing}"
    assert not extra, f"{description}: mounted files missing from policy root: {extra}"
    for path, content in expected.items():
        assert mounted[path] == content, f"{description}: mounted {path} differs from the policy root"
    # Measure the serialized object: the annotation is the JSON encoding of the
    # whole ConfigMap, so character counts miss UTF-8 inflation of non-ASCII text.
    payload = json.dumps(config_map).encode("utf-8")
    assert len(payload) <= MAX_POLICY_BYTES, (
        f"{description}: policy bundle exceeds the {MAX_POLICY_BYTES // 1000} KB ConfigMap budget"
    )


def write_overlay(directory: Path, component: Path, image: str) -> Path:
    """Compose a throwaway overlay from the base and a policy component.

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
        "namespace": "auto-merge-agent",
        "resources": [relative(ROOT / "deploy" / "base"), "namespace.yaml"],
        "components": [relative(component)],
        "patches": [{"path": "security.yaml"}],
        "images": [transform],
    }
    (directory / "kustomization.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    for filename in ["namespace.yaml", "security.yaml"]:
        shutil.copyfile(ROOT / "deploy" / "overlays" / "kind" / filename, directory / filename)
    return directory


@requires_kustomize
def test_openshift_overlay_mounts_complete_production_policy(tmp_path):
    overlay = ROOT / "deploy" / "overlays" / "openshift"
    assert_packaging_complete(POLICY_ROOT, overlay, "deploy/overlays/openshift")
    # Reconstruct the files as the container mounts them and validate that
    # exact tree, not the source checkout.
    volume, config_map = policy_volume(render(overlay))
    mounted_root = reconstruct(mounted_files(volume, config_map), tmp_path / "mounted")
    assert ConfigStore(mounted_root).validate_all() == ["github.com/opendatahub-io/agent-og-auto-merge-test"]


@requires_kustomize
def test_fixture_component_mounts_complete_fixture_policy(tmp_path):
    component = ROOT / "tests" / "fixtures" / "deployment"
    overlay = write_overlay(tmp_path / "overlay", component, "auto-merge-agent:ci")
    assert_packaging_complete(component, overlay, "fixture overlay")
    volume, config_map = policy_volume(render(overlay))
    mounted_root = reconstruct(mounted_files(volume, config_map), tmp_path / "mounted")
    assert ConfigStore(mounted_root).validate_all() == ["github.com/ci-owner/fixture"]


@requires_kustomize
def test_profiles_owner_defaults_and_context_round_trip_through_packaging(tmp_path):
    """The documented add-a-file workflow keeps the mount complete."""
    component = tmp_path / "policy"
    shutil.copytree(ROOT / "tests" / "fixtures" / "deployment", component)
    (component / "profiles").mkdir()
    (component / "profiles" / "docs.yaml").write_text(
        "schema_version: 1\nrules:\n  - name: docs\n    type: changed_paths_only\n    paths: ['docs/**']\n",
        encoding="utf-8",
    )
    (component / "configs/github.com/ci-owner/config.yaml").write_text(
        "schema_version: 1\nrules:\n  - profile: docs\n", encoding="utf-8"
    )
    (component / "configs/github.com/ci-owner/fixture/context.md").write_text("Deployment guidance\n", encoding="utf-8")
    additions = [
        ("docs-profile.yaml", "profiles/docs.yaml"),
        ("ci-owner-config.yaml", "configs/github.com/ci-owner/config.yaml"),
        ("ci-owner-fixture-context.md", "configs/github.com/ci-owner/fixture/context.md"),
    ]
    manifest = yaml.safe_load((component / "kustomization.yaml").read_text(encoding="utf-8"))
    manifest["configMapGenerator"][0]["files"] += [f"{key}={path}" for key, path in additions]
    (component / "kustomization.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    patch = yaml.safe_load((component / "volume.yaml").read_text(encoding="utf-8"))
    items = patch["spec"]["template"]["spec"]["volumes"][0]["configMap"]["items"]
    items += [{"key": key, "path": path} for key, path in additions]
    (component / "volume.yaml").write_text(yaml.safe_dump(patch, sort_keys=False), encoding="utf-8")
    overlay = write_overlay(tmp_path / "overlay", component, "auto-merge-agent:ci")
    assert_packaging_complete(component, overlay, "extended fixture")
    volume, config_map = policy_volume(render(overlay))
    mounted_root = reconstruct(mounted_files(volume, config_map), tmp_path / "mounted")
    loaded = ConfigStore(mounted_root).load("github.com", "ci-owner", "fixture")
    assert loaded.policy.rules[0].name == "docs"
    assert loaded.llm_context == "Deployment guidance\n"


@requires_kustomize
def test_unpackaged_owner_policy_fails_completeness(tmp_path):
    """An owner-level policy left out of the packaging must fail CI.

    Without this check, the source root and the mounted tree could both pass
    validate-config while enforcing different trusted_authors: the owner layer
    only affects the source side, and the deployed service keeps falling back
    to the repository OWNERS approvers.
    """
    tree = tmp_path / "deploy"
    shutil.copytree(ROOT / "deploy", tree)
    owner_config = tree / "policy" / "configs/github.com/opendatahub-io/config.yaml"
    owner_config.write_text("schema_version: 1\ntrusted_authors: [org-reviewer]\n", encoding="utf-8")
    # Both sides remain individually valid...
    source = ConfigStore(tree / "policy")
    assert source.validate_all()
    overlay = tree / "overlays" / "openshift"
    volume, config_map = policy_volume(render(overlay))
    mounted_root = reconstruct(mounted_files(volume, config_map), tmp_path / "mounted")
    assert ConfigStore(mounted_root).validate_all()
    # ...while enforcing different trusted authors...
    arguments = ("github.com", "opendatahub-io", "agent-og-auto-merge-test")
    assert source.load(*arguments).trusted_authors == ["org-reviewer"]
    assert ConfigStore(mounted_root).load(*arguments).trusted_authors == ["accorvin"]
    # ...so only the completeness check catches the divergence.
    with pytest.raises(AssertionError, match=r"policy files not mounted: \['configs/github.com/opendatahub-io/config.yaml'\]"):
        assert_packaging_complete(tree / "policy", overlay, "mutated overlay")


@requires_kustomize
def test_policy_change_updates_configmap_hash_and_pod_template(tmp_path):
    tree = tmp_path / "deploy"
    shutil.copytree(ROOT / "deploy", tree)
    overlay = tree / "overlays" / "kind"

    def rendered():
        docs = render(overlay)
        volume, config_map = policy_volume(docs)
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        return deployment["spec"]["template"], volume["name"], config_map["metadata"]["name"]

    first_template, first_volume, first_config_map = rendered()
    assert first_volume == first_config_map
    with (tree / "policy" / "config" / "global.yaml").open("a") as f:
        f.write("\n# rollout change\n")
    second_template, second_volume, second_config_map = rendered()
    # The generated hash changes with the policy, the Deployment reference
    # follows it, and the pod template change rolls out a replacement pod.
    assert second_config_map != first_config_map
    assert second_volume == second_config_map
    assert second_template != first_template


@requires_kustomize
def test_rendered_platform_security():
    docs = render(ROOT / "deploy" / "overlays" / "openshift")
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert pod["automountServiceAccountToken"] is False
    assert "runAsUser" not in pod["securityContext"]
    images = {c["image"] for c in pod["containers"] + pod["initContainers"]}
    assert len(images) == 1 and ":latest" not in images.pop()
    for container in pod["containers"] + pod["initContainers"]:
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
    route = next(d for d in docs if d["kind"] == "Route")
    assert route["spec"]["tls"]["insecureEdgeTerminationPolicy"] == "Redirect"
    assert route["spec"]["path"] == "/webhook"

    docs = render(ROOT / "deploy" / "overlays" / "kind")
    assert not any(d["kind"] == "Route" for d in docs)
    pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 1000780000
