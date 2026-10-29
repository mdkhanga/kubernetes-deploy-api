"""`deploy`: command-line UI for deploy-api.

Talks only HTTP to the REST API (no Kubernetes code here). API location:
--api-url or the DEPLOY_API_URL environment variable (default http://localhost:8090).

Deployments can be referred to by full id, id prefix, name, or namespace/name.
Exit codes: 0 success, 1 API error or not found, 2 usage error or API unreachable.
"""

import shlex
import time
import uuid
from datetime import datetime, timezone

import httpx
import typer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

DEFAULT_API_URL = "http://localhost:8090"
REFRESH_SECONDS = 5

HEALTH_STYLES = {
    "HEALTHY": "bold green",
    "DEGRADED": "bold yellow",
    "PROGRESSING": "bold blue",
    "FAILING": "bold red",
    "UNKNOWN": "dim",
}
STATE_STYLES = {"running": "green", "waiting": "yellow", "terminated": "red", "unknown": "dim"}

app = typer.Typer(help="Create, inspect and manage Kubernetes deployments via deploy-api.", no_args_is_help=True)
console = Console()
err = Console(stderr=True)

# Tests inject an httpx.MockTransport here instead of a running server.
_transport: httpx.BaseTransport | None = None


# ---------- HTTP client ----------

