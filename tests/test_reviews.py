from auto_merge_agent.reviews import CodeRabbitReviewer
from auto_merge_agent.types import PullRequestRef, PullRequestSnapshot, ReviewArtifact


def pr(sha="a" * 40):
    return PullRequestSnapshot(ref=PullRequestRef(owner="acme", repo="repo", number=1), head_sha=sha, base_sha="b" * 40, state="open", draft=False, title="Test", url="https://github.com/acme/repo/pull/1", changed_files=1)


def summary(sha: str, risk="Minimal", prefix=""):
    body = f"""<!-- This is an auto-generated comment: summarize by coderabbit.ai -->
{prefix}
<!-- final_review_risk_start -->
**Merge Risk:** **⚪ {risk}** · up to `{sha[:5]}`
<!-- final_review_risk_coverage:{{"sourceCommitId":"{sha}","coveredCommitId":"{sha}","kind":"reviewed"}} -->
<!-- final_review_risk_end -->
"""
    return ReviewArtifact(kind="issue_comment", author="coderabbitai[bot]", body=body, url="https://example.com/summary", updated_at="2026-10-09T10:00:00Z")


def test_summary_can_establish_complete_review_without_review_event():
    current = pr()
    real_format = summary(current.head_sha).model_copy(update={"body": summary(current.head_sha).body.replace("**⚪ Minimal**", "_⚪ Minimal_")})
    assessment = CodeRabbitReviewer().assess(current, [real_format])
    assert assessment.state == "complete"
    assert assessment.covered_head_sha == current.head_sha
    assert assessment.merge_risk.value == "minimal"


def test_major_finding_normalized_to_high():
    current = pr()
    review = ReviewArtifact(kind="review", id=7, author="coderabbitai[bot]", commit_id=current.head_sha,
                            url="https://example.com/review", body="")
    issue = ReviewArtifact(kind="review_comment", review_id=7, author="coderabbitai[bot]", commit_id=current.head_sha,
                           original_commit_id=current.head_sha,
                           body="**🎯 Functional Correctness** | **🟠 Major** | **⚡ Quick win**\n\n**Fix compilation.**",
                           url="https://example.com/finding")
    assessment = CodeRabbitReviewer().assess(current, [summary(current.head_sha, "Moderate"), review, issue])
    assert assessment.state == "complete"
    assert assessment.merge_risk.value == "moderate"
    assert [finding.severity.value for finding in assessment.findings] == ["high"]
    assert assessment.findings[0].summary == "Fix compilation."


def test_icon_severity_finding_header_is_parsed():
    """CodeRabbit's newer comment format: an icon-prefixed severity leads, separated by middot."""
    current = pr()
    review = ReviewArtifact(kind="review", id=7, author="coderabbitai[bot]", commit_id=current.head_sha,
                            url="https://example.com/review", body="")
    issue = ReviewArtifact(kind="review_comment", review_id=7, author="coderabbitai[bot]", commit_id=current.head_sha,
                           original_commit_id=current.head_sha,
                           body=(
                               "<!-- coderabbit-ai-generated:v1 -->\n\n"
                               '**<picture><img src="https://example.com/amber-16.svg" width="16" height="16" alt=""></picture> Minor**'
                               " · **Data Integrity & Integration** · **Quick win**\n\n"
                               "**Document the trusted-approver prerequisite.**\n\n"
                               "<!-- cr-indicator-types:potential_issue -->"
                           ),
                           url="https://example.com/finding")
    assessment = CodeRabbitReviewer().assess(current, [summary(current.head_sha, "Moderate"), review, issue])
    assert assessment.state == "complete"
    assert [finding.severity.value for finding in assessment.findings] == ["medium"]
    assert assessment.findings[0].summary == "Document the trusted-approver prerequisite."


