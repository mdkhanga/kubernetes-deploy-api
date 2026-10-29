"""Names and limits shared by the API, the Kubernetes gateway and the tests."""

# Every Deployment we create carries this label; we only ever read or modify
# Deployments that have it, so the API can never touch workloads it didn't create.
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "deploy-api"

# Immutable selector label tying a Deployment to its pods (value = deployment name).
NAME_LABEL = "deploy-api/name"

RESERVED_LABELS = {MANAGED_BY_LABEL, NAME_LABEL}

# Same annotation `kubectl rollout restart` sets on the pod template.
RESTARTED_AT_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

DEFAULT_NAMESPACE = "default"
DEFAULT_REPLICAS = 1
MAX_REPLICAS = 100