class Api:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=15, transport=_transport)

    def call(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.TransportError as e:
            err.print(f"[red]Cannot reach deploy-api at {self.base_url}[/red] ({e}). Is the server running?")
            raise typer.Exit(2)
        if response.status_code >= 400:
            print_api_error(response)
            raise typer.Exit(1)
        return response.json()

    def stream_lines(self, path: str, params: dict):
        """Yield response lines as the server sends them (no read timeout: follow can be quiet)."""
        try:
            with self.http.stream("GET", path, params=params, timeout=httpx.Timeout(15, read=None)) as response:
                if response.status_code >= 400:
                    response.read()
                    print_api_error(response)
                    raise typer.Exit(1)
                yield from response.iter_lines()
        except httpx.TransportError as e:
            err.print(f"[red]Cannot reach deploy-api at {self.base_url}[/red] ({e}). Is the server running?")
            raise typer.Exit(2)


def print_api_error(response: httpx.Response) -> None:
    try:
        error = response.json()["error"]
    except (ValueError, KeyError, TypeError):
        err.print(f"[red]Error {response.status_code}[/red]: {response.text}")
        return
    err.print(f"[red]Error {response.status_code} {error['code']}[/red]: {error['message']}")
    for detail in error.get("details", []):
        field = f"{detail['field']}: " if detail.get("field") else ""
        err.print(f"  - {field}{detail['message']}")


def api(ctx: typer.Context) -> Api:
    return ctx.obj


def resolve(client: Api, ref: str) -> dict:
    """Find one deployment by full id, namespace/name, name, or unique id prefix."""
    if _is_uuid(ref):
        return client.call("GET", f"/api/deployments/{ref}")
    items = client.call("GET", "/api/deployments")["items"]
    for match in (
        lambda d: f"{d['namespace']}/{d['name']}" == ref,
        lambda d: d["name"] == ref,
        lambda d: d["id"].startswith(ref),
    ):
        found = [d for d in items if match(d)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            names = ", ".join(f"{d['namespace']}/{d['name']} ({d['id'][:8]})" for d in found)
            err.print(f"[red]'{ref}' is ambiguous[/red]: {names}. Use namespace/name or a longer id.")
            raise typer.Exit(1)
    err.print(f"[red]No deployment matches '{ref}'[/red]")
    raise typer.Exit(1)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return len(value) == 36
    except ValueError:
        return False


# ---------- Rendering ----------

def health_text(health: str) -> Text:
    return Text(health, style=HEALTH_STYLES.get(health, ""))


def age(timestamp: str | None) -> str:
    if not timestamp:
        return "-"
    seconds = int((datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{max(seconds, 0)}s"


def render_list(items: list[dict], wide: bool) -> Table:
    table = Table(title=f"Deployments ({len(items)})", title_justify="left")
    for column in ("ID", "NAMESPACE", "NAME", "IMAGE", "READY", "UP-TO-DATE", "AVAILABLE", "HEALTH", "AGE"):
        table.add_column(column, no_wrap=True)
    if wide:
        table.add_column("REASON")
    for d in items:
        r = d["replicas"]
        row = [
            d["id"][:8], d["namespace"], d["name"], d["image"],
            f"{r['ready']}/{r['desired']}", str(r["updated"]), str(r["available"]),
            health_text(d["health"]), age(d["created_at"]),
        ]
        if wide:
            row.append(d["health_reason"])
        table.add_row(*row)
    return table


def render_status(d: dict) -> Group:
    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold")
    header.add_column()
    header.add_row("Name", f"{d['namespace']}/{d['name']}")
    header.add_row("ID", d["id"])
    header.add_row("Image", d["image"])
    header.add_row("Health", Text.assemble(health_text(d["health"]), f"  {d['health_reason']}"))
    header.add_row("Created", f"{d['created_at']} ({age(d['created_at'])} ago)")
    header.add_row("Last updated", str(d.get("last_updated_at") or "-"))
    if d.get("restarted_at"):
        header.add_row("Restarted", d["restarted_at"])

    r = d["replicas"]
    replicas = Table(title="Replicas", title_justify="left")
    for column in ("DESIRED", "CURRENT", "UPDATED", "READY", "AVAILABLE", "UNAVAILABLE"):
        replicas.add_column(column, justify="right")
    replicas.add_row(*(str(r[k]) for k in ("desired", "current", "updated", "ready", "available", "unavailable")))

    conditions = Table(title="Conditions", title_justify="left")
    for column in ("TYPE", "STATUS", "REASON", "MESSAGE", "LAST UPDATE"):
        conditions.add_column(column)
    for c in d["conditions"]:
        conditions.add_row(c["type"], c["status"], c.get("reason") or "", c.get("message") or "",
                           age(c.get("last_update_time")) + " ago" if c.get("last_update_time") else "-")

    pods = Table(title=f"Pods ({len(d.get('pods', []))})", title_justify="left")
    for column in ("NAME", "PHASE", "READY", "RESTARTS", "NODE", "CONTAINERS", "AGE"):
        pods.add_column(column)
    for p in d.get("pods", []):
        containers = Text()
        for i, c in enumerate(p["containers"]):
            if i:
                containers.append("\n")
            containers.append(f"{c['name']}: ")
            containers.append(c["state"], style=STATE_STYLES.get(c["state"], ""))
            if c.get("reason"):
                containers.append(f" ({c['reason']})")
        if p.get("reason"):
            containers.append(f"\npod: {p['reason']}", style="yellow")
        pods.add_row(p["name"], p["phase"], "yes" if p["ready"] else "no", str(p["restart_count"]),
                     p.get("node") or "-", containers, age(p.get("created_at")))

    return Group(Panel(header, title="Deployment", title_align="left"), replicas, conditions, pods)


def watch(render, interval: int = REFRESH_SECONDS) -> None:
    """Redraw every `interval` seconds until Ctrl+C."""
    with Live(render(), console=console, auto_refresh=False) as live:
        try:
            while True:
                time.sleep(interval)
                live.update(render(), refresh=True)
        except KeyboardInterrupt:
            pass


def parse_pairs(values: list[str] | None, option: str) -> list[tuple[str, str]]:
    pairs = []
    for value in values or []:
        key, sep, val = value.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"expected KEY=VALUE, got '{value}'", param_hint=option)
        pairs.append((key, val))
    return pairs


# ---------- Commands ----------

@app.callback()
def main(
    ctx: typer.Context,
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url", envvar="DEPLOY_API_URL", help="deploy-api base URL"),
):
    ctx.obj = Api(api_url)


@app.command()
def create(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", help="Deployment name (DNS-1123)"),
    image: str = typer.Option(..., "--image", help="Container image, e.g. nginx:1.27"),
    namespace: str = typer.Option("default", "--namespace", "-n", help="Namespace (created if missing)"),
    replicas: int = typer.Option(1, "--replicas", "-r", help="Desired replicas"),
    port: int | None = typer.Option(None, "--port", help="Container port"),
    cpu_request: str | None = typer.Option(None, "--cpu-request", help="e.g. 100m"),
    memory_request: str | None = typer.Option(None, "--memory-request", help="e.g. 64Mi"),
    cpu_limit: str | None = typer.Option(None, "--cpu-limit", help="e.g. 500m"),
    memory_limit: str | None = typer.Option(None, "--memory-limit", help="e.g. 128Mi"),
    env: list[str] | None = typer.Option(None, "--env", "-e", help="KEY=VALUE (repeatable)"),
    label: list[str] | None = typer.Option(None, "--label", "-l", help="KEY=VALUE (repeatable)"),
    command: str | None = typer.Option(None, "--command", help="Command override, e.g. 'sh -c'"),
    arg: list[str] | None = typer.Option(None, "--arg", help="Argument (repeatable)"),
):
    """Create a deployment."""
    body: dict = {"name": name, "image": image, "namespace": namespace, "replicas": replicas}
    if port is not None:
        body["port"] = port
    requests = {k: v for k, v in (("cpu", cpu_request), ("memory", memory_request)) if v}
    limits = {k: v for k, v in (("cpu", cpu_limit), ("memory", memory_limit)) if v}
    if requests or limits:
        body["resources"] = {**({"requests": requests} if requests else {}), **({"limits": limits} if limits else {})}
    if env:
        body["env"] = [{"name": k, "value": v} for k, v in parse_pairs(env, "--env")]
    if label:
        body["labels"] = dict(parse_pairs(label, "--label"))
    if command:
        body["command"] = shlex.split(command)
    if arg:
        body["args"] = arg

    d = api(ctx).call("POST", "/api/deployments", json=body)
    console.print(f"Created deployment [bold]{d['namespace']}/{d['name']}[/bold]  id: {d['id']}")
    console.print(Text.assemble("Health: ", health_text(d["health"]), f"  {d['health_reason']}"))


@app.command("list")
def list_deployments(
    ctx: typer.Context,
    namespace: str | None = typer.Option(None, "--namespace", "-n", help="Only this namespace"),
    wide: bool = typer.Option(False, "--wide", help="Show the health reason"),
    watch_: bool = typer.Option(False, "--watch", "-w", help=f"Refresh every {REFRESH_SECONDS}s until Ctrl+C"),
):
    """List deployments with their health."""
    client = api(ctx)
    params = {"namespace": namespace} if namespace else {}

    def render():
        return render_list(client.call("GET", "/api/deployments", params=params)["items"], wide)

    watch(render) if watch_ else console.print(render())


@app.command()
def status(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="id, id prefix, name or namespace/name"),
    watch_: bool = typer.Option(False, "--watch", "-w", help=f"Refresh every {REFRESH_SECONDS}s until Ctrl+C"),
):
    """Detailed status: replicas, conditions and pods."""
    client = api(ctx)
    dep_id = resolve(client, ref)["id"]

    def render():
        return render_status(client.call("GET", f"/api/deployments/{dep_id}"))

    watch(render) if watch_ else console.print(render())


@app.command()
def scale(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="id, id prefix, name or namespace/name"),
    replicas: int = typer.Option(..., "--replicas", "-r", help="Desired replicas"),
):
    """Scale a deployment."""
    client = api(ctx)
    d = resolve(client, ref)
    updated = client.call("PATCH", f"/api/deployments/{d['id']}", json={"replicas": replicas})
    console.print(f"Scaled [bold]{updated['namespace']}/{updated['name']}[/bold] to {replicas} replicas")


@app.command("set-image")
def set_image(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="id, id prefix, name or namespace/name"),
    image: str = typer.Option(..., "--image", help="New container image"),
):
    """Change the container image (rolling update)."""
    client = api(ctx)
    d = resolve(client, ref)
    updated = client.call("PATCH", f"/api/deployments/{d['id']}", json={"image": image})
    console.print(f"Updated [bold]{updated['namespace']}/{updated['name']}[/bold] image to {updated['image']} (rolling update started)")


