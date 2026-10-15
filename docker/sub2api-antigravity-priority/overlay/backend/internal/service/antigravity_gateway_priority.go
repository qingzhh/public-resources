package service

import (
	"context"
	"net/http"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
)

// Only Antigravity OAuth traffic participates. Native upstream/API-key accounts
// retain their existing transport and scheduling semantics.
type antigravityPriorityReporter interface {
	NotifyResult(context.Context, *Account, string, int)
}

func (s *AntigravityGatewayService) SetPriorityPlugin(plugin *AntigravityPriorityService) {
	if plugin == nil {
		s.priorityPlugin = nil
		return
	}
	s.priorityPlugin = plugin
}

func reportAntigravityPriority(reporter antigravityPriorityReporter, ctx context.Context, account *Account, model string, status int) {
	if reporter == nil || account == nil || account.Platform != PlatformAntigravity || account.Type != AccountTypeOAuth {
		return
	}
	// Request cancellation must not discard an already-observed upstream result.
	eventCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	reporter.NotifyResult(eventCtx, account, model, status)
}

func (s *AntigravityGatewayService) finishPriorityRequest(ctx context.Context, c *gin.Context, account *Account, model string, result **ForwardResult, err *error) {
	if *err != nil || *result == nil || (*result).ClientDisconnect || ctx.Err() != nil {
		return
	}
	if _, failed := GetOpsStreamError(c); failed {
		return
	}
	reportAntigravityPriority(s.priorityPlugin, ctx, account, model, http.StatusOK)
}

func (s *AntigravityGatewayService) priorityUpstream(base HTTPUpstream, account *Account, model string) HTTPUpstream {
	if s.priorityPlugin == nil || base == nil || account == nil || account.Platform != PlatformAntigravity || account.Type != AccountTypeOAuth {
		return base
	}
	if _, wrapped := base.(*antigravityPriorityUpstream); wrapped {
		return base
	}
	return &antigravityPriorityUpstream{base: base, reporter: s.priorityPlugin, account: account, model: model}
}

type antigravityPriorityUpstream struct {
	base     HTTPUpstream
	reporter antigravityPriorityReporter
	account  *Account
	model    string
}

func (u *antigravityPriorityUpstream) observe(req *http.Request, response *http.Response, err error) (*http.Response, error) {
	if err == nil && response != nil && response.StatusCode == http.StatusTooManyRequests {
		reportAntigravityPriority(u.reporter, req.Context(), u.account, u.model, response.StatusCode)
	}
	return response, err
}

func (u *antigravityPriorityUpstream) Do(req *http.Request, proxyURL string, accountID int64, concurrency int) (*http.Response, error) {
	response, err := u.base.Do(req, proxyURL, accountID, concurrency)
	return u.observe(req, response, err)
}

func (u *antigravityPriorityUpstream) DoWithTLS(req *http.Request, proxyURL string, accountID int64, concurrency int, profile *tlsfingerprint.Profile) (*http.Response, error) {
	response, err := u.base.DoWithTLS(req, proxyURL, accountID, concurrency, profile)
	return u.observe(req, response, err)
}
