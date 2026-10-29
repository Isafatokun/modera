// Package modera provides a bounded, synchronous client for the Modera API.
package modera

import (
	"bytes"
	"context"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"mime/multipart"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

const maxImageBytes = 8 * 1024 * 1024
const maxResponseBytes = 1024 * 1024

var policyPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)
var requestPattern = regexp.MustCompile(`^req_[0-9a-f]{32}$`)
var codePattern = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
var categoryPattern = regexp.MustCompile(`^[a-z][a-z0-9_./-]{0,63}$`)

// Options bounds all attempts, including retry waits, by TotalTimeout and the
// caller's earlier context deadline. Zero fields select defaults. AttemptTimeout
// zero means each attempt uses the remaining total budget.
type Options struct {
	MaxAttempts    int
	TotalTimeout   time.Duration
	AttemptTimeout time.Duration
	BaseDelay      time.Duration
	MaxDelay       time.Duration
	HTTPClient     *http.Client
	AllowHTTP      bool
}

// Client is safe for concurrent use. It retains no moderation results.
type Client struct {
	endpoint string
	apiKey   string
	options  Options
	http     *http.Client
	random   func() float64
	now      func() time.Time
	wait     func(context.Context, time.Duration) error
}

func (c *Client) String() string   { return "modera.Client{credentials:redacted}" }
func (c *Client) GoString() string { return c.String() }

// APIError is a valid API error. Error intentionally omits untrusted message text.
type APIError struct {
	Status     int
	Code       string
	RequestID  string
	Retryable  bool
	RetryAfter time.Duration
	Attempts   int
}

func (e *APIError) Error() string {
	return fmt.Sprintf("modera: HTTP %d %s (request %s, attempts %d)", e.Status, e.Code, e.RequestID, e.Attempts)
}

// ProtocolError means no trustworthy moderation decision was received.
type ProtocolError struct{ Status int }

func (e *ProtocolError) Error() string {
	return fmt.Sprintf("modera: invalid API response (HTTP %d)", e.Status)
}

// New requires HTTPS except for loopback or explicit Options.AllowHTTP.
// API keys, URL credentials, query strings and fragments are never put in errors.
func New(baseURL, apiKey string, options Options) (*Client, error) {
	u, err := url.Parse(baseURL)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.Opaque != "" || (u.Scheme != "https" && u.Scheme != "http") {
		return nil, errors.New("modera: invalid service base URL")
	}
	if u.Scheme == "http" && !options.AllowHTTP {
		ip := net.ParseIP(u.Hostname())
		if u.Hostname() != "localhost" && (ip == nil || !ip.IsLoopback()) {
			return nil, errors.New("modera: HTTPS is required outside loopback")
		}
	}
	if len(apiKey) < 32 || len(apiKey) > 512 {
		return nil, errors.New("modera: invalid API key")
	}
	for _, r := range apiKey {
		if r < 33 || r > 126 {
			return nil, errors.New("modera: invalid API key")
		}
	}
	if options.MaxAttempts == 0 {
		options.MaxAttempts = 3
	}
	if options.TotalTimeout == 0 {
		options.TotalTimeout = 30 * time.Second
	}
	if options.BaseDelay == 0 {
		options.BaseDelay = 200 * time.Millisecond
	}
	if options.MaxDelay == 0 {
		options.MaxDelay = 2 * time.Second
	}
	if options.MaxAttempts < 1 || options.MaxAttempts > 5 || options.TotalTimeout <= 0 || options.AttemptTimeout < 0 || options.BaseDelay < 0 || options.MaxDelay < options.BaseDelay {
		return nil, errors.New("modera: invalid retry options")
	}
	var h http.Client
	if options.HTTPClient != nil {
		h = *options.HTTPClient
	}
	h.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }
	u.Path = strings.TrimRight(u.Path, "/") + "/v1/moderations"
	u.RawPath = ""
	return &Client{endpoint: u.String(), apiKey: apiKey, options: options, http: &h, random: rand.Float64, now: time.Now, wait: waitContext}, nil
}

