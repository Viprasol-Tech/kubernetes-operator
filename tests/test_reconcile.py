"""Unit tests for the pure reconcile loop.

These tests do not import kopf or the Kubernetes client — they exercise the
deterministic core directly with hand-built cases.
"""

from __future__ import annotations

import pytest

from k8s_operator.reconcile import (
    DEFAULT_PORT,
    DEFAULT_REPLICAS,
    DEFAULT_SERVICE_PORT,
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


def _kinds(actions: list[Action]) -> list[ActionKind]:
    return [a.kind for a in actions]


# --- normalize_spec --------------------------------------------------------


def test_normalize_applies_defaults() -> None:
    spec = normalize_spec({"image": "nginx:1.27"})
    assert spec.image == "nginx:1.27"
    assert spec.replicas == DEFAULT_REPLICAS
    assert spec.port == DEFAULT_PORT
    assert spec.service_port == DEFAULT_SERVICE_PORT
    assert spec.env == {}
    assert spec.config == {}
    assert spec.resources.is_empty()


def test_normalize_keeps_explicit_values() -> None:
    spec = normalize_spec({"image": "nginx:1.27", "replicas": 5, "port": 8080})
    assert spec.replicas == 5
    assert spec.port == 8080


def test_normalize_requires_image() -> None:
    with pytest.raises(ValueError, match="image"):
        normalize_spec({"replicas": 2})


def test_normalize_rejects_empty_image() -> None:
    with pytest.raises(ValueError, match="image"):
        normalize_spec({"image": ""})


def test_normalize_rejects_negative_replicas() -> None:
    with pytest.raises(ValueError, match="replicas"):
        normalize_spec({"image": "nginx", "replicas": -1})


def test_normalize_rejects_bool_replicas() -> None:
    # bool is a subclass of int; it must not be accepted as a replica count.
    with pytest.raises(ValueError, match="replicas"):
        normalize_spec({"image": "nginx", "replicas": True})


def test_normalize_rejects_out_of_range_port() -> None:
    with pytest.raises(ValueError, match="port"):
        normalize_spec({"image": "nginx", "port": 70000})


def test_normalize_rejects_zero_port() -> None:
    with pytest.raises(ValueError, match="port"):
        normalize_spec({"image": "nginx", "port": 0})


def test_normalize_rejects_bool_port() -> None:
    with pytest.raises(ValueError, match="port"):
        normalize_spec({"image": "nginx", "port": True})


def test_normalize_rejects_non_mapping() -> None:
    with pytest.raises(ValueError, match="mapping"):
        normalize_spec(["not", "a", "dict"])  # type: ignore[arg-type]


# --- normalize_spec: env ---------------------------------------------------


def test_normalize_parses_env() -> None:
    spec = normalize_spec({"image": "nginx", "env": {"LOG_LEVEL": "info", "TZ": "UTC"}})
    assert spec.env == {"LOG_LEVEL": "info", "TZ": "UTC"}


def test_normalize_rejects_non_mapping_env() -> None:
    with pytest.raises(ValueError, match="env"):
        normalize_spec({"image": "nginx", "env": ["A=B"]})


def test_normalize_rejects_non_string_env_value() -> None:
    with pytest.raises(ValueError, match="env"):
        normalize_spec({"image": "nginx", "env": {"PORT": 8080}})


def test_normalize_rejects_empty_env_key() -> None:
    with pytest.raises(ValueError, match="env"):
        normalize_spec({"image": "nginx", "env": {"": "x"}})


# --- normalize_spec: config ------------------------------------------------


def test_normalize_parses_config() -> None:
    spec = normalize_spec({"image": "nginx", "config": {"app.conf": "key=value"}})
    assert spec.config == {"app.conf": "key=value"}


def test_normalize_rejects_non_string_config_value() -> None:
    with pytest.raises(ValueError, match="config"):
        normalize_spec({"image": "nginx", "config": {"k": 5}})


# --- normalize_spec: resources ---------------------------------------------


def test_normalize_parses_resources() -> None:
    spec = normalize_spec(
        {
            "image": "nginx",
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "500m", "memory": "256Mi"},
            },
        }
    )
    assert spec.resources.cpu_request == "100m"
    assert spec.resources.memory_request == "128Mi"
    assert spec.resources.cpu_limit == "500m"
    assert spec.resources.memory_limit == "256Mi"
    assert not spec.resources.is_empty()


