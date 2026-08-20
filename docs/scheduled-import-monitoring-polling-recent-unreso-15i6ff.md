# Scheduled Import Monitoring — Polling Recent Unresolved Errors Without Alert Noise

Short answer: poll the error tracker on a fixed schedule, but page only when recent unresolved errors coincide with missing import results across multiple expected windows; deduplicate on incident state, then send the same evidence-rich notification to Slack and email.

An error count alone is a poor proxy for a stopped logistics import. A healthy importer can emit recoverable row errors while continuing to publish shipments, and a dead importer can emit nothing at all. I carry the pager, and I've been woken by alerts that meant nothing and missed the one that mattered. The useful question at 3 a.m. isn't “did an error occur?” It is “what page fired, and what evidence says the scheduled job stopped producing results?”

This note treats signal quality as the primary constraint. The polling process is deliberately small, the alert rule is stateful, and the notification includes enough evidence to decide whether to investigate without opening a dashboard first.

## How should a cron job poll recent unresolved errors without built-in alerting?

Run a separate monitor after each expected import window. It queries a narrow time range from the tracking API, reads the import's success marker from the system of record, and evaluates both signals together. “Recent” must be derived from the scheduler's expected completion time, not from an arbitrary dashboard range. For a job due every 15 minutes, a reasonable starting policy might allow a 10-minute completion grace period and require two missed result windows before paging; those are operating choices, not universal constants, so tune them from actual import duration and recovery data.

The invariant is simple: **the monitor must observe production, not mere execution**. A cron process exiting zero proves only that its process reached a successful exit. For a logistics feed, production might mean a committed batch ID, a nonzero count of accepted shipment records, or an advancing source watermark. Pick one durable marker that downstream consumers also trust.

Keep the polling cursor and the notification cursor separate. The first bounds what the next API request reads. The second records which incident state has already produced an alert. If one timestamp serves both jobs, a successful poll followed by a failed notification can advance past evidence that nobody saw. Retries then look quiet even though the incident remains open.

Here is the decision table I would put in the runbook before writing code:

| Import result | Recent unresolved errors | Action |
|---|---:|---|
| Advancing | Any | Record diagnostics; do not page for stoppage |
| Missing for one window | None | Wait through the configured grace policy |
| Missing for repeated windows | Present | Page with batch, window, and error evidence |
| Missing for repeated windows | None | Page as silent failure; the absence of errors is evidence too |

That last row matters most. Polling only for unresolved errors cannot detect a scheduler that never started, a credential rejection before the tracking client initialized, or a process killed before it reported. The result marker closes that blind spot.

Quiet is not healthy.

## Alert on incident transitions, not repeated observations

Google's SRE monitoring guidance separates symptoms from causes and argues that paging should be tied to urgent, user-visible conditions. In this case the symptom is stale logistics data, while an unresolved exception is supporting cause evidence. Treating every exception as a page reverses that relationship and hands the on-call engineer a stream of causes with no statement of impact.

Model the monitor as a tiny state machine: healthy, pending, firing, and recovered. Healthy moves to pending after one late window. Pending moves to firing after the configured consecutive miss count. Firing stays firing without sending another page unless the incident fingerprint changes materially. A fresh successful result moves any non-healthy state to recovered, sends one recovery notification, then returns to healthy on the next run. This makes Slack and email delivery a consequence of a transition rather than a consequence of every five-minute poll.

Don't fingerprint on the raw error message. IDs, timestamps, file names, and row numbers often change on every occurrence, turning one failure into hundreds of “new” incidents. A useful fingerprint combines stable dimensions such as importer name, tenant or depot, expected schedule, and failure class. Keep high-cardinality details in the notification body, where they help diagnosis without controlling deduplication.

No dashboard gets a vote here.

The notification should say when the last successful batch completed, which expected windows were missed, how many unresolved errors were returned, the oldest error time, and a correlation or batch identifier. Slack is good for shared awareness; email is useful as an independent delivery path and a durable record. Sending both does not justify counting both as separate incidents, and delivery failure should be retried from an outbox rather than changing the monitoring state back to healthy.

## A Go polling path with explicit failure boundaries

The following Go sketch assumes an internal error-tracking adapter returns a stable JSON contract. `ERROR_API_URL` is the configured collection endpoint; no vendor route is implied. The result probe is another adapter backed by the import system of record. The example concentrates on the boundary that tends to go wrong: it does not acknowledge an incident until every configured notifier accepts the message.

