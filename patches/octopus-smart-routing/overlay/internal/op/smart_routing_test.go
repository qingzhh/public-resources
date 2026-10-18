package op

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	dbpkg "github.com/bestruirui/octopus/internal/db"
	"github.com/bestruirui/octopus/internal/model"
	"github.com/bestruirui/octopus/internal/transformer/outbound"
)

func TestSmartRoutingUsesOnlyRealSuccessfulTokenProfile(t *testing.T) {
	logs := []model.RelayLog{
		{Success: true, BillInputTokens: intPointer(100), CacheReadTokens: intPointer(200), CacheWriteTokens: intPointer(50), OutputTokens: 25},
		{Success: false, BillInputTokens: intPointer(9999), OutputTokens: 9999},
	}
	price := model.LLMPrice{Input: 2, Output: 8, CacheRead: 0.5, CacheWrite: 3}
	cost, known := expectedCostFromLogs(logs, price, 0.2)
	if !known {
		t.Fatal("expected price profile to be known")
	}
	want := (100*2 + 25*8 + 200*0.5 + 50*3) * 1e-6 * 0.2
	if cost != want {
		t.Fatalf("cost = %.12f, want %.12f", cost, want)
	}
	zeroCost, zeroKnown := expectedCostFromLogs(logs, price, 0)
	if !zeroKnown || zeroCost != 0 {
		t.Fatalf("zero multiplier cost = %.12f known=%t, want 0 true", zeroCost, zeroKnown)
	}
}

func TestSmartRoutingDoesNotInventTokenProfile(t *testing.T) {
	cost, known := expectedCostFromLogs([]model.RelayLog{{Success: false, InputTokens: 1000, OutputTokens: 250}}, model.LLMPrice{Input: 1, Output: 1}, 1)
	if known || cost != 0 {
		t.Fatalf("cost=%f known=%t, want unknown zero profile", cost, known)
	}
}

func TestSmartRoutingUnknownPriceUsesConfiguredMultiplier(t *testing.T) {
	items := []model.GroupItem{
		{ID: 1},
		{ID: 2},
		{ID: 3},
	}
	profiles := map[int]smartRoutingProfile{
		1: {priceKnown: false},
		2: {priceKnown: false},
		3: {priceKnown: false},
	}
	scores := smartRoutingPriceScores(items, profiles, map[int]float64{1: 0.1, 2: 0.3, 3: 0.1})
	if scores[1] != 1 || scores[3] != 1 || scores[2] != 0 {
		t.Fatalf("unknown price scores = %#v, want equal low multipliers first", scores)
	}

	neutral := smartRoutingPriceScores([]model.GroupItem{{ID: 4}}, map[int]smartRoutingProfile{4: {priceKnown: false}}, map[int]float64{4: 0.1})
	if neutral[4] != 0.5 {
		t.Fatalf("equal/only unknown multiplier score = %v, want neutral 0.5", neutral[4])
	}

	mixedItems := []model.GroupItem{{ID: 5}, {ID: 6}, {ID: 7}, {ID: 12}}
	mixedProfiles := map[int]smartRoutingProfile{
		5:  {priceKnown: true, expectedCost: 100},
		6:  {priceKnown: false},
		7:  {priceKnown: true, expectedCost: 1},
		12: {priceKnown: true, expectedCost: 2},
	}
	mixed := smartRoutingPriceScores(mixedItems, mixedProfiles, map[int]float64{5: 0.08, 6: 0.12, 7: 1, 12: 0.05})
	if mixed[5] <= mixed[6] || mixed[6] <= mixed[7] {
		t.Fatalf("mixed known/unknown price scores = %#v, want configured multiplier to remain primary", mixed)
	}

	zero := smartRoutingPriceScores([]model.GroupItem{{ID: 10}, {ID: 11}}, map[int]smartRoutingProfile{
		10: {priceKnown: false},
		11: {priceKnown: false},
	}, map[int]float64{10: 0, 11: 1})
	if zero[10] <= zero[11] {
		t.Fatalf("zero multiplier scores = %#v, want zero cost first", zero)
	}

	negative := smartRoutingPriceScores([]model.GroupItem{{ID: 8}, {ID: 9}}, map[int]smartRoutingProfile{
		8: {priceKnown: false},
		9: {priceKnown: false},
	}, map[int]float64{8: -1, 9: 1})
	if negative[8] != 0.5 || negative[9] != 0.5 {
		t.Fatalf("negative multiplier fallback scores = %#v, want neutral fallback", negative)
	}
}

