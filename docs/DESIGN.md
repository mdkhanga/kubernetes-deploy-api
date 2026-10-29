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
| 22 | **CLI calls the REST API**, not Kubernetes directly; lives in `cli/` of the same repo but never imports `app/` | One place for Kubernetes logic; the CLI is a UI for the service and could be split out unchanged | CLI with its own client; separate repo |
| 23 | **CLI = Typer + Rich, installed as `deploy`** via `pyproject.toml` console script | Subcommands from function signatures; colored tables; built-in confirm prompt; matches the spec's `deploy ...` examples | argparse + plain print |
| 24 | **CLI references: full id, id prefix, name, or namespace/name**, resolved client-side from the list; ambiguity is an error | UIDs are long to type; names are friendlier; never guess between matches | Full id only |
| 25 | **CLI `--watch` refreshes every 5s** (Rich Live) on `list` and `status` | Spec's auto-refresh requirement, applied to the CLI | Manual re-run |
| 26 | **CLI exit codes: 0 ok, 1 API error/not found, 2 usage error/unreachable**; API errors printed with code and field details | Scriptable (smoke test) and readable | Always 1 |
| 27 | **CLI tests use `httpx.MockTransport`** | Exercise real request bodies and output without a server | Mocking the Api class |
| 27a | **Logs streamed as `text/plain` over plain HTTP** (chunked), read from Kubernetes with `_preload_content=False` and split with the client's `iter_resp_lines` | Works with curl and the CLI with no extra protocol; lines appear as they are written | WebSocket / Server-Sent Events (needed for a browser UI, not for a CLI) |
| 27b | **Default = all pods, lines prefixed with the pod name; `pod` narrows to one** | Matches what an operator wants first ("what are my pods saying?"); prefixes keep lines attributable | Require a pod name |
| 27c | **Follow mode: one reader thread per pod feeding a queue**; non-follow reads pod by pod | Live interleaving across pods; grouped output when reading history | asyncio rewrite of the Kubernetes calls |
| 27d | **Log streams opened before the response starts** | Errors (unknown pod, container not started) still return structured JSON; a pod that cannot serve logs is reported inline when reading all pods | Errors mid-stream |
| 27e | **Streams closed in `finally`** when the response ends or the client disconnects; no timeout on followed streams (10s otherwise) | No leaked Kubernetes connections; a quiet followed log is not cut off. The v37 client rejects a `None` read timeout, so follow passes no timeout (the API server was reached moments earlier to list pods). Limitation: after a client disconnects, cleanup happens when the next log line arrives | Per-read timeouts |
| 27f | **Gateway tests run the real client methods with only the network call stubbed** | The v37 client validates arguments (pydantic); pure mocks hid a bad `_request_timeout` value that failed at runtime | Mock-only tests |
| 27g | **Optional Service is owned by the Deployment** (ownerReference, `blockOwnerDeletion`) and selects the immutable name label | Deleting the deployment garbage-collects the Service (and Foreground delete waits for it); no cleanup code or orphans | Deleting the Service explicitly |
| 27h | **Create with a Service is all-or-nothing**: if the Service fails (name taken, NodePort allocated), the Deployment is deleted and the error returned | The caller never gets an error while a half-created deployment keeps running | Return success with a warning |
| 27i | **Service types limited to ClusterIP and NodePort**; `port` defaults to the container port; `node_port` only with NodePort | What the spec asks; LoadBalancer depends on the environment | Allow LoadBalancer |
| 27j | **Access URLs = cluster DNS name, plus `<node-ip>:<nodePort>`** | Node IPs vary by environment (and need extra RBAC to list nodes); the placeholder is honest | Listing nodes to fill in IPs |
| 28 | **Integration tests in pytest (`-m integration`), not a shell smoke script** | Real assertions and pass/fail counts; same tooling as unit tests; excluded from the default fast run | Bash + curl smoke script |
| 29 | **Integration tests run the app in-process** (TestClient + real KubernetesGateway) | Full HTTP → service → Kubernetes path without managing a server process | Starting uvicorn in a fixture |
| 30 | **One throwaway namespace per run, deleted at the end; `wait_for` polling** | Isolation from real workloads; Kubernetes is eventually consistent, so tests poll with timeouts instead of sleeping fixed times | Shared namespace; fixed sleeps |

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
  (list, create, patch, delete), pods (list), pods/log (get), services (list, create),
  namespaces (get, create).
