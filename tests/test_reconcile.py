"""Unit tests for the pure reconcile loop.

These tests do not import kopf or the Kubernetes client — they exercise the
deterministic core directly with hand-built cases.
"""

from __future__ import annotations

import pytest

from k8s_operator.reconcile import (
    DEFAULT_PORT,
    DEFAULT_REPLICAS,
    Action,
    ActionKind,
    ObservedDeployment,
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


def test_normalize_rejects_non_mapping() -> None:
    with pytest.raises(ValueError, match="mapping"):
        normalize_spec(["not", "a", "dict"])  # type: ignore[arg-type]


# --- reconcile: create -----------------------------------------------------


def test_reconcile_creates_when_absent() -> None:
    actions = reconcile({"image": "nginx:1.27", "replicas": 3, "port": 8080}, None)
    assert _kinds(actions) == [ActionKind.CREATE]
    create = actions[0]
    assert create.target_replicas == 3
    assert create.image == "nginx:1.27"
    assert create.port == 8080


def test_reconcile_create_uses_defaults() -> None:
    actions = reconcile({"image": "nginx"}, None)
    assert actions[0].target_replicas == DEFAULT_REPLICAS
    assert actions[0].port == DEFAULT_PORT


# --- reconcile: noop -------------------------------------------------------


def test_reconcile_noop_when_in_sync() -> None:
    observed = ObservedDeployment(replicas=3, image="nginx:1.27", port=8080)
    actions = reconcile({"image": "nginx:1.27", "replicas": 3, "port": 8080}, observed)
    assert _kinds(actions) == [ActionKind.NOOP]


def test_reconcile_noop_with_defaulted_spec() -> None:
    observed = ObservedDeployment(replicas=DEFAULT_REPLICAS, image="nginx", port=DEFAULT_PORT)
    actions = reconcile({"image": "nginx"}, observed)
    assert _kinds(actions) == [ActionKind.NOOP]


# --- reconcile: scale ------------------------------------------------------


def test_reconcile_scales_up() -> None:
    observed = ObservedDeployment(replicas=2, image="nginx:1.27", port=80)
    actions = reconcile({"image": "nginx:1.27", "replicas": 5}, observed)
    assert _kinds(actions) == [ActionKind.SCALE]
    assert actions[0].target_replicas == 5


def test_reconcile_scales_down() -> None:
    observed = ObservedDeployment(replicas=5, image="nginx:1.27", port=80)
    actions = reconcile({"image": "nginx:1.27", "replicas": 1}, observed)
    assert _kinds(actions) == [ActionKind.SCALE]
    assert actions[0].target_replicas == 1


def test_reconcile_scale_to_zero() -> None:
    observed = ObservedDeployment(replicas=3, image="nginx:1.27", port=80)
    actions = reconcile({"image": "nginx:1.27", "replicas": 0}, observed)
    assert _kinds(actions) == [ActionKind.SCALE]
    assert actions[0].target_replicas == 0


# --- reconcile: image / port changes --------------------------------------


def test_reconcile_updates_image() -> None:
    observed = ObservedDeployment(replicas=3, image="nginx:1.26", port=80)
    actions = reconcile({"image": "nginx:1.27", "replicas": 3}, observed)
    assert _kinds(actions) == [ActionKind.UPDATE_IMAGE]
    assert actions[0].image == "nginx:1.27"


def test_reconcile_updates_port() -> None:
    observed = ObservedDeployment(replicas=3, image="nginx:1.27", port=80)
    actions = reconcile({"image": "nginx:1.27", "replicas": 3, "port": 8080}, observed)
    assert _kinds(actions) == [ActionKind.UPDATE_PORT]
    assert actions[0].port == 8080


# --- reconcile: combined changes & ordering -------------------------------


def test_reconcile_combines_image_and_scale_in_order() -> None:
    observed = ObservedDeployment(replicas=2, image="nginx:1.26", port=80)
    actions = reconcile({"image": "nginx:1.27", "replicas": 5}, observed)
    # image/port updates come before scale.
    assert _kinds(actions) == [ActionKind.UPDATE_IMAGE, ActionKind.SCALE]
    assert actions[0].image == "nginx:1.27"
    assert actions[1].target_replicas == 5


def test_reconcile_all_three_changes() -> None:
    observed = ObservedDeployment(replicas=1, image="nginx:1.26", port=80)
    actions = reconcile({"image": "nginx:1.27", "replicas": 4, "port": 8080}, observed)
    assert _kinds(actions) == [
        ActionKind.UPDATE_IMAGE,
        ActionKind.UPDATE_PORT,
        ActionKind.SCALE,
    ]


def test_actions_are_immutable() -> None:
    actions = reconcile({"image": "nginx"}, None)
    with pytest.raises(AttributeError):
        actions[0].image = "other"  # type: ignore[misc]
