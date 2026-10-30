package modera

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func ensembleResult(t *testing.T) Result {
	t.Helper()
	var result Result
	if err := json.Unmarshal([]byte(valid("image")), &result); err != nil {
		t.Fatal(err)
	}
	result.Findings[0].Model = "fixture/model"
	result.Findings = append(result.Findings, Finding{Category: "detect.region", Score: 0.8, Source: "model", Model: "fixture/detector"})
	result.Models = append(result.Models, ModelVersion{ID: "fixture/detector", Revision: "2"})
	result.Coverage.Evaluated = append(result.Coverage.Evaluated, "detect.region")
	result.Decision = Decision{Action: "review", Reasons: []string{"toxicity", "detect.region"}, MatchedRules: []string{"signals-together"}}
	return result
}

func TestClientRetainsModelAttributionAndCompositeRule(t *testing.T) {
	response := ensembleResult(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewEncoder(w).Encode(response); err != nil {
			t.Error(err)
		}
	}))
	defer server.Close()
	client := mustClient(t, server.URL, Options{})
	result, err := client.ModerateImage(context.Background(), []byte("fixture bytes"), "default")
	if err != nil {
		t.Fatal(err)
	}
	if result.Findings[0].Model != "fixture/model" || result.Findings[1].Model != "fixture/detector" || len(result.Decision.MatchedRules) != 1 || result.Decision.MatchedRules[0] != "signals-together" {
		t.Fatal("ensemble provenance was lost")
	}
}

func TestInvalidEnsembleProvenanceIsRejected(t *testing.T) {
	cases := map[string]func(*Result){
		"unknown model":           func(r *Result) { r.Findings[0].Model = "unknown/model" },
		"missing attribution":     func(r *Result) { r.Findings[0].Model = "" },
		"rules model":             func(r *Result) { r.Findings[0].Source = "rules"; version := "1"; r.RulesVersion = &version },
		"duplicate rule":          func(r *Result) { r.Decision.MatchedRules = append(r.Decision.MatchedRules, "signals-together") },
		"allow with matched rule": func(r *Result) { r.Decision.Action = "allow"; r.Decision.Reasons = nil },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			result := ensembleResult(t)
			change(&result)
			if result.validate("image") == nil {
				t.Fatal("invalid provenance accepted")
			}
		})
	}
}
