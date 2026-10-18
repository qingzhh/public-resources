package grouphealth

import (
	"context"
	"sync"
)

var globalProbeSlots = make(chan struct{}, 4)
var accountProbeSlots sync.Map // account ID (or negative channel ID) -> chan struct{}

// AcquireProbeSlot enforces the process-wide active-probe limits. The caller
// must invoke the returned release function exactly once when acquired is true.
func AcquireProbeSlot(ctx context.Context, accountKey int) (release func(), acquired bool) {
	raw, _ := accountProbeSlots.LoadOrStore(accountKey, make(chan struct{}, 1))
	accountSlot := raw.(chan struct{})
	select {
	case accountSlot <- struct{}{}:
	case <-ctx.Done():
		return nil, false
	}

	select {
	case globalProbeSlots <- struct{}{}:
		return func() {
			<-globalProbeSlots
			<-accountSlot
		}, true
	case <-ctx.Done():
		<-accountSlot
		return nil, false
	}
}
