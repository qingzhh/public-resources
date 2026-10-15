package service

import (
	"context"
	"encoding/json"
	"errors"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"antigravity-priority/bridge"
)

func TestAntigravityPrioritySyncRepairsDriftAndSurvivesRestart(t *testing.T) {
	ctx := context.Background()
	host, repo := priorityTestHost()
	host.authorityPath = filepath.Join(t.TempDir(), "priority-authority.json")
	if changed, err := host.reconcilePriorities(ctx, false); err != nil || changed != 0 || repo.calls != 0 {
		t.Fatalf("initial adoption wrote accounts: changed=%d err=%v", changed, err)
	}
	if err := host.SaveAuth(ctx, "original-cpa-id", json.RawMessage(`{"priority":999,"disabled":false}`)); err != nil {
		t.Fatal(err)
	}
	account := repo.accounts[1]
	account.Priority, account.Concurrency, account.Schedulable, account.Status = 923, 10, false, StatusError
	account.Credentials["access_token"] = "refreshed"
	account.Extra = map[string]any{"native_setting": true}
	before := *account
	if changed, err := host.reconcilePriorities(ctx, true); err != nil || changed != 1 || account.Priority != 1 {
		t.Fatalf("plugin priority not restored: changed=%d err=%v", changed, err)
	}
	before.Priority = 1
	if !reflect.DeepEqual(before, *account) || !reflect.DeepEqual(repo.update, AccountBulkUpdate{Priority: repo.update.Priority}) {
		t.Fatal("priority reconciliation changed an unrelated native setting")
	}
	calls := repo.calls
	if changed, err := host.reconcilePriorities(ctx, true); err != nil || changed != 0 || calls != repo.calls {
		t.Fatal("unchanged inventory generated extra database writes")
	}
	priorities, err := loadPriorityAuthority(host.authorityPath)
	if err != nil || priorities[1] != 1 {
		t.Fatal("effective priority was not persisted")
	}
	account.Priority = 777
	restarted := &antigravityPriorityHost{repo: repo, identities: host.identities, authorityPath: host.authorityPath, priorities: priorities}
	if changed, err := restarted.reconcilePriorities(ctx, false); err != nil || changed != 0 || account.Priority != 777 {
		t.Fatal("disabled automatic apply unexpectedly wrote accounts")
	}
	if changed, err := restarted.reconcilePriorities(ctx, true); err != nil || changed != 1 || account.Priority != 1 {
		t.Fatal("restart adopted native drift over the saved plugin value")
	}
	info, err := os.Stat(host.authorityPath)
	if err != nil || info.Mode().Perm()&0077 != 0 {
		t.Fatal("authority state is not private")
	}
}

type priorityFailingRepo struct {
	*priorityRepoStub
	fail bool
}

func (r *priorityFailingRepo) BulkUpdate(ctx context.Context, ids []int64, update AccountBulkUpdate) (int64, error) {
	if r.fail {
		return 0, errors.New("injected write failure")
	}
	return r.priorityRepoStub.BulkUpdate(ctx, ids, update)
}

func TestAntigravityPrioritySyncRejectsFailedDecisionsAndRetries(t *testing.T) {
	ctx := context.Background()
	host, repo := priorityTestHost()
	failing := &priorityFailingRepo{priorityRepoStub: repo}
	host.repo = failing
	if _, err := host.reconcilePriorities(ctx, false); err != nil {
		t.Fatal(err)
	}
	failing.fail = true
	if err := host.SaveAuth(ctx, "original-cpa-id", json.RawMessage(`{"priority":999}`)); err == nil {
		t.Fatal("failed engine write reported success")
	}
	if host.priorities[1] != 900 {
		t.Fatal("a failed decision replaced the effective priority")
	}
	repo.accounts[1].Priority = 950
	if changed, err := host.reconcilePriorities(ctx, true); err == nil || changed != 0 {
		t.Fatal("failed synchronization was hidden")
	}
	failing.fail = false
	if changed, err := host.reconcilePriorities(ctx, true); err != nil || changed != 1 || repo.accounts[1].Priority != 900 {
		t.Fatal("synchronization did not recover after a transient write failure")
	}
}

