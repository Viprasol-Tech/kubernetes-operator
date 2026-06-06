"""Kubernetes operator for the WebApp custom resource.

The pure, fully-tested reconcile loop lives in :mod:`k8s_operator.reconcile`.
The kopf event handlers that wire it to a live cluster live in
:mod:`k8s_operator.handlers` (which imports kopf, an optional runtime dependency).
"""

from k8s_operator.reconcile import (
    FINALIZER,
    Action,
    ActionKind,
    Condition,
    ObservedConfigMap,
    ObservedDeployment,
    ObservedService,
    ObservedState,
    ResourceRequirements,
    WebAppSpec,
    compute_conditions,
    deletion_plan,
    normalize_spec,
    reconcile,
)

__version__ = "0.2.0"

__all__ = [
    "Action",
    "ActionKind",
    "Condition",
    "FINALIZER",
    "ObservedConfigMap",
    "ObservedDeployment",
    "ObservedService",
    "ObservedState",
    "ResourceRequirements",
    "WebAppSpec",
    "compute_conditions",
    "deletion_plan",
    "normalize_spec",
    "reconcile",
    "__version__",
]
