from src.media_error_service import ProcessingFailure, handle_processing_failure


class RecordingBackend:
    def __init__(self, delivery_enabled: bool) -> None:
        self.enabled = delivery_enabled
        self.capture = None

    def capture_exception(self, payload: dict, failure_id: str) -> dict:
        self.capture = (payload, failure_id)
        return {"error_group_id": "group-transcode-7"}

    def delivery_enabled(self, creator_id: str) -> bool:
        assert creator_id == "creator-42"
        return self.enabled


def test_active_creator_delivery_holds_failed_asset_for_review() -> None:
    backend = RecordingBackend(delivery_enabled=True)
    failure = ProcessingFailure(
        failure_id="failure-001",
        asset_id="asset-1080p",
        creator_id="creator-42",
        processing_stage="transcode",
        source_format="video/quicktime",
        exception_type="CodecError",
        exception_message="audio stream could not be decoded",
    )

    decision = handle_processing_failure(failure, backend)

    payload, idempotency_key = backend.capture
    assert payload["fingerprint"] == ["asset-1080p", "transcode"]
    assert idempotency_key == "failure-001"
    assert decision.error_group_id == "group-transcode-7"
    assert decision.action == "hold_for_review"
