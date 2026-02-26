# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import base64
import logging
import os
from typing import Any

from fastapi import Body, FastAPI
from pydantic import BaseModel, TypeAdapter

app = FastAPI()

webhook = logging.getLogger(__name__)
webhook.setLevel(logging.INFO)
logging.basicConfig(format="[%(asctime)s] %(levelname)s: %(message)s")


def _is_true(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _env_required(name: str, *, allow_empty: bool = False) -> str:
    """Return env var value, but fail if it is not present.

    `allow_empty=True` allows the variable to be set to an empty string (e.g. to mean "match any").
    """
    if name not in os.environ:
        raise ValueError(f"Missing required environment variable: {name}")
    value = _env(name, "")
    if not allow_empty and not value:
        raise ValueError(f"Environment variable {name} must be non-empty")
    return value


def _parse_label_selector(selector: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    selector = selector.strip()
    if not selector:
        return labels
    for entry in selector.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError(f"Invalid label selector entry (expected key=value): {entry!r}")
        label_key, label_value = entry.split("=", 1)
        label_key, label_value = label_key.strip(), label_value.strip()
        if not label_key:
            raise ValueError(f"Invalid label selector entry (empty key): {entry!r}")
        labels[label_key] = label_value
    return labels


class MutatorConfig(BaseModel):
    """Configuration loaded from environment variables."""

    # Sysctl injection
    sysctl_name: str = "net.ipv4.tcp_retries2"
    sysctl_value: str = "5"

    # Optional namespace scope + label selection
    target_namespace: str = ""
    target_labels: dict[str, str] = {}
    require_labels: dict[str, str] = {}

    # Container/image matching
    target_container_names: list[str]
    target_image_substr: str

    # Check if the object is managed by Juju
    require_juju_managed: bool = True

    @classmethod
    def from_env(cls) -> "MutatorConfig":
        sysctl_name = _env("SYSCTL_NAME", cls.sysctl_name)
        sysctl_value = _env("SYSCTL_VALUE", cls.sysctl_value)

        target_namespace = _env("TARGET_NAMESPACE", "")
        target_labels = _parse_label_selector(_env("TARGET_LABELS", ""))
        require_labels = _parse_label_selector(_env("REQUIRE_LABELS", ""))

        require_juju_managed = _is_true(
            os.getenv("REQUIRE_JUJU_MANAGED"),
            default=cls.require_juju_managed,
        )

        # allow empty string to mean "match any image"
        target_image_substr = _env_required("TARGET_IMAGE_SUBSTR", allow_empty=True)

        raw_container_names = _env_required("TARGET_CONTAINER_NAMES", allow_empty=False)
        target_container_names = [n.strip() for n in raw_container_names.split(",") if n.strip()]
        if not target_container_names:
            raise ValueError("TARGET_CONTAINER_NAMES did not contain any valid container names")

        return cls(
            sysctl_name=sysctl_name,
            sysctl_value=sysctl_value,
            target_namespace=target_namespace,
            target_labels=target_labels,
            require_labels=require_labels,
            target_container_names=target_container_names,
            target_image_substr=target_image_substr,
            require_juju_managed=require_juju_managed,
        )


CFG = MutatorConfig.from_env()


class Patch(BaseModel):
    op: str
    path: str
    value: Any | None = None


ADAPTER = TypeAdapter(list[Patch])


def _b64_patch(patch_operations: list[Patch]) -> str:
    return base64.b64encode(ADAPTER.dump_json(patch_operations)).decode()


def _get_pod_spec_and_prefix(k8s_object: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """Return (pod_spec, jsonpatch_prefix) for supported objects.

    - Pod: prefix "/spec"
    - Workloads with pod template: prefix "/spec/template/spec"
    - CronJob: prefix "/spec/jobTemplate/spec/template/spec"
    """
    object_kind = (k8s_object.get("kind") or "").lower()
    object_spec = k8s_object.get("spec") or {}

    if object_kind == "pod":
        pod_spec = object_spec if isinstance(object_spec, dict) else {}
        return (pod_spec, "/spec")

    # CronJob spec.jobTemplate.spec.template.spec
    if (
        object_kind == "cronjob"
        and isinstance(object_spec, dict)
        and isinstance(object_spec.get("jobTemplate"), dict)
    ):
        job_template = object_spec.get("jobTemplate") or {}
        job_template_spec = job_template.get("spec") or {}
        if isinstance(job_template_spec, dict) and isinstance(
            job_template_spec.get("template"), dict
        ):
            pod_template = job_template_spec.get("template") or {}
            pod_spec = pod_template.get("spec") or {}
            if isinstance(pod_spec, dict):
                return (pod_spec, "/spec/jobTemplate/spec/template/spec")

    # StatefulSet/Deployment/DaemonSet/ReplicaSet/Job/ etc.
    if isinstance(object_spec, dict) and isinstance(object_spec.get("template"), dict):
        pod_template = object_spec.get("template") or {}
        pod_spec = pod_template.get("spec") or {}
        if isinstance(pod_spec, dict):
            return (pod_spec, "/spec/template/spec")

    if isinstance(object_spec, dict) and isinstance(object_spec.get("containers"), list):
        return (object_spec, "/spec")

    return None


def _pod_spec_matches_target(pod_spec: dict[str, Any]) -> bool:
    container_specs = pod_spec.get("containers") or []
    for container_spec in container_specs:
        if not isinstance(container_spec, dict):
            continue
        container_name = container_spec.get("name") or ""
        if CFG.target_container_names and container_name not in CFG.target_container_names:
            continue
        container_image = container_spec.get("image", "")
        if CFG.target_image_substr:
            if not (
                isinstance(container_image, str) and CFG.target_image_substr in container_image
            ):
                continue
        return True
    return False


def _labels_match(labels: dict[str, Any]) -> bool:
    if not CFG.target_labels:
        return True
    for label_key, label_value in CFG.target_labels.items():
        if labels.get(label_key) != label_value:
            return False
    return True


def _object_matches_scope(k8s_object: dict[str, Any]) -> bool:
    metadata = k8s_object.get("metadata") or {}
    object_labels = metadata.get("labels") or {}
    object_namespace = metadata.get("namespace") or ""

    if CFG.target_namespace and object_namespace != CFG.target_namespace:
        return False

    if not _labels_match(object_labels):
        return False

    for label_key, label_value in CFG.require_labels.items():
        if object_labels.get(label_key) != label_value:
            return False

    if CFG.require_juju_managed and object_labels.get("app.kubernetes.io/managed-by") != "juju":
        return False

    pod_spec_info = _get_pod_spec_and_prefix(k8s_object)
    if not pod_spec_info:
        return False
    pod_spec, _ = pod_spec_info
    return _pod_spec_matches_target(pod_spec)


def _build_sysctl_patch_ops(k8s_object: dict[str, Any]) -> list[Patch]:
    pod_spec_info = _get_pod_spec_and_prefix(k8s_object)
    if not pod_spec_info:
        return []

    pod_spec, jsonpatch_prefix = pod_spec_info
    security_context = pod_spec.get("securityContext")
    desired_sysctl = {"name": CFG.sysctl_name, "value": str(CFG.sysctl_value)}

    # no securityContext -> add it
    if security_context is None:
        return [
            Patch(
                op="add",
                path=f"{jsonpatch_prefix}/securityContext",
                value={"sysctls": [desired_sysctl]},
            )
        ]

    # securityContext exists but no sysctls -> add sysctls list
    if "sysctls" not in security_context or security_context.get("sysctls") is None:
        return [
            Patch(
                op="add",
                path=f"{jsonpatch_prefix}/securityContext/sysctls",
                value=[desired_sysctl],
            )
        ]

    # sysctls exists -> replace if present with a different value
    existing_sysctls = security_context.get("sysctls") or []
    for sysctl_index, sysctl_entry in enumerate(existing_sysctls):
        if not isinstance(sysctl_entry, dict):
            continue
        if sysctl_entry.get("name") != CFG.sysctl_name:
            continue
        if str(sysctl_entry.get("value")) == str(CFG.sysctl_value):
            return []
        return [
            Patch(
                op="replace",
                path=f"{jsonpatch_prefix}/securityContext/sysctls/{sysctl_index}/value",
                value=str(CFG.sysctl_value),
            )
        ]

    return [
        Patch(
            op="add",
            path=f"{jsonpatch_prefix}/securityContext/sysctls/-",
            value=desired_sysctl,
        )
    ]


def admission_review(uid: str, message: str, patch_operations: list[Patch]) -> dict[str, Any]:
    admission_response: dict[str, Any] = {
        "uid": uid,
        "allowed": True,
        "status": {"message": message},
    }
    if patch_operations:
        admission_response["patchType"] = "JSONPatch"
        admission_response["patch"] = _b64_patch(patch_operations)
    return {
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": admission_response,
    }


@app.post("/mutate")
def mutate_admission_review(admission_review_body: dict = Body(...)):
    admission_request = admission_review_body.get("request") or {}
    request_uid = admission_request.get("uid", "")
    k8s_object = admission_request.get("object") or {}
    object_name = (k8s_object.get("metadata") or {}).get("name") or "unknown"
    webhook.info("mutate called for %s", object_name)
    if not request_uid or not isinstance(k8s_object, dict):
        return admission_review(
            request_uid or "unknown", "Malformed admission request; allowing.", []
        )

    if not _object_matches_scope(k8s_object):
        return admission_review(request_uid, "Object out of scope; allowing without mutation.", [])

    try:
        patch_operations = _build_sysctl_patch_ops(k8s_object)
    except Exception as e:
        webhook.exception("Failed building sysctl patch: %s", e)
        return admission_review(
            request_uid, "Failed to build patch; allowing without mutation.", []
        )

    if patch_operations:
        webhook.info("Injecting pod sysctl %s=%s via JSONPatch", CFG.sysctl_name, CFG.sysctl_value)
        return admission_review(request_uid, "Injected pod sysctl.", patch_operations)

    return admission_review(request_uid, "Sysctl already set; no mutation.", [])