def test_normalize_resources_partial() -> None:
    spec = normalize_spec({"image": "nginx", "resources": {"requests": {"cpu": "250m"}}})
    assert spec.resources.cpu_request == "250m"
    assert spec.resources.memory_request is None
    assert spec.resources.cpu_limit is None


def test_normalize_rejects_invalid_quantity() -> None:
    with pytest.raises(ValueError, match="quantity"):
        normalize_spec({"image": "nginx", "resources": {"requests": {"cpu": "abc"}}})


def test_normalize_rejects_negative_quantity() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        normalize_spec({"image": "nginx", "resources": {"limits": {"memory": "-5Mi"}}})


def test_normalize_rejects_non_mapping_resources() -> None:
    with pytest.raises(ValueError, match="resources"):
        normalize_spec({"image": "nginx", "resources": "big"})


def test_resource_requirements_to_k8s_omits_unset() -> None:
    rr = ResourceRequirements(cpu_request="100m")
    assert rr.to_k8s() == {"requests": {"cpu": "100m"}}


def test_resource_requirements_empty_to_k8s() -> None:
    assert ResourceRequirements().to_k8s() == {}


# --- reconcile: create -----------------------------------------------------


def test_reconcile_creates_when_absent() -> None:
    actions = reconcile({"image": "nginx:1.27", "replicas": 3, "port": 8080}, None)
    # No config requested -> no ConfigMap; Deployment then Service created.
    assert _kinds(actions) == [ActionKind.CREATE, ActionKind.CREATE_SERVICE]
    create = actions[0]
    assert create.target_replicas == 3
    assert create.image == "nginx:1.27"
    assert create.port == 8080


def test_reconcile_create_uses_defaults() -> None:
    actions = reconcile({"image": "nginx"}, None)
    create = actions[0]
    assert create.target_replicas == DEFAULT_REPLICAS
    assert create.port == DEFAULT_PORT


def test_reconcile_create_carries_env_and_resources() -> None:
    actions = reconcile(
        {
            "image": "nginx",
            "env": {"TZ": "UTC"},
            "resources": {"requests": {"cpu": "100m"}},
        },
        None,
    )
    create = actions[0]
    assert create.env == {"TZ": "UTC"}
    assert create.resources is not None
    assert create.resources.cpu_request == "100m"


def test_reconcile_create_with_config_makes_configmap_first() -> None:
    actions = reconcile({"image": "nginx", "config": {"a": "b"}}, None)
    assert _kinds(actions) == [
        ActionKind.CREATE_CONFIGMAP,
        ActionKind.CREATE,
        ActionKind.CREATE_SERVICE,
    ]
    assert actions[0].config == {"a": "b"}


# --- reconcile: noop -------------------------------------------------------


def test_reconcile_noop_when_in_sync_legacy() -> None:
    # Legacy single-arg form: only the Deployment is considered, but the Service
    # is still managed -> it must be created.
    observed = ObservedDeployment(replicas=3, image="nginx:1.27", port=8080)
    actions = reconcile({"image": "nginx:1.27", "replicas": 3, "port": 8080}, observed)
    assert _kinds(actions) == [ActionKind.CREATE_SERVICE]


def test_reconcile_full_noop_when_everything_in_sync() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=3, image="nginx:1.27", port=8080),
        service=ObservedService(port=DEFAULT_SERVICE_PORT, target_port=8080),
    )
    actions = reconcile({"image": "nginx:1.27", "replicas": 3, "port": 8080}, state)
    assert _kinds(actions) == [ActionKind.NOOP]


def test_reconcile_noop_with_defaulted_spec() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=DEFAULT_REPLICAS, image="nginx", port=DEFAULT_PORT),
        service=ObservedService(),
    )
    actions = reconcile({"image": "nginx"}, state)
    assert _kinds(actions) == [ActionKind.NOOP]


# --- reconcile: scale ------------------------------------------------------


def test_reconcile_scales_up() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=2, image="nginx:1.27", port=80),
        service=ObservedService(),
    )
    actions = reconcile({"image": "nginx:1.27", "replicas": 5}, state)
    assert _kinds(actions) == [ActionKind.SCALE]
    assert actions[0].target_replicas == 5


def test_reconcile_scales_down() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=5, image="nginx:1.27", port=80),
        service=ObservedService(),
    )
    actions = reconcile({"image": "nginx:1.27", "replicas": 1}, state)
    assert _kinds(actions) == [ActionKind.SCALE]
    assert actions[0].target_replicas == 1