func TestSmartRoutingAutoRankStrictlyPrefersLowerMultiplier(t *testing.T) {
	items := []model.GroupItem{
		{ID: 1, Priority: 1, HealthState: model.GroupItemHealthHealthy, PriceMultiplier: 0.15, CompositeScore: 0.95},
		{ID: 2, Priority: 2, HealthState: model.GroupItemHealthUnknown, PriceMultiplier: 0.08, CompositeScore: 0.05},
		{ID: 3, Priority: 3, HealthState: model.GroupItemHealthHealthy, PriceMultiplier: 1, CompositeScore: 1},
		{ID: 4, Priority: 4, HealthState: model.GroupItemHealthHealthy, PriceMultiplier: 0.1, CompositeScore: 0},
		{ID: 5, Priority: 5, HealthState: model.GroupItemHealthDegraded, PriceMultiplier: 0, CompositeScore: 1},
	}

	sortSmartRoutingItems(items)

	want := []int{2, 4, 1, 3, 5}
	for index, item := range items {
		if item.ID != want[index] {
			t.Fatalf("rank %d item = %d (x%g, %s), want item %d", index+1, item.ID, item.PriceMultiplier, item.HealthState, want[index])
		}
	}
}

func TestMedianTTFTIgnoresTotalDurationAndFailures(t *testing.T) {
	logs := []model.RelayLog{
		{Success: true, Ftut: 300, UseTime: 9000},
		{Success: true, Ftut: 100, UseTime: 8000},
		{Success: true, Ftut: 200, UseTime: 7000},
		{Success: false, Ftut: 1, UseTime: 1},
	}
	value, known := medianTTFT(logs)
	if !known || value != 200 {
		t.Fatalf("median TTFT = %d known=%t, want 200 true", value, known)
	}

	if value, known := medianTTFT([]model.RelayLog{{Success: true, UseTime: 42}}); known || value != 0 {
		t.Fatalf("total duration must not be used as TTFT: %d known=%t", value, known)
	}
}

func TestSmartWeightsValidationAndDefaults(t *testing.T) {
	group := model.Group{Mode: model.GroupModeSmartFailover}
	normalizeSmartGroupDefaults(&group)
	if group.PriceWeight != 70 || group.SpeedWeight != 30 || group.SessionKeepTime != 30*60 {
		t.Fatalf("smart defaults = weights %d/%d session %d", group.PriceWeight, group.SpeedWeight, group.SessionKeepTime)
	}
	if err := validateSmartWeights(70, 20); err == nil {
		t.Fatal("expected invalid weight total to be rejected")
	}
	if err := validateSmartWeights(70, 30); err != nil {
		t.Fatalf("expected 70/30 weights to pass: %v", err)
	}
}

