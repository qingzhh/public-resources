package bridge

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"antigravity-priority/internal/config"
)

func TestAutomaticPrioritySyncRespectsPluginControls(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 10, 15, 12, 0, 0, 0, time.FixedZone("local", 8*60*60))
	for _, test := range []struct {
		name, raw       string
		autoApply, want bool
	}{
		{"enabled", `{"enabled":true}`, true, true},
		{"disabled", `{"enabled":false}`, true, false},
		{"manual_only", `{"enabled":true}`, false, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			engine, err := New(ctx, Options{Host: emptyHost{}, StateCachePath: filepath.Join(t.TempDir(), "cache.json"), ConfigJSON: []byte(test.raw)})
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				if err := engine.Shutdown(ctx); err != nil {
					t.Error(err)
				}
			})
			// auto_apply belongs to persisted dynamic configuration, not Register.
			dynamic := config.Default().Dynamic()
			dynamic.AutoApply = test.autoApply
			if err := engine.runtime.SetDynamicConfig(ctx, dynamic); err != nil {
				t.Fatal(err)
			}
			if engine.AutomaticPrioritySyncEnabled(now) != test.want {
				t.Fatal("automatic apply policy differs from sync policy")
			}
			for _, schedule := range []config.ScheduleConfig{
				{Paused: true, WindowStart: "09:00", WindowEnd: "23:00"},
				{WindowEnabled: true, WindowStart: "13:00", WindowEnd: "14:00"},
			} {
				if err := engine.runtime.SetScheduleConfig(ctx, schedule); err != nil {
					t.Fatal(err)
				}
				if engine.AutomaticPrioritySyncEnabled(now) {
					t.Fatal("paused/out-of-window synchronization is enabled")
				}
			}
			if err := engine.runtime.SetScheduleConfig(ctx, config.ScheduleConfig{WindowEnabled: true, WindowStart: "11:00", WindowEnd: "13:00"}); err != nil {
				t.Fatal(err)
			}
			if engine.AutomaticPrioritySyncEnabled(now) != test.want {
				t.Fatal("local schedule window was not honored")
			}
			if err := engine.Shutdown(ctx); err != nil {
				t.Fatal(err)
			}
			if engine.AutomaticPrioritySyncEnabled(now) {
				t.Fatal("synchronization remains enabled after shutdown")
			}
		})
	}
}

func TestAutomaticPrioritySyncPreservesScheduleAfterRestart(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 10, 15, 12, 0, 0, 0, time.FixedZone("local", 8*60*60))
	for _, schedule := range []config.ScheduleConfig{
		{Paused: true, WindowStart: "09:00", WindowEnd: "23:00"},
		{WindowEnabled: true, WindowStart: "13:00", WindowEnd: "14:00"},
	} {
		opts := Options{Host: emptyHost{}, StateCachePath: filepath.Join(t.TempDir(), "cache.json"), ConfigJSON: []byte(`{"enabled":true}`)}
		engine, err := New(ctx, opts)
		if err != nil {
			t.Fatal(err)
		}
		dynamic := config.Default().Dynamic()
		dynamic.AutoApply, dynamic.Schedule = true, schedule
		if err = engine.runtime.SetDynamicConfig(ctx, dynamic); err != nil {
			_ = engine.Shutdown(ctx)
			t.Fatal(err)
		}
		if err = engine.Shutdown(ctx); err != nil {
			t.Fatal(err)
		}
		restarted, err := New(ctx, opts)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if err := restarted.Shutdown(ctx); err != nil {
				t.Error(err)
			}
		})
		cfg, err := restarted.runtime.Config()
		if err != nil || !cfg.AutoApply {
			t.Fatal("restart fixture did not retain automatic apply")
		}
		if restarted.AutomaticPrioritySyncEnabled(now) {
			t.Fatal("restart ignored persisted pause or schedule window")
		}
	}
}
