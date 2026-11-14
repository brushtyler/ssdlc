> [!NOTE]
> **Agent:** Codex (CLI v0.163.0)
> **Model:** non noto
> **Reasoning Level:** non noto

# Issue #22 final validation

Source `b3236051d98a4af1655fd3803843de283e11ab60`, integrated with main `609bf27`, is ready for human PR review. All 1,497 test executions passed in 1,468.850 seconds; all 12 isolated recovery cases passed. The full suite collected failures without stopping at the first error.

Review Codex and Claude approved; QA passed with 58 explicit requirement results; AppSec returned SECURE. [Final gate records](final-gates.json), the four complete human assessments, [QA results](qa-requirement-results.json), [executed requirement trace](executed-requirement-test-trace.json), [13 AC38 scenario groups](ac38-integration-scenarios.json) and [host audit](host-final-audit.json) retain the source binding and limitations. Most QA rows are test-trace PASS; the reviewers disclose targeted source inspection. These results do not imply exhaustive source auditing.

The original Claude review rendering duplicated its explicit verdict. A separately authorized host-only canonicalization removed only that identical trailing duplicate; report, findings, typed verdict and observed cost were unchanged. Codex's original native approval was preserved. Neither reviewer was called again. The original failed native Claude outcome remains preserved, with its hash and repair provenance in [the protocol receipt](recorded-review-protocol-repair.json). QA and AppSec then passed through native evaluators.

Gitleaks retains exit code 1: AppSec independently confirmed four current-file protocol/synthetic false positives and the two historical synthetic redaction fixtures. No allowlist was changed. The pinned pip-audit 2.10.1 project dependency audit found zero vulnerabilities in six resolved dependencies; the default image lacks pip-audit, so this audit ran in disposable validation scratch. It is not an audit of the entire runner image.

The frozen native development history was not rewritten or advanced. Final integrated-source validation used native evaluator, runner and cumulative-limit components in an explicitly authorized external host job. [Operator validation code](operator-validation/) and offline adapter checks are included. No source was modified after the final suite or source-bound assessments. Raw private transcripts, full logs, account details and authorization records are excluded; published records retain hashes to private originals. Synthetic secret literals in the public AppSec prose are replaced by descriptive placeholders.

Earlier rejected candidates remain historical evidence in the sibling folders. They do not authorize this source. GitHub CI is a separate PR check; its live status is visible on the PR.
