package handlers

import (
	"testing"

	"github.com/bestruirui/octopus/internal/model"
	"github.com/bestruirui/octopus/internal/relay/balancer"
)

func TestSelectProbeKeysFallsBackToAllEnabledKeys(t *testing.T) {
	keys := []model.ChannelKey{
		{ID: 1, Enabled: true, ChannelKey: "first"},
		{ID: 2, Enabled: false, ChannelKey: "disabled"},
		{ID: 3, Enabled: true, ChannelKey: "  "},
		{ID: 4, Enabled: true, ChannelKey: "fourth"},
	}

	selected := selectProbeKeys(keys, nil)
	if len(selected) != 2 || selected[0].ID != 1 || selected[1].ID != 4 {
		t.Fatalf("selected keys = %+v, want IDs [1 4]", selected)
	}
}

func TestSelectProbeKeysLimitsProbeToTrippedKeys(t *testing.T) {
	keys := []model.ChannelKey{
		{ID: 1, Enabled: true, ChannelKey: "first"},
		{ID: 2, Enabled: true, ChannelKey: "second"},
		{ID: 3, Enabled: true, ChannelKey: "third"},
	}
	tripped := map[int]balancer.CircuitSnapshot{
		2: {},
		3: {},
	}

	selected := selectProbeKeys(keys, tripped)
	if len(selected) != 2 || selected[0].ID != 2 || selected[1].ID != 3 {
		t.Fatalf("selected keys = %+v, want IDs [2 3]", selected)
	}
}
