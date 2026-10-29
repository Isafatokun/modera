package modera

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const testKey = "test-only-key-for-modera-client-not-a-deployment-secret"
const reqID = "req_0123456789abcdef0123456789abcdef"

func valid(kind string) string {
	return `{"request_id":"` + reqID + `","input_type":"` + kind + `","policy":{"id":"default","version":"1"},"decision":{"action":"allow","reasons":[]},"findings":[{"category":"toxicity","score":0.1,"source":"model"}],"coverage":{"evaluated":["toxicity"],"unsupported":[],"not_applicable":[]},"models":[{"id":"fixture/model","revision":"1"}],"preprocessing_version":"1"}`
}
func apiFailure(w http.ResponseWriter, status int, code string, retry bool) {
	w.Header().Set("X-Request-ID", reqID)
	w.WriteHeader(status)
	fmt.Fprintf(w, `{"error":{"code":%q,"retryable":%v,"request_id":%q}}`, code, retry, reqID)
}
func mustClient(t *testing.T, base string, options Options) *Client {
	t.Helper()
	c, err := New(base, testKey, options)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func TestTransientResponseIsRetriedAndImageBodyReplays(t *testing.T) {
	var calls int
	var bodies []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Header.Get("Authorization") != "Bearer "+testKey {
			t.Error("missing auth")
		}
		if err := r.ParseMultipartForm(9 << 20); err != nil {
			t.Error(err)
			return
		}
		defer r.MultipartForm.RemoveAll()
		file, _, err := r.FormFile("file")
		if err != nil {
			t.Error(err)
			return
		}
		data, _ := io.ReadAll(file)
		file.Close()
		bodies = append(bodies, string(data))
		if r.FormValue("policy") != "photos" {
			t.Error("policy changed")
		}
		if calls == 1 {
			w.Header().Set("Retry-After", "2")
			apiFailure(w, 503, "service_busy", true)
			return
		}
		fmt.Fprint(w, strings.Replace(valid("image"), `"id":"default"`, `"id":"photos"`, 1))
	}))
	defer server.Close()
	c := mustClient(t, server.URL, Options{})
	c.random = func() float64 { return 0.5 }
	var delays []time.Duration
	c.wait = func(_ context.Context, d time.Duration) error { delays = append(delays, d); return nil }
	result, err := c.ModerateImage(context.Background(), []byte("snapshot image bytes"), "photos")
	if err != nil || result == nil {
		t.Fatal(err)
	}
	if calls != 2 || len(bodies) != 2 || bodies[0] != bodies[1] || delays[0] < 2*time.Second {
		t.Fatal(calls, bodies, delays)
	}
}

func TestPermanentAndMalformedResponsesDoNotRetry(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   string
	}{
		{"bad_auth", 401, `{"error":{"code":"authentication_required","retryable":true,"request_id":"` + reqID + `"}}`},
		{"not_implemented", 501, `{"error":{"code":"moderation_not_implemented","retryable":false,"request_id":"` + reqID + `"}}`},
		{"html", 503, "<html>unavailable</html>"},
		{"invalid_result", 200, strings.Replace(valid("text"), `"score":0.1,`, "", 1)},
		{"null_score", 200, strings.Replace(valid("text"), `"score":0.1`, `"score":null`, 1)},
		{"coverage_gap", 200, strings.Replace(valid("text"), `"evaluated":["toxicity"]`, `"evaluated":["toxicity","threat"]`, 1)},
		{"false_allow", 200, strings.Replace(valid("text"), `"action":"allow"`, `"action":"missing"`, 1)},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				w.WriteHeader(tc.status)
				fmt.Fprint(w, tc.body)
			}))
			defer server.Close()
			c := mustClient(t, server.URL, Options{})
			result, err := c.ModerateText(context.Background(), "hello", "")
			if err == nil || result != nil || calls != 1 {
				t.Fatal(result, err, calls)
			}
		})
	}
}

