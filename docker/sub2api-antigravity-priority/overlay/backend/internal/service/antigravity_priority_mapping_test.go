package service

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestAntigravityPriorityOffsetRoundTrip(t *testing.T) {
	for _, pair := range [][2]int{{999, 1}, {998, 2}, {100, 900}, {99, 901}, {1, 999}, {0, 1000}, {-1, 1001}} {
		native, err := translateAntigravityPriority(pair[0])
		if err != nil || native != pair[1] {
			t.Fatalf("plugin %d mapped to %d, want %d: %v", pair[0], native, pair[1], err)
		}
		logical, err := translateAntigravityPriority(native)
		if err != nil || logical != pair[0] {
			t.Fatal("offset conversion did not round-trip")
		}
	}
	const maxInt = int(^uint(0) >> 1)
	for _, value := range []int{-maxInt - 1, 999 - maxInt} {
		if _, err := translateAntigravityPriority(value); err == nil {
			t.Fatal("overflow was accepted")
		}
	}
	if _, err := translateAntigravityPriority(1000 - maxInt); err != nil {
		t.Fatal("valid integer boundary rejected")
	}
}

func TestAntigravityPriorityRequiresCompletedMappingMigration(t *testing.T) {
	file := filepath.Join(t.TempDir(), "priority-mapping.json")
	if err := validateAntigravityPriorityMapping(file); err == nil {
		t.Fatal("missing migration marker accepted")
	}
	for _, raw := range []string{`{}`, `null`, `{"schema_version":1,"native_offset":0}`, `{"schema_version":2,"native_offset":1000}`, `{"schema_version":1,"native_offset":1000}`} {
		if err := os.WriteFile(file, []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
		err := validateAntigravityPriorityMapping(file)
		valid := raw == `{"schema_version":1,"native_offset":1000}`
		if (err == nil) != valid {
			t.Fatalf("migration marker acceptance mismatch: %v", err)
		}
	}
}

func TestAntigravityPriorityListReturnsLogicalValuesAndPreservesConcurrency(t *testing.T) {
	host, repo := priorityTestHost()
	repo.accounts[1].Concurrency = 10
	files, err := host.ListAuthFiles(t.Context())
	if err != nil || len(files) != 1 || files[0].Priority != 100 {
		t.Fatal("account inventory does not expose plugin priority 100")
	}
	if err := host.SaveAuth(t.Context(), "original-cpa-id", json.RawMessage(`{"priority":999,"disabled":false}`)); err != nil {
		t.Fatal(err)
	}
	if repo.accounts[1].Priority != 1 || repo.accounts[1].Concurrency != 10 || repo.update.Concurrency != nil {
		t.Fatal("plugin write changed administrator concurrency")
	}
}

func TestAntigravityPriorityNewAccountDefaultIsUnassigned(t *testing.T) {
	t.Setenv("ANTIGRAVITY_PRIORITY_ENABLED", "true")
	if initialAntigravityNativePriority(PlatformAntigravity, AccountTypeOAuth, 0) != 1000 {
		t.Fatal("new OAuth default would be interpreted as a boost")
	}
	for _, explicit := range []int{1, 900, 1000, 1001} {
		if initialAntigravityNativePriority(PlatformAntigravity, AccountTypeOAuth, explicit) != explicit {
			t.Fatal("explicit administrator priority changed")
		}
	}
	if initialAntigravityNativePriority(PlatformAntigravity, AccountTypeUpstream, 0) != 0 || initialAntigravityNativePriority(PlatformOpenAI, AccountTypeOAuth, 0) != 0 {
		t.Fatal("unmanaged account creation default changed")
	}
	t.Setenv("ANTIGRAVITY_PRIORITY_ENABLED", "false")
	if initialAntigravityNativePriority(PlatformAntigravity, AccountTypeOAuth, 0) != 0 {
		t.Fatal("disabled plugin changed account defaults")
	}
}