func TestAntigravityPrioritySyncPreservesResetAndExplicitZero(t *testing.T) {
	ctx := context.Background()
	host, repo := priorityTestHost()
	directory := t.TempDir()
	host.authorityPath, host.statePath = filepath.Join(directory, "priority-authority.json"), filepath.Join(directory, "host-state.json")
	if err := host.SaveAuth(ctx, "original-cpa-id", json.RawMessage(`{"disabled":false}`)); err != nil {
		t.Fatal(err)
	}
	repo.accounts[1].Priority = 12
	if _, err := host.reconcilePriorities(ctx, true); err != nil || repo.accounts[1].Priority != 1000 || !host.priorityIsMissing(repo.accounts[1]) {
		t.Fatal("reset was replaced by native drift")
	}
	if err := host.SaveAuth(ctx, "original-cpa-id", json.RawMessage(`{"priority":0}`)); err != nil || host.priorityIsMissing(repo.accounts[1]) {
		t.Fatal("explicit zero was confused with reset")
	}
}

func TestAntigravityPrioritySyncAdoptsNewOAuthAndForgetsRemovedAccounts(t *testing.T) {
	ctx := context.Background()
	host, repo := priorityTestHost()
	if _, err := host.reconcilePriorities(ctx, false); err != nil {
		t.Fatal(err)
	}
	repo.accounts[2] = &Account{ID: 2, Platform: PlatformAntigravity, Type: AccountTypeOAuth, Priority: 1000, Concurrency: 10}
	repo.accounts[3] = &Account{ID: 3, Platform: PlatformAntigravity, Type: AccountTypeUpstream, Priority: 1}
	repo.accounts[4] = &Account{ID: 4, Platform: PlatformAnthropic, Type: AccountTypeOAuth, Priority: 1}
	if changed, err := host.reconcilePriorities(ctx, true); err != nil || changed != 0 || len(host.priorities) != 2 || host.priorities[2] != 1000 {
		t.Fatal("new OAuth adoption or other account filtering failed")
	}
	repo.accounts[2].Priority = 19
	if _, err := host.reconcilePriorities(ctx, true); err != nil || repo.accounts[2].Priority != 1000 {
		t.Fatal("newly adopted account is not synchronized")
	}
	delete(repo.accounts, 1)
	repo.accounts[2].Type = AccountTypeAPIKey
	if _, err := host.reconcilePriorities(ctx, true); err != nil || len(host.priorities) != 0 {
		t.Fatal("removed or ineligible accounts retained authority")
	}
}