func TestRetryAfterBeyondBudgetReturnsTypedErrorWithoutSleep(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Retry-After", "3600")
		apiFailure(w, 503, "service_busy", true)
	}))
	defer server.Close()
	c := mustClient(t, server.URL, Options{TotalTimeout: time.Second})
	c.wait = func(context.Context, time.Duration) error { t.Fatal("must not sleep beyond budget"); return nil }
	_, err := c.ModerateText(context.Background(), "hello", "")
	var api *APIError
	if !errors.As(err, &api) || calls != 1 || api.RetryAfter != time.Hour || api.Attempts != 1 {
		t.Fatal(err, calls)
	}
}

func TestContextCancelsActiveRequest(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		select {
		case <-r.Context().Done():
		case <-time.After(time.Second):
		}
	}))
	defer server.Close()
	c := mustClient(t, server.URL, Options{TotalTimeout: 100 * time.Millisecond})
	start := time.Now()
	result, err := c.ModerateText(context.Background(), "hello", "")
	if result != nil || !errors.Is(err, context.DeadlineExceeded) || calls.Load() != 1 || time.Since(start) > 2*time.Second {
		t.Fatal(result, err, calls.Load())
	}
}

func TestContextCancelsRetryWait(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Retry-After", "1")
		apiFailure(w, 503, "service_busy", true)
	}))
	defer server.Close()
	c := mustClient(t, server.URL, Options{TotalTimeout: 5 * time.Second})
	ctx, cancel := context.WithCancel(context.Background())
	c.wait = func(ctx context.Context, d time.Duration) error { cancel(); return waitContext(ctx, d) }
	_, err := c.ModerateText(ctx, "hello", "")
	if !errors.Is(err, context.Canceled) || calls != 1 {
		t.Fatal(err, calls)
	}
}

func TestAttemptLimitAndRequestIDMismatch(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; apiFailure(w, 503, "check_unavailable", true) }))
	defer server.Close()
	c := mustClient(t, server.URL, Options{MaxAttempts: 2})
	c.wait = func(context.Context, time.Duration) error { return nil }
	_, err := c.ModerateText(context.Background(), "hello", "")
	var api *APIError
	if !errors.As(err, &api) || api.Attempts != 2 || calls != 2 {
		t.Fatal(err, calls)
	}
	mismatch := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Request-ID", "req_ffffffffffffffffffffffffffffffff")
		fmt.Fprint(w, valid("text"))
	}))
	defer mismatch.Close()
	result, err := mustClient(t, mismatch.URL, Options{}).ModerateText(context.Background(), "hello", "")
	if result != nil || err == nil {
		t.Fatal("accepted inconsistent request ID")
	}
}

func TestRedirectNeverForwardsCredentials(t *testing.T) {
	targetCalls := 0
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { targetCalls++ }))
	defer target.Close()
	source := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, target.URL, 307) }))
	defer source.Close()
	_, err := mustClient(t, source.URL, Options{}).ModerateText(context.Background(), "hello", "")
	if err == nil || targetCalls != 0 {
		t.Fatal(err, targetCalls)
	}
}

func TestInputsAndSecrets(t *testing.T) {
	for _, base := range []string{"http://example.com", "https://user:password@example.com", "https://example.com?key=secret", "https://example.com#fragment"} {
		if _, err := New(base, testKey, Options{}); err == nil {
			t.Fatal("accepted invalid base URL")
		}
	}
	c := mustClient(t, "https://example.com", Options{})
	for _, text := range []string{" ", strings.Repeat("a", 4097), string([]byte{0xff})} {
		if _, err := c.ModerateText(context.Background(), text, ""); err == nil {
			t.Fatal("invalid text accepted")
		}
	}
	if _, err := c.ModerateImage(context.Background(), make([]byte, maxImageBytes+1), ""); err == nil {
		t.Fatal("oversized image accepted")
	}
	if strings.Contains(fmt.Sprintf("%v %+v %#v", c, c, c), testKey) {
		t.Fatal("key appears in formatted client")
	}
}

