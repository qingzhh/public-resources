package service

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"antigravity-priority/bridge"
)

func TestAntigravityPriorityOriginalResetPersistsAbsentPriority(t *testing.T) {
	host, repo := priorityTestHost()
	host.statePath = filepath.Join(t.TempDir(), "host-state.json")
	host.missing = map[int64]bool{}
	engine, err := bridge.New(context.Background(), bridge.Options{Host: host, StateCachePath: filepath.Join(t.TempDir(), "cache.json"), ConfigJSON: []byte(`{"enabled":true,"auto_apply":false}`)})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := engine.Shutdown(context.Background()); err != nil {
			t.Error(err)
		}
	})
	recorder := httptest.NewRecorder()
	engine.Handler().ServeHTTP(recorder, httptest.NewRequest("POST", "/api/v1/admin/antigravity-priority/reset", nil))
	if recorder.Code != 200 || repo.accounts[1].Priority != 1000 {
		t.Fatalf("original reset failed: status=%d priority=%d", recorder.Code, repo.accounts[1].Priority)
	}
	document, err := host.GetAuth(context.Background(), "original-cpa-id")
	if err != nil {
		t.Fatal(err)
	}
	fields := map[string]json.RawMessage{}
	if err = json.Unmarshal(document.JSON, &fields); err != nil {
		t.Fatal(err)
	}
	if _, present := fields["priority"]; present {
		t.Fatal("reset priority remains explicit instead of absent")
	}
	missing, err := loadPriorityMissing(host.statePath)
	if err != nil || !missing[1] {
		t.Fatal("reset state not persistent")
	}
	restarted := &antigravityPriorityHost{repo: repo, identities: host.identities, statePath: host.statePath, missing: missing}
	files, err := restarted.ListAuthFiles(context.Background())
	if err != nil || len(files) != 1 || !files[0].PriorityMissing {
		t.Fatal("reset semantics lost on restart")
	}
	if err = restarted.SaveAuth(context.Background(), "original-cpa-id", json.RawMessage(`{"auth_index":"original-cpa-id","priority":0,"disabled":false}`)); err != nil {
		t.Fatal(err)
	}
	files, err = restarted.ListAuthFiles(context.Background())
	if err != nil || files[0].PriorityMissing {
		t.Fatal("explicit zero cannot be distinguished from absent priority")
	}
	if repo.accounts[1].GetCredential("access_token") != "old" || repo.accounts[1].GetCredential("refresh_token") != "retained" {
		t.Fatal("reset modified OAuth credentials")
	}
}
