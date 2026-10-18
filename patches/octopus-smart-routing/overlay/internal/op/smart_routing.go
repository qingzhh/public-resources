package op

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/bestruirui/octopus/internal/db"
	"github.com/bestruirui/octopus/internal/model"
)

const smartRoutingHistoryWindow = 7 * 24 * time.Hour
const smartRoutingSampleLimit = 500

const (
	smartRoutingMultiplierWeight   = 0.8
	smartRoutingMeasuredCostWeight = 0.2
)

var ErrSmartRoutingModeRequired = errors.New("smart routing mode is required")

type smartRoutingProfile struct {
	expectedCost float64
	priceKnown   bool
	speedMS      float64
	speedSource  string
	successRate  float64
	sampleCount  int
	lastRequest  *time.Time
}

// SmartRoutingRefresh recomputes control-plane rankings. The relay path only
// reads persisted GroupItem fields, so request handling never performs history
// queries or price calculations.
func SmartRoutingRefresh(ctx context.Context) error {
	groups, err := GroupList(ctx)
	if err != nil {
		return err
	}
	for _, group := range groups {
		if group.Mode != model.GroupModeSmartFailover {
			continue
		}
		if err := SmartRoutingRefreshGroup(ctx, group.ID); err != nil {
			return err
		}
	}
	return nil
}

func SmartRoutingRefreshGroup(ctx context.Context, groupID int) error {
	return smartRoutingRefreshGroup(ctx, groupID, smartRoutingRefreshOptions{})
}

func SmartRoutingRefreshGroupNow(ctx context.Context, groupID int, clearManualRank bool) error {
	return smartRoutingRefreshGroup(ctx, groupID, smartRoutingRefreshOptions{
		clearManualRank: clearManualRank,
		startSchedule:   true,
		now:             time.Now(),
	})
}

func SmartRoutingRefreshDueGroups(ctx context.Context, now time.Time) error {
	var groups []model.Group
	if err := db.GetDB().WithContext(ctx).
		Where("mode = ? AND smart_reorder_interval > 0 AND smart_reorder_next_at IS NOT NULL AND smart_reorder_next_at <= ?", model.GroupModeSmartFailover, now).
		Order("smart_reorder_next_at ASC, id ASC").
		Find(&groups).Error; err != nil {
		return fmt.Errorf("list due smart routing groups: %w", err)
	}

	var refreshErrors []error
	for _, group := range groups {
		dueAt := *group.SmartReorderNextAt
		if err := smartRoutingRefreshGroup(ctx, group.ID, smartRoutingRefreshOptions{
			clearManualRank: group.SmartReorderClearManual,
			scheduledAt:     &dueAt,
			now:             now,
		}); err != nil {
			refreshErrors = append(refreshErrors, fmt.Errorf("refresh due smart routing group %d: %w", group.ID, err))
		}
	}
	return errors.Join(refreshErrors...)
}

type smartRoutingRefreshOptions struct {
	clearManualRank bool
	startSchedule   bool
	scheduledAt     *time.Time
	now             time.Time
}

