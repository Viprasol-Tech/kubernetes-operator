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
    FINALIZER,
    Action,
    ActionKind,
    Condition,
    ObservedConfigMap,
    ObservedDeployment,
    ObservedService,
    ObservedState,
    ResourceRequirements,
    compute_conditions,
    deletion_plan,
    normalize_spec,
    reconcile,
)

GROUP = "apps.viprasol.com"
VERSION = "v1"
PLURAL = "webapps"

# Label used to mark resources owned/managed by a WebApp instance.
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "viprasol-webapp-operator"


def _deployment_name(name: str) -> str:
    return f"{name}-web"


def _service_name(name: str) -> str:
    return f"{name}-web"


def _configmap_name(name: str) -> str:
    return f"{name}-config"


def _labels(dep_name: str) -> dict[str, str]:
    return {"app": dep_name, MANAGED_BY_LABEL: MANAGED_BY_VALUE}


def _env_list(env: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": k, "value": v} for k, v in sorted(env.items())]


def _build_deployment(name: str, namespace: str, spec_dict: dict[str, Any]) -> dict[str, Any]:
    """Render a Deployment manifest (as a dict) for a WebApp spec."""
    spec = normalize_spec(spec_dict)
    dep_name = _deployment_name(name)
    labels = _labels(dep_name)
    container: dict[str, Any] = {
        "name": "web",
        "image": spec.image,
        "ports": [{"containerPort": spec.port}],
    }
    if spec.env:
        container["env"] = _env_list(spec.env)
    if not spec.resources.is_empty():
        container["resources"] = spec.resources.to_k8s()
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": dep_name, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": spec.replicas,
            "selector": {"matchLabels": {"app": dep_name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {"containers": [container]},
            },
        },
    }


def _build_service(name: str, namespace: str, spec_dict: dict[str, Any]) -> dict[str, Any]:
    """Render a Service manifest (as a dict) for a WebApp spec."""
    spec = normalize_spec(spec_dict)
    dep_name = _deployment_name(name)
    svc_name = _service_name(name)
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": svc_name, "namespace": namespace, "labels": _labels(dep_name)},
        "spec": {
            "selector": {"app": dep_name},
            "ports": [{"port": spec.service_port, "targetPort": spec.port}],
        },
    }


def _build_configmap(name: str, namespace: str, spec_dict: dict[str, Any]) -> dict[str, Any]:
    """Render a ConfigMap manifest (as a dict) for a WebApp spec."""
    spec = normalize_spec(spec_dict)
    dep_name = _deployment_name(name)
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": _configmap_name(name),
            "namespace": namespace,
            "labels": _labels(dep_name),
        },
        "data": dict(spec.config),
    }


