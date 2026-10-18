package migrate

import (
	"testing"

	"github.com/bestruirui/octopus/internal/model"
	"github.com/glebarez/sqlite"
	"gorm.io/gorm"
)

func TestMigrateSmartFailoverFieldsBackfillsSafeDefaults(t *testing.T) {
	database, err := gorm.Open(sqlite.Open(":memory:"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	if err := database.AutoMigrate(&model.Group{}, &model.GroupItem{}, &model.SiteUserGroup{}); err != nil {
		t.Fatalf("auto migrate: %v", err)
	}

	group := model.Group{Name: "legacy", Mode: model.GroupModeFailover, PriceWeight: 70, SpeedWeight: 30}
	if err := database.Create(&group).Error; err != nil {
		t.Fatalf("create group: %v", err)
	}
	item := model.GroupItem{GroupID: group.ID, ChannelID: 1, ModelName: "test", Priority: 1, Weight: 1}
	if err := database.Create(&item).Error; err != nil {
		t.Fatalf("create item: %v", err)
	}
	siteGroup := model.SiteUserGroup{SiteAccountID: 1, GroupKey: "legacy", Name: "legacy"}
	if err := database.Create(&siteGroup).Error; err != nil {
		t.Fatalf("create site group: %v", err)
	}
	if err := database.Model(&model.Group{}).Where("id = ?", group.ID).Updates(map[string]any{"price_weight": 0, "speed_weight": 0}).Error; err != nil {
		t.Fatalf("seed invalid weights: %v", err)
	}
	if err := database.Model(&model.GroupItem{}).Where("id = ?", item.ID).Updates(map[string]any{"price_multiplier": 0, "health_state": ""}).Error; err != nil {
		t.Fatalf("seed invalid item: %v", err)
	}
	if err := database.Model(&model.SiteUserGroup{}).Where("id = ?", siteGroup.ID).Update("price_multiplier", 0).Error; err != nil {
		t.Fatalf("seed invalid multiplier: %v", err)
	}

	if err := migrateSmartFailoverFields(database); err != nil {
		t.Fatalf("first migration: %v", err)
	}
	if err := migrateSmartFailoverFields(database); err != nil {
		t.Fatalf("idempotent migration: %v", err)
	}
	if err := database.First(&group, group.ID).Error; err != nil {
		t.Fatalf("reload group: %v", err)
	}
	if group.PriceWeight != 70 || group.SpeedWeight != 30 {
		t.Fatalf("weights = %d/%d, want 70/30", group.PriceWeight, group.SpeedWeight)
	}
	if err := database.First(&item, item.ID).Error; err != nil {
		t.Fatalf("reload item: %v", err)
	}
	if item.PriceMultiplier != 1 || item.HealthState != model.GroupItemHealthUnknown {
		t.Fatalf("item defaults = multiplier %v health %q", item.PriceMultiplier, item.HealthState)
	}
	if err := database.First(&siteGroup, siteGroup.ID).Error; err != nil {
		t.Fatalf("reload site group: %v", err)
	}
	if siteGroup.PriceMultiplier != 1 {
		t.Fatalf("site group multiplier = %v, want 1", siteGroup.PriceMultiplier)
	}
}
