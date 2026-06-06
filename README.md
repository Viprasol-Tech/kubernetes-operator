<div align="center">
  <img src="docs/assets/logo.png" alt="Viprasol Tech" width="120" />

  <h1>Kubernetes Operator</h1>

  <p><strong>A production-shaped Kubernetes operator scaffold in Python (kopf) — with a pure, fully-tested reconcile loop at its core.</strong></p>

  <p><em>Built and maintained by Viprasol Tech.</em></p>

  <p>
    <a href="https://github.com/Viprasol-Tech/kubernetes-operator/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT" /></a>
    <a href="https://github.com/Viprasol-Tech/kubernetes-operator"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+" /></a>
    <a href="https://github.com/Viprasol-Tech/kubernetes-operator"><img src="https://img.shields.io/badge/framework-kopf-326ce5.svg" alt="kopf" /></a>
    <a href="https://github.com/Viprasol-Tech/kubernetes-operator"><img src="https://img.shields.io/badge/lint-ruff-261230.svg" alt="ruff" /></a>
    <a href="https://github.com/Viprasol-Tech/kubernetes-operator"><img src="https://img.shields.io/badge/types-mypy-2a6db2.svg" alt="mypy" /></a>
  </p>
</div>

---

This repository implements a real Kubernetes operator for a custom resource called **`WebApp`**. A `WebApp` declares a container `image`, a `replicas` count, and a `port`; the operator creates and continuously reconciles a child `Deployment` to match.

The defining feature is its architecture: **all decision logic lives in a pure, side-effect-free, unit-tested module** (`src/k8s_operator/reconcile.py`). The kopf wiring (`handlers.py`) only translates the actions that the pure loop returns into Kubernetes API calls. This makes the hardest part of any operator — the reconcile logic — trivial to test offline, with zero cluster required.

## Features

- **Pure reconcile loop** — `reconcile(spec, observed) -> list[Action]` is deterministic, has no I/O, and is fully covered by `pytest` (scale up/down, scale-to-zero, image change, port change, combined changes, no-op when in sync, defaulting, and validation).
- **WebApp CRD** — a real `CustomResourceDefinition` with an OpenAPI v3 schema, defaults, validation bounds, printer columns, and a status subresource.
- **kopf handlers** — `create` / `update` / `resume` handlers that observe the child Deployment, run the pure loop, and apply the resulting actions as strategic-merge patches; new Deployments are adopted as owned children via `kopf.adopt`.
- **Deploy-ready manifests** — namespace, `ServiceAccount`, least-privilege `ClusterRole`/`ClusterRoleBinding`, and a hardened operator `Deployment`.
- **Strict tooling** — `ruff`, `mypy --strict` on the typed core, and `pytest` all green.

## Project layout

```
src/k8s_operator/
  reconcile.py   # PURE, tested core — no kopf/k8s imports
  handlers.py    # kopf wiring (imports kopf + kubernetes; optional runtime dep)
  __init__.py
tests/
  test_reconcile.py
manifests/
  crd.yaml             # the WebApp CustomResourceDefinition
  deployment.yaml      # operator Deployment + RBAC
  example-webapp.yaml  # a sample WebApp resource
```

## How the reconcile loop works

```python
from k8s_operator.reconcile import reconcile, ObservedDeployment

# Nothing exists yet -> create it
reconcile({"image": "nginx:1.27", "replicas": 3}, None)
# [Action(kind=CREATE, target_replicas=3, image='nginx:1.27', port=80)]

# Out of date image + replica count -> ordered patch actions
observed = ObservedDeployment(replicas=2, image="nginx:1.26", port=80)
reconcile({"image": "nginx:1.27", "replicas": 5}, observed)
# [Action(kind=UPDATE_IMAGE, image='nginx:1.27'), Action(kind=SCALE, target_replicas=5)]

# In sync -> a single NOOP
observed = ObservedDeployment(replicas=5, image="nginx:1.27", port=80)
reconcile({"image": "nginx:1.27", "replicas": 5}, observed)
# [Action(kind=NOOP, reason='deployment matches spec')]
```

## Quickstart (local development & tests)

```bash
# 1. Clone
git clone https://github.com/Viprasol-Tech/kubernetes-operator.git
cd kubernetes-operator

# 2. (optional) create a virtualenv, then install dev tooling
python -m pip install -e ".[dev]"

# 3. Run the verification gate
ruff check .
ruff format .
mypy src/k8s_operator/reconcile.py src/k8s_operator/__init__.py
PYTHONPATH=src python -m pytest -q
```

No Kubernetes cluster is needed to run the tests — that is the point of the pure-core design.

## Running the operator against a real cluster

You need a working `kubeconfig` (e.g. a local [kind](https://kind.sigs.k8s.io/) or [minikube](https://minikube.sigs.k8s.io/) cluster), then:

```bash
# 1. Install the CRD
kubectl apply -f manifests/crd.yaml

# 2a. Run the operator locally (uses your kubeconfig) for development
python -m pip install -e ".[operator]"
kopf run -m k8s_operator.handlers --all-namespaces --verbose

# 2b. ...OR deploy the operator into the cluster (RBAC + Deployment)
kubectl apply -f manifests/deployment.yaml

# 3. Create a WebApp and watch the operator reconcile a Deployment for it
kubectl apply -f manifests/example-webapp.yaml
kubectl get webapps
kubectl get deployments -l app.kubernetes.io/managed-by=viprasol-webapp-operator

# 4. Edit the spec and watch it converge
kubectl patch webapp hello --type merge -p '{"spec":{"replicas":5,"image":"nginx:1.27"}}'
kubectl get deploy hello-web -w
```

When deploying via `manifests/deployment.yaml`, build and push a container image first and update the `image:` field to point at it (the manifest references `ghcr.io/viprasol-tech/kubernetes-operator:0.1.0` as a placeholder).

## Contributing

Contributions are welcome. Please:

1. Fork the repo and create a feature branch.
2. Keep all decision logic in `reconcile.py` (pure, no I/O) and add `pytest` cases for it.
3. Ensure `ruff check .`, `mypy`, and `pytest` all pass before opening a PR.

## Disclaimer

This software is provided for educational and operational reference purposes. Review the RBAC, security context, and reconcile behaviour before running it against any production cluster. You are responsible for what you deploy.

## Contact — Viprasol Tech Private Limited

- Website: [viprasol.com](https://viprasol.com)
- Email: [support@viprasol.com](mailto:support@viprasol.com)
- Telegram: [t.me/viprasol_help](https://t.me/viprasol_help) | WhatsApp: +91 96336 52112
- GitHub: [@Viprasol-Tech](https://github.com/Viprasol-Tech) | [LinkedIn](https://www.linkedin.com/in/viprasol/) | X [@viprasol](https://twitter.com/viprasol)

## License

[MIT](LICENSE) (c) 2025 Viprasol Tech Private Limited
