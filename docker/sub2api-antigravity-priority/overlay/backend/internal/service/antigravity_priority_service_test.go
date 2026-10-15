package service

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"antigravity-priority/bridge"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

type priorityRepoStub struct {
	accounts   map[int64]*Account
	update     AccountBulkUpdate
	beforeBulk func()
	calls      int
}

func (r *priorityRepoStub) ListByPlatform(context.Context, string) ([]Account, error) {
	out := []Account{}
	for _, a := range r.accounts {
		out = append(out, *a)
	}
	return out, nil
}
func (r *priorityRepoStub) GetByID(_ context.Context, id int64) (*Account, error) {
	if a := r.accounts[id]; a != nil {
		copy := *a
		return &copy, nil
	}
	return nil, errors.New("not found")
}
func (r *priorityRepoStub) BulkUpdate(_ context.Context, ids []int64, update AccountBulkUpdate) (int64, error) {
	if r.beforeBulk != nil {
		r.beforeBulk()
	}
	r.update = update
	r.calls++
	account := r.accounts[ids[0]]
	if account == nil {
		return 0, nil
	}
	if update.Priority != nil {
		account.Priority = *update.Priority
	}
	if update.Status != nil {
		account.Status = *update.Status
	}
	return 1, nil
}
func priorityTestHost() (*antigravityPriorityHost, *priorityRepoStub) {
	repo := &priorityRepoStub{accounts: map[int64]*Account{1: {ID: 1, Name: "same-name", Platform: PlatformAntigravity, Type: AccountTypeOAuth, Priority: 900, Status: StatusActive, Schedulable: true, Concurrency: 2, Credentials: map[string]any{"access_token": "old", "refresh_token": "retained", "project_id": "project", "custom": map[string]any{"value": true}}}}}
	return &antigravityPriorityHost{repo: repo, identities: map[int64]string{1: "original-cpa-id"}}, repo
}

func TestAntigravityPriorityAdapterPriorityAndCredentialRefresh(t *testing.T) {
	for _, logical := range []int{999, 998, 100, 99, 0, -1} {
		t.Run(strings.ReplaceAll(http.StatusText(logical), " ", "_")+"priority", func(t *testing.T) {
			host, repo := priorityTestHost()
			account := repo.accounts[1]
			document, err := host.GetAuth(context.Background(), "original-cpa-id")
			if err != nil {
				t.Fatal(err)
			}
			if document.Path != "" || document.Name != "original-cpa-id" {
				t.Fatal("document bypasses callback")
			}
			fields := map[string]any{}
			if err = json.Unmarshal(document.JSON, &fields); err != nil {
				t.Fatal(err)
			}
			fields["priority"] = logical
			fields["disabled"] = true
			raw, err := json.Marshal(fields)
			if err != nil {
				t.Fatal(err)
			}
			repo.beforeBulk = func() {
				account.Credentials = map[string]any{"access_token": "refreshed", "refresh_token": "retained", "new_field": true}
			}
			if err = host.SaveAuth(context.Background(), document.Name, raw); err != nil {
				t.Fatal(err)
			}
			if account.Priority != 1000-logical || account.Status != StatusDisabled || account.GetCredential("access_token") != "refreshed" || account.Credentials["new_field"] != true || !account.Schedulable {
				t.Fatal("priority/status mapping or credential refresh preservation failed")
			}
			updated, err := host.GetAuth(context.Background(), document.Name)
			if err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(updated.JSON, &fields); err != nil || fields["priority"] != float64(logical) {
				t.Fatal("native priority did not round-trip to the plugin value")
			}
			update := repo.update
			if update.Credentials != nil || update.Extra != nil || update.Schedulable != nil || update.Name != nil || update.ProxyID != nil || update.Concurrency != nil || update.RateMultiplier != nil || update.ProbeEnabled != nil {
				t.Fatal("plugin updates unrelated columns")
			}
		})
	}
}

