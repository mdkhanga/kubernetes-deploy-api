# deploy-api

A REST service and a CLI that create, manage and monitor Kubernetes Deployments on a
real cluster. Every read comes live from the Kubernetes API; nothing is cached.

- **API:** FastAPI + the official Kubernetes Python client
- **CLI:** `deploy` (Typer + Rich), a terminal UI that calls the API
- **Health:** each deployment gets `HEALTHY`, `DEGRADED`, `FAILING`, `PROGRESSING` or `UNKNOWN`, with a reason
- **Design and decisions:** see [docs/DESIGN.md](docs/DESIGN.md)

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .
uvicorn app.main:app --port 8090 --reload          # terminal 1

deploy create --name web --image nginx:latest -n dev -r 3     # terminal 2
deploy list --watch
deploy status web
```

## Status

| Feature | State |
| --- | --- |
| Create, list, get, delete deployments | Done |
| Deployment and pod-level status, aggregated health | Done |
| PATCH (scale, image) and rollout restart | Done |
| CLI (`deploy create/list/status/scale/set-image/restart/delete`) | Done |
| `deploy logs` (stretch) | Planned |

## Prerequisites

- Python 3.12+
- A Kubernetes cluster and a kubeconfig pointing at it. Examples use a local
  [k3d](https://k3d.io) cluster: `k3d cluster create learn`
- `kubectl` (optional, to see what the API did)

The API uses `~/.kube/config` (current context) when run locally, or its
ServiceAccount when run inside a pod.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .          # installs the `deploy` CLI command
```

## Run

```bash
uvicorn app.main:app --port 8090 --reload
```

- API base URL: `http://localhost:8090/api/deployments`
- Interactive docs (Swagger UI): http://localhost:8090/docs
- Liveness: `GET /healthz`

## Endpoints

| Method | Path | Description | Success |
| --- | --- | --- | --- |
| POST | `/api/deployments` | Create a Deployment (creates the namespace if needed) | 201 |
| GET | `/api/deployments?namespace=<ns>` | List managed deployments, optional namespace filter | 200 |
| GET | `/api/deployments/{id}` | Detail: replicas, conditions, pods, health | 200 |
| PATCH | `/api/deployments/{id}` | Scale (`replicas`) and/or change `image` (rolling update) | 200 |
| POST | `/api/deployments/{id}/restart` | Rollout restart, like `kubectl rollout restart` | 202 |
| DELETE | `/api/deployments/{id}` | Delete with cascade (Foreground) | 202 |

`{id}` is the Deployment's Kubernetes **UID**, returned by create and list.

## CLI

The `deploy` command is a UI for the REST API (it only talks HTTP to it).
Start the API first, then:

```bash
deploy create --name web --image nginx:latest -n dev -r 3 --port 80
deploy create --name broken --image nginx:nonexistent -n dev -r 2
deploy list                     # table with color-coded health
deploy list -n dev --wide       # namespace filter + health reason
deploy list --watch             # refresh every 5s (Ctrl+C to stop)
deploy status broken            # replicas, conditions, pods with container states
deploy status web --watch
deploy scale web --replicas 5
deploy set-image web --image nginx:1.27
deploy restart web
deploy delete broken            # asks for confirmation (--yes to skip)
```

| Command | Purpose |
| --- | --- |
| `create --name --image [-n NS] [-r N] [--port] [--cpu-request] [--memory-request] [--cpu-limit] [--memory-limit] [-e K=V]... [-l K=V]... [--command "..."] [--arg A]...` | Create a deployment |
| `list [-n NS] [--wide] [--watch]` | All deployments with health (green HEALTHY, yellow DEGRADED, blue PROGRESSING, red FAILING) |
| `status REF [--watch]` | Detail: health + reason, replica counts, conditions, pod table |
| `scale REF --replicas N` | Scale |
| `set-image REF --image IMG` | Rolling update to a new image |
| `restart REF` | Rollout restart |
| `delete REF [--yes]` | Delete with confirmation |

- `REF` can be the full id, an id prefix (`c0bca7db`), a name (`web`) or `namespace/name`.
  Ambiguous references are rejected with the list of matches.
- API location: `--api-url` or `DEPLOY_API_URL` (default `http://localhost:8090`).
- Exit codes: `0` success, `1` API error or not found, `2` usage error or API unreachable.
- Without `pip install -e .`, use `python -m cli.deploy ...`.

## Walkthrough against a cluster (REST API with curl)

