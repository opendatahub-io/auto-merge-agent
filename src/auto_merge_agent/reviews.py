from __future__ import annotations

import json
import re

from .types import Finding, MergeRisk, PullRequestSnapshot, ReviewArtifact, ReviewAssessment, Severity


_RISK = re.compile(r"\*\*Merge Risk:\*\*\s*[*_]*[^\w_]*(Minimal|Low|Moderate|High|Critical)(?=$|[^A-Za-z])", re.I)
_RISK_SECTION = re.compile(r"<!-- final_review_risk_start -->(.*?)<!-- final_review_risk_end -->", re.S)
_COVERAGE = re.compile(r"<!-- final_review_risk_coverage:(\{[^\n]+\}) -->")
_BOLD_TOKEN = re.compile(r"\*\*([^*\n]+?)\*\*")
_SEVERITY_WORD = re.compile(r"^(?:<[^>]+>\s*)*\W*(Critical|Major|Minor|Trivial|Info)$", re.I)
_OUTSIDE_COUNT = re.compile(r"Outside diff range comments \((\d+)\)")
_ACTIONABLE_COUNT = re.compile(r"Actionable comments posted:\s*(\d+)")
_OUTSIDE_ITEM = re.compile(
    r"<summary><strong>(?:<[^>]+>\s*)*[^\w<]*(Critical|Major|Minor|Trivial|Info)</strong>\s*·\s*([^<]+)",
    re.I,
)
_SEVERITY_MAP = {
    "critical": Severity.critical,
    "major": Severity.high,
    "minor": Severity.medium,
    "trivial": Severity.low,
    "info": Severity.info,
}


def _header_line(body: str) -> str:
    """Return the first meaningful line: CodeRabbit findings may open with HTML comment markers."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("<!--"):
            return line
    return ""


def _finding_severity(body: str) -> Severity | None:
    """Read a finding's severity from its header line in either CodeRabbit format.

    Older headers lead with ``**Category** | **🟠 Major** | **⚡ Quick win``;
    newer ones with ``**<picture …></picture> Minor** · **Category** · **Quick win**``
    after an HTML comment marker. Exactly one bold token on the header line may
    be a bare severity word; anything else is an unrecognized format.
    """
    tokens = _BOLD_TOKEN.findall(_header_line(body))
    if len(tokens) < 2:
        return None
    words = [m.group(1) for token in tokens if (m := _SEVERITY_WORD.match(token))]
    if len(words) != 1:
        return None
    return _SEVERITY_MAP[words[0].lower()]


def _finding_title(body: str) -> str:
    lines = body.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("<!--"):
            return next((later.strip("* ") for later in lines[index + 1:] if later.startswith("**")), "CodeRabbit finding")
    return "CodeRabbit finding"


class CodeRabbitReviewer:
    """Versioned, fail-closed parser for CodeRabbit GitHub review artifacts."""

    def assess(self, pr: PullRequestSnapshot, artifacts: list[ReviewArtifact]) -> ReviewAssessment:
        comments = [
            a for a in artifacts
            if a.kind == "issue_comment" and a.author.lower() == "coderabbitai[bot]"
            and "auto-generated comment: summarize by coderabbit.ai" in a.body
        ]
        if not comments:
            return ReviewAssessment(state="waiting", reason="Waiting for a CodeRabbit summary")
        summary = max(comments, key=lambda a: a.updated_at or "")
        sections = _RISK_SECTION.findall(summary.body)
        if len(sections) != 1:
            return ReviewAssessment(state="unavailable", reason="Could not identify one CodeRabbit final risk assessment", evidence_url=summary.url)
        risk_section = sections[0]
        coverages = _COVERAGE.findall(risk_section)
        matching = False
        for raw in coverages:
            try:
                coverage = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if coverage.get("kind") == "reviewed" and coverage.get("coveredCommitId") == pr.head_sha:
                matching = True
        if not matching:
            reason = "CodeRabbit review does not cover the current head"
            if "Review limit reached" in summary.body:
                reason = "CodeRabbit review limit reached for the current head"
            elif "Currently processing new changes" in summary.body:
                reason = "CodeRabbit is reviewing the current head"
            return ReviewAssessment(state="waiting", reason=reason, evidence_url=summary.url)
        if "Review limit reached" in summary.body.split("<!-- walkthrough_start -->")[0]:
            return ReviewAssessment(state="unavailable", reason="CodeRabbit reported a review limit", evidence_url=summary.url)
        risk_match = _RISK.search(risk_section)
        if not risk_match:
            return ReviewAssessment(state="unavailable", reason="Could not parse CodeRabbit merge risk", evidence_url=summary.url)

        findings = []
        review_commits = {
            artifact.id: artifact.commit_id for artifact in artifacts
            if artifact.kind == "review" and artifact.author.lower() == "coderabbitai[bot]" and artifact.id is not None
        }
        for artifact in artifacts:
            if artifact.kind != "review_comment" or artifact.author.lower() != "coderabbitai[bot]":
                continue
            review_commit = review_commits.get(artifact.review_id)
            if review_commit != pr.head_sha:
                if review_commit is None and artifact.original_commit_id == pr.head_sha:
                    return ReviewAssessment(
                        state="unavailable", reason="CodeRabbit finding has no matching review event",
                        evidence_url=artifact.url,
                    )
                continue
            severity = _finding_severity(artifact.body)
            if severity is None:
                if "cr-indicator-types:potential_issue" in artifact.body:
                    return ReviewAssessment(state="unavailable", reason="CodeRabbit finding has unknown severity", evidence_url=artifact.url)
                continue
            title = _finding_title(artifact.body)
            findings.append(Finding(severity=severity, url=artifact.url, summary=title))
        for artifact in artifacts:
            if artifact.kind != "review" or artifact.author.lower() != "coderabbitai[bot]" or artifact.commit_id != pr.head_sha:
                continue
            count = _OUTSIDE_COUNT.search(artifact.body)
            outside_count = int(count.group(1)) if count else 0
            advertised = _ACTIONABLE_COUNT.search(artifact.body)
            inline_count = sum(
                item.kind == "review_comment" and item.author.lower() == "coderabbitai[bot]"
                and item.review_id == artifact.id for item in artifacts
            )
            if advertised and inline_count + outside_count < int(advertised.group(1)):
                return ReviewAssessment(
                    state="unavailable", reason="CodeRabbit review has missing actionable comments",
                    evidence_url=artifact.url,
                )
            if not count:
                continue
            items = _OUTSIDE_ITEM.findall(artifact.body)
            if len(items) != outside_count:
                return ReviewAssessment(
                    state="unavailable", reason="Could not parse all CodeRabbit outside-diff findings",
                    evidence_url=artifact.url,
                )
            for severity, title in items:
                findings.append(Finding(
                    severity=_SEVERITY_MAP[severity.lower()], url=artifact.url,
                    summary=title.strip(" ·"),
                ))
        return ReviewAssessment(
            state="complete", covered_head_sha=pr.head_sha,
            merge_risk=MergeRisk(risk_match.group(1).lower()), findings=findings,
            reason=f"CodeRabbit assessed merge risk as {risk_match.group(1).lower()}", evidence_url=summary.url,
        )
