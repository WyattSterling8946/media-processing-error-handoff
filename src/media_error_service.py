"""HTTP service joining processing error capture to creator delivery policy."""

from __future__ import annotations

import traceback
from typing import Callable, Literal, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .infrai_client import InfraiClient, InfraiError


class ProcessingFailure(BaseModel):
    failure_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    creator_id: str = Field(min_length=1)
    processing_stage: Literal["ingest", "transcode", "package"]
    source_format: str = Field(min_length=1)
    exception_type: str = Field(min_length=1)
    exception_message: str = Field(min_length=1)


class FailureDecision(BaseModel):
    failure_id: str
    error_group_id: str | None
    delivery_enabled: bool
    action: Literal["hold_for_review", "retry_processing"]


class ErrorBackend(Protocol):
    capture_exception: Callable[[dict, str], dict]
    delivery_enabled: Callable[[str], bool]


def handle_processing_failure(failure: ProcessingFailure, backend: ErrorBackend) -> FailureDecision:
    captured = backend.capture_exception(
        {
            "title": f"Media {failure.processing_stage} failed",
            "message": failure.exception_message,
            "level": "error",
            "fingerprint": [failure.asset_id, failure.processing_stage],
            "exception": {
                "type": failure.exception_type,
                "value": failure.exception_message,
                "stacktrace": traceback.format_stack(),
            },
            "context": {
                "failure_id": failure.failure_id,
                "asset_id": failure.asset_id,
                "creator_id": failure.creator_id,
                "processing_stage": failure.processing_stage,
                "source_format": failure.source_format,
            },
        },
        failure.failure_id,
    )
    enabled = backend.delivery_enabled(failure.creator_id)
    return FailureDecision(
        failure_id=failure.failure_id,
        error_group_id=captured.get("error_group_id"),
        delivery_enabled=enabled,
        action="hold_for_review" if enabled else "retry_processing",
    )


service = FastAPI(title="Media processing error service")


@service.post("/processing-failures", response_model=FailureDecision)
def capture_processing_failure(failure: ProcessingFailure) -> FailureDecision:
    try:
        return handle_processing_failure(failure, InfraiClient())
    except InfraiError as exc:
        caller_status = exc.status_code if 400 <= exc.status_code < 500 else 502
        raise HTTPException(status_code=caller_status, detail=exc.details) from exc
