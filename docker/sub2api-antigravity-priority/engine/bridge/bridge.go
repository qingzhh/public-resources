// Package bridge exposes the unchanged CPA plugin engine to an in-process host.
package bridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"antigravity-priority/internal/config"
	"antigravity-priority/internal/host"
	"antigravity-priority/internal/management"
	pluginruntime "antigravity-priority/internal/runtime"
)

type HostCallbacks = host.HostCallbacks
type AuthFile = host.AuthFile
type RuntimeAuth = host.RuntimeAuth
type AuthDocument = host.AuthDocument
type HTTPRequest = host.HTTPRequest
type HTTPResponse = host.HTTPResponse
type Header = host.Header

type Options struct {
	Host           HostCallbacks
	StateCachePath string
	ConfigJSON     []byte
	APIBasePath    string
	AuthStorageKey string
}

type Engine struct {
	runtime *pluginruntime.Runtime
	base    string
	page    string
	stop    sync.Once
	stopErr error
}

func New(ctx context.Context, opts Options) (*Engine, error) {
	if opts.Host == nil || strings.TrimSpace(opts.StateCachePath) == "" {
		return nil, errors.New("priority bridge requires a host and persistent cache path")
	}
	if opts.APIBasePath == "" {
		opts.APIBasePath = "/api/v1/admin/antigravity-priority"
	}
	if opts.AuthStorageKey == "" {
		opts.AuthStorageKey = "auth_token"
	}
	if !strings.HasPrefix(opts.APIBasePath, "/api/") || strings.ContainsAny(opts.APIBasePath, "?#\\") || strings.Contains(opts.APIBasePath, "..") {
		return nil, errors.New("invalid priority API base path")
	}
	opts.APIBasePath = strings.TrimRight(opts.APIBasePath, "/")
	page, err := adaptPage(opts.APIBasePath, opts.AuthStorageKey)
	if err != nil {
		return nil, err
	}
	cfg := map[string]any{"enabled": true}
	if len(opts.ConfigJSON) != 0 {
		if err = json.Unmarshal(opts.ConfigJSON, &cfg); err != nil || cfg == nil {
			return nil, errors.New("invalid priority host configuration")
		}
	}
	cfg["state_cache_path"] = opts.StateCachePath
	raw, err := json.Marshal(cfg)
	if err != nil {
		return nil, err
	}
	runtime := pluginruntime.New(pluginruntime.Options{Host: opts.Host, StateCachePath: opts.StateCachePath})
	if _, err = runtime.Register(ctx, pluginruntime.RegisterRequest{ConfigYAML: string(raw)}); err != nil {
		_ = runtime.Shutdown(ctx)
		return nil, err
	}
	return &Engine{runtime: runtime, base: opts.APIBasePath, page: page}, nil
}

// Handler requires the embedding host to authenticate every request as an administrator.
func (e *Engine) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.URL.Path, e.base+"/") {
			http.NotFound(w, r)
			return
		}
		request := r.Clone(r.Context())
		request.URL.Path = strings.TrimPrefix(r.URL.Path, e.base)
		request.URL.RawPath = ""
		request.Header.Del(management.RouteSourceHeader)
		if request.URL.Path == "/status" && request.Method == http.MethodGet {
			e.UIHandler().ServeHTTP(w, request)
			return
		}
		e.runtime.ManagementHandler().ServeHTTP(w, request)
	})
}

// UIHandler serves a static shell without account data. Its API remains admin-only.
func (e *Engine) UIHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", "GET")
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Frame-Options", "SAMEORIGIN")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		// The original static UI uses inline event handlers. No dynamic data is rendered here.
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
		_, _ = w.Write([]byte(e.page))
	})
}

func (e *Engine) NotifyResult(ctx context.Context, authIndex, model string, status int) error {
	if authIndex == "" || (status != http.StatusTooManyRequests && (status < 200 || status >= 300)) {
		return nil
	}
	payload := struct {
		Provider  string
		Model     string
		AuthIndex string
		Failed    bool
		Failure   struct{ StatusCode int }
	}{Provider: "antigravity", Model: model, AuthIndex: authIndex, Failed: status == http.StatusTooManyRequests}
	payload.Failure.StatusCode = status
	raw, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	var envelope pluginruntime.Envelope
	if err = json.Unmarshal(e.runtime.Handle(ctx, pluginruntime.MethodUsageHandle, raw), &envelope); err != nil {
		return err
	}
	if !envelope.OK {
		if envelope.Error != nil {
			return fmt.Errorf("priority usage event rejected: %s", envelope.Error.Code)
		}
		return errors.New("priority usage event rejected")
	}
	return nil
}

// AutomaticPrioritySyncEnabled shares the original automatic apply policy.
// Synchronization replays only effective host values, never a cached preview.
func (e *Engine) AutomaticPrioritySyncEnabled(now time.Time) bool {
	cfg, err := e.runtime.Config()
	if err != nil || !cfg.Enabled || !cfg.AutoApply {
		return false
	}
	schedule, err := e.runtime.GetScheduleConfig(context.Background())
	return err == nil && !schedule.Paused && config.IsInScheduleWindow(now, schedule)
}

func (e *Engine) Shutdown(ctx context.Context) error {
	e.stop.Do(func() { e.stopErr = e.runtime.Shutdown(ctx) })
	return e.stopErr
}

func replaceSection(page, first, last, replacement string) (string, error) {
	start, end := strings.Index(page, first), strings.Index(page, last)
	if start < 0 || end <= start || strings.Count(page, first) != 1 || strings.Count(page, last) != 1 {
		return "", errors.New("original priority UI anchor changed")
	}
	return page[:start] + replacement + page[end:], nil
}

func adaptPage(base, storageKey string) (string, error) {
	page := strings.ReplaceAll(management.StatusHTML, "/v0/management/plugins/antigravity-priority", base)
	key, _ := json.Marshal(storageKey)
	auth := `        function getManagementKey() {
            try { return (window.parent.localStorage.getItem(KEY) || '').trim(); } catch (_) {}
            try { return (localStorage.getItem(KEY) || '').trim(); } catch (_) {}
            return '';
        }
        function openKeyModal() {
            const target = window.parent && window.parent !== window ? window.parent : window;
            target.location.assign('/login');
        }

`
	auth = strings.ReplaceAll(auth, "KEY", string(key))
	var err error
	page, err = replaceSection(page, "        function getManagementKey() {", "        function cleanInlineThemeStyles() {", auth)
	if err != nil {
		return "", err
	}
	page, err = replaceSection(page, "    <!-- Management Key Modal -->", "    <!-- Diff & Run Result Modal -->", "")
	if err != nil {
		return "", err
	}
	button := `                <button type="button" class="btn-secondary" onclick="openKeyModal()" aria-label="Set Management Key">`
	start := strings.Index(page, button)
	if start < 0 {
		return "", errors.New("original priority UI login control changed")
	}
	end := strings.Index(page[start:], "</button>")
	if end < 0 {
		return "", errors.New("original priority UI login control is incomplete")
	}
	page = page[:start] + page[start+end+len("</button>"):]
	return adaptPriorityDisplay(page)
}