func smartRoutingRefreshGroup(ctx context.Context, groupID int, options smartRoutingRefreshOptions) error {
	group, err := GroupGet(groupID, ctx)
	if err != nil {
		return err
	}
	if (options.startSchedule || options.scheduledAt != nil) && group.Mode != model.GroupModeSmartFailover {
		return ErrSmartRoutingModeRequired
	}
	if options.now.IsZero() {
		options.now = time.Now()
	}

	profiles := make(map[int]smartRoutingProfile, len(group.Items))
	for _, item := range group.Items {
		profile, err := loadSmartRoutingProfile(ctx, item)
		if err != nil {
			return err
		}
		profiles[item.ID] = profile
	}

	multipliers := make(map[int]float64, len(group.Items))
	for _, item := range group.Items {
		multipliers[item.ID] = resolveGroupItemPriceMultiplier(ctx, item.ChannelID)
	}
	priceScores := smartRoutingPriceScores(group.Items, profiles, multipliers)
	speedScores := normalizeLowerIsBetter(group.Items, profiles, func(profile smartRoutingProfile) (float64, bool) {
		return profile.speedMS, profile.speedMS > 0
	})

	priceWeight, speedWeight := normalizedSmartWeights(*group)
	now := options.now
	updated := append([]model.GroupItem(nil), group.Items...)
	for i := range updated {
		profile := profiles[updated[i].ID]
		updated[i].ExpectedCost = profile.expectedCost
		updated[i].PriceKnown = profile.priceKnown
		updated[i].PriceMultiplier = multipliers[updated[i].ID]
		updated[i].SpeedMS = profile.speedMS
		updated[i].SpeedSource = profile.speedSource
		updated[i].SuccessRate = profile.successRate
		updated[i].SampleCount = profile.sampleCount
		updated[i].LastRequestAt = profile.lastRequest
		updated[i].PriceScore = priceScores[updated[i].ID]
		updated[i].SpeedScore = speedScores[updated[i].ID]
		updated[i].CompositeScore = updated[i].PriceScore*float64(priceWeight)/100 + updated[i].SpeedScore*float64(speedWeight)/100
		updated[i].HealthState = profileHealthState(profile, updated[i])
		updated[i].ScoreUpdatedAt = &now
	}

	sortSmartRoutingItems(updated)

	tx := db.GetDB().WithContext(ctx).Begin()
	if tx.Error != nil {
		return tx.Error
	}
	if options.clearManualRank {
		if err := tx.Model(&model.GroupItem{}).Where("group_id = ?", groupID).Update("manual_rank", nil).Error; err != nil {
			tx.Rollback()
			return fmt.Errorf("clear manual smart routing ranks: %w", err)
		}
	}
	for index, item := range updated {
		updates := map[string]any{
			"auto_rank":        index + 1,
			"expected_cost":    item.ExpectedCost,
			"speed_ms":         item.SpeedMS,
			"success_rate":     item.SuccessRate,
			"price_score":      item.PriceScore,
			"speed_score":      item.SpeedScore,
			"composite_score":  item.CompositeScore,
			"health_state":     item.HealthState,
			"price_multiplier": item.PriceMultiplier,
			"price_known":      item.PriceKnown,
			"speed_source":     item.SpeedSource,
			"sample_count":     item.SampleCount,
			"last_request_at":  item.LastRequestAt,
			"score_updated_at": now,
		}
		if err := tx.Model(&model.GroupItem{}).Where("id = ? AND group_id = ?", item.ID, groupID).Updates(updates).Error; err != nil {
			tx.Rollback()
			return err
		}
	}
	if options.startSchedule || options.scheduledAt != nil {
		scheduleUpdates := map[string]any{}
		if group.SmartReorderInterval <= 0 {
			scheduleUpdates["smart_reorder_started_at"] = nil
			scheduleUpdates["smart_reorder_next_at"] = nil
		} else if options.startSchedule {
			scheduleUpdates["smart_reorder_started_at"] = now
			scheduleUpdates["smart_reorder_next_at"] = now.Add(time.Duration(group.SmartReorderInterval) * time.Minute)
		} else if options.scheduledAt != nil {
			scheduleUpdates["smart_reorder_next_at"] = nextSmartReorderAt(*options.scheduledAt, group.SmartReorderInterval, now)
		}
		if len(scheduleUpdates) > 0 {
			if err := tx.Model(&model.Group{}).Where("id = ?", groupID).Updates(scheduleUpdates).Error; err != nil {
				tx.Rollback()
				return fmt.Errorf("update smart routing schedule: %w", err)
			}
		}
	}
	if err := tx.Commit().Error; err != nil {
		return err
	}
	return groupRefreshCacheByID(groupID, ctx)
}

func sortSmartRoutingItems(items []model.GroupItem) {
	sort.SliceStable(items, func(i, j int) bool {
		if smartHealthRank(items[i].HealthState) != smartHealthRank(items[j].HealthState) {
			return smartHealthRank(items[i].HealthState) < smartHealthRank(items[j].HealthState)
		}
		leftMultiplier := effectivePriceMultiplier(items[i].PriceMultiplier)
		rightMultiplier := effectivePriceMultiplier(items[j].PriceMultiplier)
		if leftMultiplier != rightMultiplier {
			return leftMultiplier < rightMultiplier
		}
		if items[i].CompositeScore != items[j].CompositeScore {
			return items[i].CompositeScore > items[j].CompositeScore
		}
		return items[i].Priority < items[j].Priority
	})
}

