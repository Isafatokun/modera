package modera

import (
	"encoding/json"
	"errors"
	"math"
	"strings"
	"unicode"
	"unicode/utf8"
)

// UnmarshalJSON rejects absent or null scores rather than treating them as zero.
func (f *Finding) UnmarshalJSON(data []byte) error {
	var value struct {
		Category string   `json:"category"`
		Score    *float64 `json:"score"`
		Source   string   `json:"source"`
		Model    string   `json:"model"`
	}
	if err := json.Unmarshal(data, &value); err != nil {
		return err
	}
	if value.Score == nil {
		return errors.New("modera: finding score is required")
	}
	*f = Finding{Category: value.Category, Score: *value.Score, Source: value.Source, Model: value.Model}
	return nil
}

// Version identifies a versioned policy.
type Version struct {
	ID      string `json:"id"`
	Version string `json:"version"`
}

// ModelVersion identifies a model and its artifact revision.
type ModelVersion struct {
	ID       string `json:"id"`
	Revision string `json:"revision"`
}

// Decision contains the moderation action and triggering category names.
type Decision struct {
	Action  string   `json:"action"`
	Reasons []string `json:"reasons"`
	// MatchedRules identifies composite rules contributing to the chosen action.
	MatchedRules []string `json:"matched_rules,omitempty"`
}

// Finding contains a category score and its model or rules source.
type Finding struct {
	Category string  `json:"category"`
	Score    float64 `json:"score"`
	Source   string  `json:"source"`
	// Model identifies the producer; it can be absent for a legacy single model.
	Model string `json:"model,omitempty"`
}

// Coverage distinguishes evaluated, unsupported, and inapplicable categories.
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

var errInvalidResult = errors.New("modera: invalid moderation result")

func (r *Result) validate(kind string) error {
	if !r.validEnvelope(kind) || !r.validAction() {
		return errInvalidResult
	}
	evaluated, ok := r.evaluatedCategories()
	if !ok || !r.validFindings(evaluated) || !r.hasProvenance() || !r.validReasons(evaluated) || !r.validMatchedRules() {
		return errInvalidResult
	}
	models, ok := r.modelIDs()
	if !ok || !r.validAttribution(models) {
		return errInvalidResult
	}
	return nil
}

func (r *Result) validEnvelope(kind string) bool {
	if !requestPattern.MatchString(r.RequestID) || r.InputType != kind || !policyPattern.MatchString(r.Policy.ID) || !plain(r.Policy.Version, 128) || !plain(r.PreprocessingVersion, 128) {
		return false
	}
	return r.RulesVersion == nil || plain(*r.RulesVersion, 128)
}

func (r *Result) validAction() bool {
	switch r.Decision.Action {
	case "allow", "review", "block":
	default:
		return false
	}
	return (r.Decision.Action == "allow") == (len(r.Decision.Reasons) == 0)
}

func (r *Result) hasRules() bool {
	return r.RulesVersion != nil && *r.RulesVersion != ""
}

// evaluatedCategories checks that the coverage lists are disjoint, well formed
// and non-empty, and returns the evaluated categories.
func (r *Result) evaluatedCategories() (map[string]bool, bool) {
	if len(r.Coverage.Evaluated) == 0 || len(r.Findings) == 0 {
		return nil, false
	}
	seen := map[string]bool{}
	for _, list := range [][]string{r.Coverage.Evaluated, r.Coverage.Unsupported, r.Coverage.NotApplicable} {
		for _, category := range list {
			if !categoryPattern.MatchString(category) || seen[category] {
				return nil, false
			}
			seen[category] = true
		}
	}
	evaluated := map[string]bool{}
	for _, category := range r.Coverage.Evaluated {
		evaluated[category] = true
	}
	return evaluated, true
}

// validFindings requires exactly one finding per evaluated category, each with a
// finite score from 0 to 1. A finding with no source is treated as a model finding.
func (r *Result) validFindings(evaluated map[string]bool) bool {
	findings := map[string]bool{}
	for i, f := range r.Findings {
		if !evaluated[f.Category] || findings[f.Category] || math.IsNaN(f.Score) || math.IsInf(f.Score, 0) || f.Score < 0 || f.Score > 1 {
			return false
		}
		if f.Source == "" {
			r.Findings[i].Source = "model"
			f.Source = "model"
		}
		if (f.Source != "model" && f.Source != "rules") || (f.Source == "model" && len(r.Models) == 0) || (f.Source == "rules" && !r.hasRules()) {
			return false
		}
		findings[f.Category] = true
	}
	return len(findings) == len(evaluated)
}

func (r *Result) hasProvenance() bool {
	if len(r.Models) == 0 && !r.hasRules() {
		return false
	}
	return !r.Fallback || r.hasRules()
}

func (r *Result) validReasons(evaluated map[string]bool) bool {
	reasons := map[string]bool{}
	for _, category := range r.Decision.Reasons {
		if !evaluated[category] || reasons[category] {
			return false
		}
		reasons[category] = true
	}
	return true
}

func (r *Result) validMatchedRules() bool {
	matched := map[string]bool{}
	for _, rule := range r.Decision.MatchedRules {
		if !categoryPattern.MatchString(rule) || matched[rule] || r.Decision.Action == "allow" {
			return false
		}
		matched[rule] = true
	}
	return true
}

func (r *Result) modelIDs() (map[string]bool, bool) {
	models := map[string]bool{}
	for _, model := range r.Models {
		if !plain(model.ID, 200) || !plain(model.Revision, 128) || models[model.ID] {
			return nil, false
		}
		models[model.ID] = true
	}
	return models, true
}

// validAttribution checks that findings only name listed models and that, with
// several models, every model finding says which one produced it.
func (r *Result) validAttribution(models map[string]bool) bool {
	for _, finding := range r.Findings {
		if finding.Model != "" && (finding.Source != "model" || !models[finding.Model]) {
			return false
		}
		if len(models) > 1 && finding.Source == "model" && finding.Model == "" {
			return false
		}
	}
	return true
}

func plain(value string, maximum int) bool {
	return value != "" && utf8.ValidString(value) && utf8.RuneCountInString(value) <= maximum && strings.IndexFunc(value, unicode.IsSpace) == -1
}
