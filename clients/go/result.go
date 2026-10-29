package modera

import (
	"encoding/json"
	"errors"
	"math"
	"strings"
	"unicode"
	"unicode/utf8"
)

// A missing or null score is not a zero score.
func (f *Finding) UnmarshalJSON(data []byte) error {
	var value struct {
		Category string   `json:"category"`
		Score    *float64 `json:"score"`
		Source   string   `json:"source"`
	}
	if err := json.Unmarshal(data, &value); err != nil {
		return err
	}
	if value.Score == nil {
		return errors.New("modera: finding score is required")
	}
	*f = Finding{Category: value.Category, Score: *value.Score, Source: value.Source}
	return nil
}

type Version struct {
	ID      string `json:"id"`
	Version string `json:"version"`
}
type ModelVersion struct {
	ID       string `json:"id"`
	Revision string `json:"revision"`
}
type Decision struct {
	Action  string   `json:"action"`
	Reasons []string `json:"reasons"`
}
type Finding struct {
	Category string  `json:"category"`
	Score    float64 `json:"score"`
	Source   string  `json:"source"`
}
type Coverage struct {
	Evaluated     []string `json:"evaluated"`
	Unsupported   []string `json:"unsupported"`
	NotApplicable []string `json:"not_applicable"`
}

// Result retains policy/model provenance and explicit fallback status.
type Result struct {
	RequestID            string         `json:"request_id"`
	InputType            string         `json:"input_type"`
	Policy               Version        `json:"policy"`
	Decision             Decision       `json:"decision"`
	Findings             []Finding      `json:"findings"`
	Coverage             Coverage       `json:"coverage"`
	Models               []ModelVersion `json:"models"`
	RulesVersion         *string        `json:"rules_version"`
	Fallback             bool           `json:"fallback"`
	PreprocessingVersion string         `json:"preprocessing_version"`
}

func (r *Result) validate(kind string) error {
	bad := errors.New("modera: invalid moderation result")
	if !requestPattern.MatchString(r.RequestID) || r.InputType != kind || !policyPattern.MatchString(r.Policy.ID) || !plain(r.Policy.Version, 128) || !plain(r.PreprocessingVersion, 128) {
		return bad
	}
	if r.Decision.Action != "allow" && r.Decision.Action != "review" && r.Decision.Action != "block" {
		return bad
	}
	if (r.Decision.Action == "allow") != (len(r.Decision.Reasons) == 0) {
		return bad
	}
	if r.RulesVersion != nil && !plain(*r.RulesVersion, 128) {
		return bad
	}
	if len(r.Coverage.Evaluated) == 0 || len(r.Findings) == 0 {
		return bad
	}
	coverage := map[string]bool{}
	evaluated := map[string]bool{}
	for _, list := range [][]string{r.Coverage.Evaluated, r.Coverage.Unsupported, r.Coverage.NotApplicable} {
		for _, category := range list {
			if !categoryPattern.MatchString(category) || coverage[category] {
				return bad
			}
			coverage[category] = true
		}
	}
	for _, category := range r.Coverage.Evaluated {
		evaluated[category] = true
	}
	findings := map[string]bool{}
	for i, f := range r.Findings {
		if !evaluated[f.Category] || findings[f.Category] || math.IsNaN(f.Score) || math.IsInf(f.Score, 0) || f.Score < 0 || f.Score > 1 {
			return bad
		}
		if f.Source == "" {
			r.Findings[i].Source = "model"
			f.Source = "model"
		}
		if (f.Source != "model" && f.Source != "rules") || (f.Source == "model" && len(r.Models) == 0) || (f.Source == "rules" && (r.RulesVersion == nil || *r.RulesVersion == "")) {
			return bad
		}
		findings[f.Category] = true
	}
	if len(findings) != len(evaluated) || (len(r.Models) == 0 && (r.RulesVersion == nil || *r.RulesVersion == "")) || (r.Fallback && (r.RulesVersion == nil || *r.RulesVersion == "")) {
		return bad
	}
	reasons := map[string]bool{}
	for _, category := range r.Decision.Reasons {
		if !evaluated[category] || reasons[category] {
			return bad
		}
		reasons[category] = true
	}
	models := map[string]bool{}
	for _, model := range r.Models {
		if !plain(model.ID, 200) || !plain(model.Revision, 128) || models[model.ID] {
			return bad
		}
		models[model.ID] = true
	}
	return nil
}

func plain(value string, maximum int) bool {
	return value != "" && utf8.ValidString(value) && utf8.RuneCountInString(value) <= maximum && strings.IndexFunc(value, unicode.IsSpace) == -1
}