func TestSmartRoutingRecordProbePersistsObservation(t *testing.T) {
	if dbpkg.GetDB() != nil {
		_ = dbpkg.Close()
	}
	databasePath := filepath.Join(t.TempDir(), "smart-routing-probe.db")
	if err := dbpkg.InitDB("sqlite", databasePath, false); err != nil {
		t.Fatalf("init db: %v", err)
	}
	t.Cleanup(func() { _ = dbpkg.Close() })
	if err := InitCache(); err != nil {
		t.Fatalf("init cache: %v", err)
	}

	ctx := context.Background()
	group := &model.Group{Name: "smart-probe", Mode: model.GroupModeSmartFailover}
	if err := GroupCreate(group, ctx); err != nil {
		t.Fatalf("create group: %v", err)
	}
	item := &model.GroupItem{GroupID: group.ID, ChannelID: 77, ModelName: "probe-model", Priority: 1, Weight: 1}
	if err := GroupItemAdd(item, ctx); err != nil {
		t.Fatalf("create item: %v", err)
	}
	if err := SmartRoutingRecordProbe(ctx, item.ChannelID, item.ModelName, 321, true); err != nil {
		t.Fatalf("record probe: %v", err)
	}

	var stored model.GroupItem
	if err := dbpkg.GetDB().WithContext(ctx).First(&stored, item.ID).Error; err != nil {
		t.Fatalf("reload item: %v", err)
	}
	if stored.LastProbeAt == nil || stored.LastProbeMS != 321 || !stored.LastProbeSuccess {
		t.Fatalf("probe observation not persisted: at=%v ms=%d success=%t", stored.LastProbeAt, stored.LastProbeMS, stored.LastProbeSuccess)
	}
}

func TestGroupUpdateToSmartFailoverRefreshesRankingImmediately(t *testing.T) {
	if dbpkg.GetDB() != nil {
		_ = dbpkg.Close()
	}
	databasePath := filepath.Join(t.TempDir(), "smart-routing-group-update.db")
	if err := dbpkg.InitDB("sqlite", databasePath, false); err != nil {
		t.Fatalf("init db: %v", err)
	}
	t.Cleanup(func() { _ = dbpkg.Close() })
	if err := InitCache(); err != nil {
		t.Fatalf("init cache: %v", err)
	}

	ctx := context.Background()
	group := &model.Group{
		Name: "smart-update",
		Mode: model.GroupModeFailover,
		Items: []model.GroupItem{
			{ChannelID: 101, ModelName: "smart-model-a", Priority: 1, Weight: 1},
			{ChannelID: 102, ModelName: "smart-model-b", Priority: 2, Weight: 1},
		},
	}
	if err := GroupCreate(group, ctx); err != nil {
		t.Fatalf("create group: %v", err)
	}

	mode := model.GroupModeSmartFailover
	updated, err := GroupUpdate(&model.GroupUpdateRequest{ID: group.ID, Mode: &mode}, ctx)
	if err != nil {
		t.Fatalf("update group mode: %v", err)
	}
	if updated.Mode != model.GroupModeSmartFailover {
		t.Fatalf("updated mode = %d, want %d", updated.Mode, model.GroupModeSmartFailover)
	}
	if len(updated.Items) != 2 {
		t.Fatalf("updated items = %d, want 2", len(updated.Items))
	}
	for _, item := range updated.Items {
		if item.AutoRank <= 0 || item.ScoreUpdatedAt == nil {
			t.Fatalf("item %d ranking was not refreshed: rank=%d updated_at=%v", item.ID, item.AutoRank, item.ScoreUpdatedAt)
		}
	}
}

