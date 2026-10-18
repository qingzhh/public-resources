package task

import (
	"testing"
	"time"

	"github.com/bestruirui/octopus/internal/model"
	"github.com/bestruirui/octopus/internal/relay/balancer"
)

func TestCollectSmartProbeTargetsOnlyIncludesTrippedPaidSmartMembers(t *testing.T) {
	now := time.Date(2026, 7, 26, 12, 0, 0, 0, time.UTC)
	groups := []model.Group{
		{
			Mode: model.GroupModeSmartFailover,
			Items: []model.GroupItem{
				{ChannelID: 10, ModelName: "paid", PriceMultiplier: 0.1},
				{ChannelID: 11, ModelName: "public", PriceMultiplier: 0},
				{ChannelID: 12, ModelName: "recent", PriceMultiplier: 1},
			},
		},
		{
			Mode:  model.GroupModeFailover,
			Items: []model.GroupItem{{ChannelID: 13, ModelName: "legacy", PriceMultiplier: 1}},
		},
	}
	snapshots := []balancer.CircuitSnapshot{
		{ChannelID: 10, ChannelKeyID: 100, ModelName: "paid", State: balancer.StateOpen, LastFailureAt: now.Add(-2 * time.Minute)},
		{ChannelID: 11, ChannelKeyID: 110, ModelName: "public", State: balancer.StateOpen, LastFailureAt: now.Add(-2 * time.Minute)},
		{ChannelID: 12, ChannelKeyID: 120, ModelName: "recent", State: balancer.StateOpen, LastFailureAt: now.Add(-30 * time.Second)},
		{ChannelID: 13, ChannelKeyID: 130, ModelName: "legacy", State: balancer.StateOpen, LastFailureAt: now.Add(-2 * time.Minute)},
		{ChannelID: 10, ChannelKeyID: 101, ModelName: "paid", State: balancer.StateClosed, LastFailureAt: now.Add(-2 * time.Minute)},
		{ChannelID: 10, ChannelKeyID: 102, ModelName: "paid", State: balancer.StateOpen, RemainingMS: 10_000, LastFailureAt: now.Add(-2 * time.Minute)},
	}

	targets := collectSmartProbeTargets(snapshots, smartProbeEligibleMembers(groups), now, time.Minute)
	if len(targets) != 1 {
		t.Fatalf("targets = %#v, want only one tripped paid smart member", targets)
	}
	target, ok := targets[smartProbeTargetKey(smartProbeTarget{channelID: 10, keyID: 100, modelName: "paid"})]
	if !ok || target.channelID != 10 || target.keyID != 100 {
		t.Fatalf("target = %#v, want channel 10 key 100", target)
	}
	if disabled := collectSmartProbeTargets(snapshots, smartProbeEligibleMembers(groups), now, 0); len(disabled) != 0 {
		t.Fatalf("disabled targets = %#v, want none", disabled)
	}
}

func TestSmartProbeEligibilityLetsZeroMultiplierOverrideDuplicateMembership(t *testing.T) {
	groups := []model.Group{
		{Mode: model.GroupModeSmartFailover, Items: []model.GroupItem{{ChannelID: 10, ModelName: "shared", PriceMultiplier: 1}}},
		{Mode: model.GroupModeSmartFailover, Items: []model.GroupItem{{ChannelID: 10, ModelName: "shared", PriceMultiplier: 0}}},
	}
	if eligible := smartProbeEligibleMembers(groups); len(eligible) != 0 {
		t.Fatalf("eligible = %#v, want duplicate zero-cost membership excluded", eligible)
	}
}
