from pathlib import Path

import pytest

from auto_merge_agent.config import ConfigStore
from auto_merge_agent.llm import ComplexityAnswer, LangChainJudge, SafetyAnswer
from auto_merge_agent.types import ChangedFile, RuleConfig


@pytest.mark.asyncio
async def test_truncated_patch_is_not_sent_to_model(monkeypatch):
    judge = LangChainJudge(["test"])

    async def unexpected(*args):
        pytest.fail("Incomplete evidence must not reach the model")

    monkeypatch.setattr(judge, "_ask", unexpected)
    rule = RuleConfig(name="safe", type="llm_custom", model="test", prompt="Safe?")
    file = ChangedFile(path="main.go", status="modified", additions=2, deletions=1,
                       patch="@@ -1 +1 @@\n-old\n+new")
    assert (await judge.evaluate(rule, [file], "", "")).state == "reject"


@pytest.mark.asyncio
async def test_allowed_model_id_uses_exact_api_model_without_temperature(monkeypatch):
    config = ConfigStore(Path(__file__).resolve().parents[1] / "examples").load("github.com", "mprahl", "test-automerge")
    calls = []

    class FakeModel:
        def with_structured_output(self, schema):
            calls.append(("schema", schema))
            return self

        async def ainvoke(self, messages):
            calls.append(("messages", messages))
            return SafetyAnswer(safe_to_merge=True, reason="Small wording fix")

    def fake_init_chat_model(model, *, model_provider=None, **kwargs):
        calls.append(("model", model, model_provider, kwargs))
        return FakeModel()

    monkeypatch.setattr("langchain.chat_models.init_chat_model", fake_init_chat_model)
    answer = await LangChainJudge(config.allowed_models)._ask("gpt-6-sol", SafetyAnswer, "criterion", "diff")
    assert answer.safe_to_merge
    assert ("model", "gpt-6-sol", None, {}) in calls
    assert ("schema", SafetyAnswer) in calls


@pytest.mark.asyncio
async def test_each_llm_rule_supplies_its_own_checklist_entry(monkeypatch):
    config = ConfigStore(Path(__file__).resolve().parents[1] / "examples").load("github.com", "mprahl", "test-automerge")
    rules = next(rule.all for rule in config.policy.rules if rule.name == "low-risk-maintenance")
    judge = LangChainJudge(config.allowed_models)

    async def answer(model, schema, system, user):
        if schema is ComplexityAnswer:
            return ComplexityAnswer(complexity="low", reason="Localized change")
        return SafetyAnswer(safe_to_merge=True, reason="No interface change")

    monkeypatch.setattr(judge, "_ask", answer)
    files = [ChangedFile(path="main.go", status="modified", additions=1, deletions=1, patch="@@ -1 +1 @@\n-old\n+new")]
    complexity = await judge.evaluate(rules[1], files, "Small fix", "")
    safety = await judge.evaluate(rules[2], files, "Small fix", "")
    assert complexity.state == safety.state == "pass"
    assert "complexity: low <= low" in complexity.passed_checks[0]
    assert "safe_to_merge=true" in safety.passed_checks[0]


@pytest.mark.asyncio
async def test_repository_context_reaches_both_model_calls(monkeypatch):
    config = ConfigStore(Path(__file__).resolve().parents[1] / "examples").load("github.com", "mprahl", "test-automerge")
    rules = next(rule.all for rule in config.policy.rules if rule.name == "low-risk-maintenance")
    judge = LangChainJudge(config.allowed_models)
    calls = []

    async def answer(model, schema, system, user):
        calls.append((schema, user))
        if schema is ComplexityAnswer:
            return ComplexityAnswer(complexity="low", reason="Small")
        return SafetyAnswer(safe_to_merge=True, reason="Safe")

    monkeypatch.setattr(judge, "_ask", answer)
    for rule in rules[1:]:
        verdict = await judge.evaluate(
            rule, [ChangedFile(path="main.go", status="modified", additions=1, deletions=1, patch="@@ -1 +1 @@\n-old\n+new", diff_metadata="old mode 100644\nnew mode 100755")],
            "Fix", "", {"repository/AGENTS.md": "Check API compatibility"},
        )
        assert verdict.state == "pass"
    assert [schema for schema, _ in calls] == [ComplexityAnswer, SafetyAnswer]
    assert all("Check API compatibility" in user for _, user in calls)
    assert all("new mode 100755" in user for _, user in calls)