func TestAntigravityPrioritySyncRetriesStatePersistence(t *testing.T) {
	ctx := context.Background()
	host, repo := priorityTestHost()
	directory := filepath.Join(t.TempDir(), "not-created")
	host.authorityPath = filepath.Join(directory, "priority-authority.json")
	if _, err := host.reconcilePriorities(ctx, true); err == nil || repo.calls != 0 {
		t.Fatal("missing authority storage did not stop synchronization")
	}
	if err := os.Mkdir(directory, 0700); err != nil {
		t.Fatal(err)
	}
	if _, err := host.reconcilePriorities(ctx, true); err != nil {
		t.Fatal(err)
	}
	for _, raw := range []string{`null`, `{}`, `{"schema_version":2,"native_offset":1000,"priorities":{}}`, `{"schema_version":1,"native_offset":0,"priorities":{}}`, `{"schema_version":1,"native_offset":1000,"priorities":{"-1":900}}`} {
		if err := os.WriteFile(host.authorityPath, []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := loadPriorityAuthority(host.authorityPath); err == nil {
			t.Fatal("invalid authority state was accepted")
		}
	}
}

func TestAntigravityPriorityDiscoversOAuthAfterEngineStarts(t *testing.T) {
	ctx := context.Background()
	host, repo := priorityTestHost()
	upstream := &priorityHTTPStub{status: 200, body: `{"models":{"gemini-2.5-pro":{"quotaInfo":{"windows":[{"name":"5h","remainingFraction":0.8,"resetTime":"2099-01-01T00:00:00Z"},{"name":"7d","remainingFraction":0.9,"resetTime":"2099-01-02T00:00:00Z"}]}},"claude-sonnet-4-5":{"quotaInfo":{"windows":[{"name":"5h","remainingFraction":0.8,"resetTime":"2099-01-01T00:00:00Z"},{"name":"7d","remainingFraction":0.9,"resetTime":"2099-01-02T00:00:00Z"}]}}}}`}
	host.upstream, host.tokens = upstream, &priorityTokenStub{value: "synthetic-token"}
	engine, err := bridge.New(ctx, bridge.Options{Host: host, StateCachePath: filepath.Join(t.TempDir(), "cache.json"), ConfigJSON: []byte(`{"enabled":true,"auto_apply":false}`)})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := engine.Shutdown(ctx); err != nil {
			t.Error(err)
		}
	})
	repo.accounts[2] = &Account{ID: 2, Name: "added-after-start", Platform: PlatformAntigravity, Type: AccountTypeOAuth, Priority: 1000, Status: StatusActive, Schedulable: true, Concurrency: 10, Credentials: map[string]any{"access_token": "synthetic", "project_id": "synthetic"}}
	repo.accounts[3] = &Account{ID: 3, Platform: PlatformAntigravity, Type: AccountTypeUpstream}
	repo.accounts[4] = &Account{ID: 4, Platform: PlatformAnthropic, Type: AccountTypeOAuth}
	request := func(path string) map[string]any {
		t.Helper()
		recorder := httptest.NewRecorder()
		method := "POST"
		if path == "snapshot/latest" {
			method = "GET"
		}
		engine.Handler().ServeHTTP(recorder, httptest.NewRequest(method, "/api/v1/admin/antigravity-priority/"+path, strings.NewReader(`{}`)))
		if recorder.Code != 200 {
			t.Fatalf("%s failed: HTTP %d", path, recorder.Code)
		}
		var result map[string]any
		if err := json.Unmarshal(recorder.Body.Bytes(), &result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	snapshot := request("sync")
	groups := snapshot["groups"].(map[string]any)
	for _, group := range []string{"gemini", "claude_gpt"} {
		items := groups[group].(map[string]any)["items"].([]any)
		if len(items) != 2 {
			t.Fatalf("%s has %d accounts after live addition", group, len(items))
		}
	}
	if host.identity(1) != "original-cpa-id" || host.identity(2) != "sub2api-2" {
		t.Fatal("stable imported/new identity changed")
	}
	result := request("run?mode=probe")
	outcomes, ok := result["probe_outcomes"].([]any)
	if !ok || len(outcomes) != 4 || upstream.calls != 2 || upstream.requestIDs[1] != 1 || upstream.requestIDs[2] != 1 {
		t.Fatalf("new account did not enter quota probing: outcomes=%d requests=%d", len(outcomes), upstream.calls)
	}
	for _, outcome := range outcomes {
		if outcome.(map[string]any)["status"] != "success" {
			t.Fatal("synthetic quota probe failed")
		}
	}
	preview := request("snapshot/latest")
	previewID, ok := preview["preview_id"].(string)
	if !ok || previewID == "" {
		t.Fatal("new account did not enter a fresh preview")
	}
	result = request("run?mode=apply&antigravity_model_group=gemini&preview_id=" + previewID)
	if result["failed"] != float64(0) || result["conflicts"] != float64(0) || result["uncertain"] != float64(0) || repo.accounts[2].Priority >= 1000 || repo.accounts[2].Concurrency != 10 {
		t.Fatal("new account did not receive a verified priority or its concurrency changed")
	}
}

func TestAntigravityPrioritySyncWorkerStopsWithService(t *testing.T) {
	ctx := context.Background()
	host, _ := priorityTestHost()
	engine, err := bridge.New(ctx, bridge.Options{Host: host, StateCachePath: filepath.Join(t.TempDir(), "cache.json"), ConfigJSON: []byte(`{"enabled":true,"auto_apply":false}`)})
	if err != nil {
		t.Fatal(err)
	}
	service := &AntigravityPriorityService{engine: engine, host: host}
	service.startPrioritySync()
	if err := service.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-service.syncDone:
	default:
		t.Fatal("service shutdown left a priority sync worker running")
	}
}
