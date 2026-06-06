"""Pure reconciliation logic for the WebApp custom resource.

This module is intentionally free of any Kubernetes client or kopf imports so
that it can be unit-tested deterministically and offline. The kopf wiring in
``handlers.py`` calls :func:`reconcile` and translates the returned actions into
real API calls against the cluster.

The reconcile loop is a pure function: given the *desired* state (the WebApp
``spec``) and the *observed* state of the managed child resources, it returns an
ordered list of :class:`Action` objects describing what should happen to bring
the cluster in line with the spec. The function never mutates its inputs and
never performs side effects.

A WebApp owns three child resources:

* a **Deployment** (the running pods),
* a **Service** (a stable in-cluster endpoint), and
* a **ConfigMap** (key/value configuration mounted/injected into the pods).

Alongside the reconcile loop this module also exposes two more pure helpers:

* :func:`compute_conditions` — derive Kubernetes-style status conditions, and
* :func:`deletion_plan` — the ordered teardown plan run by the finalizer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Defaults for the WebApp spec
# ---------------------------------------------------------------------------

DEFAULT_REPLICAS = 1
DEFAULT_PORT = 80
DEFAULT_SERVICE_PORT = 80

#: Finalizer string added to a WebApp so deletion runs through the operator.
FINALIZER = "apps.viprasol.com/webapp-cleanup"

#: Recognised Kubernetes resource quantity suffixes for CPU and memory.
_CPU_SUFFIXES = ("m",)
_MEM_SUFFIXES = ("Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "K", "M", "G", "T", "P", "E", "k")


class ActionKind(str, Enum):
    """The kind of action the operator should take for a child resource."""

    # Deployment
    CREATE = "create"
    SCALE = "scale"
    UPDATE_IMAGE = "update_image"
    UPDATE_PORT = "update_port"
    UPDATE_ENV = "update_env"
    UPDATE_RESOURCES = "update_resources"
    # Service
    CREATE_SERVICE = "create_service"
    UPDATE_SERVICE = "update_service"
    # ConfigMap
    CREATE_CONFIGMAP = "create_configmap"
    UPDATE_CONFIGMAP = "update_configmap"
    # Deletion (used by the finalizer plan)
    DELETE_DEPLOYMENT = "delete_deployment"
    DELETE_SERVICE = "delete_service"
    DELETE_CONFIGMAP = "delete_configmap"
    # Nothing to do
    NOOP = "noop"


#: Action kinds that operate on the child Deployment.
DEPLOYMENT_ACTION_KINDS = frozenset(
    {
        ActionKind.CREATE,
        ActionKind.SCALE,
        ActionKind.UPDATE_IMAGE,
        ActionKind.UPDATE_PORT,
        ActionKind.UPDATE_ENV,
        ActionKind.UPDATE_RESOURCES,
    }
)


@dataclass(frozen=True)
class ResourceRequirements:
    """CPU/memory requests and limits for the web container.

    All four fields are optional Kubernetes quantity strings (e.g. ``"100m"``,
    ``"128Mi"``). ``None`` means "unset" — the field is omitted from the pod
    spec and never compared during reconciliation.
    """

    cpu_request: str | None = None
    memory_request: str | None = None
    cpu_limit: str | None = None
    memory_limit: str | None = None

    def is_empty(self) -> bool:
        """Return ``True`` when no request or limit is set."""
        return not any((self.cpu_request, self.memory_request, self.cpu_limit, self.memory_limit))

    def to_k8s(self) -> dict[str, dict[str, str]]:
        """Render as a Kubernetes ``resources`` block (omitting unset fields)."""
        requests: dict[str, str] = {}
        limits: dict[str, str] = {}
        if self.cpu_request is not None:
            requests["cpu"] = self.cpu_request
        if self.memory_request is not None:
            requests["memory"] = self.memory_request
        if self.cpu_limit is not None:
            limits["cpu"] = self.cpu_limit
        if self.memory_limit is not None:
            limits["memory"] = self.memory_limit
        block: dict[str, dict[str, str]] = {}
        if requests:
            block["requests"] = requests
        if limits:
            block["limits"] = limits
        return block


@dataclass(frozen=True)
class Action:
    """A single declarative action returned by :func:`reconcile`.

    Attributes:
        kind: What category of change is required.
        reason: Human-readable explanation, useful for events/logging.
        target_replicas: Desired replica count (set for CREATE/SCALE).
        image: Desired container image (set for CREATE/UPDATE_IMAGE).
        port: Desired container port (set for CREATE/UPDATE_PORT).
        env: Desired environment variables (set for CREATE/UPDATE_ENV).
        resources: Desired resource requirements (set for CREATE/UPDATE_RESOURCES).
        service_port: Desired Service port (set for service actions).
        config: Desired ConfigMap data (set for configmap actions).
    """

    kind: ActionKind
    reason: str
    target_replicas: int | None = None
    image: str | None = None
    port: int | None = None
    env: dict[str, str] | None = None
    resources: ResourceRequirements | None = None
    service_port: int | None = None
    config: dict[str, str] | None = None


@dataclass(frozen=True)
class WebAppSpec:
    """Normalized, validated view of a WebApp ``spec``."""

    image: str
    replicas: int = DEFAULT_REPLICAS
    port: int = DEFAULT_PORT
    service_port: int = DEFAULT_SERVICE_PORT
    env: dict[str, str] = field(default_factory=dict)
    resources: ResourceRequirements = field(default_factory=ResourceRequirements)
    config: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ObservedDeployment:
    """The observed state of the child Deployment managed by a WebApp.

    Passing ``None`` for the whole object to :func:`reconcile` means the
    Deployment does not exist yet.
    """

    replicas: int
    image: str
    port: int = DEFAULT_PORT
    env: dict[str, str] = field(default_factory=dict)
    resources: ResourceRequirements = field(default_factory=ResourceRequirements)
    ready_replicas: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ObservedService:
    """The observed state of the child Service managed by a WebApp."""

    port: int = DEFAULT_SERVICE_PORT
    target_port: int = DEFAULT_PORT


@dataclass(frozen=True)
class ObservedConfigMap:
    """The observed state of the child ConfigMap managed by a WebApp."""

    data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ObservedState:
    """Bundle of all observed child resources for a WebApp.

    Any field may be ``None`` to indicate that the corresponding child does not
    exist yet.
    """

    deployment: ObservedDeployment | None = None
    service: ObservedService | None = None
    configmap: ObservedConfigMap | None = None


@dataclass(frozen=True)
class Condition:
    """A Kubernetes-style status condition.

    Attributes:
        type: Condition type, e.g. ``"Ready"`` or ``"Progressing"``.
        status: ``"True"``, ``"False"`` or ``"Unknown"``.
        reason: Machine-readable CamelCase reason.
        message: Human-readable detail.
    """

    type: str
    status: str
    reason: str
    message: str


# ---------------------------------------------------------------------------
# Validation / normalization
# ---------------------------------------------------------------------------


def _validate_port(value: Any, field_name: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"spec.{field_name} must be an integer")
    if not (1 <= value <= 65535):
        raise ValueError(f"spec.{field_name} must be between 1 and 65535")
    return value


def _normalize_env(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("spec.env must be a mapping of string to string")
    env: dict[str, str] = {}
    for key, val in raw.items():
        if not isinstance(key, str) or not key:
            raise ValueError("spec.env keys must be non-empty strings")
        if not isinstance(val, str):
            raise ValueError(f"spec.env[{key!r}] must be a string")
        env[key] = val
    return env


def _normalize_config(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("spec.config must be a mapping of string to string")
    config: dict[str, str] = {}
    for key, val in raw.items():
        if not isinstance(key, str) or not key:
            raise ValueError("spec.config keys must be non-empty strings")
        if not isinstance(val, str):
            raise ValueError(f"spec.config[{key!r}] must be a string")
        config[key] = val
    return config


def _validate_quantity(value: Any, field_name: str, suffixes: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"spec.resources.{field_name} must be a non-empty string")
    body = value
    for suffix in sorted(suffixes, key=len, reverse=True):
        if body.endswith(suffix):
            body = body[: -len(suffix)]
            break
    try:
        number = float(body)
    except ValueError as exc:
        raise ValueError(f"spec.resources.{field_name} {value!r} is not a valid quantity") from exc
    if number < 0:
        raise ValueError(f"spec.resources.{field_name} must be non-negative")
    return value


def _normalize_resources(raw: Any) -> ResourceRequirements:
    if raw is None:
        return ResourceRequirements()
    if not isinstance(raw, dict):
        raise ValueError("spec.resources must be a mapping")
    requests = raw.get("requests") or {}
    limits = raw.get("limits") or {}
    if not isinstance(requests, dict) or not isinstance(limits, dict):
        raise ValueError("spec.resources.requests/limits must be mappings")
    return ResourceRequirements(
        cpu_request=_validate_quantity(requests.get("cpu"), "requests.cpu", _CPU_SUFFIXES),
        memory_request=_validate_quantity(requests.get("memory"), "requests.memory", _MEM_SUFFIXES),
        cpu_limit=_validate_quantity(limits.get("cpu"), "limits.cpu", _CPU_SUFFIXES),
        memory_limit=_validate_quantity(limits.get("memory"), "limits.memory", _MEM_SUFFIXES),
    )


def normalize_spec(spec: dict[str, Any]) -> WebAppSpec:
    """Validate a raw WebApp ``spec`` dict and apply defaults.

    Args:
        spec: The ``.spec`` mapping from a WebApp custom resource.

    Returns:
        A :class:`WebAppSpec` with defaults filled in.

    Raises:
        ValueError: If required fields are missing or values are invalid.
    """
    if not isinstance(spec, dict):
        raise ValueError("spec must be a mapping")

    image = spec.get("image")
    if not image or not isinstance(image, str):
        raise ValueError("spec.image is required and must be a non-empty string")

    replicas = spec.get("replicas", DEFAULT_REPLICAS)
    if not isinstance(replicas, int) or isinstance(replicas, bool):
        raise ValueError("spec.replicas must be an integer")
    if replicas < 0:
        raise ValueError("spec.replicas must be >= 0")

    port = _validate_port(spec.get("port"), "port", DEFAULT_PORT)
    service_port = _validate_port(spec.get("servicePort"), "servicePort", DEFAULT_SERVICE_PORT)
    env = _normalize_env(spec.get("env"))
    resources = _normalize_resources(spec.get("resources"))
    config = _normalize_config(spec.get("config"))

    return WebAppSpec(
        image=image,
        replicas=replicas,
        port=port,
        service_port=service_port,
        env=env,
        resources=resources,
        config=config,
    )


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


def _reconcile_deployment(desired: WebAppSpec, observed: ObservedDeployment | None) -> list[Action]:
    if observed is None:
        return [
            Action(
                kind=ActionKind.CREATE,
                reason="child Deployment does not exist",
                target_replicas=desired.replicas,
                image=desired.image,
                port=desired.port,
                env=dict(desired.env),
                resources=desired.resources,
            )
        ]

    actions: list[Action] = []
    if observed.image != desired.image:
        actions.append(
            Action(
                kind=ActionKind.UPDATE_IMAGE,
                reason=f"image {observed.image!r} -> {desired.image!r}",
                image=desired.image,
            )
        )
    if observed.port != desired.port:
        actions.append(
            Action(
                kind=ActionKind.UPDATE_PORT,
                reason=f"port {observed.port} -> {desired.port}",
                port=desired.port,
            )
        )
    if observed.env != desired.env:
        actions.append(
            Action(
                kind=ActionKind.UPDATE_ENV,
                reason="environment variables drifted from spec",
                env=dict(desired.env),
            )
        )
    if observed.resources != desired.resources:
        actions.append(
            Action(
                kind=ActionKind.UPDATE_RESOURCES,
                reason="resource requests/limits drifted from spec",
                resources=desired.resources,
            )
        )
    if observed.replicas != desired.replicas:
        actions.append(
            Action(
                kind=ActionKind.SCALE,
                reason=f"replicas {observed.replicas} -> {desired.replicas}",
                target_replicas=desired.replicas,
            )
        )
    return actions


def _reconcile_service(desired: WebAppSpec, observed: ObservedService | None) -> list[Action]:
    if observed is None:
        return [
            Action(
                kind=ActionKind.CREATE_SERVICE,
                reason="child Service does not exist",
                service_port=desired.service_port,
                port=desired.port,
            )
        ]
    if observed.port != desired.service_port or observed.target_port != desired.port:
        return [
            Action(
                kind=ActionKind.UPDATE_SERVICE,
                reason=(
                    f"service port {observed.port}->{desired.service_port} "
                    f"target {observed.target_port}->{desired.port}"
                ),
                service_port=desired.service_port,
                port=desired.port,
            )
        ]
    return []


def _reconcile_configmap(desired: WebAppSpec, observed: ObservedConfigMap | None) -> list[Action]:
    # No config requested and none exists: nothing to manage.
    if not desired.config and observed is None:
        return []
    if observed is None:
        return [
            Action(
                kind=ActionKind.CREATE_CONFIGMAP,
                reason="child ConfigMap does not exist",
                config=dict(desired.config),
            )
        ]
    if observed.data != desired.config:
        return [
            Action(
                kind=ActionKind.UPDATE_CONFIGMAP,
                reason="config data drifted from spec",
                config=dict(desired.config),
            )
        ]
    return []


def reconcile(
    spec: dict[str, Any],
    observed: ObservedDeployment | ObservedState | None,
) -> list[Action]:
    """Compute the actions needed to drive ``observed`` toward ``spec``.

    This is a pure function with no side effects. The returned list is ordered
    so the caller can apply it directly: ConfigMap actions first (so pods read
    fresh config), then Deployment actions, then Service actions. When
    everything is in sync a single :attr:`ActionKind.NOOP` action is returned.

    For backwards compatibility, ``observed`` may be a bare
    :class:`ObservedDeployment` (or ``None``) — in that case only the Deployment
    is reconciled and the Service/ConfigMap are treated as managed only when the
    spec requests them. Pass an :class:`ObservedState` to reconcile all three
    child resources together.

    Args:
        spec: The raw WebApp ``.spec`` mapping.
        observed: The observed child state. ``None`` means nothing exists yet.

    Returns:
        An ordered list of :class:`Action` objects. Never empty.
    """
    desired = normalize_spec(spec)

    if isinstance(observed, ObservedState):
        state = observed
    elif observed is None:
        state = ObservedState()
    else:
        state = ObservedState(deployment=observed)

    actions: list[Action] = []
    actions.extend(_reconcile_configmap(desired, state.configmap))
    actions.extend(_reconcile_deployment(desired, state.deployment))
    actions.extend(_reconcile_service(desired, state.service))

    if not actions:
        return [Action(kind=ActionKind.NOOP, reason="all child resources match spec")]
    return actions


# ---------------------------------------------------------------------------
# Status conditions
# ---------------------------------------------------------------------------


def compute_conditions(
    spec: dict[str, Any],
    observed: ObservedState,
) -> list[Condition]:
    """Derive ``Ready`` and ``Progressing`` conditions for a WebApp.

    Pure and side-effect free. ``Ready`` is ``True`` only when the Deployment
    exists and every desired replica is ready; ``Progressing`` is ``True``
    whenever the observed state has not yet converged to the spec.

    Args:
        spec: The raw WebApp ``.spec`` mapping.
        observed: The observed child state.

    Returns:
        A list of :class:`Condition` objects (``Ready`` then ``Progressing``).
    """
    desired = normalize_spec(spec)
    dep = observed.deployment

    if dep is None:
        ready = Condition(
            type="Ready",
            status="False",
            reason="DeploymentMissing",
            message="child Deployment has not been created yet",
        )
        progressing = Condition(
            type="Progressing",
            status="True",
            reason="Creating",
            message="creating child resources",
        )
        return [ready, progressing]

    converged = reconcile(spec, observed)[0].kind is ActionKind.NOOP
    fully_ready = dep.ready_replicas >= desired.replicas

    if converged and fully_ready:
        ready = Condition(
            type="Ready",
            status="True",
            reason="AllReplicasReady",
            message=f"{dep.ready_replicas}/{desired.replicas} replicas ready",
        )
        progressing = Condition(
            type="Progressing",
            status="False",
            reason="Reconciled",
            message="observed state matches spec",
        )
    else:
        ready = Condition(
            type="Ready",
            status="False",
            reason="NotReady" if not fully_ready else "Reconciling",
            message=f"{dep.ready_replicas}/{desired.replicas} replicas ready",
        )
        progressing = Condition(
            type="Progressing",
            status="True",
            reason="Reconciling",
            message="driving observed state toward spec",
        )
    return [ready, progressing]


# ---------------------------------------------------------------------------
# Finalizer / deletion plan
# ---------------------------------------------------------------------------


def deletion_plan(observed: ObservedState) -> list[Action]:
    """Return the ordered teardown plan for a WebApp being deleted.

    Pure and side-effect free. Children are removed in the reverse of creation
    order — Service, then Deployment, then ConfigMap — and only resources that
    actually exist are included. When nothing exists, a single
    :attr:`ActionKind.NOOP` action is returned so callers always have something
    to log before removing the finalizer.

    Args:
        observed: The observed child state at deletion time.

    Returns:
        An ordered, non-empty list of delete :class:`Action` objects.
    """
    actions: list[Action] = []
    if observed.service is not None:
        actions.append(Action(kind=ActionKind.DELETE_SERVICE, reason="removing child Service"))
    if observed.deployment is not None:
        actions.append(
            Action(kind=ActionKind.DELETE_DEPLOYMENT, reason="removing child Deployment")
        )
    if observed.configmap is not None:
        actions.append(Action(kind=ActionKind.DELETE_CONFIGMAP, reason="removing child ConfigMap"))
    if not actions:
        return [Action(kind=ActionKind.NOOP, reason="no child resources to delete")]
    return actions
