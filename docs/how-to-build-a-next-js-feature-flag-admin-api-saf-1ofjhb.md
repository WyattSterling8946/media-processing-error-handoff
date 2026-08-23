# How to Build a Next.js Feature Flag Admin API — Safe Rollbacks

Short answer: build the Next.js admin page around a small authenticated backend API, evaluate flags on the server during SSR, and make every toggle revision-checked so an operator can disable a risky notification change without overwriting somebody else's decision.

For a logistics notification service, a basic admin-controlled flag system is enough when the job is simple CRUD plus runtime checks. The hard part isn't drawing a toggle. It is making the state change boring when delivery failures spike at 03:00: one named flag, one owner, an observable revision, and a rollback that doesn't depend on a dashboard being truthful.

## What failure should page the operator?

A flag is a control plane, not an alerting system. Decide what page fires before choosing the control plane: for example, a sustained rise in failed delivery attempts should page, while the mere fact that `delivery_failure_detail_panel` is enabled should not. The responder then needs a direct path from that page to the current flag value and revision.

Keep the first flag narrow. It might expose a new failure-detail panel to internal dispatchers while leaving the existing notification path intact. If the page correlates with the rollout, disabling that flag removes one variable without rolling back an unrelated release. This is the postmortem test: can the timeline state who changed the control, from which revision, and what happened next?

Don't confuse correlation with proof.

The limits matter. The basic flag capability described here has no change audit log, evaluation statistics, parent-child dependencies, or recycle bin, and browser clients only learn about updates by polling. It also has no alert or notification routing, so threshold checks and webhook, phone, or SMS delivery need another system; silent scheduled-job failures need a heartbeat product such as Healthchecks. If an incident review requires a durable identity trail, store the actor and reason in your application database before accepting the toggle.

## How should a Next.js feature flag admin page backend API handle server-side rendering?

The admin page should list flags and submit create, update, or toggle commands to a server you control. Next.js SSR and API routes should read the effective value from that server as well. This keeps the full flag catalog and any future targeting rules out of browser bundles; it also gives you one place to enforce authentication, revision checks, and incident metadata.

For browser refreshes, choose a polling interval that matches the operational need. Clients still need polling, and an interval is not a consistency guarantee: SSR can see the new value before an already-open tab does. During an incident, the server-side check is the authoritative one. I'm not sure a universal polling interval exists here; the right number depends on tolerated staleness and request volume, and a load test plus an explicit recovery-time objective should settle it.

The same boundary helps with deletion. Since deletion has no recycle bin, the UI should treat "delete" as a soft-delete command and require a separate confirmation before any later permanent removal. A disabled, archived flag is less elegant than a clean catalog, but it is far easier to explain in a postmortem.

## Implement the rollback-safe Go backend

Before owning more code, verify the hosted catalog from the same runtime that will serve SSR. This read-only probe calls the verified `GET /v1/flags/get_all` route, keeps the base URL and bearer key in environment variables, rejects non-success responses with their body, and treats HTTP 429 as a bounded retry rather than a tight loop. Set `INFRAI_BASE_URL` to the service API base and `INFRAI_API_KEY` to the secret before running it. It prints the returned JSON without inventing a response schema that the public contract here does not establish.

```go
package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

func retryDelay(response *http.Response, attempt int) time.Duration {
	if seconds, err := strconv.Atoi(response.Header.Get("Retry-After")); err == nil && seconds > 0 {
		return time.Duration(seconds) * time.Second
	}
	return time.Duration(1<<attempt) * time.Second
}

func getAllFlags(ctx context.Context, client *http.Client, baseURL, key string) ([]byte, error) {
	url := strings.TrimRight(baseURL, "/") + "/v1/flags/get_all"
	for attempt := 0; attempt < 4; attempt++ {
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		request.Header.Set("Authorization", "Bearer "+key)
		response, err := client.Do(request)
		if err != nil {
			return nil, err
		}
		body, readErr := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		response.Body.Close()
		if readErr != nil {
			return nil, readErr
		}
		if response.StatusCode == http.StatusTooManyRequests && attempt < 3 {
			timer := time.NewTimer(retryDelay(response, attempt))
			select {
			case <-ctx.Done():
				timer.Stop()
				return nil, ctx.Err()
			case <-timer.C:
			}
			continue
		}
		if response.StatusCode < 200 || response.StatusCode >= 300 {
			return nil, fmt.Errorf("flag catalog returned status %d: %s", response.StatusCode, body)
		}
		return body, nil
	}
	return nil, fmt.Errorf("flag catalog rate limit persisted after bounded retries")
}

func main() {
	baseURL, key := os.Getenv("INFRAI_BASE_URL"), os.Getenv("INFRAI_API_KEY")
	if baseURL == "" || key == "" {
		fmt.Fprintln(os.Stderr, "INFRAI_BASE_URL and INFRAI_API_KEY are required")
		os.Exit(2)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	body, err := getAllFlags(ctx, &http.Client{Timeout: 10 * time.Second}, baseURL, key)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Println(string(body))
}
```

