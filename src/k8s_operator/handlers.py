# mypy: disable-error-code="untyped-decorator"
"""kopf event handlers that wire the pure reconcile loop to a live cluster.

This module imports :mod:`kopf` and the Kubernetes Python client, both of which
are optional *runtime* dependencies (install with ``pip install '.[operator]'``).
It is deliberately NOT imported by the test suite — the testable logic lives in
:mod:`k8s_operator.reconcile`.

Run it with::

    kopf run -m k8s_operator.handlers --verbose
"""

from __future__ import annotations

from typing import Any

import kopf
import kubernetes

from k8s_operator.reconcile import (
    Action,
    ActionKind,
    ObservedDeployment,
    normalize_spec,
    reconcile,
)

GROUP = "apps.viprasol.com"
VERSION = "v1"
PLURAL = "webapps"

# Label used to mark Deployments owned/managed by a WebApp instance.
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "viprasol-webapp-operator"


def _deployment_name(name: str) -> str:
    return f"{name}-web"


def _build_deployment(name: str, namespace: str, spec_dict: dict[str, Any]) -> dict[str, Any]:
    """Render a Deployment manifest (as a dict) for a WebApp spec."""
    spec = normalize_spec(spec_dict)
    dep_name = _deployment_name(name)
    labels = {
        "app": dep_name,
        MANAGED_BY_LABEL: MANAGED_BY_VALUE,
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": dep_name, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": spec.replicas,
            "selector": {"matchLabels": {"app": dep_name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        {
                            "name": "web",
                            "image": spec.image,
                            "ports": [{"containerPort": spec.port}],
                        }
                    ]
                },
            },
        },
    }


def _observe(
    apps_api: kubernetes.client.AppsV1Api, name: str, namespace: str
) -> ObservedDeployment | None:
    """Read the current child Deployment, returning ``None`` if absent."""
    dep_name = _deployment_name(name)
    try:
        dep = apps_api.read_namespaced_deployment(dep_name, namespace)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            return None
        raise

    container = dep.spec.template.spec.containers[0]
    ports = container.ports or []
    port = ports[0].container_port if ports else 80
    return ObservedDeployment(
        replicas=dep.spec.replicas or 0,
        image=container.image,
        port=port,
    )


def _apply(
    apps_api: kubernetes.client.AppsV1Api,
    name: str,
    namespace: str,
    spec_dict: dict[str, Any],
    actions: list[Action],
    logger: Any,
) -> None:
    """Translate reconcile actions into Kubernetes API calls."""
    dep_name = _deployment_name(name)
    for action in actions:
        if action.kind is ActionKind.NOOP:
            logger.info("WebApp %s/%s in sync: %s", namespace, name, action.reason)
            continue

        if action.kind is ActionKind.CREATE:
            body = _build_deployment(name, namespace, spec_dict)
            kopf.adopt(body)
            apps_api.create_namespaced_deployment(namespace, body)
            logger.info("Created Deployment %s/%s (%s)", namespace, dep_name, action.reason)
            continue

        # SCALE / UPDATE_IMAGE / UPDATE_PORT are all patches to the same object.
        patch = _patch_for(action)
        apps_api.patch_namespaced_deployment(dep_name, namespace, patch)
        logger.info("Patched Deployment %s/%s (%s)", namespace, dep_name, action.reason)


def _patch_for(action: Action) -> dict[str, Any]:
    """Build a strategic-merge patch body for a single action."""
    if action.kind is ActionKind.SCALE:
        return {"spec": {"replicas": action.target_replicas}}
    if action.kind is ActionKind.UPDATE_IMAGE:
        return {
            "spec": {"template": {"spec": {"containers": [{"name": "web", "image": action.image}]}}}
        }
    if action.kind is ActionKind.UPDATE_PORT:
        return {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "web", "ports": [{"containerPort": action.port}]}]
                    }
                }
            }
        }
    raise ValueError(f"no patch for action kind {action.kind}")


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
def reconcile_webapp(
    spec: dict[str, Any],
    name: str,
    namespace: str,
    logger: Any,
    **_: Any,
) -> dict[str, Any]:
    """Reconcile a WebApp resource against its child Deployment."""
    apps_api = kubernetes.client.AppsV1Api()
    observed = _observe(apps_api, name, namespace)
    actions = reconcile(dict(spec), observed)
    _apply(apps_api, name, namespace, dict(spec), actions, logger)
    return {"actions": [a.kind.value for a in actions]}