```go
package monitor

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

type Error struct {
	ID         string    `json:"id"`
	OccurredAt time.Time `json:"occurred_at"`
	Class      string    `json:"class"`
}

type ErrorPage struct {
	Items []Error `json:"items"`
}

type IncidentState struct {
	ConsecutiveMisses int
	LastNotifiedKey   string
}

type ResultProbe interface {
	LastCompleted(ctx context.Context, importer string) (time.Time, error)
}

type StateStore interface {
	Load(ctx context.Context, importer string) (IncidentState, error)
	Save(ctx context.Context, importer string, state IncidentState) error
}

type Notifier interface {
	Send(ctx context.Context, subject, body string) error
}

type Monitor struct {
	Client        *http.Client
	ErrorAPI      string
	Probe         ResultProbe
	Store         StateStore
	Notifiers     []Notifier // Configure Slack and email implementations.
	Grace         time.Duration
	MissesToAlert int
}

func (m *Monitor) Run(ctx context.Context, importer string, now time.Time) error {
	lastCompleted, err := m.Probe.LastCompleted(ctx, importer)
	if err != nil {
		return fmt.Errorf("read last completed import: %w", err)
	}

	state, err := m.Store.Load(ctx, importer)
	if err != nil {
		return fmt.Errorf("load monitor state: %w", err)
	}

	cutoff := now.Add(-m.Grace)
	if !lastCompleted.Before(cutoff) {
		state.ConsecutiveMisses = 0
		state.LastNotifiedKey = ""
		return m.Store.Save(ctx, importer, state)
	}

	state.ConsecutiveMisses++
	errors, err := m.unresolvedSince(ctx, cutoff)
	if err != nil {
		return fmt.Errorf("poll unresolved errors: %w", err)
	}
	if state.ConsecutiveMisses < m.MissesToAlert {
		return m.Store.Save(ctx, importer, state)
	}

	key := importer + ":stalled:" + lastCompleted.UTC().Format(time.RFC3339)
	if key == state.LastNotifiedKey {
		return m.Store.Save(ctx, importer, state)
	}

	subject := fmt.Sprintf("Import stalled: %s", importer)
	body := fmt.Sprintf(
		"last completed=%s; consecutive misses=%d; recent unresolved errors=%d",
		lastCompleted.UTC().Format(time.RFC3339),
		state.ConsecutiveMisses,
		len(errors),
	)
	for _, notifier := range m.Notifiers {
		if err := notifier.Send(ctx, subject, body); err != nil {
			return fmt.Errorf("send alert: %w", err)
		}
	}

	state.LastNotifiedKey = key
	return m.Store.Save(ctx, importer, state)
}

func (m *Monitor) unresolvedSince(ctx context.Context, since time.Time) ([]Error, error) {
	u, err := url.Parse(m.ErrorAPI)
	if err != nil {
		return nil, fmt.Errorf("parse error API URL: %w", err)
	}
	q := u.Query()
	q.Set("status", "unresolved")
	q.Set("since", since.UTC().Format(time.RFC3339))
	q.Set("limit", strconv.Itoa(100))
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := m.Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected error API status %d", resp.StatusCode)
	}

	var page ErrorPage
	if err := json.NewDecoder(resp.Body).Decode(&page); err != nil {
		return nil, err
	}
	return page.Items, nil
}
```

Set a client timeout, cap every page, and follow the adapter's documented pagination contract before deploying this. The snippet deliberately leaves Slack and email behind the same `Notifier` interface because transport code should not decide whether an incident exists. In production, give each destination an idempotency key, persist an outbox entry and monitor delivery age. Otherwise a brief mail or chat outage can either lose the page or create a retry storm.

Test the transitions with a fake clock and recorded adapter responses: one miss must not fire, the threshold miss must fire once, the next identical poll must remain quiet, a changed stable fingerprint may fire, and a fresh result must recover. Then run a canary that writes a synthetic completed-import marker. I'm not sure which grace interval fits a given carrier feed without its duration distribution and service objective; those two inputs should settle the choice, not instinct.

Watch the monitor itself. Record polling latency, adapter failures, notification-outbox age, and time since the last successful evaluation. Log volume has a real operating cost; for example, CloudWatch publishes per-GB ingestion pricing, so retaining full API payloads on every poll is a questionable default. Store compact decision evidence routinely and preserve full payloads only under a defined sampling or incident policy.

## Where this design stops fitting

Polling is **not suitable when detection must be faster than a defensible poll interval**, when the API cannot query by time and resolution state, or when its pagination cannot provide a consistent enough view. In those cases, prefer an event stream or webhook feeding the same state machine. Keep scheduled reconciliation anyway if silent event loss is within the threat model; the fast path and the completeness path solve different problems.

Also avoid a local state file when cron can overlap, move between hosts, or run in ephemeral compute. Use a store with compare-and-set or a lease so two evaluators cannot page for the same transition. If the tracking API already provides well-tested alert rules, deduplication, delivery retries, and recovery messages that match the import's service objective, stick with that built-in path. A custom poller buys control, but it also creates another monitor whose clock, credentials, storage, and delivery path now need ownership.

The postmortem test is blunt: could the alert explain the customer-visible symptom, could a retry send it twice, and could the job disappear without emitting an error? If any answer is uncomfortable, the page isn't ready.

## References

- https://sre.google/sre-book/monitoring-distributed-systems/
- https://aws.amazon.com/cloudwatch/pricing/
