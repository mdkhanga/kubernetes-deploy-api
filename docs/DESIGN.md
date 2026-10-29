# deploy-api: design notes

A small service that manages Kubernetes Deployments through a REST API and reports
their live health. This document lists the choices we made and why.

## Architecture

```
 CLI / curl / Swagger UI
          │ HTTP + JSON
          ▼
 ┌──────────────────────────── deploy-api (one process) ────────────────────────────┐
 │ routes.py      endpoints; input validated by pydantic models (models.py)         │
 │ main.py        maps every failure to {"error": {code, message, details}}         │
 │ service.py     business logic: build Deployment, resolve id, assemble status     │
 │ convert.py     Kubernetes objects → response models        (pure)                │
 │ health.py      Deployment + Pods → status + reason         (pure)                │
 │ k8s_gateway.py the only code calling the Kubernetes API                          │
 └───────────────────────────────────────┬──────────────────────────────────────────┘
                                         ▼
                              kube-apiserver → Deployments, Pods, Namespaces
```

The service stores no state. The cluster is the source of truth, and every GET
reads it live.

## Decisions

| # | Decision | Why | Alternatives considered |
| --- | --- | --- | --- |
| 1 | **Python 3.12, FastAPI, official `kubernetes` client** | Readable; pydantic validation and Swagger docs for free; mature official client with simple error handling (`ApiException.status`) | Go + client-go (more verbose), TypeScript + client-node (error unwrapping friction). Note: patch content-type must be set explicitly in this client too (see 15a) |
| 2 | **No cache or database** | The spec requires live status; the API server is already the database | Informer cache (faster, but adds staleness and complexity) |
| 3 | **`id` = Deployment UID** | Spec returns the UID on create; globally unique; a deleted-then-recreated deployment gets a new id | `namespace/name` (cheaper lookup, but not what the spec hints at) |
| 4 | **UID lookup = list managed deployments across namespaces, match UID** | The API has no "get by UID"; one live call is fine at this scale | Encoding namespace in the id |
| 5 | **Managed label `app.kubernetes.io/managed-by=deploy-api`** on Deployment and pods | List/get/patch/delete only ever see our own deployments; the API cannot touch system workloads | Annotations (not selectable) |
| 6 | **Selector = `deploy-api/name=<name>` only** | Selectors are immutable; user labels can never break it. Reserved labels cannot be overridden | Selecting on user labels |
| 7 | **Pods found via the Deployment's own selector** | Same as kubectl; includes old and new pods during a rollout | Walking ownerReferences Pod → ReplicaSet → Deployment |
| 8 | **List makes one pods call** and groups pods by the name label | Avoids N+1 API calls | One pods call per deployment |
| 9 | **Namespace auto-created** (409 race ignored) | Spec requirement; idempotent | Require existing namespaces |
| 10 | **Validation in pydantic models calling pure rule functions** | Every bad field reported in one 400; rules unit-tested in isolation; Kubernetes' own `parse_quantity` for resources | Letting the API server reject (less clear messages, fails one field at a time) |
| 11 | **Unknown request fields rejected** (`extra="forbid"`) | A typo like `"replica": 3` fails loudly instead of being ignored | Ignore extras |
| 12 | **One error shape**, FastAPI's 422 converted to 400 | Spec asks for 400 + structured errors; clients parse one format | Framework defaults |
| 13 | **Kubernetes error mapping:** 404→404, 409→409, 400/422→400, others→500 with the API server's message | "Meaningful message" for 500s, e.g. the forbidden or quota text | Generic 500 |
| 14 | **10s timeout on every Kubernetes call** | An unreachable API server returns a clear 500 instead of hanging | Client defaults |
| 15 | **Credentials: in-cluster first, then kubeconfig**, loaded lazily | Same code runs locally and in a pod; importing the app needs no cluster (tests) | Explicit flag |
| 15a | **PATCH and restart send a strategic merge patch** (content-type set explicitly) | Applied atomically by the API server, so no read-modify-write and no conflict retries; containers are matched by name, so only the image changes. Same mechanism as `kubectl scale`, `set image`, `rollout restart`. The v37 Python client defaults PATCH to JSON Patch, so the content-type must be explicit (pinned by a test) | GET + PUT with retry on 409 conflict |
| 15b | **Restart = `kubectl.kubernetes.io/restartedAt` annotation on the pod template**, returns 202 | Identical to `kubectl rollout restart`; changing the template triggers a normal rolling update | Deleting pods (no rolling guarantees) |
| 16 | **DELETE uses `propagationPolicy: Foreground`**, returns 202 | Owned ReplicaSets and Pods are garbage-collected before the Deployment disappears; deletion is asynchronous | Background (Deployment vanishes first), Orphan |
| 17 | **`last_updated_at` = newest of condition `lastUpdateTime` and `managedFields[].time`**, else creation time | Deployments have no "updated at" field; this covers both spec writes and status changes | Only creation time |
| 18 | **Health is a pure function with ordered rules** returning status + reason | Easy to reason about and to test exhaustively; the reason explains the color | Logic spread across endpoints |
| 19 | **Pod errors make a deployment FAILING even if old pods still serve** | A broken release should be loud, not hidden as DEGRADED | DEGRADED while capacity remains |
| 20 | **Fresh start (`ContainerCreating`) is PROGRESSING, not FAILING** | "No replicas ready" only means failure once the rollout has settled | Literal "0 ready = FAILING" |
| 21 | **Gateway interface + in-memory FakeGateway for tests** | Endpoint tests run in under a second with no cluster; failures (403, 500, unreachable) are easy to simulate | envtest / kind in CI (slower) |
| 22 | **CLI calls the REST API**, not Kubernetes directly | One place for Kubernetes logic; the CLI is a UI for the service | CLI with its own client |

## Health rules (first match wins)

| Status | Condition |
| --- | --- |
| UNKNOWN | Deployment is being deleted, or has no status yet |
| FAILING | A non-terminating pod has a container (init containers included) waiting on `CrashLoopBackOff`, `ImagePullBackOff`, `ErrImagePull`, `InvalidImageName`, `CreateContainerConfigError`, `CreateContainerError` or `RunContainerError` |
| FAILING | `Progressing=False` with reason `ProgressDeadlineExceeded` |
| FAILING | `ReplicaFailure=True` (pods cannot be created: quota, PodSecurity, forbidden) |
| HEALTHY / PROGRESSING | Desired is 0: HEALTHY when no pods remain, else PROGRESSING |
| PROGRESSING | `observedGeneration < generation`, or `updated < desired`, or old pods remain (`current > updated`) |
| FAILING | Settled with 0 ready |
| DEGRADED | Settled with 0 < ready < desired |
| HEALTHY | ready = desired and available ≥ desired |
| UNKNOWN | Anything else |

## Assumptions and limits

- One container per Deployment (the request schema describes a single container).
- No authentication on the API; access to the cluster is whatever the kubeconfig
  or ServiceAccount allows. In-cluster, the ServiceAccount needs: deployments
  (list, create, patch, delete), pods (list), namespaces (get, create).
- Replicas capped at 100 to catch typos; adjustable in `app/constants.py`.
- UID lookup scans all managed deployments; fine for hundreds, would need an index
  (or a namespace in the id) for many thousands.