def test_reconcile_scale_to_zero() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=3, image="nginx:1.27", port=80),
        service=ObservedService(),
    )
    actions = reconcile({"image": "nginx:1.27", "replicas": 0}, state)
    assert _kinds(actions) == [ActionKind.SCALE]
    assert actions[0].target_replicas == 0


# --- reconcile: image / port changes --------------------------------------


def test_reconcile_updates_image() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=3, image="nginx:1.26", port=80),
        service=ObservedService(),
    )
    actions = reconcile({"image": "nginx:1.27", "replicas": 3}, state)
    assert _kinds(actions) == [ActionKind.UPDATE_IMAGE]
    assert actions[0].image == "nginx:1.27"


def test_reconcile_updates_port_updates_service_too() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=3, image="nginx:1.27", port=80),
        service=ObservedService(port=DEFAULT_SERVICE_PORT, target_port=80),
    )
    actions = reconcile({"image": "nginx:1.27", "replicas": 3, "port": 8080}, state)
    # Container port change also drifts the Service target port.
    assert _kinds(actions) == [ActionKind.UPDATE_PORT, ActionKind.UPDATE_SERVICE]
    assert actions[0].port == 8080
    assert actions[1].port == 8080


# --- reconcile: env / resources changes -----------------------------------


def test_reconcile_updates_env() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=1, image="nginx", port=80, env={"OLD": "1"}),
        service=ObservedService(),
    )
    actions = reconcile({"image": "nginx", "env": {"NEW": "2"}}, state)
    assert _kinds(actions) == [ActionKind.UPDATE_ENV]
    assert actions[0].env == {"NEW": "2"}


def test_reconcile_no_env_change_when_equal() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=1, image="nginx", port=80, env={"A": "1"}),
        service=ObservedService(),
    )
    actions = reconcile({"image": "nginx", "env": {"A": "1"}}, state)
    assert _kinds(actions) == [ActionKind.NOOP]


def test_reconcile_updates_resources() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=1, image="nginx", port=80),
        service=ObservedService(),
    )
    actions = reconcile({"image": "nginx", "resources": {"requests": {"cpu": "100m"}}}, state)
    assert _kinds(actions) == [ActionKind.UPDATE_RESOURCES]
    assert actions[0].resources is not None
    assert actions[0].resources.cpu_request == "100m"


# --- reconcile: service ----------------------------------------------------


def test_reconcile_creates_service_when_absent() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=1, image="nginx", port=80),
        service=None,
    )
    actions = reconcile({"image": "nginx"}, state)
    assert _kinds(actions) == [ActionKind.CREATE_SERVICE]
    assert actions[0].service_port == DEFAULT_SERVICE_PORT


def test_reconcile_updates_service_port() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=1, image="nginx", port=80),
        service=ObservedService(port=80, target_port=80),
    )
    actions = reconcile({"image": "nginx", "servicePort": 8080}, state)
    assert _kinds(actions) == [ActionKind.UPDATE_SERVICE]
    assert actions[0].service_port == 8080


# --- reconcile: configmap --------------------------------------------------


def test_reconcile_creates_configmap_when_config_requested() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=1, image="nginx", port=80),
        service=ObservedService(),
        configmap=None,
    )
    actions = reconcile({"image": "nginx", "config": {"k": "v"}}, state)
    assert _kinds(actions) == [ActionKind.CREATE_CONFIGMAP]
    assert actions[0].config == {"k": "v"}


def test_reconcile_updates_configmap_on_drift() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=1, image="nginx", port=80),
        service=ObservedService(),
        configmap=ObservedConfigMap(data={"k": "old"}),
    )
    actions = reconcile({"image": "nginx", "config": {"k": "new"}}, state)
    assert _kinds(actions) == [ActionKind.UPDATE_CONFIGMAP]
    assert actions[0].config == {"k": "new"}


def test_reconcile_no_configmap_when_none_requested() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=1, image="nginx", port=80),
        service=ObservedService(),
        configmap=None,
    )
    actions = reconcile({"image": "nginx"}, state)
    assert ActionKind.CREATE_CONFIGMAP not in _kinds(actions)


# --- reconcile: combined changes & ordering -------------------------------


def test_reconcile_combines_image_and_scale_in_order() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=2, image="nginx:1.26", port=80),
        service=ObservedService(),
    )
    actions = reconcile({"image": "nginx:1.27", "replicas": 5}, state)
    # image/port updates come before scale.
    assert _kinds(actions) == [ActionKind.UPDATE_IMAGE, ActionKind.SCALE]
    assert actions[0].image == "nginx:1.27"
    assert actions[1].target_replicas == 5


