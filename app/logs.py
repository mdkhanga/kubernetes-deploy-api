"""Turn open pod log streams into one text stream for the HTTP response."""

import queue
import threading
from collections.abc import Iterator

from app.k8s_gateway import LogStream


def single_pod(stream: LogStream) -> Iterator[str]:
    try:
        for line in stream.lines():
            yield line + "\n"
    finally:
        stream.close()


def all_pods(streams: dict[str, LogStream], unavailable: dict[str, str], follow: bool) -> Iterator[str]:
    """Lines prefixed with their pod name. Without follow: pod by pod. With follow:
    one reader thread per pod feeds a queue, so lines appear as they are written."""
    width = max(len(name) for name in (*streams, *unavailable))

    def prefix(name: str, line: str) -> str:
        return f"{name:<{width}} | {line}\n"

    try:
        for name, reason in unavailable.items():
            yield prefix(name, f"[logs unavailable: {reason}]")
        if not follow:
            for name, stream in streams.items():
                for line in stream.lines():
                    yield prefix(name, line)
            return

        lines: queue.Queue = queue.Queue()
        finished = object()

        def pump(name: str, stream: LogStream) -> None:
            try:
                for line in stream.lines():
                    lines.put(prefix(name, line))
            except Exception as e:  # stream closed or connection dropped
                lines.put(prefix(name, f"[log stream ended: {e}]"))
            finally:
                lines.put(finished)

        for name, stream in streams.items():
            threading.Thread(target=pump, args=(name, stream), daemon=True).start()
        remaining = len(streams)
        while remaining:
            item = lines.get()
            if item is finished:
                remaining -= 1
            else:
                yield item
    finally:
        # Runs when the stream ends or the client goes away: stop reading from Kubernetes.
        for stream in streams.values():
            stream.close()
