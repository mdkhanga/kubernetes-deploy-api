"""Aggregated health of a deployment: one status + a human-readable reason.

Pure function of the live Deployment and its Pods. Rules, first match wins:

  UNKNOWN      deletion in progress, or no status reported yet
  FAILING      a pod is stuck on an error (CrashLoopBackOff, ImagePullBackOff, ...),
               or the rollout exceeded its progress deadline,
               or the ReplicaSet cannot create pods (ReplicaFailure, e.g. quota),
               or no replicas are ready while no rollout is in progress
  PROGRESSING  a rollout or scale is in progress
  DEGRADED     some but not all replicas are ready
  HEALTHY      all desired replicas are ready and available (including scaled to 0)
  UNKNOWN      anything else

Decision: pod-level errors make a deployment FAILING even while older pods still
serve traffic (e.g. a bad image pushed to a healthy deployment), so a broken
release is loud rather than hidden as DEGRADED.
"""

from kubernetes.client import V1Deployment, V1Pod

from app.models import HealthStatus

# Container "waiting" reasons that mean the container cannot start on its own.
FAILING_WAITING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
}


def _pod_problems(pods: list[V1Pod]) -> dict[str, list[str]]:
    """{reason: [pod names]} for pods whose containers are stuck on a failing reason.
    Pods already being deleted are ignored (e.g. old pods after a rollback)."""
    problems: dict[str, list[str]] = {}
    for pod in pods:
        if pod.metadata.deletion_timestamp or not pod.status:
            continue
        statuses = (pod.status.init_container_statuses or []) + (pod.status.container_statuses or [])
        for cs in statuses:
            waiting = cs.state.waiting if cs.state else None
            if waiting and waiting.reason in FAILING_WAITING_REASONS:
                problems.setdefault(waiting.reason, []).append(pod.metadata.name)
                break  # one reason per pod is enough
    return problems


def compute_health(deployment: V1Deployment, pods: list[V1Pod]) -> tuple[HealthStatus, str]:
    if deployment.metadata.deletion_timestamp:
        return HealthStatus.UNKNOWN, "Deletion in progress"
    status = deployment.status
    if status is None:
        return HealthStatus.UNKNOWN, "No status reported yet"

    desired = deployment.spec.replicas if deployment.spec.replicas is not None else 1
    current = status.replicas or 0
    updated = status.updated_replicas or 0
    ready = status.ready_replicas or 0
    available = status.available_replicas or 0
    conditions = {c.type: c for c in (status.conditions or [])}

    # --- FAILING: definite errors ---
    problems = _pod_problems(pods)
    if problems:
        parts = [f"{reason} in {len(names)} pod(s): {', '.join(sorted(names))}" for reason, names in sorted(problems.items())]
        return HealthStatus.FAILING, f"{'; '.join(parts)} ({ready}/{desired} ready)"

    progressing = conditions.get("Progressing")
    if progressing and progressing.status == "False" and progressing.reason == "ProgressDeadlineExceeded":
        return HealthStatus.FAILING, f"Rollout exceeded its progress deadline: {progressing.message}"

    replica_failure = conditions.get("ReplicaFailure")
    if replica_failure and replica_failure.status == "True":
        return HealthStatus.FAILING, f"Pods cannot be created: {replica_failure.message}"

    # --- Scaled to zero ---
    if desired == 0:
        if current == 0:
            return HealthStatus.HEALTHY, "Scaled to 0 replicas"
        return HealthStatus.PROGRESSING, f"Scaling down to 0 ({current} pod(s) remaining)"

    # --- PROGRESSING: the controller is still converging ---
    not_observed = (status.observed_generation or 0) < (deployment.metadata.generation or 0)
    if not_observed or updated < desired or current > updated:
        return HealthStatus.PROGRESSING, f"Rollout in progress: {updated}/{desired} updated, {ready}/{desired} ready"

    # --- Steady state ---
    if ready == 0:
        return HealthStatus.FAILING, f"No replicas ready (0/{desired})"
    if ready < desired:
        return HealthStatus.DEGRADED, f"{ready}/{desired} replicas ready"
    if available >= desired:
        return HealthStatus.HEALTHY, f"All {desired} replicas ready"
    return HealthStatus.UNKNOWN, f"{ready}/{desired} ready but only {available} available"
