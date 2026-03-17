#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Apply test Deployment, assert webhook injected sysctl."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / "test_deployment.yaml"


def kubectl(*args: str) -> subprocess.CompletedProcess:
    k = os.environ.get("KUBECTL") or ("kubectl" if shutil.which("kubectl") else "microk8s kubectl")
    cmd = k.split() + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def main() -> int:
    r = kubectl("apply", "-f", str(MANIFEST))
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        return 1

    # Poll up to 30 times (2s each) for the webhook-injected sysctl to appear in the deployment spec.
    for _ in range(30):
        r = kubectl("get", "deployment", "e2e-test-app", "-n", "default", "-o", "json")
        if r.returncode != 0:
            time.sleep(2)
            continue
        spec = json.loads(r.stdout).get("spec", {}).get("template", {}).get("spec") or {}
        sysctls = (spec.get("securityContext") or {}).get("sysctls") or []
        if any(s.get("name") == "net.ipv4.tcp_retries2" and str(s.get("value")) == "5" for s in sysctls):
            print("OK: deployment has sysctl net.ipv4.tcp_retries2=5")
            kubectl("delete", "-f", str(MANIFEST), "--ignore-not-found=true", "--wait=false")
            return 0
        time.sleep(2)

    print("Timeout: deployment spec did not get sysctl net.ipv4.tcp_retries2=5", file=sys.stderr)
    r = kubectl("get", "deployment", "e2e-test-app", "-n", "default", "-o", "json")
    if r.returncode == 0:
        spec = json.loads(r.stdout).get("spec", {}).get("template", {}).get("spec") or {}
        print(json.dumps(spec, indent=2), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