def test_ambiguous_severity_header_fails_closed():
    current = pr()
    review = ReviewArtifact(kind="review", id=7, author="coderabbitai[bot]", commit_id=current.head_sha,
                            url="https://example.com/review", body="")
    issue = ReviewArtifact(kind="review_comment", review_id=7, author="coderabbitai[bot]", commit_id=current.head_sha,
                           original_commit_id=current.head_sha,
                           body="**Critical** · **Major**\n\n**Two severity words.**\n<!-- cr-indicator-types:potential_issue -->",
                           url="https://example.com/finding")
    assessment = CodeRabbitReviewer().assess(current, [summary(current.head_sha), review, issue])
    assert assessment.state == "unavailable"
    assert assessment.reason == "CodeRabbit finding has unknown severity"


def test_risk_is_read_only_from_the_covered_final_assessment():
    current = pr()
    comment = summary(current.head_sha, "High", prefix="Quoted example: **Merge Risk:** Minimal")
    assessment = CodeRabbitReviewer().assess(current, [comment])
    assert assessment.state == "complete"
    assert assessment.merge_risk.value == "high"


def test_coverage_outside_final_assessment_does_not_establish_current_review():
    current = pr()
    comment = summary("b" * 40, prefix=(
        f'<!-- final_review_risk_coverage:{{"coveredCommitId":"{current.head_sha}","kind":"reviewed"}} -->'
    ))
    assessment = CodeRabbitReviewer().assess(current, [comment])
    assert assessment.state != "complete"


def test_relocated_comment_from_older_review_is_not_current_finding():
    current = pr("c" * 40)
    old_review = ReviewArtifact(kind="review", id=7, author="coderabbitai[bot]",
                                commit_id="a" * 40, url="https://example.com/old-review", body="")
    moved_comment = ReviewArtifact(
        kind="review_comment", review_id=7, author="coderabbitai[bot]",
        commit_id=current.head_sha, original_commit_id="a" * 40,
        body="**🔒 Security & Privacy** | **🟠 Major** | **⚡ Quick win**",
        url="https://example.com/old-finding",
    )
    assessment = CodeRabbitReviewer().assess(current, [summary(current.head_sha), old_review, moved_comment])
    assert assessment.state == "complete"
    assert assessment.findings == []


def test_stale_risk_and_rate_limit_do_not_qualify():
    current = pr("c" * 40)
    old = summary("a" * 40, "Low", prefix="Review limit reached")
    assessment = CodeRabbitReviewer().assess(current, [old])
    assert assessment.state == "waiting"
    assert assessment.covered_head_sha is None


def test_outside_diff_finding_in_current_head_review_is_counted():
    current = pr()
    outside = ReviewArtifact(
        kind="review", author="coderabbitai[bot]", commit_id=current.head_sha,
        url="https://github.com/opendatahub-io/odh-dashboard/pull/10155#pullrequestreview-5474127827",
        body="""> **⚠️ Outside diff range comments (1)**
> <details>
> <summary><strong>🟡 Minor</strong> · Reset stale existing-provider selections when the… · <code>AddProviderReferenceWizard.tsx:81-92</code></summary><blockquote>
> **🎯 Functional Correctness** | **🟡 Minor** | **⚡ Quick win**
""",
    )
    assessment = CodeRabbitReviewer().assess(current, [summary(current.head_sha, "Low"), outside])
    assert assessment.state == "complete"
    assert [finding.severity.value for finding in assessment.findings] == ["medium"]
    assert assessment.findings[0].url == outside.url


def test_unparsed_outside_diff_finding_fails_closed():
    current = pr()
    outside = ReviewArtifact(
        kind="review", author="coderabbitai[bot]", commit_id=current.head_sha,
        url="https://example.com/review", body="Outside diff range comments (1)\nunknown format",
    )
    assessment = CodeRabbitReviewer().assess(current, [summary(current.head_sha), outside])
    assert assessment.state == "unavailable"


def test_missing_actionable_comment_fails_closed():
    current = pr()
    review = ReviewArtifact(
        kind="review", id=12, author="coderabbitai[bot]", commit_id=current.head_sha,
        url="https://example.com/review", body="**Actionable comments posted: 2**",
    )
    assessment = CodeRabbitReviewer().assess(current, [summary(current.head_sha), review])
    assert assessment.state == "unavailable"
