package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"antigravity-priority/bridge"
)

// AntigravityPriorityService owns one instance of the original priority engine.
type AntigravityPriorityService struct {
	engine *bridge.Engine
	host   *antigravityPriorityHost
}

type priorityAccountRepository interface {
	ListByPlatform(context.Context, string) ([]Account, error)
	GetByID(context.Context, int64) (*Account, error)
	BulkUpdate(context.Context, []int64, AccountBulkUpdate) (int64, error)
}
type priorityTokenProvider interface {
	GetAccessToken(context.Context, *Account) (string, error)
}
type antigravityPriorityHost struct {
	repo       priorityAccountRepository
	upstream   HTTPUpstream
	tokens     priorityTokenProvider
	identities map[int64]string
	stateMu    sync.RWMutex
	statePath  string
	missing    map[int64]bool
}

func NewAntigravityPriorityService(repo AccountRepository, upstream HTTPUpstream, tokens *AntigravityTokenProvider) (*AntigravityPriorityService, error) {
	service := &AntigravityPriorityService{}
	enabled := strings.TrimSpace(os.Getenv("ANTIGRAVITY_PRIORITY_ENABLED"))
	if enabled == "" || enabled == "0" || strings.EqualFold(enabled, "false") {
		return service, nil
	}
	if enabled != "1" && !strings.EqualFold(enabled, "true") {
		return nil, errors.New("ANTIGRAVITY_PRIORITY_ENABLED must be true or false")
	}
	if repo == nil || upstream == nil || tokens == nil {
		return nil, errors.New("priority plugin host dependencies missing")
	}
	cache := strings.TrimSpace(os.Getenv("ANTIGRAVITY_PRIORITY_STATE_CACHE"))
	if cache == "" {
		cache = "/app/data/antigravity-priority/cache.json"
	}
	if !filepath.IsAbs(cache) {
		return nil, errors.New("priority cache path must be absolute")
	}
	if err := os.MkdirAll(filepath.Dir(cache), 0700); err != nil {
		return nil, fmt.Errorf("prepare priority cache: %w", err)
	}
	identityPath := strings.TrimSpace(os.Getenv("ANTIGRAVITY_PRIORITY_IDENTITIES"))
	if identityPath == "" {
		identityPath = filepath.Join(filepath.Dir(cache), "identities.json")
	}
	identities, err := loadAntigravityPriorityIdentities(identityPath)
	if err != nil {
		return nil, err
	}
	statePath := filepath.Join(filepath.Dir(cache), "host-state.json")
	missing, err := loadPriorityMissing(statePath)
	if err != nil {
		return nil, err
	}
	host := &antigravityPriorityHost{repo: repo, upstream: upstream, tokens: tokens, identities: identities, statePath: statePath, missing: missing}
	if _, err = host.ListAuthFiles(context.Background()); err != nil {
		return nil, err
	}
	config, err := os.ReadFile(filepath.Join(filepath.Dir(cache), "host-config.json"))
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("read priority host configuration: %w", err)
	}
	engine, err := bridge.New(context.Background(), bridge.Options{Host: host, StateCachePath: cache, ConfigJSON: config,
		APIBasePath: "/api/v1/admin/antigravity-priority", AuthStorageKey: "auth_token"})
	if err != nil {
		return nil, err
	}
	service.engine, service.host = engine, host
	return service, nil
}

func loadAntigravityPriorityIdentities(path string) (map[int64]string, error) {
	result := map[int64]string{}
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return result, nil
	}
	if err != nil {
		return nil, errors.New("cannot read priority identity mapping")
	}
	var values map[string]string
	if err = json.Unmarshal(raw, &values); err != nil || values == nil {
		return nil, errors.New("invalid priority identity mapping")
	}
	seen := map[string]bool{}
	for key, value := range values {
		id, err := strconv.ParseInt(key, 10, 64)
		if err != nil || id <= 0 || strconv.FormatInt(id, 10) != key || strings.TrimSpace(value) != value || value == "" || seen[value] {
			return nil, errors.New("invalid or duplicate priority identity")
		}
		result[id] = value
		seen[value] = true
	}
	return result, nil
}