def _observe_deployment(
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
    env = {e.name: (e.value or "") for e in (container.env or [])}
    resources = _observe_resources(container.resources)
    status = dep.status
    ready = status.ready_replicas if status and status.ready_replicas else 0
    return ObservedDeployment(
        replicas=dep.spec.replicas or 0,
        image=container.image,
        port=port,
        env=env,
        resources=resources,
        ready_replicas=ready,
    )


def _observe_resources(raw: Any) -> ResourceRequirements:
    if raw is None:
        return ResourceRequirements()
    requests = raw.requests or {}
    limits = raw.limits or {}
    return ResourceRequirements(
        cpu_request=requests.get("cpu"),
        memory_request=requests.get("memory"),
        cpu_limit=limits.get("cpu"),
        memory_limit=limits.get("memory"),
    )


def _observe_service(
    core_api: kubernetes.client.CoreV1Api, name: str, namespace: str
) -> ObservedService | None:
    svc_name = _service_name(name)
    try:
        svc = core_api.read_namespaced_service(svc_name, namespace)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            return None
        raise
    ports = svc.spec.ports or []
    if not ports:
        return ObservedService()
    first = ports[0]
    target = first.target_port if isinstance(first.target_port, int) else first.port
    return ObservedService(port=first.port, target_port=target)


def _observe_configmap(
    core_api: kubernetes.client.CoreV1Api, name: str, namespace: str
) -> ObservedConfigMap | None:
    cm_name = _configmap_name(name)
    try:
        cm = core_api.read_namespaced_config_map(cm_name, namespace)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404:
            return None
        raise
    return ObservedConfigMap(data=dict(cm.data or {}))


def _observe(
    apps_api: kubernetes.client.AppsV1Api,
    core_api: kubernetes.client.CoreV1Api,
    name: str,
    namespace: str,
) -> ObservedState:
    """Read all child resources into an :class:`ObservedState`."""
    return ObservedState(
        deployment=_observe_deployment(apps_api, name, namespace),
        service=_observe_service(core_api, name, namespace),
        configmap=_observe_configmap(core_api, name, namespace),
    )


def _patch_for(action: Action) -> dict[str, Any]:
    """Build a strategic-merge patch body for a single Deployment action."""
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
    if action.kind is ActionKind.UPDATE_ENV:
        return {
            "spec": {
                "template": {
                    "spec": {"containers": [{"name": "web", "env": _env_list(action.env or {})}]}
                }
            }
        }
    if action.kind is ActionKind.UPDATE_RESOURCES:
        resources = action.resources or ResourceRequirements()
        return {
            "spec": {
                "template": {
                    "spec": {"containers": [{"name": "web", "resources": resources.to_k8s()}]}
                }
            }
        }
    raise ValueError(f"no patch for action kind {action.kind}")


def _apply(
    apps_api: kubernetes.client.AppsV1Api,
    core_api: kubernetes.client.CoreV1Api,
    name: str,
    namespace: str,
    spec_dict: dict[str, Any],
    actions: list[Action],
    logger: Any,
) -> None:
    """Translate reconcile actions into Kubernetes API calls."""
    dep_name = _deployment_name(name)
    svc_name = _service_name(name)
    cm_name = _configmap_name(name)
    for action in actions:
        if action.kind is ActionKind.NOOP:
            logger.info("WebApp %s/%s in sync: %s", namespace, name, action.reason)
        elif action.kind is ActionKind.CREATE:
            body = _build_deployment(name, namespace, spec_dict)
            kopf.adopt(body)
            apps_api.create_namespaced_deployment(namespace, body)
            logger.info("Created Deployment %s/%s (%s)", namespace, dep_name, action.reason)
        elif action.kind is ActionKind.CREATE_SERVICE:
            body = _build_service(name, namespace, spec_dict)
            kopf.adopt(body)
            core_api.create_namespaced_service(namespace, body)
            logger.info("Created Service %s/%s (%s)", namespace, svc_name, action.reason)
        elif action.kind is ActionKind.UPDATE_SERVICE:
            body = _build_service(name, namespace, spec_dict)
            core_api.patch_namespaced_service(svc_name, namespace, body)
            logger.info("Patched Service %s/%s (%s)", namespace, svc_name, action.reason)
        elif action.kind is ActionKind.CREATE_CONFIGMAP:
            body = _build_configmap(name, namespace, spec_dict)
            kopf.adopt(body)
            core_api.create_namespaced_config_map(namespace, body)
            logger.info("Created ConfigMap %s/%s (%s)", namespace, cm_name, action.reason)
        elif action.kind is ActionKind.UPDATE_CONFIGMAP:
            body = _build_configmap(name, namespace, spec_dict)
            core_api.patch_namespaced_config_map(cm_name, namespace, body)
            logger.info("Patched ConfigMap %s/%s (%s)", namespace, cm_name, action.reason)
        else:
            patch = _patch_for(action)
            apps_api.patch_namespaced_deployment(dep_name, namespace, patch)
            logger.info("Patched Deployment %s/%s (%s)", namespace, dep_name, action.reason)


def _conditions_to_status(conditions: list[Condition]) -> list[dict[str, str]]:
    return [
        {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
        for c in conditions
    ]


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
def reconcile_webapp(
    spec: dict[str, Any],
    name: str,
    namespace: str,
    patch: Any,
    logger: Any,
    **_: Any,
) -> dict[str, Any]:
    """Reconcile a WebApp resource against its child resources."""
    apps_api = kubernetes.client.AppsV1Api()
    core_api = kubernetes.client.CoreV1Api()
    observed = _observe(apps_api, core_api, name, namespace)
    actions = reconcile(dict(spec), observed)
    _apply(apps_api, core_api, name, namespace, dict(spec), actions, logger)

    # Re-observe and publish status conditions.
    observed_after = _observe(apps_api, core_api, name, namespace)
    conditions = compute_conditions(dict(spec), observed_after)
    patch.status["conditions"] = _conditions_to_status(conditions)
    return {"actions": [a.kind.value for a in actions]}


@kopf.on.delete(GROUP, VERSION, PLURAL)
def delete_webapp(
    name: str,
    namespace: str,
    logger: Any,
    **_: Any,
) -> None:
    """Finalizer: tear down owned children in a deterministic order.

    Owner references already cascade-delete the children, but running an
    explicit, ordered plan keeps logs clear and lets the operator clean up
    resources it may have created outside the owner graph.
    """
    apps_api = kubernetes.client.AppsV1Api()
    core_api = kubernetes.client.CoreV1Api()
    observed = _observe(apps_api, core_api, name, namespace)
    for action in deletion_plan(observed):
        if action.kind is ActionKind.DELETE_SERVICE:
            core_api.delete_namespaced_service(_service_name(name), namespace)
        elif action.kind is ActionKind.DELETE_DEPLOYMENT:
            apps_api.delete_namespaced_deployment(_deployment_name(name), namespace)
        elif action.kind is ActionKind.DELETE_CONFIGMAP:
            core_api.delete_namespaced_config_map(_configmap_name(name), namespace)
        logger.info("WebApp %s/%s teardown: %s", namespace, name, action.reason)


__all__ = ["FINALIZER", "reconcile_webapp", "delete_webapp"]