func TestRetryAfterDatesAndOversizedResponses(t *testing.T) {
	now := time.Now().UTC().Truncate(time.Second)
	if retryAfter(now.Add(3*time.Second).Format(http.TimeFormat), now) != 3*time.Second {
		t.Fatal("HTTP-date ignored")
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, strings.Repeat("x", maxResponseBytes+1)) }))
	defer server.Close()
	result, err := mustClient(t, server.URL, Options{}).ModerateText(context.Background(), "hello", "")
	if err == nil || result != nil {
		t.Fatal("oversized response accepted")
	}
}

func TestTextPreservesUnicodeAndURLRequestDoesNotFetchSource(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var body map[string]json.RawMessage
		if json.NewDecoder(r.Body).Decode(&body) != nil {
			t.Error("bad JSON")
			return
		}
		if calls == 1 {
			if !strings.Contains(string(body["input"]), "École") {
				t.Error("text altered")
			}
			fmt.Fprint(w, valid("text"))
		} else {
			fmt.Fprint(w, valid("image"))
		}
	}))
	defer server.Close()
	c := mustClient(t, server.URL, Options{})
	if _, err := c.ModerateText(context.Background(), "École\nbonjour", ""); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ModerateImageURL(context.Background(), "https://unreachable.invalid/image.jpg", ""); err != nil {
		t.Fatal(err)
	}
	if calls != 2 {
		t.Fatal(calls)
	}
}

func TestAttemptTimeoutCanRecoverWithinTotalBudget(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			select {
			case <-r.Context().Done():
			case <-time.After(time.Second):
			}
			return
		}
		fmt.Fprint(w, valid("text"))
	}))
	defer server.Close()
	c := mustClient(t, server.URL, Options{TotalTimeout: time.Second, AttemptTimeout: 100 * time.Millisecond})
	c.wait = func(context.Context, time.Duration) error { return nil }
	result, err := c.ModerateText(context.Background(), "hello", "")
	if err != nil || result == nil || calls.Load() != 2 {
		t.Fatal(result, err, calls.Load())
	}
}

func TestUnexpectedPolicyIsRejectedButExplicitFallbackIsPreserved(t *testing.T) {
	reply := strings.Replace(valid("text"), `"id":"default"`, `"id":"other"`, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, reply) }))
	defer server.Close()
	c := mustClient(t, server.URL, Options{})
	result, err := c.ModerateText(context.Background(), "hello", "")
	if result != nil || err == nil {
		t.Fatal("unexpected policy accepted")
	}
	reply = strings.Replace(reply, `"source":"model"`, `"source":"rules"`, 1)
	reply = strings.Replace(reply, `"preprocessing_version":"1"`, `"preprocessing_version":"1","rules_version":"reduced-1","fallback":true`, 1)
	result, err = c.ModerateText(context.Background(), "hello", "")
	if err != nil || result == nil || !result.Fallback || result.Policy.ID != "other" {
		t.Fatal(result, err)
	}
}

func TestConcurrentUseAndLongRetryAfter(t *testing.T) {
	if retryAfter("999999999999999999999999999999", time.Now()) != time.Duration(1<<63-1) {
		t.Fatal("overflowing retry delay was ignored")
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, valid("text")) }))
	defer server.Close()
	c := mustClient(t, server.URL, Options{})
	failures := make(chan error, 20)
	var group sync.WaitGroup
	for range 20 {
		group.Add(1)
		go func() {
			defer group.Done()
			_, err := c.ModerateText(context.Background(), "hello", "")
			failures <- err
		}()
	}
	group.Wait()
	close(failures)
	for err := range failures {
		if err != nil {
			t.Fatal(err)
		}
	}
}

func TestExplicitPrivateHTTPOption(t *testing.T) {
	if _, err := New("http://modera:8000", testKey, Options{AllowHTTP: true}); err != nil {
		t.Fatal(err)
	}
	if _, err := New("https://example.com?", testKey, Options{}); err == nil {
		t.Fatal("empty query accepted")
	}
}
