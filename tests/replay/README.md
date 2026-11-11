# Offline runtime replay

`fixtures/issue22-cache.json` defines the baseline source, role completion
contract, seeded interruption and expected outcomes. Each case creates a real
Git repository and pins its task plan to that baseline commit. Graders and role
adapters live outside the disposable repository the developer can modify.

Run `python3 -m unittest discover -s tests -p test_candidate_capture.py -v`.
The adapters implement role contracts without parsing prompt wording or calling
providers. Real Python subprocesses clear their environment and generate caches;
real Git/index/journal operations exercise crash reconciliation. Docker mount
construction is checked offline; the separate credential-free Docker smoke
replay additionally validates those mounts against the daemon.

These cases are the I0 subset of A2, not the completed autonomy benchmark or
independent correctness oracle. Additional stop handlers and protected baseline
metrics arrive in later increments.


The I2 v2 corpus adds design oscillation, diagnosis citation and empty
post-diagnosis ADR. Run `python3 -I ssdlc_replay_gate.py --output FILE`.
The host verifies `schemas/recovery-grader-baseline.json` before loading the
semantic test helpers. Changes to this directory or imported grader/fake files
require a reviewed baseline revision and matching pins; candidate tasks cannot
perform that revision. The report omits unmeasured ready-PR/live-call metrics.


The issue #32 baseline (`i2-integrity-v3`) pins the isolated entrypoint and its
separate CI job. Only pinned test modules and fixtures are staged for imports;
new `tests/test_*.py` files are not discovered or reachable through the original
tests directory. All twelve corpus expectations are unchanged. Direct in-process
benchmark execution is useful for development but is not the isolated CI gate.


The current `i2-fixtures-v4` baseline extracts shared Git/scheduler and design
fixture definitions unchanged into `repository_fixtures.py`. The protected
replay imports that pinned module directly and does not import the ordinary
execution-mode test modules. Those tests remain in full unit discovery with
unchanged assertions; candidate tasks still cannot change replay helpers,
scenarios, expectations or the isolated gate.