func TestGroupUpdateAddingItemReturnsCurrentSiteGroupMultiplier(t *testing.T) {
	ctx := setupSiteOpTestDB(t)
	site, account := createSiteOpTestSiteAccount(t, ctx, "editor-price-site", "editor-price-account")

	siteGroup := model.SiteUserGroup{
		SiteAccountID:   account.ID,
		GroupKey:        "free",
		Name:            "Free",
		PriceMultiplier: 0.05,
	}
	if err := dbpkg.GetDB().WithContext(ctx).Create(&siteGroup).Error; err != nil {
		t.Fatalf("create site user group: %v", err)
	}
	channel := &model.Channel{
		Name:     "editor-price-channel",
		Type:     outbound.OutboundTypeOpenAIChat,
		Enabled:  true,
		BaseUrls: []model.BaseUrl{{URL: "https://example.com/v1"}},
		Keys:     []model.ChannelKey{{Enabled: true, ChannelKey: "test-key"}},
		Model:    "editor-price-model",
	}
	if err := ChannelCreate(channel, ctx); err != nil {
		t.Fatalf("create channel: %v", err)
	}
	if err := dbpkg.GetDB().WithContext(ctx).Create(&model.SiteChannelBinding{
		SiteID:          site.ID,
		SiteAccountID:   account.ID,
		SiteUserGroupID: &siteGroup.ID,
		GroupKey:        siteGroup.GroupKey,
		ChannelID:       channel.ID,
	}).Error; err != nil {
		t.Fatalf("create site channel binding: %v", err)
	}

	group := &model.Group{Name: "editor-price-group", Mode: model.GroupModeSmartFailover}
	if err := GroupCreate(group, ctx); err != nil {
		t.Fatalf("create smart group: %v", err)
	}
	updated, err := GroupUpdate(&model.GroupUpdateRequest{
		ID: group.ID,
		ItemsToAdd: []model.GroupItemAddRequest{{
			ChannelID: channel.ID,
			ModelName: "editor-price-model",
			Priority:  1,
			Weight:    1,
		}},
	}, ctx)
	if err != nil {
		t.Fatalf("add group item: %v", err)
	}
	if len(updated.Items) != 1 {
		t.Fatalf("updated items = %d, want 1", len(updated.Items))
	}
	item := updated.Items[0]
	if item.PriceMultiplier != siteGroup.PriceMultiplier {
		t.Fatalf("new item multiplier = %v, want %v", item.PriceMultiplier, siteGroup.PriceMultiplier)
	}
	if item.AutoRank != 1 || item.ScoreUpdatedAt == nil {
		t.Fatalf("new item ranking = %d updated_at=%v, want rank 1 with timestamp", item.AutoRank, item.ScoreUpdatedAt)
	}
}