func priorityUnavailable(w http.ResponseWriter, _ *http.Request) {
	http.Error(w, "Antigravity Priority is disabled", http.StatusServiceUnavailable)
}
func (s *AntigravityPriorityService) Handler() http.Handler {
	if s == nil || s.engine == nil {
		return http.HandlerFunc(priorityUnavailable)
	}
	return s.engine.Handler()
}
func (s *AntigravityPriorityService) UIHandler() http.Handler {
	if s == nil || s.engine == nil {
		return http.HandlerFunc(priorityUnavailable)
	}
	return s.engine.UIHandler()
}
func (s *AntigravityPriorityService) NotifyResult(ctx context.Context, account *Account, model string, status int) {
	if s == nil || s.engine == nil || !isPriorityAccount(account) {
		return
	}
	if err := s.engine.NotifyResult(ctx, s.host.identity(account.ID), model, status); err != nil {
		slog.Warn("antigravity_priority_feedback_failed", "account_id", account.ID)
	}
}
func (s *AntigravityPriorityService) Shutdown(ctx context.Context) error {
	if s == nil || s.engine == nil {
		return nil
	}
	return s.engine.Shutdown(ctx)
}
func isPriorityAccount(account *Account) bool {
	return account != nil && account.Platform == PlatformAntigravity && account.Type == AccountTypeOAuth
}
func (h *antigravityPriorityHost) identity(id int64) string {
	if value := h.identities[id]; value != "" {
		return value
	}
	return "sub2api-" + strconv.FormatInt(id, 10)
}

func (h *antigravityPriorityHost) ListAuthFiles(ctx context.Context) ([]bridge.AuthFile, error) {
	accounts, err := h.repo.ListByPlatform(ctx, PlatformAntigravity)
	if err != nil {
		return nil, err
	}
	files := make([]bridge.AuthFile, 0, len(accounts))
	seen := map[string]bool{}
	for i := range accounts {
		account := &accounts[i]
		if !isPriorityAccount(account) {
			continue
		}
		index := h.identity(account.ID)
		if seen[index] {
			return nil, errors.New("priority identity collision")
		}
		seen[index] = true
		files = append(files, bridge.AuthFile{Name: account.Name, AuthIndex: index, Type: PlatformAntigravity, Provider: PlatformAntigravity,
			Status: account.Status, Disabled: account.Status == StatusDisabled, Unavailable: !account.Schedulable || (account.Status != StatusActive && account.Status != StatusDisabled),
			Priority: -account.Priority, PriorityMissing: h.priorityIsMissing(account), Account: account.GetCredential("email"), Email: account.GetCredential("email")})
	}
	return files, nil
}

func (h *antigravityPriorityHost) account(ctx context.Context, index string) (*Account, error) {
	accounts, err := h.repo.ListByPlatform(ctx, PlatformAntigravity)
	if err != nil {
		return nil, err
	}
	var id int64
	for i := range accounts {
		if isPriorityAccount(&accounts[i]) && h.identity(accounts[i].ID) == index {
			if id != 0 {
				return nil, errors.New("priority identity collision")
			}
			id = accounts[i].ID
		}
	}
	if id == 0 {
		return nil, errors.New("priority account not found")
	}
	account, err := h.repo.GetByID(ctx, id)
	if err != nil {
		return nil, err
	}
	if !isPriorityAccount(account) {
		return nil, errors.New("priority account no longer eligible")
	}
	return account, nil
}

func (h *antigravityPriorityHost) document(account *Account) (bridge.AuthDocument, error) {
	fields := make(map[string]any, len(account.Credentials)+5)
	for key, value := range account.Credentials {
		fields[key] = value
	}
	index := h.identity(account.ID)
	fields["priority"], fields["disabled"] = -account.Priority, account.Status == StatusDisabled
	if h.priorityIsMissing(account) {
		delete(fields, "priority")
	}
	fields["type"], fields["auth_index"], fields["name"] = PlatformAntigravity, index, account.Name
	raw, err := json.Marshal(fields)
	if err != nil {
		return bridge.AuthDocument{}, err
	}
	// An empty Path forces the unchanged engine to use SaveAuth, never CPA files.
	return bridge.AuthDocument{AuthIndex: index, Name: index, JSON: raw}, nil
}
func (h *antigravityPriorityHost) GetAuth(ctx context.Context, index string) (bridge.AuthDocument, error) {
	account, err := h.account(ctx, index)
	if err != nil {
		return bridge.AuthDocument{}, err
	}
	return h.document(account)
}
func (h *antigravityPriorityHost) GetRuntime(ctx context.Context, index string) (bridge.RuntimeAuth, error) {
	account, err := h.account(ctx, index)
	if err != nil {
		return bridge.RuntimeAuth{}, err
	}
	document, err := h.document(account)
	if err != nil {
		return bridge.RuntimeAuth{}, err
	}
	return bridge.RuntimeAuth{AuthIndex: index, Name: account.Name, Provider: PlatformAntigravity, Disabled: account.Status == StatusDisabled, Metadata: document.JSON}, nil
}