func nextSmartReorderAt(dueAt time.Time, intervalMinutes int, now time.Time) time.Time {
	interval := time.Duration(intervalMinutes) * time.Minute
	if interval <= 0 || dueAt.After(now) {
		return dueAt
	}
	steps := now.Sub(dueAt)/interval + 1
	return dueAt.Add(steps * interval)
}

func SmartRoutingRefreshSiteUserGroup(ctx context.Context, siteUserGroupID int) error {
	if siteUserGroupID <= 0 {
		return nil
	}
	var groupIDs []int
	if err := db.GetDB().WithContext(ctx).Model(&model.GroupItem{}).
		Joins("JOIN site_channel_bindings ON site_channel_bindings.channel_id = group_items.channel_id").
		Where("site_channel_bindings.site_user_group_id = ?", siteUserGroupID).
		Distinct("group_items.group_id").
		Order("group_items.group_id ASC").
		Pluck("group_items.group_id", &groupIDs).Error; err != nil {
		return fmt.Errorf("list routing groups for site user group %d: %w", siteUserGroupID, err)
	}
	for _, groupID := range groupIDs {
		group, err := GroupGet(groupID, ctx)
		if err != nil {
			return fmt.Errorf("load routing group %d for site user group %d: %w", groupID, siteUserGroupID, err)
		}
		if group.Mode != model.GroupModeSmartFailover {
			continue
		}
		if err := SmartRoutingRefreshGroup(ctx, groupID); err != nil {
			return fmt.Errorf("refresh routing group %d for site user group %d: %w", groupID, siteUserGroupID, err)
		}
	}
	return nil
}

func loadSmartRoutingProfile(ctx context.Context, item model.GroupItem) (smartRoutingProfile, error) {
	var logs []model.RelayLog
	cutoff := time.Now().Add(-smartRoutingHistoryWindow).Unix()
	err := db.GetDB().WithContext(ctx).
		Select("time", "success", "ftut", "input_tokens", "bill_input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens").
		Where("time >= ? AND channel_id = ? AND LOWER(actual_model_name) = ?", cutoff, item.ChannelID, strings.ToLower(item.ModelName)).
		Order("time DESC").Limit(smartRoutingSampleLimit).Find(&logs).Error
	if err != nil {
		return smartRoutingProfile{}, err
	}

	profile := smartRoutingProfile{speedSource: "unknown"}
	var successCount int
	for i, entry := range logs {
		if i == 0 {
			at := time.Unix(entry.Time, 0)
			profile.lastRequest = &at
		}
		if !entry.Success {
			continue
		}
		successCount++
	}
	profile.sampleCount = len(logs)
	if len(logs) > 0 {
		profile.successRate = float64(successCount) / float64(len(logs))
	}

	price, priceErr := LLMGet(strings.ToLower(item.ModelName))
	if priceErr == nil {
		multiplier := resolveGroupItemPriceMultiplier(ctx, item.ChannelID)
		profile.expectedCost, profile.priceKnown = expectedCostFromLogs(logs, price, multiplier)
	}

	if ttft, ok := medianTTFT(logs); ok {
		profile.speedMS = float64(ttft)
		profile.speedSource = "ttft"
	} else if item.LastProbeSuccess && item.LastProbeMS > 0 {
		profile.speedMS = float64(item.LastProbeMS)
		profile.speedSource = "probe"
	} else if duration, ok := latestProbeDuration(ctx, item); ok {
		profile.speedMS = float64(duration)
		profile.speedSource = "probe"
	} else if channel, getErr := ChannelGet(item.ChannelID, ctx); getErr == nil {
		for _, baseURL := range channel.BaseUrls {
			if baseURL.Delay > 0 && (profile.speedMS == 0 || float64(baseURL.Delay) < profile.speedMS) {
				profile.speedMS = float64(baseURL.Delay)
				profile.speedSource = "head"
			}
		}
	}
	return profile, nil
}