func TestSmartRoutingReorderScheduleLifecycle(t *testing.T) {
	if dbpkg.GetDB() != nil {
		_ = dbpkg.Close()
	}
	databasePath := filepath.Join(t.TempDir(), "smart-routing-schedule.db")
	if err := dbpkg.InitDB("sqlite", databasePath, false); err != nil {
		t.Fatalf("init db: %v", err)
	}
	t.Cleanup(func() { _ = dbpkg.Close() })
	if err := InitCache(); err != nil {
		t.Fatalf("init cache: %v", err)
	}

	ctx := context.Background()
	manualOne, manualTwo := 1, 2
	group := &model.Group{
		Name:                 "smart-schedule",
		Mode:                 model.GroupModeSmartFailover,
		SmartReorderInterval: 5,
		Items: []model.GroupItem{
			{ChannelID: 201, ModelName: "scheduled-model-a", Priority: 1, Weight: 1, ManualRank: &manualOne},
			{ChannelID: 202, ModelName: "scheduled-model-b", Priority: 2, Weight: 1, ManualRank: &manualTwo},
		},
	}
	if err := GroupCreate(group, ctx); err != nil {
		t.Fatalf("create smart group: %v", err)
	}

	before := time.Now()
	if err := SmartRoutingRefreshGroupNow(ctx, group.ID, false); err != nil {
		t.Fatalf("immediate refresh preserving manual ranks: %v", err)
	}
	var started model.Group
	if err := dbpkg.GetDB().WithContext(ctx).First(&started, group.ID).Error; err != nil {
		t.Fatalf("reload started schedule: %v", err)
	}
	if started.SmartReorderStartedAt == nil || started.SmartReorderNextAt == nil {
		t.Fatalf("schedule was not started: started=%v next=%v", started.SmartReorderStartedAt, started.SmartReorderNextAt)
	}
	if started.SmartReorderStartedAt.Before(before) || started.SmartReorderNextAt.Sub(*started.SmartReorderStartedAt) != 5*time.Minute {
		t.Fatalf("unexpected immediate schedule: started=%v next=%v", started.SmartReorderStartedAt, started.SmartReorderNextAt)
	}
	assertManualRankCount(t, ctx, group.ID, 2)

	firstStartedAt := *started.SmartReorderStartedAt
	time.Sleep(time.Millisecond)
	if err := SmartRoutingRefreshGroupNow(ctx, group.ID, true); err != nil {
		t.Fatalf("immediate refresh clearing manual ranks: %v", err)
	}
	var restarted model.Group
	if err := dbpkg.GetDB().WithContext(ctx).First(&restarted, group.ID).Error; err != nil {
		t.Fatalf("reload restarted schedule: %v", err)
	}
	if restarted.SmartReorderStartedAt == nil || !restarted.SmartReorderStartedAt.After(firstStartedAt) {
		t.Fatalf("immediate refresh did not restart timer: first=%v restarted=%v", firstStartedAt, restarted.SmartReorderStartedAt)
	}
	assertManualRankCount(t, ctx, group.ID, 0)

	manualOne, manualTwo = 1, 2
	if err := dbpkg.GetDB().WithContext(ctx).Model(&model.GroupItem{}).Where("group_id = ?", group.ID).
		Updates(map[string]any{"manual_rank": 1}).Error; err != nil {
		t.Fatalf("restore manual ranks: %v", err)
	}
	dueNow := time.Now().Truncate(time.Second)
	dueAt := dueNow.Add(-11 * time.Minute)
	originalStartedAt := dueNow.Add(-30 * time.Minute)
	if err := dbpkg.GetDB().WithContext(ctx).Model(&model.Group{}).Where("id = ?", group.ID).Updates(map[string]any{
		"smart_reorder_interval":     5,
		"smart_reorder_clear_manual": false,
		"smart_reorder_started_at":   originalStartedAt,
		"smart_reorder_next_at":      dueAt,
	}).Error; err != nil {
		t.Fatalf("prepare due schedule: %v", err)
	}
	if err := groupRefreshCacheByID(group.ID, ctx); err != nil {
		t.Fatalf("refresh prepared group cache: %v", err)
	}
	if err := SmartRoutingRefreshDueGroups(ctx, dueNow); err != nil {
		t.Fatalf("refresh due groups preserving manual ranks: %v", err)
	}
	var advanced model.Group
	if err := dbpkg.GetDB().WithContext(ctx).First(&advanced, group.ID).Error; err != nil {
		t.Fatalf("reload advanced schedule: %v", err)
	}
	wantNext := dueAt.Add(15 * time.Minute)
	if advanced.SmartReorderNextAt == nil || !advanced.SmartReorderNextAt.Equal(wantNext) {
		t.Fatalf("next schedule = %v, want %v", advanced.SmartReorderNextAt, wantNext)
	}
	if advanced.SmartReorderStartedAt == nil || !advanced.SmartReorderStartedAt.Equal(originalStartedAt) {
		t.Fatalf("scheduled refresh changed start time: got %v want %v", advanced.SmartReorderStartedAt, originalStartedAt)
	}
	assertManualRankCount(t, ctx, group.ID, 2)

	clearDueAt := dueNow.Add(-time.Minute)
	if err := dbpkg.GetDB().WithContext(ctx).Model(&model.Group{}).Where("id = ?", group.ID).Updates(map[string]any{
		"smart_reorder_clear_manual": true,
		"smart_reorder_next_at":      clearDueAt,
	}).Error; err != nil {
		t.Fatalf("prepare clearing schedule: %v", err)
	}
	if err := groupRefreshCacheByID(group.ID, ctx); err != nil {
		t.Fatalf("refresh clearing group cache: %v", err)
	}
	if err := SmartRoutingRefreshDueGroups(ctx, dueNow); err != nil {
		t.Fatalf("refresh due groups clearing manual ranks: %v", err)
	}
	assertManualRankCount(t, ctx, group.ID, 0)

	zero := 0
	disabled, err := GroupUpdate(&model.GroupUpdateRequest{ID: group.ID, SmartReorderInterval: &zero}, ctx)
	if err != nil {
		t.Fatalf("disable smart schedule: %v", err)
	}
	if disabled.SmartReorderStartedAt != nil || disabled.SmartReorderNextAt != nil {
		t.Fatalf("disabled schedule retained timestamps: started=%v next=%v", disabled.SmartReorderStartedAt, disabled.SmartReorderNextAt)
	}

	positive := 3
	idle, err := GroupUpdate(&model.GroupUpdateRequest{ID: group.ID, SmartReorderInterval: &positive}, ctx)
	if err != nil {
		t.Fatalf("configure idle smart schedule: %v", err)
	}
	if idle.SmartReorderStartedAt != nil || idle.SmartReorderNextAt != nil {
		t.Fatalf("positive interval started without immediate refresh: started=%v next=%v", idle.SmartReorderStartedAt, idle.SmartReorderNextAt)
	}
}