func waitContext(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func normalizePolicy(policy string) (string, error) {
	if policy == "" {
		policy = "default"
	}
	if !policyPattern.MatchString(policy) {
		return "", errors.New("modera: invalid policy name")
	}
	return policy, nil
}

// ModerateText preserves submitted text. It does not silently truncate tokens.
func (c *Client) ModerateText(ctx context.Context, text, policy string) (*Result, error) {
	if !utf8.ValidString(text) || strings.TrimSpace(text) == "" || utf8.RuneCountInString(text) > 4096 {
		return nil, errors.New("modera: text must contain 1-4096 Unicode characters")
	}
	p, err := normalizePolicy(policy)
	if err != nil {
		return nil, err
	}
	body, _ := json.Marshal(map[string]any{"input": map[string]string{"type": "text", "text": text}, "policy": p})
	return c.execute(ctx, body, "application/json", "text", p)
}

// ModerateImageURL sends the URL to Modera; the client never fetches that URL.
func (c *Client) ModerateImageURL(ctx context.Context, imageURL, policy string) (*Result, error) {
	p, err := normalizePolicy(policy)
	if err != nil {
		return nil, err
	}
	if imageURL == "" {
		return nil, errors.New("modera: image URL is required")
	}
	body, _ := json.Marshal(map[string]any{"input": map[string]any{"type": "image", "source": map[string]string{"type": "url", "url": imageURL}}, "policy": p})
	return c.execute(ctx, body, "application/json", "image", p)
}

// ModerateImage snapshots a bounded image body before the first network attempt.
// The caller must not mutate image while this method is copying it.
func (c *Client) ModerateImage(ctx context.Context, image []byte, policy string) (*Result, error) {
	if len(image) < 1 || len(image) > maxImageBytes {
		return nil, errors.New("modera: image must contain 1 byte-8 MiB")
	}
	p, err := normalizePolicy(policy)
	if err != nil {
		return nil, err
	}
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	file, err := writer.CreateFormFile("file", "upload")
	if err != nil {
		return nil, err
	}
	if _, err = file.Write(image); err != nil {
		return nil, err
	}
	if err = writer.WriteField("policy", p); err != nil {
		return nil, err
	}
	if err = writer.Close(); err != nil {
		return nil, err
	}
	return c.execute(ctx, body.Bytes(), writer.FormDataContentType(), "image", p)
}

func (c *Client) execute(parent context.Context, body []byte, contentType, kind, requestedPolicy string) (*Result, error) {
	if parent == nil {
		return nil, errors.New("modera: context is required")
	}
	ctx, cancel := context.WithTimeout(parent, c.options.TotalTimeout)
	defer cancel()
	for attempt := 1; attempt <= c.options.MaxAttempts; attempt++ {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		requestCtx := ctx
		stop := func() {}
		if c.options.AttemptTimeout > 0 {
			requestCtx, stop = context.WithTimeout(ctx, c.options.AttemptTimeout)
		}
		request, err := http.NewRequestWithContext(requestCtx, http.MethodPost, c.endpoint, bytes.NewReader(body))
		if err != nil {
			stop()
			return nil, errors.New("modera: could not construct request")
		}
		request.Header.Set("Authorization", "Bearer "+c.apiKey)
		request.Header.Set("Content-Type", contentType)
		request.Header.Set("Accept", "application/json")
		response, err := c.http.Do(request)
		if err != nil {
			attemptErr := requestCtx.Err()
			stop()
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			if attempt == c.options.MaxAttempts || (!retryTransport(err) && !errors.Is(attemptErr, context.DeadlineExceeded)) {
				return nil, errors.New("modera: transport request failed")
			}
			if err = c.pause(ctx, c.backoff(attempt)); err != nil {
				return nil, err
			}
			continue
		}
		data, readErr := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
		response.Body.Close()
		attemptErr := requestCtx.Err()
		stop()
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if attemptErr != nil {
			if attempt < c.options.MaxAttempts && errors.Is(attemptErr, context.DeadlineExceeded) {
				if err = c.pause(ctx, c.backoff(attempt)); err != nil {
					return nil, err
				}
				continue
			}
			return nil, attemptErr
		}
		if readErr != nil {
			canRetryStatus := response.StatusCode == 200 || response.StatusCode == 408 || response.StatusCode == 429 || response.StatusCode == 503
			if attempt < c.options.MaxAttempts && canRetryStatus && retryTransport(readErr) {
				if err = c.pause(ctx, c.backoff(attempt)); err != nil {
					return nil, err
				}
				continue
			}
			return nil, errors.New("modera: response body read failed")
		}
		if len(data) > maxResponseBytes {
			return nil, &ProtocolError{Status: response.StatusCode}
		}
		if response.StatusCode == http.StatusOK {
			var result Result
			if json.Unmarshal(data, &result) != nil || result.validate(kind) != nil || (!result.Fallback && result.Policy.ID != requestedPolicy) || (response.Header.Get("X-Request-ID") != "" && result.RequestID != response.Header.Get("X-Request-ID")) {
				return nil, &ProtocolError{Status: response.StatusCode}
			}
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			return &result, nil
		}
		var envelope struct {
			Error struct {
				Code      string `json:"code"`
				Retryable bool   `json:"retryable"`
				RequestID string `json:"request_id"`
			} `json:"error"`
		}
		if json.Unmarshal(data, &envelope) != nil || !codePattern.MatchString(envelope.Error.Code) || !requestPattern.MatchString(envelope.Error.RequestID) || (response.Header.Get("X-Request-ID") != "" && envelope.Error.RequestID != response.Header.Get("X-Request-ID")) {
			return nil, &ProtocolError{Status: response.StatusCode}
		}
		retryable := envelope.Error.Retryable && (response.StatusCode == 408 || response.StatusCode == 429 || response.StatusCode == 503)
		apiErr := &APIError{Status: response.StatusCode, Code: envelope.Error.Code, RequestID: envelope.Error.RequestID, Retryable: retryable, RetryAfter: retryAfter(response.Header.Get("Retry-After"), c.now()), Attempts: attempt}
		if !retryable || attempt == c.options.MaxAttempts {
			return nil, apiErr
		}
		delay := max(c.backoff(attempt), apiErr.RetryAfter)
		if deadline, ok := ctx.Deadline(); ok && delay >= time.Until(deadline) {
			return nil, apiErr
		}
		if err = c.pause(ctx, delay); err != nil {
			return nil, err
		}
	}
	return nil, errors.New("modera: attempts exhausted")
}

func (c *Client) pause(ctx context.Context, delay time.Duration) error { return c.wait(ctx, delay) }
func (c *Client) backoff(attempt int) time.Duration {
	limit := c.options.BaseDelay
	for i := 1; i < attempt; i++ {
		if limit >= c.options.MaxDelay/2 {
			limit = c.options.MaxDelay
			break
		}
		limit *= 2
	}
	return time.Duration(float64(min(limit, c.options.MaxDelay)) * c.random())
}
func retryAfter(header string, now time.Time) time.Duration {
	if header != "" && strings.IndexFunc(header, func(r rune) bool { return r < '0' || r > '9' }) == -1 {
		seconds, err := strconv.ParseUint(header, 10, 64)
		if err != nil || seconds > uint64((time.Duration(1<<63-1))/time.Second) {
			return time.Duration(1<<63 - 1)
		}
		return time.Duration(seconds) * time.Second
	}
	if when, err := http.ParseTime(header); err == nil && when.After(now) {
		return when.Sub(now)
	}
	return 0
}
func retryTransport(err error) bool {
	var unknown x509.UnknownAuthorityError
	var invalid x509.CertificateInvalidError
	var hostname x509.HostnameError
	if errors.As(err, &unknown) || errors.As(err, &invalid) || errors.As(err, &hostname) {
		return false
	}
	var network net.Error
	return (errors.As(err, &network) && (network.Timeout() || network.Temporary())) || errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, syscall.ECONNRESET) || errors.Is(err, syscall.ECONNREFUSED)
}