def test_reconcile_all_deployment_changes_order() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=1, image="nginx:1.26", port=80),
        service=ObservedService(port=DEFAULT_SERVICE_PORT, target_port=80),
    )
    actions = reconcile({"image": "nginx:1.27", "replicas": 4, "port": 8080}, state)
    assert _kinds(actions) == [
        ActionKind.UPDATE_IMAGE,
        ActionKind.UPDATE_PORT,
        ActionKind.SCALE,
        ActionKind.UPDATE_SERVICE,
    ]


def test_reconcile_configmap_first_then_deployment_then_service() -> None:
    state = ObservedState(
        deployment=None,
        service=None,
        configmap=None,
    )
    actions = reconcile({"image": "nginx", "config": {"a": "b"}}, state)
    assert _kinds(actions) == [
        ActionKind.CREATE_CONFIGMAP,
        ActionKind.CREATE,
        ActionKind.CREATE_SERVICE,
    ]


def test_actions_are_immutable() -> None:
    actions = reconcile({"image": "nginx"}, None)
    with pytest.raises(AttributeError):
        actions[0].image = "other"  # type: ignore[misc]


def test_reconcile_validates_spec() -> None:
    with pytest.raises(ValueError, match="image"):
        reconcile({"replicas": 2}, None)


# --- compute_conditions ----------------------------------------------------


def test_conditions_when_deployment_missing() -> None:
    conditions = compute_conditions({"image": "nginx"}, ObservedState())
    ready, progressing = conditions
    assert ready.type == "Ready"
    assert ready.status == "False"
    assert ready.reason == "DeploymentMissing"
    assert progressing.status == "True"


def test_conditions_ready_when_converged_and_replicas_ready() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=3, image="nginx", port=80, ready_replicas=3),
        service=ObservedService(),
    )
    ready, progressing = compute_conditions({"image": "nginx", "replicas": 3}, state)
    assert ready.status == "True"
    assert ready.reason == "AllReplicasReady"
    assert progressing.status == "False"


def test_conditions_not_ready_when_replicas_pending() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=3, image="nginx", port=80, ready_replicas=1),
        service=ObservedService(),
    )
    ready, progressing = compute_conditions({"image": "nginx", "replicas": 3}, state)
    assert ready.status == "False"
    assert "1/3" in ready.message
    assert progressing.status == "True"


def test_conditions_progressing_when_drifted() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=3, image="nginx:1.26", port=80, ready_replicas=3),
        service=ObservedService(),
    )
    ready, progressing = compute_conditions({"image": "nginx:1.27", "replicas": 3}, state)
    assert progressing.status == "True"
    assert ready.status == "False"


def test_conditions_are_condition_instances() -> None:
    conditions = compute_conditions({"image": "nginx"}, ObservedState())
    assert all(isinstance(c, Condition) for c in conditions)


# --- deletion_plan ---------------------------------------------------------


def test_deletion_plan_orders_service_deployment_configmap() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=1, image="nginx", port=80),
        service=ObservedService(),
        configmap=ObservedConfigMap(data={"k": "v"}),
    )
    plan = deletion_plan(state)
    assert _kinds(plan) == [
        ActionKind.DELETE_SERVICE,
        ActionKind.DELETE_DEPLOYMENT,
        ActionKind.DELETE_CONFIGMAP,
    ]


def test_deletion_plan_skips_absent_children() -> None:
    state = ObservedState(
        deployment=ObservedDeployment(replicas=1, image="nginx", port=80),
        service=None,
        configmap=None,
    )
    plan = deletion_plan(state)
    assert _kinds(plan) == [ActionKind.DELETE_DEPLOYMENT]


def test_deletion_plan_noop_when_nothing_exists() -> None:
    plan = deletion_plan(ObservedState())
    assert _kinds(plan) == [ActionKind.NOOP]


def test_deletion_plan_only_configmap() -> None:
    state = ObservedState(configmap=ObservedConfigMap(data={"a": "b"}))
    plan = deletion_plan(state)
    assert _kinds(plan) == [ActionKind.DELETE_CONFIGMAP]


# --- misc ------------------------------------------------------------------


def test_finalizer_constant_is_namespaced() -> None:
    assert FINALIZER == "apps.viprasol.com/webapp-cleanup"
