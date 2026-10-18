package model

import "testing"

func TestSmartProbeIntervalAllowsZeroAndRejectsNegative(t *testing.T) {
	if err := (&Setting{Key: SettingKeySmartProbeInterval, Value: "0"}).Validate(); err != nil {
		t.Fatalf("zero interval rejected: %v", err)
	}
	if err := (&Setting{Key: SettingKeySmartProbeInterval, Value: "-1"}).Validate(); err == nil {
		t.Fatal("negative interval must be rejected")
	}
}