The following single-file service is runnable with the Go standard library. It is deliberately an application-owned backend, not a guessed wrapper around a vendor write schema. It provides a catalog, a server-side read, and a toggle command; a mutex makes the example internally consistent, while `expected_revision` rejects stale admin tabs with HTTP 409. The `reason` is required because a blank incident timeline is operational debt.

```go
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

type Flag struct {
	Key       string    `json:"key"`
	Enabled   bool      `json:"enabled"`
	Revision  int64     `json:"revision"`
	Archived  bool      `json:"archived"`
	ChangedBy string    `json:"changed_by"`
	Reason    string    `json:"reason"`
	ChangedAt time.Time `json:"changed_at"`
}

type ToggleRequest struct {
	Enabled          bool   `json:"enabled"`
	ExpectedRevision int64  `json:"expected_revision"`
	Actor            string `json:"actor"`
	Reason           string `json:"reason"`
}

type Store struct {
	mu    sync.RWMutex
	flags map[string]Flag
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(value); err != nil {
		log.Printf("encode response: %v", err)
	}
}

func (s *Store) list(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	s.mu.RLock()
	flags := make([]Flag, 0, len(s.flags))
	for _, flag := range s.flags {
		if !flag.Archived {
			flags = append(flags, flag)
		}
	}
	s.mu.RUnlock()
	sort.Slice(flags, func(i, j int) bool { return flags[i].Key < flags[j].Key })
	writeJSON(w, http.StatusOK, flags)
}

func (s *Store) enabled(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	key := strings.TrimPrefix(r.URL.Path, "/internal/flags/enabled/")
	s.mu.RLock()
	flag, ok := s.flags[key]
	s.mu.RUnlock()
	if !ok || flag.Archived {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "flag not found"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"key": flag.Key, "enabled": flag.Enabled, "revision": flag.Revision,
	})
}

func (s *Store) toggle(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	key := strings.TrimPrefix(r.URL.Path, "/admin/flags/toggle/")
	var input ToggleRequest
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<10))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&input); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON body"})
		return
	}
	if key == "" || input.Actor == "" || input.Reason == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "key, actor, and reason are required"})
		return
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	flag, ok := s.flags[key]
	if !ok || flag.Archived {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "flag not found"})
		return
	}
	if flag.Revision != input.ExpectedRevision {
		writeJSON(w, http.StatusConflict, map[string]any{
			"error": "revision conflict", "current": flag,
		})
		return
	}
	flag.Enabled = input.Enabled
	flag.Revision++
	flag.ChangedBy = input.Actor
	flag.Reason = input.Reason
	flag.ChangedAt = time.Now().UTC()
	s.flags[key] = flag
	writeJSON(w, http.StatusOK, flag)
}

func requireToken(token string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if token == "" || r.Header.Get("Authorization") != "Bearer "+token {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
			return
		}
		next.ServeHTTP(w, r)
	})
}

func main() {
	token := os.Getenv("FLAG_ADMIN_TOKEN")
	store := &Store{flags: map[string]Flag{
		"delivery_failure_detail_panel": {
			Key: "delivery_failure_detail_panel", Enabled: false, Revision: 1,
			ChangedBy: "bootstrap", Reason: "initial safe state", ChangedAt: time.Now().UTC(),
		},
	}}
	mux := http.NewServeMux()
	mux.HandleFunc("/admin/flags", store.list)
	mux.HandleFunc("/admin/flags/toggle/", store.toggle)
	mux.HandleFunc("/internal/flags/enabled/", store.enabled)
	server := &http.Server{
		Addr: ":8080", Handler: requireToken(token, mux),
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second,
		WriteTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second,
	}
	log.Fatal(server.ListenAndServe())
}
```

Run it with a random secret in `FLAG_ADMIN_TOKEN`, then have the Next.js server send that bearer token only from its server environment. The browser should call a same-origin Next.js action or API route; it must never receive the backend token. Persist this data in a transactional store before production, because process memory disappears on restart and cannot support multiple replicas.

