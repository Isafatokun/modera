package modera

import (
	"math"
	"strings"
	"testing"
)

func validResult() *Result {
	rules := "rules-1"
	return &Result{
		RequestID:            "req_" + strings.Repeat("a", 32),
		InputType:            "text",
		Policy:               Version{ID: "default", Version: "1"},
		Decision:             Decision{Action: "block", Reasons: []string{"toxicity", "rules.words"}},
		Findings:             []Finding{{Category: "toxicity", Score: 0.9, Source: "model"}, {Category: "rules.words", Score: 1, Source: "rules"}},
		Coverage:             Coverage{Evaluated: []string{"toxicity", "rules.words"}, Unsupported: []string{"hate"}, NotApplicable: []string{}},
		Models:               []ModelVersion{{ID: "example/model", Revision: "abc123"}},
		RulesVersion:         &rules,
		PreprocessingVersion: "text-v1",
	}
}

func TestResultValidation(t *testing.T) {
	empty := ""
	spaced := "has space"
	nan := math.NaN()
	cases := []struct {
		name   string
		change func(*Result)
		valid  bool
	}{
		{"complete result", func(r *Result) {}, true},
		{"allow without reasons", func(r *Result) { r.Decision = Decision{Action: "allow"} }, true},
		{"rules only", func(r *Result) {
			r.Models = nil
			r.Findings = []Finding{{Category: "rules.words", Score: 1, Source: "rules"}}
			r.Coverage.Evaluated = []string{"rules.words"}
			r.Decision.Reasons = []string{"rules.words"}
		}, true},
		{"fallback with rules provenance", func(r *Result) { r.Fallback = true }, true},
		{"two attributed models", func(r *Result) {
			r.Models = []ModelVersion{{ID: "a/one", Revision: "1"}, {ID: "b/two", Revision: "2"}}
			r.Findings[0].Model = "a/one"
		}, true},
		{"composite rule reported", func(r *Result) { r.Decision.MatchedRules = []string{"both-signals"} }, true},

		{"bad request ID", func(r *Result) { r.RequestID = "req_short" }, false},
		{"wrong input type", func(r *Result) { r.InputType = "image" }, false},
		{"bad policy ID", func(r *Result) { r.Policy.ID = "bad id!" }, false},
		{"empty policy version", func(r *Result) { r.Policy.Version = "" }, false},
		{"spaced preprocessing version", func(r *Result) { r.PreprocessingVersion = "a b" }, false},
		{"unknown action", func(r *Result) { r.Decision.Action = "maybe" }, false},
		{"allow with reasons", func(r *Result) { r.Decision.Action = "allow" }, false},
		{"block without reasons", func(r *Result) { r.Decision.Reasons = nil }, false},
		{"empty rules version", func(r *Result) { r.RulesVersion = &empty }, false},
		{"spaced rules version", func(r *Result) { r.RulesVersion = &spaced }, false},
		{"nothing evaluated", func(r *Result) { r.Coverage.Evaluated = nil }, false},
		{"no findings", func(r *Result) { r.Findings = nil }, false},
		{"bad coverage category", func(r *Result) { r.Coverage.Unsupported = []string{"Bad Category"} }, false},
		{"category in two coverage lists", func(r *Result) { r.Coverage.Unsupported = []string{"toxicity"} }, false},
		{"duplicate within a list", func(r *Result) { r.Coverage.NotApplicable = []string{"x", "x"} }, false},
		{"finding outside coverage", func(r *Result) { r.Findings[0].Category = "other" }, false},
		{"duplicate finding", func(r *Result) { r.Findings[1].Category = "toxicity" }, false},
		{"missing finding", func(r *Result) { r.Findings = r.Findings[:1] }, false},
		{"score above one", func(r *Result) { r.Findings[0].Score = 1.01 }, false},
		{"negative score", func(r *Result) { r.Findings[0].Score = -0.1 }, false},
		{"NaN score", func(r *Result) { r.Findings[0].Score = nan }, false},
		{"infinite score", func(r *Result) { r.Findings[0].Score = math.Inf(1) }, false},
		{"unknown source", func(r *Result) { r.Findings[0].Source = "oracle" }, false},
		{"model finding without models", func(r *Result) { r.Models = nil }, false},
		{"rules finding without rules version", func(r *Result) { r.RulesVersion = nil }, false},
		{"no provenance at all", func(r *Result) {
			r.Models, r.RulesVersion = nil, nil
			r.Findings = []Finding{{Category: "toxicity", Score: 0.9, Source: "rules"}}
			r.Coverage.Evaluated = []string{"toxicity"}
			r.Decision.Reasons = []string{"toxicity"}
		}, false},
		{"fallback without rules", func(r *Result) {
			r.Fallback = true
			r.RulesVersion = nil
			r.Findings = []Finding{{Category: "toxicity", Score: 0.9, Source: "model"}}
			r.Coverage.Evaluated = []string{"toxicity"}
			r.Decision.Reasons = []string{"toxicity"}
		}, false},
		{"reason outside coverage", func(r *Result) { r.Decision.Reasons = []string{"unknown"} }, false},
		{"duplicate reason", func(r *Result) { r.Decision.Reasons = []string{"toxicity", "toxicity"} }, false},
		{"bad matched rule", func(r *Result) { r.Decision.MatchedRules = []string{"Bad Rule"} }, false},
		{"duplicate matched rule", func(r *Result) { r.Decision.MatchedRules = []string{"x", "x"} }, false},
		{"matched rule on allow", func(r *Result) {
			r.Decision = Decision{Action: "allow", MatchedRules: []string{"x"}}
		}, false},
		{"model without ID", func(r *Result) { r.Models[0].ID = "" }, false},
		{"model with long revision", func(r *Result) { r.Models[0].Revision = strings.Repeat("r", 129) }, false},
		{"duplicate model", func(r *Result) { r.Models = append(r.Models, r.Models[0]) }, false},
		{"finding names an unlisted model", func(r *Result) { r.Findings[0].Model = "other/model" }, false},
		{"rules finding names a model", func(r *Result) { r.Findings[1].Model = "example/model" }, false},
		{"several models need attribution", func(r *Result) {
			r.Models = []ModelVersion{{ID: "a/one", Revision: "1"}, {ID: "b/two", Revision: "2"}}
		}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			result := validResult()
			c.change(result)
			err := result.validate("text")
			if c.valid && err != nil {
				t.Fatalf("expected a valid result, got %v", err)
			}
			if !c.valid && err == nil {
				t.Fatal("expected the result to be rejected")
			}
		})
	}
}

func TestMissingSourceDefaultsToModel(t *testing.T) {
	result := validResult()
	result.Findings = []Finding{{Category: "toxicity", Score: 0.9}}
	result.Coverage.Evaluated = []string{"toxicity"}
	result.Decision.Reasons = []string{"toxicity"}
	if err := result.validate("text"); err != nil {
		t.Fatal(err)
	}
	if result.Findings[0].Source != "model" {
		t.Fatalf("source = %q, want model", result.Findings[0].Source)
	}
}
