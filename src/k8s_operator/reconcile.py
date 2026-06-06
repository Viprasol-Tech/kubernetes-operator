"""Pure reconciliation logic for the WebApp custom resource.

This module is intentionally free of any Kubernetes client or kopf imports so
that it can be unit-tested deterministically and offline. The kopf wiring in
``handlers.py`` calls :func:`reconcile` and translates the returned actions into
real API calls against the cluster.

The reconcile loop is a pure function: given the *desired* state (the WebApp
``spec``) and the *observed* state of the managed child Deployment, it returns
an ordered list of :class:`Action` objects describing what should happen to
bring the cluster in line with the spec. The function never mutates its inputs
and never performs side effects.
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


class ActionKind(str, Enum):
    """The kind of action the operator should take for a child Deployment."""

    CREATE = "create"
    SCALE = "scale"
    UPDATE_IMAGE = "update_image"
    UPDATE_PORT = "update_port"
    NOOP = "noop"


@dataclass(frozen=True)
class Action:
    """A single declarative action returned by :func:`reconcile`.

    Attributes:
        kind: What category of change is required.
        reason: Human-readable explanation, useful for events/logging.
        target_replicas: Desired replica count (set for CREATE/SCALE).
        image: Desired container image (set for CREATE/UPDATE_IMAGE).
        port: Desired container port (set for CREATE/UPDATE_PORT).
    """

    kind: ActionKind
    reason: str
    target_replicas: int | None = None
    image: str | None = None
    port: int | None = None


@dataclass(frozen=True)
class WebAppSpec:
    """Normalized, validated view of a WebApp ``spec``."""

    image: str
    replicas: int = DEFAULT_REPLICAS
    port: int = DEFAULT_PORT


@dataclass(frozen=True)
class ObservedDeployment:
    """The observed state of the child Deployment managed by a WebApp.

    ``None`` for the whole object (passed to :func:`reconcile` as ``None``)
    means the Deployment does not exist yet.
    """

    replicas: int
    image: str
    port: int = DEFAULT_PORT
    extra: dict[str, Any] = field(default_factory=dict)


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

    port = spec.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool):
        raise ValueError("spec.port must be an integer")
    if not (1 <= port <= 65535):
        raise ValueError("spec.port must be between 1 and 65535")

    return WebAppSpec(image=image, replicas=replicas, port=port)


def reconcile(
    spec: dict[str, Any],
    observed: ObservedDeployment | None,
) -> list[Action]:
    """Compute the actions needed to drive ``observed`` toward ``spec``.

    This is a pure function with no side effects. The returned list is ordered:
    a CREATE (when nothing exists) is the only action; otherwise scale and
    update actions are returned together so the caller can apply them in one
    patch. When everything is in sync a single :attr:`ActionKind.NOOP` action
    is returned.

    Args:
        spec: The raw WebApp ``.spec`` mapping.
        observed: The observed child Deployment, or ``None`` if it does not
            exist yet.

    Returns:
        An ordered list of :class:`Action` objects. Never empty.
    """
    desired = normalize_spec(spec)

    if observed is None:
        return [
            Action(
                kind=ActionKind.CREATE,
                reason="child Deployment does not exist",
                target_replicas=desired.replicas,
                image=desired.image,
                port=desired.port,
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

    if observed.replicas != desired.replicas:
        actions.append(
            Action(
                kind=ActionKind.SCALE,
                reason=f"replicas {observed.replicas} -> {desired.replicas}",
                target_replicas=desired.replicas,
            )
        )

    if not actions:
        return [Action(kind=ActionKind.NOOP, reason="deployment matches spec")]

    return actions
