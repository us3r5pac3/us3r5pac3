from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class Assertion(BaseModel):
    """A single check to run against the model response."""

    kind: Literal[
        "contains",
        "not_contains",
        "regex",
        "equals",
        "json_schema",
        "max_latency_ms",
        "max_tokens",
        "min_tokens",
        "refusal",
    ]
    value: Any = None


class TestCase(BaseModel):
    __test__ = False  # tell pytest not to collect this as a test class

    id: str
    description: str | None = None
    messages: list[Message]
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=1)
    seed: int | None = None
    tags: list[str] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)


class TestSuite(BaseModel):
    __test__ = False  # tell pytest not to collect this as a test class

    name: str
    cases: list[TestCase]


class CompletionResult(BaseModel):
    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    finish_reason: str | None = None
    request_id: str | None = None


class AssertionOutcome(BaseModel):
    assertion: Assertion
    passed: bool
    detail: str | None = None


class CaseOutcome(BaseModel):
    case_id: str
    tags: list[str]
    passed: bool
    completion: CompletionResult | None
    assertions: list[AssertionOutcome]
    error: str | None = None
