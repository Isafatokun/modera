// Command moderate demonstrates application handling of Modera decisions.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"regexp"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	modera "github.com/Isafatokun/modera/clients/go"
)

const (
	exitPublish   = 0
	exitUsage     = 1
	exitReview    = 2
	exitReject    = 3
	exitPending   = 4
	maxImageBytes = 8 << 20
)

var policyName = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// These are example application dispositions, not new Modera API actions.
type output struct {
	Disposition string         `json:"disposition"`
	Result      *modera.Result `json:"result,omitempty"`
	Error       *failure       `json:"error,omitempty"`
}

type failure struct {
	Kind      string `json:"kind"`
	Code      string `json:"code,omitempty"`
	Status    int    `json:"status,omitempty"`
	RequestID string `json:"request_id,omitempty"`
	Retryable *bool  `json:"retryable,omitempty"`
	Attempts  int    `json:"attempts,omitempty"`
}

func writeOutput(writer io.Writer, value output, status int) int {
	if err := json.NewEncoder(writer).Encode(value); err != nil {
		return exitUsage
	}
	return status
}

func disposition(result *modera.Result, err error) (output, int) {
	if err != nil {
		reason := &failure{Kind: "client"}
		var api *modera.APIError
		var protocol *modera.ProtocolError
		switch {
		case errors.As(err, &api):
			reason = &failure{Kind: "api", Code: api.Code, Status: api.Status, RequestID: api.RequestID, Retryable: &api.Retryable, Attempts: api.Attempts}
		case errors.Is(err, context.DeadlineExceeded):
			reason.Kind = "deadline"
		case errors.Is(err, context.Canceled):
			reason.Kind = "cancelled"
		case errors.As(err, &protocol):
			reason.Kind = "protocol"
		}
		return output{Disposition: "pending", Error: reason}, exitPending
	}
	if result == nil {
		return output{Disposition: "pending", Error: &failure{Kind: "missing_result"}}, exitPending
	}
	switch result.Decision.Action {
	case "allow":
		// Fallback allows go to review in this example; blocks remain rejects.
		if result.Fallback {
			return output{Disposition: "review", Result: result}, exitReview
		}
		return output{Disposition: "publish", Result: result}, exitPublish
	case "review":
		return output{Disposition: "review", Result: result}, exitReview
	case "block":
		return output{Disposition: "reject", Result: result}, exitReject
	default:
		return output{Disposition: "pending", Error: &failure{Kind: "unknown_action"}}, exitPending
	}
}

func readInput(reader io.Reader, kind string) ([]byte, error) {
	limit := int64(4 * 4096)
	if kind == "image" {
		limit = maxImageBytes
	}
	if kind == "image-url" {
		limit = 2048
	}
	data, err := io.ReadAll(io.LimitReader(reader, limit+1))
	if err != nil || len(data) == 0 || int64(len(data)) > limit {
		return nil, errors.New("could not read bounded input")
	}
	if kind == "text" && (!utf8.Valid(data) || strings.TrimSpace(string(data)) == "" || utf8.RuneCount(data) > 4096) {
		return nil, errors.New("text must contain 1-4096 Unicode characters")
	}
	if kind == "image-url" {
		if !utf8.Valid(data) {
			return nil, errors.New("image URL must be UTF-8")
		}
		data = []byte(strings.TrimSpace(string(data)))
		if len(data) == 0 {
			return nil, errors.New("image URL is required")
		}
	}
	return data, nil
}

func run(args []string, stdin io.Reader, stdout, diagnostics io.Writer, key string) int {
	flags := flag.NewFlagSet("moderate", flag.ContinueOnError)
	flags.SetOutput(diagnostics)
	base := flags.String("base-url", "http://127.0.0.1:8000", "Modera service base URL")
	kind := flags.String("input", "text", "Input from stdin: text, image or image-url")
	policy := flags.String("policy", "default", "Configured Modera policy")
	timeout := flags.Duration("timeout", 10*time.Second, "Total network/retry deadline")
	attempts := flags.Int("attempts", 3, "Maximum SDK attempts (1-5)")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return exitPublish
		}
		return exitUsage
	}
	validInput := *kind == "text" || *kind == "image" || *kind == "image-url"
	validPolicy := *policy == "" || policyName.MatchString(*policy)
	validOptions := *attempts >= 1 && *attempts <= 5 && *timeout > 0
	if flags.NArg() != 0 || !validInput || !validPolicy || !validOptions {
		fmt.Fprintln(diagnostics, "invalid flags: check input type, policy name, attempts and timeout")
		return exitUsage
	}
	client, err := modera.New(*base, key, modera.Options{TotalTimeout: *timeout, MaxAttempts: *attempts})
	if err != nil {
		// Do not echo endpoint, key, content or an untrusted error message.
		fmt.Fprintln(diagnostics, "invalid service URL, API key or request options")
		return exitUsage
	}
	data, err := readInput(stdin, *kind)
	if err != nil {
		fmt.Fprintln(diagnostics, "invalid or unreadable input; check size and encoding")
		return exitUsage
	}
	// Before input is complete, normal signal behavior interrupts a blocked stdin
	// read. During network work, cancellation produces an explicit pending outcome.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var result *modera.Result
	switch *kind {
	case "text":
		result, err = client.ModerateText(ctx, string(data), *policy)
	case "image":
		result, err = client.ModerateImage(ctx, data, *policy)
	case "image-url":
		result, err = client.ModerateImageURL(ctx, string(data), *policy)
	}
	value, status := disposition(result, err)
	code := writeOutput(stdout, value, status)
	if code == exitUsage {
		fmt.Fprintln(diagnostics, "could not write the application outcome")
	}
	return code
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr, os.Getenv("MODERA_API_KEY")))
}
