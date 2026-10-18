package balancer

import (
	"fmt"
	"sync"
	"time"

	"github.com/bestruirui/octopus/internal/model"
)

const zeroCostFailureCooldown = 60 * time.Second

var (
	globalZeroCostTakeover sync.Map
	zeroCostNow            = time.Now
)

type zeroCostTakeoverState struct {
	mu          sync.Mutex
	lastAttempt time.Time
	lastFailure time.Time
	inFlight    bool
	nextIndex   int
}

type zeroCostTakeoverClaim struct {
	state *zeroCostTakeoverState
	once  sync.Once
}

func zeroCostTakeoverKey(groupID, apiKeyID int, requestModel string) string {
	return fmt.Sprintf("%d:%d:%s", groupID, apiKeyID, requestModel)
}

func claimZeroCostTakeover(group model.Group, apiKeyID int, requestModel string, candidateCount, stickyIndex int) (*zeroCostTakeoverClaim, int) {
	if group.Mode != model.GroupModeSmartFailover || !group.ZeroCostTakeoverEnabled || candidateCount == 0 {
		return nil, -1
	}
	if stickyIndex >= candidateCount {
		return nil, -1
	}
	key := zeroCostTakeoverKey(group.ID, apiKeyID, requestModel)
	raw, _ := globalZeroCostTakeover.LoadOrStore(key, &zeroCostTakeoverState{})
	state := raw.(*zeroCostTakeoverState)
	now := zeroCostNow()

	state.mu.Lock()
	defer state.mu.Unlock()
	if state.inFlight {
		return nil, -1
	}
	if !state.lastFailure.IsZero() && now.Sub(state.lastFailure) < zeroCostFailureCooldown {
		return nil, -1
	}
	if stickyIndex < 0 && group.ZeroCostTakeoverInterval > 0 && !state.lastAttempt.IsZero() && now.Sub(state.lastAttempt) < time.Duration(group.ZeroCostTakeoverInterval)*time.Second {
		return nil, -1
	}
	selected := stickyIndex
	if selected < 0 {
		selected = state.nextIndex % candidateCount
		state.nextIndex = (state.nextIndex + 1) % candidateCount
	}
	state.lastAttempt = now
	state.inFlight = true
	return &zeroCostTakeoverClaim{state: state}, selected
}

func (claim *zeroCostTakeoverClaim) release(failed bool) {
	if claim == nil || claim.state == nil {
		return
	}
	claim.once.Do(func() {
		claim.state.mu.Lock()
		if failed {
			claim.state.lastFailure = zeroCostNow()
		}
		claim.state.inFlight = false
		claim.state.mu.Unlock()
	})
}
