"""Turn live Kubernetes objects into API response models. Pure functions: no API calls."""

from datetime import datetime

from kubernetes.client import V1ContainerStatus, V1Deployment, V1Pod

from app.health import compute_health
from app.models import (
    Condition,
    ContainerStatus,
    DeploymentDetail,
    DeploymentSummary,
    PodStatus,
    ReplicaCounts,
)


def replica_counts(d: V1Deployment) -> ReplicaCounts:
    status = d.status
    desired = d.spec.replicas if d.spec.replicas is not None else 1
    available = (status.available_replicas if status else None) or 0
    unavailable = status.unavailable_replicas if status else None
    return ReplicaCounts(
        desired=desired,
        current=(status.replicas if status else None) or 0,
        updated=(status.updated_replicas if status else None) or 0,
        ready=(status.ready_replicas if status else None) or 0,
        available=available,
        # Kubernetes omits this field until the controller has acted; derive it then.
        unavailable=unavailable if unavailable is not None else max(desired - available, 0),
    )


def conditions(d: V1Deployment) -> list[Condition]:
    return [
        Condition(
            type=c.type,
            status=c.status,
            reason=c.reason,
            message=c.message,
            last_update_time=c.last_update_time,
            last_transition_time=c.last_transition_time,
        )
        for c in ((d.status.conditions if d.status else None) or [])
    ]


def last_updated_at(d: V1Deployment) -> datetime | None:
    """Deployments have no 'updated at' field. Use the newest of: condition
    updates (status changes) and managedFields times (spec/metadata writes)."""
    times = [c.last_update_time for c in ((d.status.conditions if d.status else None) or []) if c.last_update_time]
    times += [m.time for m in (d.metadata.managed_fields or []) if m.time]
    return max(times) if times else d.metadata.creation_timestamp


def container_status(cs: V1ContainerStatus) -> ContainerStatus:
    state, reason, message, exit_code = "unknown", None, None, None
    if cs.state and cs.state.waiting:
        state, reason, message = "waiting", cs.state.waiting.reason, cs.state.waiting.message
    elif cs.state and cs.state.running:
        state = "running"
    elif cs.state and cs.state.terminated:
        t = cs.state.terminated
        state, reason, message, exit_code = "terminated", t.reason, t.message, t.exit_code
    return ContainerStatus(
        name=cs.name,
        image=cs.image,
        ready=bool(cs.ready),
        restart_count=cs.restart_count or 0,
        state=state,
        reason=reason,
        message=message,
        exit_code=exit_code,
    )


def pod_status(p: V1Pod) -> PodStatus:
    status = p.status
    containers = [container_status(cs) for cs in ((status.container_statuses if status else None) or [])]
    if not containers:
        # Not started yet (e.g. Pending/unschedulable): list the containers from the spec.
        containers = [
            ContainerStatus(name=c.name, image=c.image, ready=False, restart_count=0, state="unknown")
            for c in p.spec.containers
        ]
    pod_conditions = (status.conditions if status else None) or []
    ready = any(c.type == "Ready" and c.status == "True" for c in pod_conditions)

    reason, message = (status.reason, status.message) if status else (None, None)
    for c in pod_conditions:
        if c.type == "PodScheduled" and c.status == "False":
            reason, message = c.reason, c.message

    return PodStatus(
        name=p.metadata.name,
        phase=(status.phase if status else None) or "Unknown",
        node=p.spec.node_name,
        ready=ready,
        restart_count=sum(c.restart_count for c in containers),
        reason=reason,
        message=message,
        containers=containers,
        created_at=p.metadata.creation_timestamp,
    )


def to_summary(d: V1Deployment, pods: list[V1Pod]) -> DeploymentSummary:
    health, health_reason = compute_health(d, pods)
    return DeploymentSummary(
        id=d.metadata.uid,
        name=d.metadata.name,
        namespace=d.metadata.namespace,
        image=d.spec.template.spec.containers[0].image,
        labels=d.metadata.labels or {},
        replicas=replica_counts(d),
        health=health,
        health_reason=health_reason,
        conditions=conditions(d),
        created_at=d.metadata.creation_timestamp,
        last_updated_at=last_updated_at(d),
    )


def to_detail(d: V1Deployment, pods: list[V1Pod]) -> DeploymentDetail:
    summary = to_summary(d, pods)
    return DeploymentDetail(
        **summary.model_dump(),
        pods=sorted((pod_status(p) for p in pods), key=lambda p: p.name),
    )
