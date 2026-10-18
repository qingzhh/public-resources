package relay

import (
	"errors"
	"net/http"
	"testing"

	"github.com/bestruirui/octopus/internal/model"
	"github.com/bestruirui/octopus/internal/relay/balancer"
)

func TestZeroCostCapacityFailureIsSoftAndSingleAttempt(t *testing.T) {
	busy := errors.New(`upstream error: 500: {"error":{"message":"当前模型 gpt-5.6-sol 负载已经达到上限，请稍后重试","code":"get_channel_failed"}}`)
	if !isZeroCostCapacityFailure(http.StatusInternalServerError, busy) {
		t.Fatal("known zero-cost capacity response must be classified as busy")
	}
	if got := circuitFailureKind(true, http.StatusInternalServerError, busy, true); got != balancer.FailureSoftRateLimit {
		t.Fatalf("capacity failure kind = %v, want soft", got)
	}
	group := model.Group{RetryEnabled: true, MaxRetries: 3}
	if got := sameChannelAttemptLimit(group, true); got != 1 {
		t.Fatalf("zero-cost takeover attempts = %d, want 1", got)
	}
}

func TestZeroCostCapacityFailureClassificationIsNarrow(t *testing.T) {
	cases := []struct {
		name   string
		status int
		err    error
		want   bool
	}{
		{name: "rate limit", status: http.StatusTooManyRequests, err: errors.New("rate limited"), want: true},
		{name: "unavailable", status: http.StatusServiceUnavailable, err: errors.New("unavailable"), want: true},
		{name: "no available channel", status: http.StatusInternalServerError, err: errors.New("no available channel for model"), want: true},
		{name: "generic server error", status: http.StatusInternalServerError, err: errors.New("database failed"), want: false},
		{name: "unauthorized", status: http.StatusUnauthorized, err: errors.New("invalid token"), want: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := isZeroCostCapacityFailure(tc.status, tc.err); got != tc.want {
				t.Fatalf("isZeroCostCapacityFailure(%d, %q) = %t, want %t", tc.status, tc.err, got, tc.want)
			}
		})
	}
}

func TestNonTakeoverRetryBehaviorIsUnchanged(t *testing.T) {
	group := model.Group{RetryEnabled: true, MaxRetries: 3}
	if got := sameChannelAttemptLimit(group, false); got != 3 {
		t.Fatalf("ordinary attempts = %d, want 3", got)
	}
	busy := errors.New("no available channel")
	if got := circuitFailureKind(true, http.StatusInternalServerError, busy, false); got != balancer.FailureHard {
		t.Fatalf("ordinary 500 failure kind = %v, want hard", got)
	}
}
