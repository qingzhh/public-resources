package grouphealth

import (
	"context"
	"testing"
	"time"
)

func TestProbeLimiterSerializesSameAccount(t *testing.T) {
	release, ok := AcquireProbeSlot(context.Background(), 101)
	if !ok {
		t.Fatal("expected first account slot")
	}
	defer release()

	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancel()
	if secondRelease, acquired := AcquireProbeSlot(ctx, 101); acquired {
		secondRelease()
		t.Fatal("same account probe must not run concurrently")
	}
}

func TestProbeLimiterCapsGlobalConcurrency(t *testing.T) {
	releases := make([]func(), 0, 4)
	for accountID := 1; accountID <= 4; accountID++ {
		release, ok := AcquireProbeSlot(context.Background(), accountID)
		if !ok {
			t.Fatalf("expected global slot %d", accountID)
		}
		releases = append(releases, release)
	}
	defer func() {
		for _, release := range releases {
			release()
		}
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancel()
	if release, acquired := AcquireProbeSlot(ctx, 5); acquired {
		release()
		t.Fatal("fifth concurrent probe must wait for a global slot")
	}
}
