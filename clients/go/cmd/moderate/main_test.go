package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const testKey = "example-test-key-not-a-real-deployment-credential"
const requestID = "req_0123456789abcdef0123456789abcdef"

func response(kind, action string, fallback bool) string {
	reasons := "[]"
	if action != "allow" {
		reasons = `["toxicity"]`
	}
	extra, source := "", "model"
	if fallback {
		extra = `,"rules_version":"fixture-v1"`
		source = "rules"
	}
	return fmt.Sprintf(`{"request_id":%q,"input_type":%q,"policy":{"id":"default","version":"1"},"decision":{"action":%q,"reasons":%s},"findings":[{"category":"toxicity","score":0.5,"source":%q}],"coverage":{"evaluated":["toxicity"]},"models":[{"id":"fixture/model","revision":"1"}],"preprocessing_version":"1","fallback":%v%s}`, requestID, kind, action, reasons, source, fallback, extra)
}

func TestDispositions(t *testing.T) {
	for _, tc := range []struct {
		action   string
		fallback bool
		want     string
		exit     int
	}{
		{"allow", false, "publish", 0}, {"review", false, "review", 2}, {"block", false, "reject", 3},
		{"allow", true, "review", 2}, {"block", true, "reject", 3},
	} {
		t.Run(fmt.Sprintf("%s/fallback=%v", tc.action, tc.fallback), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer "+testKey {
					t.Error("missing credential")
				}
				fmt.Fprint(w, response("text", tc.action, tc.fallback))
			}))
			defer server.Close()
			var stdout, stderr bytes.Buffer
			code := run([]string{"--base-url", server.URL}, strings.NewReader("private submitted text"), &stdout, &stderr, testKey)
			var got output
			if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
				t.Fatal(err)
			}
			if code != tc.exit || got.Disposition != tc.want || got.Result == nil || got.Error != nil || got.Result.Fallback != tc.fallback {
				t.Fatal(code, stdout.String(), stderr.String())
			}
			if strings.Contains(stdout.String(), "private submitted text") || strings.Contains(stdout.String()+stderr.String(), testKey) {
				t.Fatal("input or credential leaked")
			}
		})
	}
}

func TestFailuresRemainPending(t *testing.T) {
	for _, tc := range []struct {
		name   string
		status int
		body   string
		kind   string
	}{
		{"busy", 503, `{"error":{"code":"service_busy","retryable":true,"request_id":"` + requestID + `","message":"private server details"}}`, "api"},
		{"malformed", 200, "private server details", "protocol"},
		{"unauthorized", 401, `{"error":{"code":"authentication_required","retryable":false,"request_id":"` + requestID + `"}}`, "api"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.Header().Set("Retry-After", "60")
				w.WriteHeader(tc.status)
				fmt.Fprint(w, tc.body)
			}))
			defer server.Close()
			var stdout, stderr bytes.Buffer
			code := run([]string{"--base-url", server.URL, "--timeout", "1s"}, strings.NewReader("hello"), &stdout, &stderr, testKey)
			var got output
			if json.Unmarshal(stdout.Bytes(), &got) != nil || code != exitPending || got.Disposition != "pending" || got.Result != nil || got.Error == nil || got.Error.Kind != tc.kind || calls.Load() != 1 {
				t.Fatal(code, stdout.String(), stderr.String(), calls.Load())
			}
			if tc.kind == "api" && (got.Error.Retryable == nil || *got.Error.Retryable != (tc.status == 503)) {
				t.Fatal("API retryability was not explicit")
			}
			if strings.Contains(stdout.String()+stderr.String(), "private server details") {
				t.Fatal("server details leaked")
			}
		})
	}
}