func TestSmartRoutingImmediateRefreshRejectsNonSmartGroup(t *testing.T) {
	if dbpkg.GetDB() != nil {
		_ = dbpkg.Close()
	}
	databasePath := filepath.Join(t.TempDir(), "smart-routing-mode-guard.db")
	if err := dbpkg.InitDB("sqlite", databasePath, false); err != nil {
		t.Fatalf("init db: %v", err)
	}
	t.Cleanup(func() { _ = dbpkg.Close() })
	if err := InitCache(); err != nil {
		t.Fatalf("init cache: %v", err)
	}

	ctx := context.Background()
	group := &model.Group{Name: "not-smart", Mode: model.GroupModeFailover, SmartReorderInterval: 5}
	if err := GroupCreate(group, ctx); err != nil {
		t.Fatalf("create non-smart group: %v", err)
	}
	if err := SmartRoutingRefreshGroupNow(ctx, group.ID, false); !errors.Is(err, ErrSmartRoutingModeRequired) {
		t.Fatalf("immediate refresh error = %v, want %v", err, ErrSmartRoutingModeRequired)
	}
	var stored model.Group
	if err := dbpkg.GetDB().WithContext(ctx).First(&stored, group.ID).Error; err != nil {
		t.Fatalf("reload non-smart group: %v", err)
	}
	if stored.SmartReorderStartedAt != nil || stored.SmartReorderNextAt != nil {
		t.Fatalf("non-smart group started a schedule: started=%v next=%v", stored.SmartReorderStartedAt, stored.SmartReorderNextAt)
	}
}

func assertManualRankCount(t *testing.T, ctx context.Context, groupID int, want int64) {
	t.Helper()
	var count int64
	if err := dbpkg.GetDB().WithContext(ctx).Model(&model.GroupItem{}).
		Where("group_id = ? AND manual_rank IS NOT NULL", groupID).Count(&count).Error; err != nil {
		t.Fatalf("count manual ranks: %v", err)
	}
	if count != want {
		t.Fatalf("manual rank count = %d, want %d", count, want)
	}
}

func TestSuccessfulProbeNewerThanTrafficRestoresHealth(t *testing.T) {
	requestAt := time.Now().Add(-time.Minute)
	probeAt := time.Now()
	profile := smartRoutingProfile{sampleCount: 10, successRate: 0.2, lastRequest: &requestAt}
	item := model.GroupItem{LastProbeAt: &probeAt, LastProbeSuccess: true}
	if got := profileHealthState(profile, item); got != model.GroupItemHealthHealthy {
		t.Fatalf("health after newer successful probe = %q, want healthy", got)
	}

	newRequestAt := probeAt.Add(time.Minute)
	profile.lastRequest = &newRequestAt
	if got := profileHealthState(profile, item); got != model.GroupItemHealthDegraded {
		t.Fatalf("health after newer failed traffic = %q, want degraded", got)
	}
}

func TestSmartRoutingOnlyPushesDegradedCandidatesBehindRanking(t *testing.T) {
	if smartHealthRank(model.GroupItemHealthHealthy) != smartHealthRank(model.GroupItemHealthUnknown) {
		t.Fatal("healthy and unknown candidates must share the automatic ranking tier")
	}
	if smartHealthRank(model.GroupItemHealthDegraded) <= smartHealthRank(model.GroupItemHealthUnknown) {
		t.Fatal("degraded candidates must remain behind usable automatic candidates")
	}
}

func intPointer(value int) *int { return &value }
