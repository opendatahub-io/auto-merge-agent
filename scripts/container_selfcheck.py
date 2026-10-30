"""Runtime self-check executed inside the deployed container by the smoke test.

Run with ``kubectl exec -i <pod> -c agent -- python - < this file``; the script
takes no arguments and any failed assertion exits non-zero.
"""
import os
from pathlib import Path

assert os.getuid() == 1000780000
assert not Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists()
assert Path("/etc/github-app/private-key.pem").read_text().startswith("-----BEGIN")
assert Path("/etc/auto-merge-agent/configs/github.com/ci-owner/fixture/OWNERS").is_file()
for name in ["/app/unexpected-write", "/etc/auto-merge-agent/OWNERS"]:
    try:
        Path(name).write_text("unexpected")
    except OSError:
        pass
    else:
        raise AssertionError(name + " is writable")
Path("/tmp/writable-check").write_text("ok")
