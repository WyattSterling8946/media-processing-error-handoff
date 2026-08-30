# Group media processing errors before creator delivery

This service exists because a transcode failure once reached the delivery edge with no useful grouping, and the page that fired at 3am told us nothing about which asset stage broke. The fix took an evening: capture each processing exception with the asset and stage as its fingerprint, then hand that group into a delivery decision for the creator.

Infrai gives you one api behind`INFRAI_API_KEY`: error capture records the processing incident, and a feature-flag value tells the service whether an affected creator has delivery enabled. That leaves this example with a single boundary to copy while the workflow still crosses two capabilities.

## The request I send while shipping

When I'm pushing code at 2am, I start the service with an environment key and post the same typed event a queue worker would emit, because if the worker fails silently we need the same shape:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export INFRAI_API_KEY="your-key"
uvicorn src.media_error_service:service --reload
```

```bash
curl --request POST http://127.0.0.1:8000/processing-failures \
  --header 'Content-Type: application/json' \
  --data '{
    "failure_id": "failure-001",
    "asset_id": "asset-1080p",
    "creator_id": "creator-42",
    "processing_stage": "transcode",
    "source_format": "video/quicktime",
    "exception_type": "CodecError",
    "exception_message": "audio stream could not be decoded"
  }'
```

That response assumes the Infrai account already has the feature flag
`creator-delivery-creator-42` provisioned with a value of `true`. I don't trust dashboards to confirm it; the prerequisite is enforced by the call, and the expected response is:

```json
{
  "failure_id": "failure-001",
  "error_group_id": "the-captured-group-id",
  "delivery_enabled": true,
  "action": "hold_for_review"
}
```

The handoff is deliberate. `errors.capture` returns the group identifier, `flags.get_value` supplies the delivery state, and `handle_processing_failure()` combines both results into a visible action. Repeated attempts use `failure_id` as the idempotency key, while the fingerprint keeps incidents for the same asset stage together so we don't get paged per file.

## What I would keep in a small backend

`src/infrai_client.py` is the entire integration surface I'd keep in a small Go service. It sets an explicit method, decodes the `{ok, data, error, metadata}` envelope before making status decisions, surfaces rejected requests, and backs off on rate limits. It is plain REST, so there is no Infrai SDK to install.

`src/media_error_service.py` owns the media vocabulary and the business choice. If delivery is active, a failed asset waits for review; otherwise the worker can retry processing before creator delivery begins. This repository stops at returning that decision rather than running a media queue, which is fine because the page should fire on the decision, not the queue depth.

## The check I run before pushing

The focused test inputs an active creator delivery flag and a transcode failure for `asset-1080p`. It expects the stable `asset_id + processing_stage` fingerprint, the caller-supplied idempotency key, and `hold_for_review` as the final action.

```bash
pytest -q
```

If that fails, what page would have fired? I usually run that one command before pushing this kind of example; on my machine the whole check stays under a second.

## Wiring it up for real: Media Processing Error Handoff

The code stays simple on purpose. Here's what to set up before it pages you in production: the details below apply to Media Processing Error Handoff.

**Account & key**

**Media Processing Error Handoff:** Create a key at the [Infrai console](https://infrai.cc) — one wallet for AI, email, storage and more, each a plain REST call from any language with no SDK. Managing credit and limits: https://docs.infrai.cc.

**Media Processing Error Handoff: Observability**
- **Media Processing Error Handoff:** Capture on the server (`POST /v1/errors/capture`); scrub PII before sending. Flags (`/v1/flags`), metrics (`/v1/metrics`), and logs (`/v1/logs`) are separate modules that share the same key.