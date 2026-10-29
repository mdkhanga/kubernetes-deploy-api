"""Aggregated health of a deployment. Placeholder until Step 3."""

from kubernetes.client import V1Deployment, V1Pod

from app.models import HealthStatus


def compute_health(deployment: V1Deployment, pods: list[V1Pod]) -> tuple[HealthStatus, str]:
    return HealthStatus.UNKNOWN, "health not computed yet"
