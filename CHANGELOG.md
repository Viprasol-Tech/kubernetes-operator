# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2025-06-06

### Added

- **Multi-resource reconciliation.** A `WebApp` now owns a **Service** and a
  **ConfigMap** alongside its **Deployment**. New action kinds
  (`create_service`, `update_service`, `create_configmap`, `update_configmap`)
  are emitted by the pure reconcile loop and applied by the kopf handlers.
- **Richer CRD spec.** New optional fields: `servicePort`, `env` (string map),
  `config` (rendered into the child ConfigMap), and `resources`
  (`requests`/`limits` for CPU and memory). Each is validated in
  `normalize_spec` with precise error messages.
- **Status conditions.** A new pure `compute_conditions(spec, observed)` derives
  Kubernetes-style `Ready` and `Progressing` conditions; the handler publishes
  them on the WebApp `.status.conditions` subresource.
- **Finalizer / deletion plan.** A new pure `deletion_plan(observed)` returns an
  ordered teardown plan (Service → Deployment → ConfigMap), skipping resources
  that do not exist. A `@kopf.on.delete` handler runs it.
- **New typed structures:** `ObservedService`, `ObservedConfigMap`,
  `ObservedState`, `ResourceRequirements`, `Condition`, and a `FINALIZER`
  constant — all exported from the package root.
- **More tests.** The pure-core suite grew from 21 to 58 tests covering env and
  resource validation, Service/ConfigMap reconciliation, ordering, status
  conditions, and the deletion plan.

### Changed

- `reconcile()` now accepts either a bare `ObservedDeployment` (legacy form) or
  an `ObservedState` bundling all three child resources. The returned actions
  are ordered ConfigMap → Deployment → Service.
- Deployment drift detection now also reconciles `env` and `resources`.
- `mypy --strict` now type-checks `handlers.py` in addition to the pure core.
- CRD, example manifest, and README updated for the new fields and behaviour.

## [0.1.0] - 2025-06-06

### Added

- Initial release: a Python/kopf Kubernetes operator for a `WebApp` custom
  resource with a pure, fully-tested reconcile loop managing a child Deployment
  (create, scale, image and port updates, no-op when in sync).
- WebApp CRD with an OpenAPI v3 schema, printer columns, and a status
  subresource.
- Deploy-ready manifests (CRD, operator Deployment, RBAC, example WebApp).

[0.2.0]: https://github.com/Viprasol-Tech/kubernetes-operator/releases/tag/v0.2.0
[0.1.0]: https://github.com/Viprasol-Tech/kubernetes-operator/releases/tag/v0.1.0