Each step below has a CLI equivalent in the [CLI](#cli) section, e.g. step 3 is
`deploy create --name broken --image nginx:nonexistent -n dev -r 2` and step 4 is `deploy status broken`.

Set variables on their own line. `ID=x curl .../$ID` on one line does **not**
work: the shell expands `$ID` before the assignment applies.

### 1. Create a healthy deployment

```bash
curl -s -X POST localhost:8090/api/deployments \
  -H 'Content-Type: application/json' \
  -d '{"name": "web", "namespace": "dev", "image": "nginx:latest", "replicas": 3, "port": 80}' \
  | python -m json.tool
```

The response is `201` with the deployment, its `id` (UID) and initial status
(`PROGRESSING`, 0 ready). Check that it's real:

```bash
kubectl get deploy,pods -n dev -l app.kubernetes.io/managed-by=deploy-api
```

### 2. Watch it become healthy

```bash
curl -s 'localhost:8090/api/deployments?namespace=dev' \
  | python -c "import sys,json; [print(d['name'], d['health'], '-', d['health_reason']) for d in json.load(sys.stdin)['items']]"
```

```
web PROGRESSING - Rollout in progress: 3/3 updated, 1/3 ready
web HEALTHY - All 3 replicas ready
```

### 3. Deploy a bad image and see the failure

```bash
curl -s -X POST localhost:8090/api/deployments \
  -H 'Content-Type: application/json' \
  -d '{"name": "broken", "namespace": "dev", "image": "nginx:nonexistent", "replicas": 2}' \
  | python -m json.tool
```

After ~20 seconds the list shows:

```
broken FAILING - ImagePullBackOff in 2 pod(s): broken-5cb68f846f-9ml5j, broken-5cb68f846f-gjf66 (0/2 ready)
web HEALTHY - All 3 replicas ready
```

### 4. Inspect pod-level detail

```bash
BAD=<id of broken>
curl -s localhost:8090/api/deployments/$BAD | python -m json.tool
```

Abridged response:

```json
{
  "id": "c0bca7db-1a39-4139-aa73-831e51702c48",
  "name": "broken",
  "namespace": "dev",
  "image": "nginx:nonexistent",
  "replicas": {"desired": 2, "current": 2, "updated": 2, "ready": 0, "available": 0, "unavailable": 2},
  "health": "FAILING",
  "health_reason": "ImagePullBackOff in 2 pod(s): ... (0/2 ready)",
  "conditions": [
    {"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable", "...": "..."},
    {"type": "Progressing", "status": "True", "reason": "ReplicaSetUpdated", "...": "..."}
  ],
  "created_at": "2026-10-29T17:05:12Z",
  "last_updated_at": "2026-10-29T17:05:13Z",
  "pods": [
    {
      "name": "broken-5cb68f846f-9ml5j",
      "phase": "Pending",
      "node": "k3d-learn-agent-0",
      "ready": false,
      "restart_count": 0,
      "containers": [
        {"name": "broken", "image": "nginx:nonexistent", "state": "waiting",
         "reason": "ImagePullBackOff", "message": "Back-off pulling image \"nginx:nonexistent\""}
      ]
    }
  ]
}
```

### 5. Scale, change the image, restart

```bash
ID=<id of web>

# Scale to 5
curl -s -X PATCH localhost:8090/api/deployments/$ID -H 'Content-Type: application/json' \
  -d '{"replicas": 5}' | python -m json.tool

# New image: a rolling update (PROGRESSING, then HEALTHY)
curl -s -X PATCH localhost:8090/api/deployments/$ID -H 'Content-Type: application/json' \
  -d '{"image": "nginx:1.27"}' | python -m json.tool

# Bad image onto the healthy deployment: old pods keep serving, health is FAILING
curl -s -X PATCH localhost:8090/api/deployments/$ID -H 'Content-Type: application/json' \
  -d '{"image": "nginx:nonexistent"}' | python -m json.tool

# Rollout restart: new pods, same spec; restarted_at is set
curl -s -X POST localhost:8090/api/deployments/$ID/restart | python -m json.tool
kubectl get pods -n dev -w
```

PATCH body: `{"replicas": <0-100>}`, `{"image": "<image>"}`, or both (one atomic
change). An empty body or unknown fields return 400.

### 6. Errors

```bash
# 409: same name in the same namespace
curl -s -X POST localhost:8090/api/deployments -H 'Content-Type: application/json' \
  -d '{"name": "web", "namespace": "dev", "image": "nginx:latest"}' | python -m json.tool

# 400: every invalid field is reported
curl -s -X POST localhost:8090/api/deployments -H 'Content-Type: application/json' \
  -d '{"name": "Bad_Name", "image": "nginx latest", "replicas": -1}' | python -m json.tool

# 404: unknown id
curl -s localhost:8090/api/deployments/nope | python -m json.tool
```

All errors share one shape:

```json
{
  "error": {
    "code": "INVALID_INPUT",
    "message": "Request validation failed",
    "details": [
      {"field": "name", "message": "must be a DNS-1123 label: ..."},
      {"field": "image", "message": "must be a valid image reference, ..."},
      {"field": "replicas", "message": "Input should be greater than or equal to 0"}
    ]
  }
}
```

| HTTP | `code` | When |
| --- | --- | --- |
| 400 | `INVALID_INPUT` | Bad request body or query, or Kubernetes rejected the object |
| 404 | `NOT_FOUND` | No managed deployment with that id |
| 409 | `CONFLICT` | Name already exists in that namespace |
| 500 | `KUBERNETES_ERROR` | Kubernetes API error (forbidden, quota, server error), with its message |
| 500 | `KUBERNETES_UNREACHABLE` | API server cannot be reached |
| 500 | `INTERNAL_ERROR` | Unexpected error |

### 7. Delete

```bash
curl -s -X DELETE localhost:8090/api/deployments/$BAD | python -m json.tool
kubectl get pods -n dev -w      # the pods are removed first, then the deployment
```

## Create request fields

| Field | Required | Default | Rules |
| --- | --- | --- | --- |
| `name` | yes | | DNS-1123 label, max 63 chars |
| `image` | yes | | Image reference, e.g. `nginx:1.27`, `registry.example.com/team/app:v1` |
| `namespace` | no | `default` | DNS-1123 label; created if missing |
| `replicas` | no | `1` | 0 to 100 |
| `port` | no | | 1 to 65535 |
| `resources.requests.cpu/memory`, `resources.limits.cpu/memory` | no | | Kubernetes quantities > 0; request ≤ limit |
| `env` | no | `[]` | `[{"name": "KEY", "value": "v"}]`; valid, unique names |
| `labels` | no | `{}` | Valid label keys/values; `app.kubernetes.io/managed-by` and `deploy-api/name` are reserved |
| `command`, `args` | no | | Lists of strings |

Unknown fields are rejected.

## Tests

```bash
pytest -v                    # unit tests: no cluster needed (~1s)
pytest -m integration -v     # integration tests: real cluster from your kubeconfig (~1-2 min)
```

**Integration tests** (`tests/integration/`) send real HTTP requests through the app
(in-process, no server needed) to the real cluster, in a throwaway namespace
`deploy-api-it-<random>` that is deleted afterwards. They cover:

| Test | Checks |
| --- | --- |
| `test_deployment_lifecycle` | create (namespace auto-created) → in list (with/without filter) → HEALTHY with Running pods → scale 2→3 → 3 ready, also in Kubernetes → delete → 404, gone from Kubernetes, pods gone |
| `test_bad_image_is_reported_as_failing` | `nginx:nonexistent` → FAILING at deployment level; pod Pending, container waiting on ImagePullBackOff/ErrImagePull |
| `test_duplicate_name_returns_409` | duplicate name → 409 CONFLICT from the real API server |
| `test_restart_replaces_pods` | restart → 202, all old pods replaced, back to HEALTHY |

They are skipped automatically when no cluster is reachable.

**Unit tests** run without a cluster: an in-memory fake replaces the Kubernetes gateway.
They cover validation rules, every endpoint and error code, the health rules, and
the exact calls the real gateway makes (strategic merge patch content-type,
Foreground delete) using mocks.

| File | Covers |
| --- | --- |
| `tests/test_validation.py` | Input rules and request models |
| `tests/test_api.py` | Every endpoint, status code and error shape (FakeGateway) |
| `tests/test_health.py` | Aggregated health rules, one named case per scenario |
| `tests/test_gateway.py` | Real gateway call details, with the Kubernetes client mocked |
| `tests/test_cli.py` | CLI commands, request bodies, output, prompts, exit codes (mocked HTTP) |

## Project layout

```
app/
  main.py          app wiring, error handlers (400/404/409/500)
  routes.py        REST endpoints
  models.py        request/response models (pydantic)
  validation.py    Kubernetes input rules
  service.py       business logic: build Deployment, resolve id, assemble status
  convert.py       Kubernetes objects -> API responses
  health.py        aggregated health rules
  k8s_gateway.py   the only code that calls the Kubernetes API
  errors.py        error types and the error JSON shape
  constants.py     labels, annotation, limits
cli/
  deploy.py        `deploy` CLI (Typer + Rich), calls the REST API
tests/             pytest suites + FakeGateway
pyproject.toml     package metadata + `deploy` console script
docs/DESIGN.md     design choices and decisions
```
