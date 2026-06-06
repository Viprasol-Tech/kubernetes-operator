"""Kubernetes operator for the WebApp custom resource.

The pure, fully-tested reconcile loop lives in :mod:`k8s_operator.reconcile`.
The kopf event handlers that wire it to a live cluster live in
:mod:`k8s_operator.handlers` (which imports kopf, an optional runtime dependency).
"""

from k8s_operator.reconcile import (
    Action,
    ActionKind,
    ObservedDeployment,
    WebAppSpec,
    normalize_spec,
    reconcile,
)

__version__ = "0.1.0"

__all__ = [
    "Action",
    "ActionKind",
    "ObservedDeployment",
    "WebAppSpec",
    "normalize_spec",
    "reconcile",
    "__version__",
]
