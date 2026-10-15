package service

import (
	"encoding/json"
	"errors"
	"os"
	"strings"
)

const antigravityPriorityOffset = 1000

// The transform is its own inverse: plugin values increase with preference,
// while native scheduler values decrease. Keep zero and cooldown values distinct.
func translateAntigravityPriority(value int) (int, error) {
	const maxInt = int(^uint(0) >> 1)
	if value < antigravityPriorityOffset-maxInt {
		return 0, errors.New("priority value overflow")
	}
	return antigravityPriorityOffset - value, nil
}

func validateAntigravityPriorityMapping(path string) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return errors.New("priority mapping migration required; run the matching recovery tool before starting this build")
	}
	var marker struct {
		SchemaVersion int `json:"schema_version"`
		NativeOffset  int `json:"native_offset"`
	}
	if json.Unmarshal(raw, &marker) != nil || marker.SchemaVersion != 1 || marker.NativeOffset != antigravityPriorityOffset {
		return errors.New("unsupported priority mapping; refusing to reinterpret existing account priorities")
	}
	return nil
}

// Native zero is the API's unspecified creation default. Preserve the plugin's
// logical zero for new OAuth accounts without changing explicit priorities.
func initialAntigravityNativePriority(platform, kind string, value int) int {
	enabled := strings.TrimSpace(os.Getenv("ANTIGRAVITY_PRIORITY_ENABLED"))
	if value == 0 && platform == PlatformAntigravity && kind == AccountTypeOAuth && (enabled == "1" || strings.EqualFold(enabled, "true")) {
		return antigravityPriorityOffset
	}
	return value
}
