package balancer

import (
	"testing"
	"time"

	"github.com/bestruirui/octopus/internal/model"
)

func TestDeleteStickyRemovesSession(t *testing.T) {
	Reset()
	SetSticky(1, "gpt-4o", 10, 20)
	if entry := GetSticky(1, "gpt-4o", 60*time.Second); entry == nil || entry.ChannelKeyID != 20 {
		t.Fatalf("expected sticky session to exist before delete, got %#v", entry)
	}

	DeleteSticky(1, "gpt-4o")
	if entry := GetSticky(1, "gpt-4o", 60*time.Second); entry != nil {
		t.Fatalf("expected sticky session to be deleted, got %#v", entry)
	}
}

func TestSmartFailoverDoesNotKeepDegradedStickyCandidate(t *testing.T) {
	Reset()
	SetSticky(1, "gpt-4o", 10, 20)
	group := model.Group{
		Mode:            model.GroupModeSmartFailover,
		SessionKeepTime: 1800,
		Items: []model.GroupItem{
			{ID: 1, ChannelID: 10, ModelName: "gpt-4o", AutoRank: 1, HealthState: model.GroupItemHealthDegraded},
			{ID: 2, ChannelID: 11, ModelName: "gpt-4o", AutoRank: 2, HealthState: model.GroupItemHealthHealthy},
		},
	}
	iter := NewIterator(group, 1, "gpt-4o")
	if !iter.Next() || iter.Item().ChannelID != 11 || iter.IsSticky() {
		t.Fatalf("expected healthy ranked candidate without sticky, got item=%+v sticky=%t", iter.Item(), iter.IsSticky())
	}
}

func TestZeroCostTakeoverOverridesPaidStickyOncePerInterval(t *testing.T) {
	Reset()
	now := time.Date(2026, 7, 23, 12, 0, 0, 0, time.UTC)
	zeroCostNow = func() time.Time { return now }
	t.Cleanup(func() { zeroCostNow = time.Now })

	SetSticky(1, "gpt-5.6-sol", 20, 200)
	group := model.Group{
		ID:                       9,
		Mode:                     model.GroupModeSmartFailover,
		SessionKeepTime:          600,
		ZeroCostTakeoverEnabled:  true,
		ZeroCostTakeoverInterval: 5,
		Items: []model.GroupItem{
			{ID: 1, ChannelID: 10, ModelName: "gpt-5.6-sol", AutoRank: 1, PriceMultiplier: 0},
			{ID: 2, ChannelID: 20, ModelName: "gpt-5.6-sol", AutoRank: 2, PriceMultiplier: 0.1},
		},
	}

	first := NewIterator(group, 1, "gpt-5.6-sol")
	defer first.Close()
	if !first.Next() || first.Item().ChannelID != 10 || !first.IsZeroCostTakeover() {
		t.Fatalf("first candidate = %+v takeover=%t, want zero-cost takeover", first.Item(), first.IsZeroCostTakeover())
	}
	first.Close()

	second := NewIterator(group, 1, "gpt-5.6-sol")
	defer second.Close()
	if !second.Next() || second.Item().ChannelID != 20 || !second.IsSticky() || second.IsZeroCostTakeover() {
		t.Fatalf("second candidate = %+v sticky=%t takeover=%t, want paid sticky during cooldown", second.Item(), second.IsSticky(), second.IsZeroCostTakeover())
	}

	now = now.Add(5 * time.Second)
	third := NewIterator(group, 1, "gpt-5.6-sol")
	defer third.Close()
	if !third.Next() || third.Item().ChannelID != 10 || !third.IsZeroCostTakeover() {
		t.Fatalf("third candidate = %+v takeover=%t, want takeover after interval", third.Item(), third.IsZeroCostTakeover())
	}
}

