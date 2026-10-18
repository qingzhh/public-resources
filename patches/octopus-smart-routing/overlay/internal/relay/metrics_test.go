package relay

import (
	"testing"

	"github.com/bestruirui/octopus/internal/model"
	transformerModel "github.com/bestruirui/octopus/internal/transformer/model"
)

// usage 完全缺失时，应使用 TransportInputTokens 兜底填充 input，output 保持 0。
func TestSetInternalResponseFallbackWhenUsageMissing(t *testing.T) {
	m := &RelayMetrics{TransportInputTokens: intPtr(123)}
	m.SetInternalResponse(&transformerModel.InternalLLMResponse{}, "test-model")

	if m.Stats.InputToken != 123 {
		t.Fatalf("input token: got %d want 123 (fallback)", m.Stats.InputToken)
	}
	if m.BillInputTokens == nil || *m.BillInputTokens != 123 {
		t.Fatalf("bill input tokens: got %v want 123", m.BillInputTokens)
	}
	if m.Stats.OutputToken != 0 {
		t.Fatalf("output token: got %d want 0", m.Stats.OutputToken)
	}
}

// usage 存在但输入侧全为 0（仅上报 output）时，input 兜底、output 保留。
func TestSetInternalResponseFallbackWhenInputZero(t *testing.T) {
	m := &RelayMetrics{TransportInputTokens: intPtr(50)}
	m.SetInternalResponse(&transformerModel.InternalLLMResponse{
		Usage: &transformerModel.Usage{PromptTokens: 0, CompletionTokens: 30},
	}, "test-model")

	if m.Stats.InputToken != 50 {
		t.Fatalf("input token: got %d want 50 (fallback)", m.Stats.InputToken)
	}
	if m.Stats.OutputToken != 30 {
		t.Fatalf("output token: got %d want 30 (preserved)", m.Stats.OutputToken)
	}
}

// 上游正常上报 input 时不触发兜底（保留真实值，而非估算值）。
func TestSetInternalResponseNoFallbackWhenInputReported(t *testing.T) {
	m := &RelayMetrics{TransportInputTokens: intPtr(999)}
	m.SetInternalResponse(&transformerModel.InternalLLMResponse{
		Usage: &transformerModel.Usage{PromptTokens: 12, CompletionTokens: 7},
	}, "test-model")

	if m.Stats.InputToken != 12 {
		t.Fatalf("input token: got %d want 12 (reported, not fallback)", m.Stats.InputToken)
	}
	if m.Stats.OutputToken != 7 {
		t.Fatalf("output token: got %d want 7", m.Stats.OutputToken)
	}
}

// 仅缓存命中（input_tokens=0 但 cache_read>0）属于已上报输入，不应被估算覆盖。
func TestSetInternalResponseNoFallbackWhenCacheOnly(t *testing.T) {
	m := &RelayMetrics{TransportInputTokens: intPtr(999)}
	m.SetInternalResponse(&transformerModel.InternalLLMResponse{
		Usage: &transformerModel.Usage{PromptTokens: 0, CacheReadInputTokens: 40, CompletionTokens: 5},
	}, "test-model")

	if m.Stats.InputToken != 0 {
		t.Fatalf("input token: got %d want 0 (cache-only is reported input)", m.Stats.InputToken)
	}
}

func TestRelayCostSnapshotUsesFinalSuccessfulAttemptMultiplier(t *testing.T) {
	failedMultiplier := 0.8
	successMultiplier := 0.1
	attempts := []model.ChannelAttempt{
		{ChannelID: 1, Status: model.AttemptFailed, PriceMultiplier: &failedMultiplier},
		{ChannelID: 2, Status: model.AttemptSuccess, PriceMultiplier: &successMultiplier},
	}

	multiplier, actualCost := relayCostSnapshot(2.5, attempts)
	if multiplier == nil || *multiplier != 0.1 {
		t.Fatalf("multiplier = %v, want 0.1", multiplier)
	}
	if actualCost == nil || *actualCost != 0.25 {
		t.Fatalf("actual cost = %v, want 0.25", actualCost)
	}
}

func TestRelayCostSnapshotPreservesZeroAndDefaultsLegacyAttempt(t *testing.T) {
	zero := 0.0
	multiplier, actualCost := relayCostSnapshot(3, []model.ChannelAttempt{{Status: model.AttemptSuccess, PriceMultiplier: &zero}})
	if multiplier == nil || *multiplier != 0 || actualCost == nil || *actualCost != 0 {
		t.Fatalf("zero multiplier snapshot = multiplier %v actual %v, want 0 and 0", multiplier, actualCost)
	}

	multiplier, actualCost = relayCostSnapshot(3, []model.ChannelAttempt{{Status: model.AttemptSuccess}})
	if multiplier == nil || *multiplier != 1 || actualCost == nil || *actualCost != 3 {
		t.Fatalf("legacy attempt snapshot = multiplier %v actual %v, want 1 and 3", multiplier, actualCost)
	}
}

func TestRelayReasoningEffortSnapshotOnlyUsesExplicitRequestValue(t *testing.T) {
	if got := relayReasoningEffortSnapshot(nil); got != nil {
		t.Fatalf("nil request snapshot = %v, want nil", got)
	}
	if got := relayReasoningEffortSnapshot(&transformerModel.InternalLLMRequest{}); got != nil {
		t.Fatalf("empty request snapshot = %v, want nil", got)
	}

	got := relayReasoningEffortSnapshot(&transformerModel.InternalLLMRequest{ReasoningEffort: "high"})
	if got == nil || *got != "high" {
		t.Fatalf("reasoning effort snapshot = %v, want high", got)
	}

	if got := relayReasoningEffortSnapshot(&transformerModel.InternalLLMRequest{}, []byte(`{"reasoning":{"effort":"medium"}}`)); got == nil || *got != "medium" {
		t.Fatalf("raw reasoning effort snapshot = %v, want medium", got)
	}
	if got := relayReasoningEffortSnapshot(&transformerModel.InternalLLMRequest{}, []byte(`{"reasoning_effort":"xhigh"}`)); got == nil || *got != "xhigh" {
		t.Fatalf("raw top-level reasoning effort snapshot = %v, want xhigh", got)
	}
}

func TestRelayUnitPriceSnapshotPreservesExplicitZeroPrices(t *testing.T) {
	input, output, cacheRead, cacheWrite := relayUnitPriceSnapshot("gpt-4.1-nano")
	if input == nil || output == nil || cacheRead == nil || cacheWrite == nil {
		t.Fatal("expected a complete price snapshot for gpt-4.1-nano")
	}
	if *input != 0.1 || *output != 0.4 || *cacheRead != 0.03 || *cacheWrite != 0 {
		t.Fatalf("price snapshot = (%v, %v, %v, %v), want (0.1, 0.4, 0.03, 0)", *input, *output, *cacheRead, *cacheWrite)
	}
}