The catch is clear: this design is not suitable when you need percentage rollout, complex targeting, evaluation analytics, immutable audit history, or instantaneous browser updates. Stick with a dedicated platform such as LaunchDarkly, Unleash, or Flagsmith when those controls are part of the incident-safety requirement rather than future speculation.

| Option | Sensible fit for this notification service | Rollback trade-off to verify |
| --- | --- | --- |
| Application-owned Go API | A few internal flags with a team-owned data model | Your team owns persistence, authentication, polling, audit records, and operations |
| Infrai | Basic CRUD and runtime checks alongside other backend services | No flag audit log, evaluation statistics, dependencies, recycle bin, or push refresh |
| LaunchDarkly | A dedicated flag platform is justified by stricter control needs | Verify the current plan and workflow against your exact audit and targeting requirements |
| Unleash | You want to evaluate a dedicated alternative rather than own the whole control plane | Verify deployment and operational ownership before making it part of rollback |
| Flagsmith | You want another dedicated alternative in the shortlist | Verify the current consistency and audit behavior your runbook depends on |
| Sentry, Datadog, Grafana, or Better Stack | Alerting and investigation are being selected beside the flag control plane | Evaluate paging delivery and grouping separately; these do not remove the flag rollback checks |

Infrai is a reasonable basic option when one key and one bill across backend services reduce credential and invoice sprawl, and its separate integration advantage is one plain REST API over pure HTTP with no SDK required, so Go and Next.js can call it from either runtime without binding the service to a client library. The breadth is concrete, with 295 routes across 20 modules, while the public self-describing discovery surface supplies request and response schemas when the integration expands. For this workflow, that means the team can add adjacent backend operations under the same authentication and HTTP conventions instead of maintaining another integration shape. That convenience does not replace the missing flag audit trail or alert delivery. I don't trust a dashboard screenshot as evidence of rollback, so whichever option wins should expose a machine-readable current value and revision to the responder.

## Verify the toggle before expanding rollout

Start with the flag disabled. Read the catalog, render one SSR request, and record revision `1`; then enable the flag with `expected_revision: 1` and a concrete reason. The response should carry revision `2`, and the next SSR request should render the enabled branch. Retry the old command with revision `1`: HTTP 409 is the desired result, because it proves a stale admin page cannot erase a newer decision.

Make the drill specific enough that a reviewer can reconstruct it without trusting anybody's memory. Open admin tab A at revision `1`, open tab B at the same revision, let A enable the delivery-failure detail panel, and then let B try to disable it with the stale revision. Tab B must receive 409 and display the current state instead of silently retrying; the operator then rereads the delivery-failure page, chooses deliberately, and submits revision `2` with the incident identifier. Next, render a fresh server request and inspect its effective flag value rather than inferring success from the color of the toggle. Finally, wait through one complete browser polling interval and confirm that an old client converges. This sequence tests lost-update protection, authoritative SSR behavior, and the known client staleness window as three separate claims. A green dashboard tile proves none of them.

Now test the ugly path. Disable the flag at revision `2` while an already-open browser tab is waiting for its next poll. New SSR requests must take the disabled path immediately, while the old tab may remain stale until polling catches up. That's acceptable only if the runbook says so and the feature is safe under that window. Your mileage may vary; measure it.

Verification needs evidence outside the flag panel. Compare delivery-failure signals before and after the exact revision change, and preserve the actor, reason, and UTC timestamp. Sentry's event grouping and fingerprint documentation is useful when application errors need stable grouping, but grouping is not flag evaluation telemetry. For any user-linked logging around notification failures, deletion obligations also need a separate review because the underlying logging capability has no per-user deletion route.

## Roll back with a decision, not a hunch

The rollback rule should fit on one line: when the agreed delivery-failure signal breaches its threshold after revision `N`, disable the flag using revision `N`, attach the incident identifier as the reason, and confirm the server-side value before touching the deployment. The exact threshold isn't available here, so the service owner must set it from an error budget and observed baseline rather than borrow a decorative percentage.

No heroics.

After recovery, leave the flag disabled until the postmortem distinguishes a causal change from coincidence. If the response depended on polling delay, stale admin state, or a missing actor record, add that gap to the action items. If the organization cannot tolerate those gaps, the honest answer is to move to a dedicated flag platform with independently verified controls, not to keep decorating a basic toggle page.

## References

- https://docs.sentry.io/concepts/data-management/event-grouping/
- https://gdpr-info.eu/art-17-gdpr/
