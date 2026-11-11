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
