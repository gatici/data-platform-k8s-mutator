# Basic k8s mutating webhook handler

This mutating admission webhook injects a pod sysctl into Kubernetes objects at admission time by
returning a JSONPatch that adds/updates `spec.securityContext.sysctls` in the embedded Pod spec.

It can target:
- Pod objects (/spec)
- controllers with a pod template (/spec/template/spec) such as StatefulSet, Deployment, DaemonSet, Job, etc.
- CronJob pod templates (/spec/jobTemplate/spec/template/spec)

By default it ensures:

- `net.ipv4.tcp_retries2=5`

This is useful for workloads that require `tcp_retries2` to be lowered in the pod network namespace
without patching pods after creation.

## Operational guideline (3 prerequisites)

To run this webhook successfully you must enable/configure these 3 items in the cluster.

### 1. cert-manager (TLS + CA injection)

This repository assumes cert-manager provisions the webhook TLS secret using `certs.yaml` and injects the CA bundle into the `MutatingWebhookConfiguration` in the `workload.yaml`.

MicroK8s:

```bash
microk8s status --wait-ready
microk8s enable cert-manager
microk8s kubectl -n cert-manager get pods
```

### 2. Admission webhooks on the API server

- The `MutatingAdmissionWebhook` admission plugin must be enabled on the API server (usually enabled by default).
- The API server must be able to reach the webhook service. The network policies/firewalls must allow it.
- The webhook must serve HTTPS and the `MutatingWebhookConfiguration` must contain a trusted CA bundle.

MicroK8s: MutatingAdmissionWebhook is enabled by default. The most useful check is whether it has been explicitly disabled:

```bash
sudo grep -E -- '--disable-admission-plugins' /var/snap/microk8s/current/args/kube-apiserver
```

If the output contains `MutatingAdmissionWebhook`, remove it from the disabled list and restart MicroK8s:

```bash
microk8s stop
microk8s start
microk8s status --wait-ready
```

You can also inspect what’s enabled:

```bash
sudo grep -E -- '--enable-admission-plugins' /var/snap/microk8s/current/args/kube-apiserver
```

### 3. kubelet: allow the unsafe sysctl

This webhook only adds the sysctl to the Pod spec. Nodes will still reject pods using unsafe sysctls unless kubelet is configured to allow them.

MicroK8s (kubelet args file):

- Edit `/var/snap/microk8s/current/args/kubelet` and add:
  - `--allowed-unsafe-sysctls=net.ipv4.tcp_retries2`
- Restart MicroK8s as above.

## Configuration (environment variables)

- `SYSCTL_NAME` (default: `net.ipv4.tcp_retries2`)
- `SYSCTL_VALUE` (default: `5`)
- `TARGET_NAMESPACE` (optional): only mutate objects in this namespace. Leave unset to match all namespaces.
- `TARGET_LABELS` (optional): comma-separated key=value pairs to match labels
  `app.kubernetes.io/name=opens211,app.kubernetes.io/managed-by=juju`
- `REQUIRE_LABELS` (optional): comma-separated key=value pairs that must be present on the object
- `TARGET_CONTAINER_NAMES` (**required**): comma-separated container names to match (e.g. `opensearch`)
- `TARGET_IMAGE_SUBSTR` (**required**): only match containers whose image contains this substring
  (set this env var to an empty string to match any image, the variable must still be present)
- `REQUIRE_JUJU_MANAGED` (default: `true`): if true, only mutate objects with
  `app.kubernetes.io/managed-by=juju`

If `TARGET_NAMESPACE`/`TARGET_LABELS` are unset, the webhook will mutate all matching admission requests it receives subject to `TARGET_CONTAINER_NAMES` / `TARGET_IMAGE_SUBSTR` / `REQUIRE_*`.


- It only adds `securityContext` when it is missing.
- If `sysctls` already exists, it will append the sysctl if missing, or replace the value if present but different.

## Important

Sysctls are applied by the kubelet at pod creation time. If the webhook is only configured to mutate CREATE operations, it will not change sysctls for existing objects. It only applies to newly-created objects.

Which objects are actually mutated is determined by MutatingWebhookConfiguration rules.
See `workload.yaml` for the current rule set (resources + operations).

## Manifests

- `certs.yaml`: Namespace + cert-manager Issuer + Certificate that provisions the webhook TLS secret.
- `workload.yaml`: webhook Deployment/Service/MutatingWebhookConfiguration
