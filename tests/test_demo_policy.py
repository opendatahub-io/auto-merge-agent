from pathlib import Path

import pytest

from auto_merge_agent.config import ConfigStore
from auto_merge_agent.engine import EvaluationEngine
from auto_merge_agent.types import (
    Check, ChangedFile, GateResult, MergeRisk, PullRequestRef,
    PullRequestSnapshot, ReviewAssessment,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
REF = PullRequestRef(owner="mprahl", repo="test-automerge", number=1)


class DemoForge:
    def __init__(self, files: list[ChangedFile]):
        self.files = files
        self.author = "mprahl"
        self.repository_files = {}
        self.context_requests = []
        self.diff_requests = 0
        self.complete_files = files
        self.checks = [
            Check(name="lint", source="github-actions", status="completed", conclusion="success"),
            Check(name="CodeRabbit", source="coderabbitai[bot]", status="completed", conclusion="success"),
        ]

    async def get_pull_request(self, ref):
        return PullRequestSnapshot(
            ref=ref, author_login=self.author, head_sha="a" * 40, base_sha="b" * 40,
            state="open", draft=False, title="Demo", url="https://example.com",
            changed_files=len(self.files),
        )

    async def get_changed_files(self, pr):
        return self.files

    async def get_complete_changed_files(self, pr, files):
        self.diff_requests += 1
        return self.complete_files

    async def get_checks(self, pr):
        return self.checks

    async def get_review_artifacts(self, pr):
        return []

    async def get_repository_file(self, pr, path):
        self.context_requests.append((path, pr.base_sha))
        return self.repository_files.get(path)


class DemoReviewer:
    def assess(self, pr, artifacts):
        return ReviewAssessment(
            state="complete", covered_head_sha=pr.head_sha,
            merge_risk=MergeRisk.minimal, reason="Current head reviewed",
        )


class DemoJudge:
    def __init__(self, state="pass"):
        self.state = state
        self.calls = 0
        self.context = None

    async def evaluate(self, gates, files, title, body, context):
        self.calls += 1
        self.context = context
        return GateResult(state=self.state, reason="Demo LLM judgment")


def file(path, additions=1, deletions=1):
    patch = f"@@ -1,{deletions} +1,{additions} @@\n" + "-old\n" * deletions + "+new\n" * additions
    return ChangedFile(path=path, status="modified", additions=additions, deletions=deletions, patch=patch)


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", [True, False])
async def test_incomplete_patch_is_retrieved_before_llm(recovered):
    complete = file("main.go", additions=2)
    forge = DemoForge([complete.model_copy(update={"patch": "@@ -1 +1 @@\n-old\n+new"})])
    if recovered:
        forge.complete_files = [complete]
    judge = DemoJudge()
    _, result = await EvaluationEngine(ConfigStore(EXAMPLES), forge, DemoReviewer(), judge).evaluate(REF)
    assert forge.diff_requests == 1
    assert result.state == ("eligible" if recovered else "human_review")
    assert judge.calls == (2 if recovered else 0)


@pytest.mark.asyncio
async def test_complete_diff_failure_waits_without_calling_llm():
    class UnavailableForge(DemoForge):
        async def get_complete_changed_files(self, pr, files):
            raise RuntimeError("Unavailable")

    forge = UnavailableForge([file("main.go").model_copy(update={"patch": None})])
    judge = DemoJudge()
    _, result = await EvaluationEngine(ConfigStore(EXAMPLES), forge, DemoReviewer(), judge).evaluate(REF)
    assert result.state == "waiting"
    assert "Complete diff unavailable" in result.reasons[0]
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_dependency_bump_qualifies_without_llm():
    judge = DemoJudge()
    engine = EvaluationEngine(ConfigStore(EXAMPLES), DemoForge([file("go.mod"), file("go.sum", 2, 2)]), DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "eligible"
    assert result.matched_rule == "go-dependencies"
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_small_go_fix_uses_llm():
    judge = DemoJudge()
    engine = EvaluationEngine(ConfigStore(EXAMPLES), DemoForge([file("main.go")]), DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "eligible"
    assert result.matched_rule == "low-risk-maintenance"
    assert judge.calls == 2


@pytest.mark.asyncio
async def test_large_server_change_stops_before_llm():
    judge = DemoJudge()
    engine = EvaluationEngine(ConfigStore(EXAMPLES), DemoForge([file("main.go", 101, 0)]), DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "human_review"
    assert "llm_complexity limit 100 exceeded" in result.reasons[0]
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_small_server_change_can_be_rejected_by_llm():
    judge = DemoJudge("reject")
    engine = EvaluationEngine(ConfigStore(EXAMPLES), DemoForge([file("main.go", 20, 0), file("README.md")]), DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "human_review"
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_all_group_requires_custom_rule_after_complexity_passes():
    class RejectSafety(DemoJudge):
        async def evaluate(self, rule, files, title, body, context):
            self.calls += 1
            return GateResult(state="reject" if rule.type == "llm_custom" else "pass", reason="Unsafe interface change")

    judge = RejectSafety()
    engine = EvaluationEngine(ConfigStore(EXAMPLES), DemoForge([file("main.go")]), DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "human_review"
    assert result.matched_rule is None
    assert "maintenance-safety" in result.reasons[0]
    assert judge.calls == 2


@pytest.mark.asyncio
async def test_coderabbit_status_does_not_satisfy_expected_ci():
    forge = DemoForge([file("go.mod"), file("go.sum")])
    forge.checks = [Check(name="CodeRabbit", source="coderabbitai[bot]", status="completed", conclusion="success")]
    judge = DemoJudge()
    engine = EvaluationEngine(ConfigStore(EXAMPLES), forge, DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "waiting"
    assert "expected CI check: lint" in result.reasons[0]
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_coderabbit_status_failure_blocks_merge_when_observed():
    forge = DemoForge([file("go.mod"), file("go.sum")])
    forge.checks[-1] = Check(name="CodeRabbit", source="coderabbitai[bot]", status="completed", conclusion="failure")
    engine = EvaluationEngine(ConfigStore(EXAMPLES), forge, DemoReviewer(), DemoJudge())
    _, result = await engine.evaluate(REF)
    assert result.state == "human_review"
    assert "CI check CodeRabbit concluded failure" in result.reasons[0]


@pytest.mark.asyncio
async def test_llm_cap_uses_shared_generated_file_exclusion():
    judge = DemoJudge()
    engine = EvaluationEngine(
        ConfigStore(EXAMPLES),
        DemoForge([file("main.go"), file("go.sum", 150, 0)]),
        DemoReviewer(), judge,
    )
    _, result = await engine.evaluate(REF)
    assert result.state == "eligible"
    assert judge.calls == 2


@pytest.mark.asyncio
async def test_custom_llm_rule_has_independent_cap(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/global.yaml").write_text((EXAMPLES / "config/global.yaml").read_text())
    repo_dir = tmp_path / "configs/github.com/mprahl/test-automerge"
    repo_dir.mkdir(parents=True)
    (repo_dir / "OWNERS").write_text("approvers: [mprahl]\n")
    (repo_dir / "config.yaml").write_text("""schema_version: 1
enabled: true
ci: {expected: [{name: lint, source: github-actions}], ignore: [{name: CodeRabbit}]}
rules:
  - name: custom-cap
    type: llm_custom
    paths: ["*.go"]
    model: gpt-6-sol
    prompt: "Is this low risk?"
    max_changed_lines: 5
""")
    judge = DemoJudge()
    engine = EvaluationEngine(ConfigStore(tmp_path), DemoForge([file("main.go", 6, 0)]), DemoReviewer(), judge)
    _, result = await engine.evaluate(REF)
    assert result.state == "human_review"
    assert "llm_custom limit 5 exceeded" in result.reasons[0]
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_author_restriction_inside_all_group_skips_llm_for_other_trusted_authors(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/global.yaml").write_text((EXAMPLES / "config/global.yaml").read_text())
    repo_dir = tmp_path / "configs/github.com/mprahl/test-automerge"
    repo_dir.mkdir(parents=True)
    (repo_dir / "OWNERS").write_text("approvers: [mprahl]\n")
    (repo_dir / "config.yaml").write_text("""schema_version: 1
enabled: true
ci: {expected: [{name: lint, source: github-actions}], ignore: [{name: CodeRabbit}]}
rules:
  - name: bot-generated
    all:
      - {name: generated-path, type: changed_paths_only, paths: [generated/synced.yaml]}
      - name: generated-safety
        type: llm_custom
        authors: ['file-sync[bot]']
        model: gpt-6-sol
        prompt: "Is this safe?"
""")
    forge = DemoForge([file("generated/synced.yaml")])
    judge = DemoJudge()
    engine = EvaluationEngine(ConfigStore(tmp_path), forge, DemoReviewer(), judge)
    _, rejected = await engine.evaluate(REF)
    assert rejected.state == "human_review"
    assert judge.calls == 0
    forge.author = "FILE-SYNC[bot]"
    _, accepted = await engine.evaluate(REF)
    assert accepted.state == "eligible"
    assert accepted.matched_rule == "bot-generated"
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_llm_context_uses_agents_and_explicit_base_files(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/global.yaml").write_text((EXAMPLES / "config/global.yaml").read_text())
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles/go-dependencies.yaml").write_text((EXAMPLES / "profiles/go-dependencies.yaml").read_text())
    repo_dir = tmp_path / "configs/github.com/mprahl/test-automerge"
    repo_dir.mkdir(parents=True)
    (repo_dir / "OWNERS").write_text("approvers: [mprahl]\n")
    (repo_dir / "config.yaml").write_text(
        (EXAMPLES / "configs/github.com/mprahl/test-automerge/config.yaml").read_text()
        + "\nllm_context_files: [docs/merge-policy.md]\n"
    )
    (repo_dir / "context.md").write_text("Policy checkout guidance")
    forge = DemoForge([file("main.go")])
    forge.repository_files = {
        "AGENTS.md": "Repository instructions",
        "CLAUDE.md": "Fallback instructions",
        "docs/merge-policy.md": "Merge policy",
    }
    judge = DemoJudge()
    _, result = await EvaluationEngine(ConfigStore(tmp_path), forge, DemoReviewer(), judge).evaluate(REF)
    assert result.state == "eligible"
    assert judge.context == {
        "policy/context.md": "Policy checkout guidance",
        "repository/AGENTS.md": "Repository instructions",
        "repository/docs/merge-policy.md": "Merge policy",
    }
    assert forge.context_requests == [("AGENTS.md", "b" * 40), ("docs/merge-policy.md", "b" * 40)]
    del forge.repository_files["docs/merge-policy.md"]
    _, missing = await EvaluationEngine(ConfigStore(tmp_path), forge, DemoReviewer(), judge).evaluate(REF)
    assert missing.state == "human_review"
    assert "Configured LLM context file missing" in missing.reasons[0]


@pytest.mark.asyncio
async def test_llm_context_falls_back_to_claude():
    forge = DemoForge([file("main.go")])
    forge.repository_files = {"CLAUDE.md": "Fallback instructions"}
    judge = DemoJudge()
    _, result = await EvaluationEngine(ConfigStore(EXAMPLES), forge, DemoReviewer(), judge).evaluate(REF)
    assert result.state == "eligible"
    assert judge.context == {"repository/CLAUDE.md": "Fallback instructions"}