func (h *antigravityPriorityHost) SaveAuth(ctx context.Context, index string, raw json.RawMessage) error {
	h.stateMu.Lock()
	defer h.stateMu.Unlock()
	account, err := h.account(ctx, index)
	if err != nil {
		return err
	}
	var fields map[string]json.RawMessage
	if err = json.Unmarshal(raw, &fields); err != nil || fields == nil {
		return errors.New("invalid priority virtual document")
	}
	if value, ok := fields["auth_index"]; ok {
		var identity string
		if json.Unmarshal(value, &identity) != nil || identity != index {
			return errors.New("priority identity mismatch")
		}
	}
	_, priorityPresent := fields["priority"]
	var update AccountBulkUpdate
	if value, ok := fields["priority"]; ok {
		var logical int
		if err = json.Unmarshal(value, &logical); err != nil {
			return errors.New("invalid priority value")
		}
		native := -logical
		if logical == native && logical != 0 {
			return errors.New("priority value overflow")
		}
		if native != account.Priority {
			update.Priority = &native
		}
	} else if account.Priority != 0 {
		zero := 0
		update.Priority = &zero
	}
	if value, ok := fields["disabled"]; ok {
		var disabled bool
		if err = json.Unmarshal(value, &disabled); err != nil {
			return errors.New("invalid priority disabled state")
		}
		// Native authentication errors and administrator schedulable flags are independent.
		if account.Status == StatusActive || account.Status == StatusDisabled {
			status := StatusActive
			if disabled {
				status = StatusDisabled
			}
			if status != account.Status {
				update.Status = &status
			}
		}
	}
	if update.Priority == nil && update.Status == nil {
		return h.setPriorityMissing(account.ID, !priorityPresent)
	}
	// Patch only these columns. A concurrent OAuth refresh must retain its credentials.
	count, err := h.repo.BulkUpdate(ctx, []int64{account.ID}, update)
	if err != nil {
		return err
	}
	if count != 1 {
		return errors.New("priority account disappeared during update")
	}
	return h.setPriorityMissing(account.ID, !priorityPresent)
}

func (h *antigravityPriorityHost) HTTPDo(ctx context.Context, input bridge.HTTPRequest) (bridge.HTTPResponse, error) {
	target, err := url.Parse(input.URL)
	if err != nil || target.Scheme != "https" || target.User != nil || target.Fragment != "" || target.RawQuery != "" ||
		(target.Port() != "" && target.Port() != "443") ||
		(target.Hostname() != "daily-cloudcode-pa.googleapis.com" && target.Hostname() != "daily-cloudcode-pa.sandbox.googleapis.com" && target.Hostname() != "cloudcode-pa.googleapis.com") ||
		target.Path != "/v1internal:retrieveUserQuotaSummary" || input.Method != http.MethodPost || len(input.Body) > 1024*1024 {
		return bridge.HTTPResponse{}, errors.New("priority quota request target rejected")
	}
	account, err := h.account(ctx, input.AuthIndex)
	if err != nil {
		return bridge.HTTPResponse{}, err
	}
	timeout, cancel := context.WithTimeout(ctx, 25*time.Second)
	defer cancel()
	token, err := h.tokens.GetAccessToken(timeout, account)
	if err != nil {
		return bridge.HTTPResponse{}, errors.New("priority quota access token unavailable")
	}
	request, err := http.NewRequestWithContext(timeout, http.MethodPost, target.String(), bytes.NewReader(input.Body))
	if err != nil {
		return bridge.HTTPResponse{}, errors.New("cannot construct priority quota request")
	}
	for _, name := range []string{"Content-Type", "User-Agent", "X-Goog-User-Project"} {
		if value := input.Headers.Get(name); value != "" {
			request.Header.Set(name, value)
		}
	}
	request.Header.Set("Authorization", "Bearer "+token)
	proxyURL := ""
	if account.Proxy != nil {
		proxyURL = account.Proxy.URL()
	}
	response, err := h.upstream.Do(request, proxyURL, account.ID, account.Concurrency)
	if err != nil {
		if response != nil && response.Body != nil {
			_ = response.Body.Close()
		}
		return bridge.HTTPResponse{}, errors.New("priority quota transport failed")
	}
	if response == nil || response.Body == nil {
		return bridge.HTTPResponse{}, errors.New("priority quota response missing")
	}
	defer func() { _ = response.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(response.Body, 4*1024*1024+1))
	if err != nil {
		return bridge.HTTPResponse{}, errors.New("priority quota response read failed")
	}
	if len(body) > 4*1024*1024 {
		return bridge.HTTPResponse{}, errors.New("priority quota response too large")
	}
	// Preserve non-2xx responses so the original engine owns 429/cooldown semantics.
	return bridge.HTTPResponse{StatusCode: response.StatusCode, Headers: bridge.Header(response.Header.Clone()), Body: body}, nil
}