func TestInputModes(t *testing.T) {
	for _, kind := range []string{"text", "image", "image-url"} {
		t.Run(kind, func(t *testing.T) {
			content := "École\nbonjour"
			if kind == "image" {
				content = "fixture image bytes"
			}
			if kind == "image-url" {
				content = "https://allowed.invalid/photo.png?token=private-fixture\n"
			}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				responseKind := "text"
				if kind == "image" {
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
					defer file.Close()
					data, err := io.ReadAll(file)
					if err != nil || string(data) != content {
						t.Error("image changed", err)
					}
					responseKind = "image"
				} else {
					var payload struct {
						Input struct {
							Text   string `json:"text"`
							Source struct {
								URL string `json:"url"`
							} `json:"source"`
						} `json:"input"`
					}
					if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
						t.Error(err)
						return
					}
					if kind == "text" && payload.Input.Text != content {
						t.Error("text changed")
					}
					if kind == "image-url" {
						responseKind = "image"
						if payload.Input.Source.URL != strings.TrimSpace(content) {
							t.Error("URL changed")
						}
					}
				}
				fmt.Fprint(w, response(responseKind, "allow", false))
			}))
			defer server.Close()
			var stdout, stderr bytes.Buffer
			code := run([]string{"--base-url", server.URL, "--input", kind}, strings.NewReader(content), &stdout, &stderr, testKey)
			if code != exitPublish {
				t.Fatal(code, stdout.String(), stderr.String())
			}
			if strings.Contains(stdout.String(), "private-fixture") {
				t.Fatal("signed URL leaked")
			}
		})
	}
}

type brokenInput struct{}

func (brokenInput) Read([]byte) (int, error) {
	return 0, fmt.Errorf("private read failure: %s", testKey)
}

type zeroReader struct{}

func (zeroReader) Read(p []byte) (int, error) { clear(p); return len(p), nil }

func TestInvalidInputAndOptionsMakeNoRequest(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer server.Close()
	for _, tc := range []struct {
		args   []string
		reader io.Reader
	}{
		{nil, strings.NewReader("")}, {nil, strings.NewReader(" ")},
		{nil, strings.NewReader(strings.Repeat("a", 4097))}, {nil, bytes.NewReader([]byte{0xff})},
		{nil, brokenInput{}}, {[]string{"--attempts", "0"}, strings.NewReader("hello")},
		{[]string{"--timeout", "0"}, strings.NewReader("hello")},
		{[]string{"--policy", "bad policy"}, strings.NewReader("hello")},
		{[]string{"--input", "video"}, strings.NewReader("hello")},
		{[]string{"--input", "image"}, io.LimitReader(zeroReader{}, maxImageBytes+1)},
		{[]string{"--input", "image-url"}, strings.NewReader("\n")},
	} {
		var stdout, stderr bytes.Buffer
		if code := run(append([]string{"--base-url", server.URL}, tc.args...), tc.reader, &stdout, &stderr, testKey); code != exitUsage || stdout.Len() != 0 || calls.Load() != 0 {
			t.Fatal(code, stdout.String(), stderr.String())
		}
		if strings.Contains(stderr.String(), testKey) {
			t.Fatal("read error leaked secret")
		}
	}
}

func TestNetworkDeadlineProducesPending(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		select {
		case <-r.Context().Done():
		case <-time.After(time.Second):
		}
	}))
	defer server.Close()
	var stdout, stderr bytes.Buffer
	code := run([]string{"--base-url", server.URL, "--timeout", "20ms"}, strings.NewReader("hello"), &stdout, &stderr, testKey)
	var got output
	if json.Unmarshal(stdout.Bytes(), &got) != nil || code != exitPending || got.Error == nil || got.Error.Kind != "deadline" || got.Result != nil {
		t.Fatal(code, stdout.String(), stderr.String())
	}
}

type brokenOutput struct{}

func (brokenOutput) Write([]byte) (int, error) {
	return 0, fmt.Errorf("private output failure: %s", testKey)
}

func TestOutputFailureDoesNotReturnPublishStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, response("text", "allow", false))
	}))
	defer server.Close()
	var stderr bytes.Buffer
	code := run([]string{"--base-url", server.URL}, strings.NewReader("hello"), brokenOutput{}, &stderr, testKey)
	if code != exitUsage || stderr.Len() == 0 || strings.Contains(stderr.String(), testKey) {
		t.Fatal(code, stderr.String())
	}
}
