"""Kubernetes-specific input rules.

Pure functions: each returns the (possibly normalized) value or raises ValueError
with a human-readable message. The pydantic models in app/models.py call them, so
every rule is enforced in one place and reported as a structured 400.
"""

import re
from decimal import Decimal

from kubernetes.utils import parse_quantity

from app.constants import RESERVED_LABELS

# RFC 1123 label: what Kubernetes requires for namespace names, and what we
# require for deployment names (they become part of pod names and labels).
_DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS1123_LABEL_MAX = 63

# Container image reference: [registry[:port]/]path[:tag][@sha256:digest]
_IMAGE_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_IMAGE_DOMAIN = r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*(?::[0-9]+)?"
_IMAGE_RE = re.compile(
    rf"^(?:{_IMAGE_DOMAIN}/)?{_IMAGE_COMPONENT}(?:/{_IMAGE_COMPONENT})*"
    r"(?::[\w][\w.-]{0,127})?"
    r"(?:@sha256:[a-f0-9]{64})?$"
)

# Environment variable names: C identifier style.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Label keys: optional DNS subdomain prefix + "/" + name; values: name or empty.
_LABEL_NAME = re.compile(r"^([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9]$")
_DNS1123_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")


def validate_dns_label(value: str) -> str:
    if not value:
        raise ValueError("must not be empty")
    if len(value) > _DNS1123_LABEL_MAX:
        raise ValueError(f"must be at most {_DNS1123_LABEL_MAX} characters")
    if not _DNS1123_LABEL.match(value):
        raise ValueError(
            "must be a DNS-1123 label: lowercase letters, digits and '-', "
            "starting and ending with a letter or digit"
        )
    return value


def validate_image(value: str) -> str:
    if not value or not _IMAGE_RE.match(value):
        raise ValueError(
            "must be a valid image reference, e.g. 'nginx:1.27' or 'registry.example.com/team/app:v1'"
        )
    return value


def parse_positive_quantity(value: str) -> Decimal:
    """Parse a Kubernetes quantity ('100m', '0.5', '128Mi') that must be > 0."""
    try:
        quantity = parse_quantity(value)
    except (ValueError, TypeError):
        raise ValueError(f"'{value}' is not a valid Kubernetes quantity (e.g. '250m', '0.5', '128Mi')")
    if quantity <= 0:
        raise ValueError(f"'{value}' must be greater than zero")
    return quantity


def validate_requests_within_limits(requests: dict[str, str], limits: dict[str, str]) -> None:
    """A request above its limit is rejected by Kubernetes; catch it early with a clear message."""
    for resource in ("cpu", "memory"):
        if resource in requests and resource in limits:
            if parse_positive_quantity(requests[resource]) > parse_positive_quantity(limits[resource]):
                raise ValueError(
                    f"{resource} request ({requests[resource]}) must not exceed its limit ({limits[resource]})"
                )


def validate_env_name(value: str) -> str:
    if not _ENV_NAME.match(value):
        raise ValueError(f"'{value}' is not a valid environment variable name")
    return value


def validate_label_key(key: str) -> str:
    if key in RESERVED_LABELS:
        raise ValueError(f"label '{key}' is reserved by the API")
    prefix, _, name = key.rpartition("/")
    if prefix and (len(prefix) > 253 or not _DNS1123_SUBDOMAIN.match(prefix)):
        raise ValueError(f"label key '{key}' has an invalid prefix")
    if not name or len(name) > 63 or not _LABEL_NAME.match(name):
        raise ValueError(f"label key '{key}' is invalid")
    return key


def validate_label_value(value: str) -> str:
    if value and (len(value) > 63 or not _LABEL_NAME.match(value)):
        raise ValueError(f"label value '{value}' is invalid (max 63 chars: letters, digits, '-', '_', '.')")
    return value
