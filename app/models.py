"""Request and response shapes of the REST API (pydantic models).

Validation rules live in app/validation.py; the validators below just call them,
so a bad request becomes a list of field-level errors in a single 400 response.
"""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import validation
from app.constants import DEFAULT_NAMESPACE, DEFAULT_REPLICAS, MAX_REPLICAS


# ---------- Requests ----------

class EnvVar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return validation.validate_env_name(v)


class ResourceValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpu: str | None = None
    memory: str | None = None

    @field_validator("cpu", "memory")
    @classmethod
    def _quantity(cls, v: str | None) -> str | None:
        if v is not None:
            validation.parse_positive_quantity(v)
        return v


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: ResourceValues | None = None
    limits: ResourceValues | None = None

    @model_validator(mode="after")
    def _requests_within_limits(self) -> "Resources":
        if self.requests and self.limits:
            validation.validate_requests_within_limits(
                self.requests.model_dump(exclude_none=True),
                self.limits.model_dump(exclude_none=True),
            )
        return self


class CreateDeploymentRequest(BaseModel):
    """Body of POST /api/deployments. Only name and image are required."""

    model_config = ConfigDict(extra="forbid")

    name: str
    image: str
    namespace: str = DEFAULT_NAMESPACE
    replicas: int = Field(default=DEFAULT_REPLICAS, ge=0, le=MAX_REPLICAS)
    port: int | None = Field(default=None, ge=1, le=65535)
    resources: Resources | None = None
    env: list[EnvVar] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    command: list[str] | None = None
    args: list[str] | None = None

    @field_validator("name", "namespace")
    @classmethod
    def _dns_label(cls, v: str) -> str:
        return validation.validate_dns_label(v)

    @field_validator("image")
    @classmethod
    def _image(cls, v: str) -> str:
        return validation.validate_image(v)

    @field_validator("labels")
    @classmethod
    def _labels(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            validation.validate_label_key(key)
            validation.validate_label_value(value)
        return v

    @field_validator("env")
    @classmethod
    def _unique_env(cls, v: list[EnvVar]) -> list[EnvVar]:
        names = [e.name for e in v]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate environment variable(s): {', '.join(duplicates)}")
        return v

    @field_validator("command")
    @classmethod
    def _command(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and (not v or any(not part for part in v)):
            raise ValueError("must be a non-empty list of non-empty strings")
        return v


class PatchDeploymentRequest(BaseModel):
    """Body of PATCH /api/deployments/{id}: scale and/or change the image."""

    model_config = ConfigDict(extra="forbid")

    replicas: int | None = Field(default=None, ge=0, le=MAX_REPLICAS)
    image: str | None = None

    @field_validator("image")
    @classmethod
    def _image(cls, v: str | None) -> str | None:
        return validation.validate_image(v) if v is not None else v

    @model_validator(mode="after")
    def _at_least_one(self) -> "PatchDeploymentRequest":
        if self.replicas is None and self.image is None:
            raise ValueError("provide at least one of: replicas, image")
        return self


# ---------- Responses ----------

class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILING = "FAILING"
    PROGRESSING = "PROGRESSING"
    UNKNOWN = "UNKNOWN"


class Condition(BaseModel):
    type: str
    status: str
    reason: str | None = None
    message: str | None = None
    last_update_time: datetime | None = None
    last_transition_time: datetime | None = None


class ReplicaCounts(BaseModel):
    desired: int
    current: int
    updated: int
    ready: int
    available: int
    unavailable: int


class ContainerStatus(BaseModel):
    name: str
    image: str
    ready: bool
    restart_count: int
    state: Literal["waiting", "running", "terminated", "unknown"]
    reason: str | None = None
    message: str | None = None
    exit_code: int | None = None


class PodStatus(BaseModel):
    name: str
    phase: str
    node: str | None = None
    ready: bool
    restart_count: int
    reason: str | None = Field(default=None, description="Pod-level reason, e.g. Unschedulable or Evicted")
    message: str | None = None
    containers: list[ContainerStatus]
    created_at: datetime | None = None


class DeploymentSummary(BaseModel):
    id: str = Field(description="The Deployment's Kubernetes UID")
    name: str
    namespace: str
    image: str
    labels: dict[str, str]
    replicas: ReplicaCounts
    health: HealthStatus
    health_reason: str
    conditions: list[Condition]
    created_at: datetime | None = None
    last_updated_at: datetime | None = None
    restarted_at: datetime | None = Field(default=None, description="Last rollout restart, if any")


class DeploymentDetail(DeploymentSummary):
    pods: list[PodStatus]


class DeploymentList(BaseModel):
    items: list[DeploymentSummary]
    count: int


class DeleteResponse(BaseModel):
    id: str
    name: str
    namespace: str
    status: Literal["DELETING"] = "DELETING"
    propagation_policy: str = "Foreground"


class ErrorDetail(BaseModel):
    field: str | None = None
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorBody
