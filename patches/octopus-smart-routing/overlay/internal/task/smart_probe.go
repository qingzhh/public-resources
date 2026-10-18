package task

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/bestruirui/octopus/internal/grouphealth"
	"github.com/bestruirui/octopus/internal/model"
	"github.com/bestruirui/octopus/internal/op"
	"github.com/bestruirui/octopus/internal/relay/balancer"
	"github.com/bestruirui/octopus/internal/utils/log"
)

type smartProbeTarget struct {
	channelID int
	keyID     int
	modelName string
}

type SmartProbeRuntimeStats struct {
	StartedAt     time.Time  `json:"started_at"`
	LastAttemptAt *time.Time `json:"last_attempt_at,omitempty"`
	Attempted     uint64     `json:"attempted"`
	Succeeded     uint64     `json:"succeeded"`
	Failed        uint64     `json:"failed"`
}

var smartProbeRuntime = struct {
	startedAt     time.Time
	lastAttemptMS atomic.Int64
	attempted     atomic.Uint64
	succeeded     atomic.Uint64
	failed        atomic.Uint64
}{startedAt: time.Now()}

func SmartProbeStats() SmartProbeRuntimeStats {
	stats := SmartProbeRuntimeStats{
		StartedAt: smartProbeRuntime.startedAt,
		Attempted: smartProbeRuntime.attempted.Load(),
		Succeeded: smartProbeRuntime.succeeded.Load(),
		Failed:    smartProbeRuntime.failed.Load(),
	}
	if unixMS := smartProbeRuntime.lastAttemptMS.Load(); unixMS > 0 {
		lastAttemptAt := time.UnixMilli(unixMS)
		stats.LastAttemptAt = &lastAttemptAt
	}
	return stats
}

func SmartProbeTask() {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	if err := runSmartProbes(ctx, grouphealth.NewProber(), time.Now()); err != nil {
		log.Warnf("smart probe task failed: %v", err)
	}
}

func runSmartProbes(ctx context.Context, prober *grouphealth.Prober, now time.Time) error {
	intervalMinutes, err := op.SettingGetInt(model.SettingKeySmartProbeInterval)
	if err != nil {
		return fmt.Errorf("get smart probe interval: %w", err)
	}
	if intervalMinutes <= 0 {
		return nil
	}
	groups, err := op.GroupList(ctx)
	if err != nil {
		return err
	}
	targets := collectSmartProbeTargets(
		balancer.ListCircuitSnapshots(),
		smartProbeEligibleMembers(groups),
		now,
		time.Duration(intervalMinutes)*time.Minute,
	)

	var wg sync.WaitGroup
	for _, target := range targets {
		target := target
		wg.Add(1)
		go func() {
			defer wg.Done()
			lockKey := -target.channelID
			if binding, getErr := op.SiteChannelBindingGetByChannelID(target.channelID, ctx); getErr == nil {
				lockKey = binding.SiteAccountID
			}
			release, acquired := grouphealth.AcquireProbeSlot(ctx, lockKey)
			if !acquired {
				return
			}
			defer release()
			runSmartProbeTarget(ctx, prober, target)
		}()
	}
	wg.Wait()
	return ctx.Err()
}

func runSmartProbeTarget(ctx context.Context, prober *grouphealth.Prober, target smartProbeTarget) {
	tripped, remaining := balancer.IsTripped(target.channelID, target.keyID, target.modelName)
	if tripped && remaining > 0 {
		return
	}
	channel, err := op.ChannelGet(target.channelID, ctx)
	if err != nil || !channel.Enabled {
		return
	}
	var selected model.ChannelKey
	for _, key := range channel.Keys {
		if key.ID == target.keyID && key.Enabled && strings.TrimSpace(key.ChannelKey) != "" {
			selected = key
			break
		}
	}
	if selected.ID == 0 {
		return
	}
	smartProbeRuntime.lastAttemptMS.Store(time.Now().UnixMilli())
	smartProbeRuntime.attempted.Add(1)
	result := prober.RunCandidate(ctx, *channel, selected, target.modelName)
	if result.Success {
		smartProbeRuntime.succeeded.Add(1)
		balancer.RecordSuccess(target.channelID, target.keyID, target.modelName)
	} else {
		smartProbeRuntime.failed.Add(1)
		balancer.RecordProbeFailure(target.channelID, target.keyID, target.modelName)
	}
	if err := op.SmartRoutingRecordProbe(ctx, target.channelID, target.modelName, result.DurationMS, result.Success); err != nil {
		log.Warnf("record smart probe channel=%d model=%s failed: %v", target.channelID, target.modelName, err)
	}
}

func smartProbeEligibleMembers(groups []model.Group) map[string]struct{} {
	eligible := make(map[string]struct{})
	zeroCost := make(map[string]struct{})
	for _, group := range groups {
		if group.Mode != model.GroupModeSmartFailover {
			continue
		}
		for _, item := range group.Items {
			key := smartProbeMemberKey(item.ChannelID, item.ModelName)
			if item.PriceMultiplier == 0 {
				zeroCost[key] = struct{}{}
				delete(eligible, key)
				continue
			}
			if _, excluded := zeroCost[key]; !excluded {
				eligible[key] = struct{}{}
			}
		}
	}
	return eligible
}

func collectSmartProbeTargets(snapshots []balancer.CircuitSnapshot, eligible map[string]struct{}, now time.Time, interval time.Duration) map[string]smartProbeTarget {
	targets := make(map[string]smartProbeTarget)
	if interval <= 0 {
		return targets
	}
	for _, snapshot := range snapshots {
		if _, ok := eligible[smartProbeMemberKey(snapshot.ChannelID, snapshot.ModelName)]; !ok {
			continue
		}
		if snapshot.State == balancer.StateClosed || (snapshot.State == balancer.StateOpen && snapshot.RemainingMS > 0) {
			continue
		}
		if !snapshot.LastFailureAt.IsZero() && now.Sub(snapshot.LastFailureAt) < interval {
			continue
		}
		target := smartProbeTarget{channelID: snapshot.ChannelID, keyID: snapshot.ChannelKeyID, modelName: snapshot.ModelName}
		targets[smartProbeTargetKey(target)] = target
	}
	return targets
}

func smartProbeMemberKey(channelID int, modelName string) string {
	return fmt.Sprintf("%d:%s", channelID, modelName)
}

func smartProbeTargetKey(target smartProbeTarget) string {
	return fmt.Sprintf("%d:%d:%s", target.channelID, target.keyID, target.modelName)
}
