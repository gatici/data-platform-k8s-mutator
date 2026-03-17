#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""
One-command deploy: generate webhook TLS certs and apply all manifests.
Calls gen_webhook_certs.generate() with parameters from CLI.

Requires: openssl, kubectl in PATH. Run from project root.

Usage:
  python -m scripts.bootstrap_webhook
  python -m scripts.bootstrap_webhook --image myimage/mutator:v1 --target-container-names myapp
  python -m scripts.bootstrap_webhook --dry-run
  python -m scripts.bootstrap_webhook --namespace myns --sysctl-name net.ipv4.tcp_retries2 --sysctl-value 5
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts.gen_webhook_certs import GenWebhookCertsConfig, generate


def _kubectl_cmd() -> list[str]:
    if os.environ.get("KUBECTL"):
        return os.environ.get("KUBECTL", "").split()
    if shutil.which("kubectl"):
        return ["kubectl"]
    return ["microk8s", "kubectl"]


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Generate webhook certs and all manifests, then apply (or --dry-run to only generate)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only generate manifests; do not kubectl apply",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: deploy/)",
    )
    parser.add_argument(
        "--namespace", default="default", help="Kubernetes namespace for webhook"
    )
    parser.add_argument(
        "--service", default="sysctl-webhook", help="Deployment/Service name"
    )
    parser.add_argument(
        "--secret-name", default="sysctl-webhook-tls", help="TLS Secret name"
    )
    parser.add_argument(
        "--webhook-config-name",
        default="sysctl-webhook",
        help="MutatingWebhookConfiguration name",
    )
    parser.add_argument(
        "--validity-days",
        type=int,
        default=3650,
        help="Certificate validity in days, by default 10 years",
    )
    parser.add_argument(
        "--image",
        default="sysctl-webhook:latest",
        help="Container image for the webhook",
    )
    parser.add_argument(
        "--target-namespaces",
        default="",
        help="TARGET_NAMESPACES env, comma-separated namespaces to mutate (empty = any)",
    )
    parser.add_argument(
        "--target-container-names",
        default="",
        help="TARGET_CONTAINER_NAMES env (required by app)",
    )
    parser.add_argument(
        "--target-image-substr",
        default="",
        help="TARGET_IMAGE_SUBSTR env; empty = any image",
    )
    parser.add_argument("--target-labels", default="", help="TARGET_LABELS env")
    parser.add_argument(
        "--sysctl-name", default="net.ipv4.tcp_retries2", help="SYSCTL_NAME env"
    )
    parser.add_argument("--sysctl-value", default="5", help="SYSCTL_VALUE env")
    parser.add_argument(
        "--image-pull-policy",
        default="",
        help="imagePullPolicy for the webhook container (e.g. Never for local images)",
    )
    args = parser.parse_args()

    out_dir = args.output_dir if args.output_dir is not None else repo_root / "deploy"

    config = GenWebhookCertsConfig(
        output_dir=out_dir,
        namespace=args.namespace,
        service=args.service,
        secret_name=args.secret_name,
        webhook_config_name=args.webhook_config_name,
        validity_days=args.validity_days,
        image=args.image,
        target_namespaces=args.target_namespaces,
        target_container_names=args.target_container_names,
        target_image_substr=args.target_image_substr,
        target_labels=args.target_labels,
        sysctl_name=args.sysctl_name,
        sysctl_value=args.sysctl_value,
        image_pull_policy=args.image_pull_policy,
    )
    if generate(config) != 0:
        return 1

    if args.dry_run:
        print("Dry run: skipping kubectl apply.")
        print(f"Generated files in {out_dir}/")
        return 0

    # Apply namespace and secret first.
    for f in ["namespace.yaml", "secret.yaml"]:
        path = out_dir / f
        if not path.exists():
            print(f"Missing {path}", file=sys.stderr)
            return 1
        subprocess.run([*_kubectl_cmd(), "apply", "-f", str(path)], check=True)

    # Apply workload so the webhook pod can start.
    workload_path = out_dir / "workload.yaml"
    if not workload_path.exists():
        print(f"Missing {workload_path}", file=sys.stderr)
        return 1
    subprocess.run([*_kubectl_cmd(), "apply", "-f", str(workload_path)], check=True)

    # Wait for the webhook pod to be ready before registering the webhook.
    subprocess.run(
        [
            *_kubectl_cmd(),
            "rollout",
            "status",
            f"deployment/{config.service}",
            "-n",
            config.namespace,
            "--timeout=120s",
        ],
        check=True,
    )

    # Now register the webhook so the API server sends admission requests to it.
    webhook_config_path = out_dir / "webhook-config.yaml"
    if not webhook_config_path.exists():
        print(f"Missing {webhook_config_path}", file=sys.stderr)
        return 1
    subprocess.run(
        [*_kubectl_cmd(), "apply", "-f", str(webhook_config_path)], check=True
    )

    print("Bootstrap complete. Webhook is running.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