func TestZeroCostTakeoverIntervalZeroSerializesAndRotates(t *testing.T) {
	Reset()
	now := time.Date(2026, 7, 23, 12, 0, 0, 0, time.UTC)
	zeroCostNow = func() time.Time { return now }
	t.Cleanup(func() { zeroCostNow = time.Now })
	group := model.Group{
		ID:                       9,
		Mode:                     model.GroupModeSmartFailover,
		SessionKeepTime:          600,
		ZeroCostTakeoverEnabled:  true,
		ZeroCostTakeoverInterval: 0,
		Items: []model.GroupItem{
			{ID: 1, ChannelID: 10, ModelName: "gpt-5.6-sol", AutoRank: 1, PriceMultiplier: 0},
			{ID: 2, ChannelID: 11, ModelName: "gpt-5.6-sol", AutoRank: 2, PriceMultiplier: 0},
			{ID: 3, ChannelID: 20, ModelName: "gpt-5.6-sol", AutoRank: 3, PriceMultiplier: 0.1},
		},
	}
	SetSticky(1, "gpt-5.6-sol", 20, 200)

	first := NewIterator(group, 1, "gpt-5.6-sol")
	if !first.Next() || first.Item().ChannelID != 10 || !first.IsZeroCostTakeover() || first.Len() != 2 {
		t.Fatalf("first takeover = %+v len=%d takeover=%t, want one free plus paid candidates", first.Item(), first.Len(), first.IsZeroCostTakeover())
	}

	now = now.Add(5 * time.Minute)
	concurrent := NewIterator(group, 1, "gpt-5.6-sol")
	defer concurrent.Close()
	if !concurrent.Next() || concurrent.Item().ChannelID != 20 || concurrent.IsZeroCostTakeover() {
		t.Fatalf("concurrent candidate = %+v takeover=%t, want paid sticky while claim is active", concurrent.Item(), concurrent.IsZeroCostTakeover())
	}

	first.Close()
	rotated := NewIterator(group, 1, "gpt-5.6-sol")
	defer rotated.Close()
	if !rotated.Next() || rotated.Item().ChannelID != 11 || !rotated.IsZeroCostTakeover() || rotated.Len() != 2 {
		t.Fatalf("rotated takeover = %+v len=%d takeover=%t, want next zero-cost candidate", rotated.Item(), rotated.Len(), rotated.IsZeroCostTakeover())
	}
}

func TestZeroCostTakeoverKeepsSuccessfulZeroCostStickyAsSingleAttempt(t *testing.T) {
	Reset()
	SetSticky(1, "gpt-5.6-sol", 10, 100)
	group := model.Group{
		ID:                       9,
		Mode:                     model.GroupModeSmartFailover,
		SessionKeepTime:          600,
		ZeroCostTakeoverEnabled:  true,
		ZeroCostTakeoverInterval: 5,
		Items: []model.GroupItem{
			{ID: 1, ChannelID: 10, ModelName: "gpt-5.6-sol", AutoRank: 1, PriceMultiplier: 0},
			{ID: 2, ChannelID: 20, ModelName: "gpt-5.6-sol", AutoRank: 2, PriceMultiplier: 0.1},
		},
	}

	iter := NewIterator(group, 1, "gpt-5.6-sol")
	defer iter.Close()
	if !iter.Next() || iter.Item().ChannelID != 10 || !iter.IsSticky() || !iter.IsZeroCostTakeover() || iter.Len() != 2 {
		t.Fatalf("candidate = %+v len=%d sticky=%t takeover=%t, want one sticky zero-cost attempt plus paid fallback", iter.Item(), iter.Len(), iter.IsSticky(), iter.IsZeroCostTakeover())
	}
}