func TestAntigravityPriorityAdapterRecoveryAndNativeFlags(t *testing.T) {
	host, repo := priorityTestHost()
	a := repo.accounts[1]
	a.Status = StatusDisabled
	a.Schedulable = false
	if err := host.SaveAuth(context.Background(), "original-cpa-id", json.RawMessage(`{"priority":100,"disabled":false}`)); err != nil {
		t.Fatal(err)
	}
	if a.Status != StatusActive || a.Schedulable {
		t.Fatal("recovery changed administrator scheduling flag")
	}
	a.Status = StatusError
	if err := host.SaveAuth(context.Background(), "original-cpa-id", json.RawMessage(`{"priority":99,"disabled":false}`)); err != nil {
		t.Fatal(err)
	}
	if a.Status != StatusError {
		t.Fatal("native auth error cleared by plugin")
	}
	repo.accounts[2] = &Account{ID: 2, Name: "same-name", Platform: PlatformAntigravity, Type: AccountTypeOAuth, Status: StatusDisabled}
	repo.accounts[3] = &Account{ID: 3, Platform: PlatformAntigravity, Type: AccountTypeUpstream}
	files, err := host.ListAuthFiles(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 2 {
		t.Fatal("inactive recovery accounts missing or upstream accounts included")
	}
	if _, err = host.GetAuth(context.Background(), "same-name"); err == nil {
		t.Fatal("ambiguous display name accepted as identity")
	}
	host.identities[1] = "sub2api-2"
	if _, err = host.ListAuthFiles(context.Background()); err == nil {
		t.Fatal("identity collision accepted")
	}
}

type priorityTokenStub struct {
	calls int
	value string
}

func (s *priorityTokenStub) GetAccessToken(context.Context, *Account) (string, error) {
	s.calls++
	return s.value, nil
}

type priorityHTTPStub struct {
	calls       int
	auth, proxy string
	id          int64
	concurrency int
	status      int
	body        string
}

func (s *priorityHTTPStub) Do(r *http.Request, proxy string, id int64, concurrency int) (*http.Response, error) {
	s.calls++
	s.auth = r.Header.Get("Authorization")
	s.proxy = proxy
	s.id = id
	s.concurrency = concurrency
	return &http.Response{StatusCode: s.status, Header: http.Header{"Retry-After": []string{"5"}}, Body: io.NopCloser(strings.NewReader(s.body))}, nil
}
func (s *priorityHTTPStub) DoWithTLS(r *http.Request, proxy string, id int64, concurrency int, _ *tlsfingerprint.Profile) (*http.Response, error) {
	return s.Do(r, proxy, id, concurrency)
}

func TestAntigravityPriorityAdapterQuotaUsesNativeTokenProxyAnd429(t *testing.T) {
	host, repo := priorityTestHost()
	tokens := &priorityTokenStub{value: "current-token"}
	upstream := &priorityHTTPStub{status: 429, body: `{"reason":"quota"}`}
	host.tokens = tokens
	host.upstream = upstream
	repo.accounts[1].Proxy = &Proxy{Protocol: "http", Host: "proxy.invalid", Port: 1234}
	for _, domain := range []string{"daily-cloudcode-pa.googleapis.com", "daily-cloudcode-pa.sandbox.googleapis.com", "cloudcode-pa.googleapis.com"} {
		response, err := host.HTTPDo(context.Background(), bridge.HTTPRequest{AuthIndex: "original-cpa-id", Method: "POST", URL: "https://" + domain + "/v1internal:retrieveUserQuotaSummary", Headers: bridge.Header{"Authorization": []string{"Bearer stale-token"}}})
		if err != nil {
			t.Fatal(err)
		}
		if response.StatusCode != 429 || string(response.Body) != upstream.body || response.Headers.Get("Retry-After") != "5" {
			t.Fatal("429 response changed")
		}
	}
	if tokens.calls != 3 || upstream.calls != 3 || upstream.auth != "Bearer current-token" || upstream.proxy != "http://proxy.invalid:1234" || upstream.id != 1 || upstream.concurrency != 2 {
		t.Fatal("native token/proxy transport not used")
	}
}

func TestAntigravityPriorityAdapterRejectsOtherTargets(t *testing.T) {
	host, _ := priorityTestHost()
	upstream := &priorityHTTPStub{}
	host.upstream = upstream
	for _, target := range []string{"http://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary", "https://daily-cloudcode-pa.googleapis.com.attacker.invalid/v1internal:retrieveUserQuotaSummary", "https://127.0.0.1/v1internal:retrieveUserQuotaSummary", "https://cloudcode-pa.googleapis.com/other", "https://user@cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary", "https://cloudcode-pa.googleapis.com:8443/v1internal:retrieveUserQuotaSummary"} {
		if _, err := host.HTTPDo(context.Background(), bridge.HTTPRequest{Method: "POST", URL: target}); err == nil {
			t.Fatal("invalid target allowed")
		}
	}
	if upstream.calls != 0 {
		t.Fatal("rejected target reached transport")
	}
}

func TestAntigravityPriorityDisabledAndIdentityValidation(t *testing.T) {
	t.Setenv("ANTIGRAVITY_PRIORITY_ENABLED", "false")
	service, err := NewAntigravityPriorityService(nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	recorder := httptest.NewRecorder()
	service.Handler().ServeHTTP(recorder, httptest.NewRequest("GET", "/", nil))
	if recorder.Code != 503 {
		t.Fatal("disabled API available")
	}
	if err = service.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, raw := range []string{`{"1":"same","2":"same"}`, `{"bad":"id"}`, `null`, `{"01":"a","1":"b"}`} {
		file := filepath.Join(t.TempDir(), "identities.json")
		if err = os.WriteFile(file, []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err = loadAntigravityPriorityIdentities(file); err == nil {
			t.Errorf("invalid identity mapping accepted: %s", raw)
		}
	}
}

func TestAntigravityPriorityWritesDriveNativeScheduling(t *testing.T) {
	host, repo := priorityTestHost()
	logicalValues := []int{-1, 0, 99, 100, 999}
	accounts := make([]*Account, 0, len(logicalValues))
	loaded := make([]accountWithLoad, 0, len(logicalValues))
	for i, logical := range logicalValues {
		id := int64(i + 1)
		identity := "test-account-" + string(rune('a'+i))
		account := &Account{ID: id, Platform: PlatformAntigravity, Type: AccountTypeOAuth, Priority: 1, Status: StatusActive, Schedulable: true}
		repo.accounts[id] = account
		host.identities[id] = identity
		raw, err := json.Marshal(map[string]any{"priority": logical, "disabled": false})
		if err != nil {
			t.Fatal(err)
		}
		if err := host.SaveAuth(context.Background(), identity, raw); err != nil {
			t.Fatal(err)
		}
		accounts = append(accounts, account)
		loaded = append(loaded, accountWithLoad{account: account, loadInfo: &AccountLoadInfo{AccountID: id}})
	}
	sortAccountsByPriorityAndLastUsed(accounts, false)
	for i, account := range accounts {
		if account.ID != int64(len(logicalValues)-i) {
			t.Fatalf("native scheduling order does not follow plugin preference: position %d account %d", i, account.ID)
		}
	}
	preferred := filterByMinPriority(loaded)
	if len(preferred) != 1 || preferred[0].account.ID != 5 || preferred[0].account.Priority != 1 {
		t.Fatal("load-aware scheduling did not select the plugin's boosted account")
	}
}