func medianTTFT(logs []model.RelayLog) (int, bool) {
	values := make([]int, 0, len(logs))
	for _, entry := range logs {
		if entry.Success && entry.Ftut > 0 {
			values = append(values, entry.Ftut)
		}
	}
	if len(values) == 0 {
		return 0, false
	}
	sort.Ints(values)
	return values[len(values)/2], true
}

func expectedCostFromLogs(logs []model.RelayLog, price model.LLMPrice, multiplier float64) (float64, bool) {
	if multiplier < 0 {
		multiplier = 1
	}
	var billInput, output, cacheRead, cacheWrite float64
	var successCount int
	for _, entry := range logs {
		if !entry.Success {
			continue
		}
		successCount++
		inputTokens := entry.InputTokens
		if entry.BillInputTokens != nil {
			inputTokens = *entry.BillInputTokens
		}
		billInput += float64(inputTokens)
		output += float64(entry.OutputTokens)
		if entry.CacheReadTokens != nil {
			cacheRead += float64(*entry.CacheReadTokens)
		}
		if entry.CacheWriteTokens != nil {
			cacheWrite += float64(*entry.CacheWriteTokens)
		}
	}
	if successCount == 0 {
		return 0, false
	}
	count := float64(successCount)
	cost := ((billInput/count)*price.Input + (output/count)*price.Output +
		(cacheRead/count)*price.CacheRead + (cacheWrite/count)*price.CacheWrite) * 1e-6 * multiplier
	return cost, true
}

func SmartRoutingRecordProbe(ctx context.Context, channelID int, modelName string, durationMS int64, success bool) error {
	now := time.Now()
	var groupIDs []int
	if err := db.GetDB().WithContext(ctx).Model(&model.GroupItem{}).
		Where("channel_id = ? AND model_name = ?", channelID, modelName).
		Distinct("group_id").Pluck("group_id", &groupIDs).Error; err != nil {
		return err
	}
	if err := db.GetDB().WithContext(ctx).Model(&model.GroupItem{}).
		Where("channel_id = ? AND model_name = ?", channelID, modelName).
		Updates(map[string]any{
			"last_probe_at":      now,
			"last_probe_ms":      durationMS,
			"last_probe_success": success,
		}).Error; err != nil {
		return err
	}
	return groupRefreshCacheByIDs(groupIDs, ctx)
}

func latestProbeDuration(ctx context.Context, item model.GroupItem) (int64, bool) {
	var attempt model.GroupHealthAttempt
	err := db.GetDB().WithContext(ctx).
		Where("channel_id = ? AND model_name = ? AND status = ? AND duration_ms > 0", item.ChannelID, item.ModelName, model.GroupHealthAttemptStatusSuccess).
		Order("id DESC").First(&attempt).Error
	return attempt.DurationMS, err == nil
}

func resolveGroupItemPriceMultiplier(ctx context.Context, channelID int) float64 {
	binding, err := SiteChannelBindingGetByChannelID(channelID, ctx)
	if err != nil || binding.SiteUserGroupID == nil {
		return 1
	}
	var group model.SiteUserGroup
	if err := db.GetDB().WithContext(ctx).Select("price_multiplier").First(&group, *binding.SiteUserGroupID).Error; err != nil || group.PriceMultiplier < 0 {
		return 1
	}
	return group.PriceMultiplier
}

func normalizeLowerIsBetter(items []model.GroupItem, profiles map[int]smartRoutingProfile, value func(smartRoutingProfile) (float64, bool)) map[int]float64 {
	result := make(map[int]float64, len(items))
	minValue, maxValue := 0.0, 0.0
	hasValue := false
	for _, item := range items {
		current, known := value(profiles[item.ID])
		if !known {
			continue
		}
		if !hasValue || current < minValue {
			minValue = current
		}
		if !hasValue || current > maxValue {
			maxValue = current
		}
		hasValue = true
	}
	for _, item := range items {
		current, known := value(profiles[item.ID])
		if !known || !hasValue {
			result[item.ID] = 0
		} else if maxValue == minValue {
			result[item.ID] = 1
		} else {
			result[item.ID] = 1 - (current-minValue)/(maxValue-minValue)
		}
	}
	return result
}

