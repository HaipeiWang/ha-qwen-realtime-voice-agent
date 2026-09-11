"""Complete JSON Schema is retained, including constraints and nested fields."""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator


@dataclass(frozen=True)
class CanonicalTool:
    name: str
    description: str
    parameters: dict[str, Any] = field(repr=False)
    read_only: bool = False

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Tool name must not be empty")
        if not isinstance(self.parameters, dict) or self.parameters.get("type") != "object":
            raise ValueError("Tool parameters must be an object JSON Schema")
        Draft202012Validator.check_schema(self.parameters)
        object.__setattr__(self, "parameters", deepcopy(self.parameters))

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        Draft202012Validator(self.parameters).validate(arguments)


@dataclass(frozen=True)
class CanonicalToolRequest:
    conversation_id: str
    turn_id: str
    call_id: str
    name: str
    arguments: dict[str, Any] = field(repr=False)
    targets: tuple[str, ...] = ()
    source: str = "model"


@dataclass(frozen=True)
class CanonicalToolResult:
    """Result evidence, distinct from transport acknowledgement or model wording."""

    execution_id: str
    status: str  # completed, accepted, failed, partial, unknown
    verified: bool = False
    successful_targets: tuple[str, ...] = ()
    failed_targets: tuple[str, ...] = ()
    detail: str = field(default="", repr=False)
    data: Any = field(default=None, repr=False)

    def __post_init__(self):
        if self.status not in {"completed", "accepted", "failed", "partial", "unknown"}:
            raise ValueError("Invalid canonical tool result status")
        if self.verified and self.status not in {"completed", "partial"}:
            raise ValueError("Only completed or partially completed actions can be verified")

    @property
    def permits_success_confirmation(self) -> bool:
        return self.status == "completed"
