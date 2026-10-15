package bridge

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

type emptyHost struct{}

func (emptyHost) ListAuthFiles(context.Context) ([]AuthFile, error) { return nil, nil }
func (emptyHost) GetAuth(context.Context, string) (AuthDocument, error) {
	return AuthDocument{}, errors.New("not found")
}
func (emptyHost) GetRuntime(context.Context, string) (RuntimeAuth, error) {
	return RuntimeAuth{}, errors.New("not found")
}
func (emptyHost) SaveAuth(context.Context, string, json.RawMessage) error {
	return errors.New("unexpected write")
}
func (emptyHost) HTTPDo(context.Context, HTTPRequest) (HTTPResponse, error) {
	return HTTPResponse{}, errors.New("unexpected network request")
}

func newTestEngine(t *testing.T) *Engine {
	t.Helper()
	engine, err := New(context.Background(), Options{Host: emptyHost{}, StateCachePath: filepath.Join(t.TempDir(), "cache.json"), ConfigJSON: []byte(`{"enabled":false}`)})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := engine.Shutdown(context.Background()); err != nil {
			t.Error(err)
		}
	})
	return engine
}

func TestBridgeOriginalUIAndScripts(t *testing.T) {
	engine := newTestEngine(t)
	recorder := httptest.NewRecorder()
	engine.UIHandler().ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/api/v1/antigravity-priority-ui", nil))
	if recorder.Code != 200 || recorder.Header().Get("X-Frame-Options") != "SAMEORIGIN" || !strings.Contains(recorder.Header().Get("Content-Security-Policy"), "frame-ancestors 'self'") {
		t.Fatal("UI security headers do not support authenticated same-origin embedding")
	}
	page := recorder.Body.String()
	for _, marker := range []string{`data-tab="overview"`, `data-tab="history"`, `data-tab="diagnostics"`, `data-tab="config"`, `data-tab="help"`, `id="diffModal"`, `auth_token`, `const BASE_PATH = "/api/v1/admin/antigravity-priority"`} {
		if !strings.Contains(page, marker) {
			t.Errorf("missing original/adapted UI marker: %s", marker)
		}
	}
	for _, marker := range []string{`id="keyModal"`, `id="manualKeyInput"`, `management_key`, `parentParams.get(`, `setSavedKey(`} {
		if strings.Contains(page, marker) {
			t.Errorf("legacy CPA credential mechanism remains: %s", marker)
		}
	}
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("node unavailable for syntax validation")
	}
	scripts := regexp.MustCompile(`(?s)<script[^>]*>(.*?)</script>`).FindAllStringSubmatch(page, -1)
	if len(scripts) == 0 {
		t.Fatal("original UI scripts missing")
	}
	for _, script := range scripts {
		file := filepath.Join(t.TempDir(), "ui.js")
		if err := os.WriteFile(file, []byte(script[1]), 0600); err != nil {
			t.Fatal(err)
		}
		if out, err := exec.Command(node, "--check", file).CombinedOutput(); err != nil {
			t.Fatalf("adapted UI JavaScript invalid: %v %s", err, out)
		}
	}
}

func TestBridgeManagementRoutes(t *testing.T) {
	engine := newTestEngine(t)
	for _, test := range []struct {
		path string
		code int
	}{
		{"/api/v1/admin/antigravity-priority/runtime-config", 200},
		{"/api/v1/admin/antigravity-priority/schedule/config", 200},
		{"/api/v1/admin/antigravity-priority/status", 200},
		{"/api/v1/admin/antigravity-priority/unknown", 404},
		{"/v0/management/plugins/antigravity-priority/runtime-config", 404},
	} {
		recorder := httptest.NewRecorder()
		request := httptest.NewRequest(http.MethodGet, test.path, nil)
		request.Header.Set("X-Antigravity-Priority-Route-Source", "resource")
		engine.Handler().ServeHTTP(recorder, request)
		if recorder.Code != test.code {
			t.Errorf("%s: got %d want %d", test.path, recorder.Code, test.code)
		}
	}
	if err := engine.NotifyResult(context.Background(), "missing", "gemini-test", 500); err != nil {
		t.Fatal(err)
	}
	if err := engine.NotifyResult(context.Background(), "", "claude-test", 429); err != nil {
		t.Fatal(err)
	}
}

func TestBridgeInvalidConfiguration(t *testing.T) {
	for _, opts := range []Options{
		{},
		{Host: emptyHost{}, StateCachePath: "cache.json", ConfigJSON: []byte("not json")},
		{Host: emptyHost{}, StateCachePath: "cache.json", APIBasePath: "https://outside.invalid"},
	} {
		if engine, err := New(context.Background(), opts); err == nil {
			_ = engine.Shutdown(context.Background())
			t.Error("invalid configuration accepted")
		}
	}
}

func TestSub2APIPriorityPresentation(t *testing.T) {
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("node unavailable for priority presentation validation")
	}
	page, err := adaptPage("/api/v1/admin/antigravity-priority", "auth_token")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(page, "sub2apiPriorityMarkup(item, isPredictedView) +") != 1 {
		t.Fatal("native priority values are not attached to the credential cards")
	}
	checks := `
const assert = require('node:assert/strict');
let currentLang = 'zh-CN';
for (const [logical, native] of [[999, -999], [100, -100], [0, 0], [-1, 1]]) {
    assert.equal(sub2apiNativePriority({priority: logical}), native);
}
assert.equal(sub2apiNativePriority({priority_missing: true}), 0);
assert.equal(sub2apiNativePriority({priority: '100'}), null);
assert.equal(sub2apiNativePriority(null), null);
const item = {current: {priority: 100}, target: {priority: 999}};
const current = sub2apiPriorityMarkup(item, false);
assert.ok(current.includes('data-sub2api-priority="current">-100</strong>'));
assert.ok(current.includes('data-sub2api-priority="target">-999</strong>'));
assert.ok(current.includes('Sub2API 目标'));
assert.ok(sub2apiPriorityMarkup(item, true).includes('Sub2API 预测'));
assert.ok(sub2apiPriorityMarkup({current: {priority: 100, disabled: true}}, false).includes('-100 [已禁用]'));
currentLang = 'en-US';
assert.ok(sub2apiPriorityMarkup(item, false).includes('Sub2API current'));
assert.ok(sub2apiPriorityMarkup(item, true).includes('Sub2API predicted'));
`
	if out, err := exec.Command(node, "-e", sub2apiPriorityScript+checks).CombinedOutput(); err != nil {
		t.Fatalf("native priority presentation failed: %v %s", err, out)
	}
}