- Replicas capped at 100 to catch typos; adjustable in `app/constants.py`.
- UID lookup scans all managed deployments; fine for hundreds, would need an index
  (or a namespace in the id) for many thousands.

## Not built: how I would add the remaining stretch goals

### Deployment history and rollback
- **History:** list the ReplicaSets owned by the Deployment (ownerReference uid). Each one
  is a revision: annotation `deployment.kubernetes.io/revision`, its pod template (image),
  creation time, and `kubernetes.io/change-cause` if set. Endpoint
  `GET /api/deployments/{id}/history`, newest first. PATCH would also set `change-cause`
  ("image nginx:1.27 via deploy-api") so history is readable.
- **Rollback:** Kubernetes has no server-side rollback API any more (it was removed with
  `extensions/v1beta1`); `kubectl rollout undo` works client-side by copying the target
  ReplicaSet's pod template into the Deployment. `POST /api/deployments/{id}/rollback`
  with optional `{"revision": N}` (default: previous) would do the same: take the template,
  drop the `pod-template-hash` label, and write it to `spec.template` with a
  `resourceVersion` precondition so a concurrent change fails with 409 instead of being
  overwritten. The Deployment controller then rolls out as usual; the restored template
  gets a new revision number, as with kubectl.
- **CLI:** `deploy history REF`, `deploy rollback REF [--revision N]`.
- **Tests:** fake ReplicaSets for revision listing and template selection; integration:
  v1 → v2 → rollback → image back to v1 and HEALTHY.

### Resource quota enforcement
- **Config:** `quotas.yaml` (path from `DEPLOY_API_QUOTAS`), validated with pydantic:
  `namespaces: {dev: {requests.cpu: "4", requests.memory: 8Gi, limits.cpu: "8", limits.memory: 16Gi}}`.
- **Apply:** at startup and whenever the API creates a namespace, create or update a
  `ResourceQuota` named `deploy-api-quota` per configured namespace (server-side apply,
  idempotent).
- **Block:** before create, scale or image change, compute what the change needs
  (replicas × per-pod requests/limits, plus the extra pods `maxSurge` allows during a
  rollout), compare with the quota's `status.hard - status.used`, and reject with a
  structured `QUOTA_EXCEEDED` error showing requested vs available. Kubernetes itself
  still enforces the quota at pod creation; the pre-check gives an immediate, clear error
  instead of a deployment that silently fails (today that case already shows as FAILING
  through the `ReplicaFailure` condition).
- Namespaces with a quota require CPU/memory requests and limits on every pod, so the API
  would require `resources` there (or apply a `LimitRange` with defaults).
- **RBAC:** resourcequotas get/list/create/patch.

### Health checks (probes)
- **Request:** `probes: {liveness, readiness, startup}`, each with exactly one handler
  (`http_get {path, port}`, `tcp_socket {port}`, `exec {command}`) plus
  `initial_delay_seconds`, `period_seconds`, `timeout_seconds`, `failure_threshold`
  with sane bounds; a pydantic discriminated union enforces "exactly one handler".
  `build_deployment` maps them to `V1Probe`; PATCH could update them (a template change,
  so a rolling update).
- **Status:** containers already report `ready`; add `started` and the latest probe
  failure from pod events (`Readiness probe failed: ...`, read via the events API filtered
  by the pod). Health rule: Running but not ready beyond the probe's grace period → DEGRADED
  with reason "readiness probe failing on N pod(s)".

### Other next steps
- Run in-cluster: Dockerfile + ServiceAccount + least-privilege ClusterRole (the RBAC list
  above) + Deployment for the API itself.
- Authentication on the API (it currently relies on network access + the kubeconfig's power).
- Scale: an informer cache / watch instead of live list calls, if read volume grows; an
  index (or namespace in the id) for UID lookup.
- Ingress creation to reach exposed services from outside the cluster.
- A web UI as an alternative to the CLI (same API; logs over WebSocket or SSE).
