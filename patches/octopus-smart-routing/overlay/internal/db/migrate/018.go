package migrate

import (
	"fmt"

	"github.com/bestruirui/octopus/internal/model"
	"gorm.io/gorm"
)

func init() {
	RegisterAfterAutoMigration(Migration{Version: 18, Up: migrateSmartFailoverFields})
}

func migrateSmartFailoverFields(db *gorm.DB) error {
	if db == nil {
		return fmt.Errorf("db is nil")
	}
	if db.Migrator().HasTable(&model.Group{}) {
		if err := db.Model(&model.Group{}).
			Where("price_weight + speed_weight <> ? OR price_weight IS NULL OR speed_weight IS NULL", 100).
			Updates(map[string]any{"price_weight": 70, "speed_weight": 30}).Error; err != nil {
			return err
		}
	}
	if db.Migrator().HasTable(&model.GroupItem{}) {
		if err := db.Model(&model.GroupItem{}).Where("price_multiplier <= 0 OR price_multiplier IS NULL").Update("price_multiplier", 1).Error; err != nil {
			return err
		}
		if err := db.Model(&model.GroupItem{}).Where("health_state = '' OR health_state IS NULL").Update("health_state", model.GroupItemHealthUnknown).Error; err != nil {
			return err
		}
	}
	if db.Migrator().HasTable(&model.SiteUserGroup{}) {
		return db.Model(&model.SiteUserGroup{}).Where("price_multiplier <= 0 OR price_multiplier IS NULL").Update("price_multiplier", 1).Error
	}
	return nil
}
