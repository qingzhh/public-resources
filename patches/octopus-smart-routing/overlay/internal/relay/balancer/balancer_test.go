package balancer

import (
	"testing"

	"github.com/bestruirui/octopus/internal/model"
)

func testItems() []model.GroupItem {
	return []model.GroupItem{
		{ID: 1, ChannelID: 1, ModelName: "m", Priority: 1, Weight: 1},
		{ID: 2, ChannelID: 2, ModelName: "m", Priority: 2, Weight: 3},
	}
}

func TestRoundRobinStateIsIndependentPerGroup(t *testing.T) {
	Reset()
	balancer := &RoundRobin{}
	first := model.Group{ID: 1, Items: testItems()}
	second := model.Group{ID: 2, Items: testItems()}

	if got := balancer.Candidates(first)[0].ID; got != 1 {
		t.Fatalf("first group first candidate = %d, want 1", got)
	}
	if got := balancer.Candidates(first)[0].ID; got != 2 {
		t.Fatalf("first group second candidate = %d, want 2", got)
	}
	if got := balancer.Candidates(second)[0].ID; got != 1 {
		t.Fatalf("second group first candidate = %d, want independent start at 1", got)
	}
}

func TestSmoothWeightedRoundRobinHonorsConfiguredRatio(t *testing.T) {
	Reset()
	balancer := &Weighted{}
	group := model.Group{ID: 7, Items: testItems()}
	counts := map[int]int{}
	for i := 0; i < 400; i++ {
		counts[balancer.Candidates(group)[0].ID]++
	}
	if counts[1] != 100 || counts[2] != 300 {
		t.Fatalf("weighted counts = %#v, want 100/300", counts)
	}
}

func TestSmartFailoverUsesHealthThenManualAndAutomaticRank(t *testing.T) {
	manual := 1
	group := model.Group{Mode: model.GroupModeSmartFailover, Items: []model.GroupItem{
		{ID: 1, Priority: 1, AutoRank: 1, HealthState: model.GroupItemHealthDegraded, CompositeScore: 1},
		{ID: 2, Priority: 2, AutoRank: 2, ManualRank: &manual, HealthState: model.GroupItemHealthHealthy, CompositeScore: .4},
		{ID: 3, Priority: 3, AutoRank: 1, HealthState: model.GroupItemHealthHealthy, CompositeScore: .9},
	}}

	got := (&SmartFailover{}).Candidates(group)
	if got[0].ID != 2 || got[1].ID != 3 || got[2].ID != 1 {
		t.Fatalf("smart order = [%d %d %d], want [2 3 1]", got[0].ID, got[1].ID, got[2].ID)
	}
}

func TestExistingModesDoNotUseSmartHealthState(t *testing.T) {
	items := []model.GroupItem{
		{ID: 1, Priority: 1, HealthState: model.GroupItemHealthDegraded},
		{ID: 2, Priority: 2, HealthState: model.GroupItemHealthHealthy},
	}
	got := (&Failover{}).Candidates(model.Group{Items: items})
	if got[0].ID != 1 {
		t.Fatalf("failover first candidate = %d, want priority order preserved", got[0].ID)
	}
}

func TestSmartFailoverRanksHealthyAndUnknownByAutomaticRankBeforeDegraded(t *testing.T) {
	group := model.Group{Mode: model.GroupModeSmartFailover, Items: []model.GroupItem{
		{ID: 1, AutoRank: 1, HealthState: model.GroupItemHealthDegraded},
		{ID: 2, AutoRank: 2, HealthState: model.GroupItemHealthUnknown},
		{ID: 3, AutoRank: 3, HealthState: model.GroupItemHealthHealthy},
	}}
	got := (&SmartFailover{}).Candidates(group)
	if got[0].ID != 2 || got[1].ID != 3 || got[2].ID != 1 {
		t.Fatalf("smart health order = [%d %d %d], want [2 3 1]", got[0].ID, got[1].ID, got[2].ID)
	}
}

func TestIteratorSnapshotsCandidatePriceMultiplier(t *testing.T) {
	group := model.Group{Mode: model.GroupModeFailover, Items: []model.GroupItem{
		{ID: 1, ChannelID: 11, ModelName: "priced-model", Priority: 1, PriceMultiplier: 0.1},
	}}
	iter := NewIterator(group, 1, "priced-model")
	if !iter.Next() {
		t.Fatal("expected one candidate")
	}
	span := iter.StartAttempt(11, 21, "priced-channel")
	span.End(model.AttemptSuccess, 200, "")

	attempts := iter.Attempts()
	if len(attempts) != 1 || attempts[0].PriceMultiplier == nil || *attempts[0].PriceMultiplier != 0.1 {
		t.Fatalf("attempt multiplier = %+v, want 0.1", attempts)
	}
}
