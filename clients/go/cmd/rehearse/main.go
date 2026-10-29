// Command rehearse records safe SDK evidence for deployment checks.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"time"

	modera "github.com/Isafatokun/modera/clients/go"
)

type attempt struct {
	Started    float64 `json:"started"`
	Status     int     `json:"status"`
	Upstream   string  `json:"upstream"`
	RequestID  string  `json:"request_id"`
	RetryAfter string  `json:"retry_after"`
}

type recorder struct {
	transport http.RoundTripper
	started   time.Time
}

func (r *recorder) RoundTrip(request *http.Request) (*http.Response, error) {
	event := attempt{Started: time.Since(r.started).Seconds()}
	response, err := r.transport.RoundTrip(request)
	if response != nil {
		event.Status = response.StatusCode
		event.Upstream = response.Header.Get("X-Rehearsal-Upstream")
		event.RequestID = response.Header.Get("X-Request-ID")
		event.RetryAfter = response.Header.Get("Retry-After")
	}
	if encodeErr := json.NewEncoder(os.Stderr).Encode(event); encodeErr != nil {
		if response != nil {
			response.Body.Close()
		}
		return nil, errors.New("rehearsal: could not record attempt")
	}
	return response, err
}

type outcome struct {
	Result   *modera.Result   `json:"result"`
	APIError *modera.APIError `json:"api_error,omitempty"`
	Failure  string           `json:"failure,omitempty"`
}

func run() error {
	base := flag.String("base-url", "", "Loopback gateway URL")
	image := flag.String("image", "", "PNG fixture path; empty selects neutral text")
	attempts := flag.Int("attempts", 1, "Maximum SDK attempts")
	flag.Parse()
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DisableKeepAlives = true
	transport.Proxy = nil
	defer transport.CloseIdleConnections()
	client, err := modera.New(*base, os.Getenv("MODERA_API_KEY"), modera.Options{
		MaxAttempts: *attempts, TotalTimeout: 15 * time.Second,
		HTTPClient: &http.Client{Transport: &recorder{transport: transport, started: time.Now()}},
	})
	if err != nil {
		return err
	}
	var result *modera.Result
	if *image != "" {
		var data []byte
		data, err = os.ReadFile(*image)
		if err != nil {
			return errors.New("rehearsal: could not read fixture")
		}
		result, err = client.ModerateImage(context.Background(), data, "default")
	} else {
		result, err = client.ModerateText(context.Background(), "Bonjour, merci pour ton aide.", "default")
	}
	output := outcome{Result: result}
	if err != nil {
		var api *modera.APIError
		var protocol *modera.ProtocolError
		switch {
		case errors.As(err, &api):
			output.APIError = api
		case errors.Is(err, context.DeadlineExceeded):
			output.Failure = "deadline"
		case errors.Is(err, context.Canceled):
			output.Failure = "cancelled"
		case errors.As(err, &protocol):
			output.Failure = "protocol"
		default:
			output.Failure = "transport"
		}
	}
	return json.NewEncoder(os.Stdout).Encode(output)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