@app.command()
def restart(ctx: typer.Context, ref: str = typer.Argument(..., help="id, id prefix, name or namespace/name")):
    """Rollout restart (new pods, same spec)."""
    client = api(ctx)
    d = resolve(client, ref)
    updated = client.call("POST", f"/api/deployments/{d['id']}/restart")
    console.print(f"Restarted [bold]{updated['namespace']}/{updated['name']}[/bold] at {updated.get('restarted_at')}")


@app.command()
def logs(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="id, id prefix, name or namespace/name"),
    pod: str | None = typer.Option(None, "--pod", "-p", help="Only this pod (default: all pods, prefixed)"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new lines until Ctrl+C"),
    tail: int = typer.Option(100, "--tail", help="Lines of history per pod"),
):
    """Show or stream logs from the deployment's pods."""
    client = api(ctx)
    d = resolve(client, ref)
    params = {"follow": str(follow).lower(), "tail": tail, **({"pod": pod} if pod else {})}
    try:
        for line in client.stream_lines(f"/api/deployments/{d['id']}/logs", params):
            console.print(line, markup=False, highlight=False, soft_wrap=True)
    except KeyboardInterrupt:
        pass


@app.command()
def delete(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="id, id prefix, name or namespace/name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Delete a deployment and its pods."""
    client = api(ctx)
    d = resolve(client, ref)
    if not yes:
        typer.confirm(
            f"Delete deployment {d['namespace']}/{d['name']} ({d['id']}) and all its pods?", abort=True
        )
    client.call("DELETE", f"/api/deployments/{d['id']}")
    console.print(f"Deleting [bold]{d['namespace']}/{d['name']}[/bold] (pods are removed first, then the deployment)")


if __name__ == "__main__":
    app()
