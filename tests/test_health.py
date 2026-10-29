from datetime import datetime, timezone

import pytest
from kubernetes.client import (
    V1Container,
    V1ContainerState,
    V1ContainerStateRunning,
    V1ContainerStateWaiting,
    V1ContainerStatus,
    V1Deployment,
    V1DeploymentCondition,
    V1DeploymentSpec,
    V1DeploymentStatus,
    V1LabelSelector,
    V1ObjectMeta,
    V1Pod,
    V1PodSpec,
    V1PodStatus,
    V1PodTemplateSpec,
)

from app.health import compute_health
from app.models import HealthStatus

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def deployment(desired=3, current=None, updated=None, ready=None, available=None,
               generation=1, observed=1, conditions=(), deleting=False, status=True):
    """Defaults describe a settled, fully healthy 3-replica deployment."""
    current = desired if current is None else current
    return V1Deployment(
        metadata=V1ObjectMeta(name="web", namespace="dev", generation=generation,
                              deletion_timestamp=NOW if deleting else None),
        spec=V1DeploymentSpec(replicas=desired, selector=V1LabelSelector(match_labels={"app": "web"}),
                              template=V1PodTemplateSpec(spec=V1PodSpec(containers=[V1Container(name="c", image="nginx")]))),
        status=V1DeploymentStatus(
            replicas=current,
            updated_replicas=current if updated is None else updated,
            ready_replicas=current if ready is None else ready,
            available_replicas=(current if ready is None else ready) if available is None else available,
            observed_generation=observed,
            conditions=[V1DeploymentCondition(type=t, status=s, reason=r, message=f"{t} {r}")
                        for t, s, r in conditions],
        ) if status else None,
    )


def pod(name="web-1", waiting=None, init_waiting=None, terminating=False):
    def cs(reason):
        state = (V1ContainerState(waiting=V1ContainerStateWaiting(reason=reason)) if reason
                 else V1ContainerState(running=V1ContainerStateRunning()))
        return V1ContainerStatus(name="c", image="nginx", image_id="", ready=reason is None,
                                 restart_count=0, state=state)
    return V1Pod(
        metadata=V1ObjectMeta(name=name, deletion_timestamp=NOW if terminating else None),
        status=V1PodStatus(
            phase="Pending" if (waiting or init_waiting) else "Running",
            container_statuses=[cs(waiting)],
            init_container_statuses=[cs(init_waiting)] if init_waiting else None,
        ),
    )


PROGRESS_DEADLINE = ("Progressing", "False", "ProgressDeadlineExceeded")
QUOTA_FAILURE = ("ReplicaFailure", "True", "FailedCreate")


@pytest.mark.parametrize("case, dep, pods, expected", [
    # --- HEALTHY ---
    ("all replicas ready", deployment(), [pod()], HealthStatus.HEALTHY),
    ("scaled to zero", deployment(desired=0, current=0), [], HealthStatus.HEALTHY),
    ("terminating pod with an error is ignored", deployment(),
     [pod(), pod("old", waiting="ImagePullBackOff", terminating=True)], HealthStatus.HEALTHY),

    # --- PROGRESSING ---
    ("just created, not yet observed", deployment(current=0, ready=0, generation=1, observed=0), [], HealthStatus.PROGRESSING),
    ("spec changed, not yet observed", deployment(generation=2, observed=1), [pod()], HealthStatus.PROGRESSING),
    ("new pods not all updated", deployment(updated=1, ready=2), [pod()], HealthStatus.PROGRESSING),
    ("old pods still around after update", deployment(current=4, updated=3, ready=3), [pod()], HealthStatus.PROGRESSING),
    ("normal ContainerCreating is not an error", deployment(current=3, ready=0, updated=3, observed=0),
     [pod(waiting="ContainerCreating")], HealthStatus.PROGRESSING),
    ("scaling down to zero", deployment(desired=0, current=2, updated=2), [pod()], HealthStatus.PROGRESSING),

    # --- DEGRADED ---
    ("some replicas not ready", deployment(ready=2, available=2), [pod()], HealthStatus.DEGRADED),

    # --- FAILING ---
    ("no replicas ready in steady state", deployment(ready=0, available=0), [], HealthStatus.FAILING),
    ("ImagePullBackOff on a new deployment", deployment(ready=0, available=0, observed=0),
     [pod(waiting="ImagePullBackOff")], HealthStatus.FAILING),
    ("ErrImagePull", deployment(ready=0, available=0), [pod(waiting="ErrImagePull")], HealthStatus.FAILING),
    ("CrashLoopBackOff even with some ready", deployment(ready=2, available=2),
     [pod(), pod("web-2", waiting="CrashLoopBackOff")], HealthStatus.FAILING),
    ("bad image pushed onto a healthy deployment", deployment(current=4, updated=1, ready=3),
     [pod(), pod("web-new", waiting="ImagePullBackOff")], HealthStatus.FAILING),
    ("init container crash looping", deployment(ready=0, available=0),
     [pod(waiting="PodInitializing", init_waiting="CrashLoopBackOff")], HealthStatus.FAILING),
    ("missing ConfigMap", deployment(ready=0, available=0),
     [pod(waiting="CreateContainerConfigError")], HealthStatus.FAILING),
    ("rollout exceeded progress deadline", deployment(updated=1, conditions=[PROGRESS_DEADLINE]), [pod()], HealthStatus.FAILING),
    ("pods cannot be created (quota)", deployment(current=0, ready=0, conditions=[QUOTA_FAILURE]), [], HealthStatus.FAILING),

    # --- UNKNOWN ---
    ("being deleted", deployment(deleting=True), [pod()], HealthStatus.UNKNOWN),
    ("no status yet", deployment(status=False), [], HealthStatus.UNKNOWN),
])
def test_compute_health(case, dep, pods, expected):
    status, reason = compute_health(dep, pods)
    assert status == expected, f"{case}: got {status} ({reason})"
    assert reason


def test_failing_reason_names_the_problem_and_pods():
    status, reason = compute_health(
        deployment(ready=0, available=0),
        [pod("web-a", waiting="ImagePullBackOff"), pod("web-b", waiting="ImagePullBackOff")],
    )
    assert status == HealthStatus.FAILING
    assert reason == "ImagePullBackOff in 2 pod(s): web-a, web-b (0/3 ready)"


def test_progress_deadline_reason_includes_kubernetes_message():
    _, reason = compute_health(deployment(updated=1, conditions=[PROGRESS_DEADLINE]), [])
    assert "progress deadline" in reason and "ProgressDeadlineExceeded" in reason