func TestZeroCostTakeoverFailureCoolsDownPoolForSixtySeconds(t *testing.T) {
	Reset()
	now := time.Date(2026, 7, 26, 12, 0, 0, 0, time.UTC)
	zeroCostNow = func() time.Time { return now }
	t.Cleanup(func() { zeroCostNow = time.Now })
	SetSticky(1, "gpt-5.6-sol", 20, 200)
	group := model.Group{
		ID:                       9,
		Mode:                     model.GroupModeSmartFailover,
		SessionKeepTime:          600,
		ZeroCostTakeoverEnabled:  true,
		ZeroCostTakeoverInterval: 5,
		Items: []model.GroupItem{
			{ID: 1, ChannelID: 10, ModelName: "gpt-5.6-sol", AutoRank: 1, PriceMultiplier: 0},
			{ID: 2, ChannelID: 11, ModelName: "gpt-5.6-sol", AutoRank: 2, PriceMultiplier: 0},
			{ID: 3, ChannelID: 20, ModelName: "gpt-5.6-sol", AutoRank: 3, PriceMultiplier: 0.1},
		},
	}

	failed := NewIterator(group, 1, "gpt-5.6-sol")
	if !failed.Next() || failed.Item().ChannelID != 10 || failed.Len() != 2 {
		t.Fatalf("failed candidate = %+v len=%d, want one public candidate plus paid fallback", failed.Item(), failed.Len())
	}
	failed.StartAttempt(10, 100, "public-a").End(model.AttemptFailed, 429, "busy")
	failed.Close()

	for _, elapsed := range []time.Duration{0, 59 * time.Second} {
		now = time.Date(2026, 7, 26, 12, 0, 0, 0, time.UTC).Add(elapsed)
		cooling := NewIterator(group, 1, "gpt-5.6-sol")
		if !cooling.Next() || cooling.Item().ChannelID != 20 || cooling.IsZeroCostTakeover() {
			t.Fatalf("candidate during cooldown at %v = %+v, want paid", elapsed, cooling.Item())
		}
		cooling.Close()
	}

	now = time.Date(2026, 7, 26, 12, 1, 0, 0, time.UTC)
	retry := NewIterator(group, 1, "gpt-5.6-sol")
	defer retry.Close()
	if !retry.Next() || retry.Item().ChannelID != 11 || !retry.IsZeroCostTakeover() || retry.Len() != 2 {
		t.Fatalf("candidate after cooldown = %+v len=%d, want one rotated public candidate", retry.Item(), retry.Len())
	}
}

func TestFailedStickyZeroCostCandidateUsesSameCooldown(t *testing.T) {
	Reset()
	now := time.Date(2026, 7, 26, 13, 0, 0, 0, time.UTC)
	zeroCostNow = func() time.Time { return now }
	t.Cleanup(func() { zeroCostNow = time.Now })
	SetSticky(1, "gpt-5.6-sol", 10, 100)
	group := model.Group{
		ID:                       9,
		Mode:                     model.GroupModeSmartFailover,
		SessionKeepTime:          600,
		ZeroCostTakeoverEnabled:  true,
		ZeroCostTakeoverInterval: 5,
		Items: []model.GroupItem{
			{ID: 1, ChannelID: 10, ModelName: "gpt-5.6-sol", AutoRank: 1, PriceMultiplier: 0},
			{ID: 2, ChannelID: 11, ModelName: "gpt-5.6-sol", AutoRank: 2, PriceMultiplier: 0},
			{ID: 3, ChannelID: 20, ModelName: "gpt-5.6-sol", AutoRank: 3, PriceMultiplier: 0.1},
		},
	}

	sticky := NewIterator(group, 1, "gpt-5.6-sol")
	if !sticky.Next() || sticky.Item().ChannelID != 10 || !sticky.IsSticky() || sticky.Len() != 2 {
		t.Fatalf("sticky candidate = %+v len=%d, want one public candidate plus paid fallback", sticky.Item(), sticky.Len())
	}
	sticky.StartAttempt(10, 100, "public-a").End(model.AttemptFailed, 503, "full")
	sticky.Close()

	cooling := NewIterator(group, 1, "gpt-5.6-sol")
	if !cooling.Next() || cooling.Item().ChannelID != 20 || cooling.IsZeroCostTakeover() {
		t.Fatalf("candidate during sticky failure cooldown = %+v, want paid", cooling.Item())
	}
	cooling.Close()

	now = now.Add(60 * time.Second)
	retry := NewIterator(group, 1, "gpt-5.6-sol")
	defer retry.Close()
	if !retry.Next() || retry.Item().ChannelID != 10 || !retry.IsSticky() || retry.Len() != 2 {
		t.Fatalf("sticky retry after cooldown = %+v len=%d, want original public sticky", retry.Item(), retry.Len())
	}
}