// smartRoutingPriceScores makes the configured site-group multiplier the
// primary comparable price signal. Real request cost refines candidates with
// the same multiplier, but cannot let a traffic-shape outlier outrank a
// materially cheaper site. Unknown-cost candidates still participate through
// their multiplier without inventing an expected token cost.
func smartRoutingPriceScores(items []model.GroupItem, profiles map[int]smartRoutingProfile, multipliers map[int]float64) map[int]float64 {
	result := make(map[int]float64, len(items))
	known := make([]model.GroupItem, 0, len(items))
	for _, item := range items {
		if profiles[item.ID].priceKnown {
			known = append(known, item)
		}
	}
	knownProfiles := make(map[int]smartRoutingProfile, len(known))
	for _, item := range known {
		knownProfiles[item.ID] = profiles[item.ID]
	}
	measuredScores := normalizeLowerIsBetter(known, knownProfiles, func(profile smartRoutingProfile) (float64, bool) {
		return profile.expectedCost, profile.priceKnown
	})
	multiplierScores := normalizeConfiguredPriceMultipliers(items, multipliers)
	for _, item := range items {
		score := multiplierScores[item.ID]
		if profiles[item.ID].priceKnown {
			score = score*smartRoutingMultiplierWeight + measuredScores[item.ID]*smartRoutingMeasuredCostWeight
		}
		result[item.ID] = score
	}
	return result
}

// normalizeConfiguredPriceMultipliers preserves price ratios instead of
// compressing 0.05 and 0.12 into nearly the same min/max score. A zero
// multiplier is free and receives the top score unless every candidate is
// also zero, in which case the signal is neutral.
func normalizeConfiguredPriceMultipliers(items []model.GroupItem, multipliers map[int]float64) map[int]float64 {
	result := make(map[int]float64, len(items))
	minPositive, maxMultiplier := 0.0, 0.0
	hasZero := false
	for _, item := range items {
		multiplier := multipliers[item.ID]
		if multiplier < 0 {
			multiplier = 1
		}
		if multiplier == 0 {
			hasZero = true
			continue
		}
		if minPositive == 0 || multiplier < minPositive {
			minPositive = multiplier
		}
		if multiplier > maxMultiplier {
			maxMultiplier = multiplier
		}
	}
	if minPositive == 0 {
		for _, item := range items {
			result[item.ID] = 0.5
		}
		return result
	}
	if maxMultiplier == minPositive {
		for _, item := range items {
			multiplier := multipliers[item.ID]
			if multiplier < 0 {
				multiplier = 1
			}
			if hasZero && multiplier == 0 {
				result[item.ID] = 1
			} else if hasZero {
				result[item.ID] = 0
			} else {
				result[item.ID] = 0.5
			}
		}
		return result
	}
	maxInverse := 1 / minPositive
	minInverse := 1 / maxMultiplier
	for _, item := range items {
		multiplier := multipliers[item.ID]
		if multiplier < 0 {
			multiplier = 1
		}
		if multiplier == 0 {
			result[item.ID] = 1
			continue
		}
		result[item.ID] = ((1 / multiplier) - minInverse) / (maxInverse - minInverse)
	}
	return result
}

func normalizedSmartWeights(group model.Group) (int, int) {
	if group.PriceWeight < 0 || group.SpeedWeight < 0 || group.PriceWeight+group.SpeedWeight != 100 {
		return 70, 30
	}
	return group.PriceWeight, group.SpeedWeight
}

func profileHealthState(profile smartRoutingProfile, item model.GroupItem) model.GroupItemHealthState {
	if item.LastProbeSuccess && item.LastProbeAt != nil &&
		(profile.lastRequest == nil || item.LastProbeAt.After(*profile.lastRequest)) {
		return model.GroupItemHealthHealthy
	}
	if profile.sampleCount < 5 {
		return model.GroupItemHealthUnknown
	}
	if profile.successRate < 0.8 {
		return model.GroupItemHealthDegraded
	}
	return model.GroupItemHealthHealthy
}

func smartHealthRank(state model.GroupItemHealthState) int {
	switch state {
	case model.GroupItemHealthHealthy, model.GroupItemHealthUnknown:
		return 0
	default:
		return 1
	}
}

func effectivePriceMultiplier(multiplier float64) float64 {
	if multiplier < 0 {
		return 1
	}
	return multiplier
}
