package service

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
)

type priorityGatewayRecorder struct {
	statuses []int
	models   []string
	canceled bool
}

func (r *priorityGatewayRecorder) NotifyResult(ctx context.Context, _ *Account, model string, status int) {
	r.statuses = append(r.statuses, status)
	r.models = append(r.models, model)
	r.canceled = ctx.Err() != nil
}

type priorityGatewayTransport struct {
	status int
	err    error
	calls  int
}

func (t *priorityGatewayTransport) Do(_ *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	t.calls++
	if t.err != nil {
		return nil, t.err
	}
	return &http.Response{StatusCode: t.status, Body: io.NopCloser(strings.NewReader("unchanged"))}, nil
}

func (t *priorityGatewayTransport) DoWithTLS(r *http.Request, proxy string, id int64, concurrency int, _ *tlsfingerprint.Profile) (*http.Response, error) {
	return t.Do(r, proxy, id, concurrency)
}

func TestAntigravityPriorityGatewayReportsEach429ExactlyOnce(t *testing.T) {
	recorder := &priorityGatewayRecorder{}
	transport := &priorityGatewayTransport{status: http.StatusTooManyRequests}
	gateway := &AntigravityGatewayService{priorityPlugin: recorder}
	account := &Account{ID: 1, Platform: PlatformAntigravity, Type: AccountTypeOAuth}
	wrapped := gateway.priorityUpstream(transport, account, "claude-sonnet-4-5")
	if twice := gateway.priorityUpstream(wrapped, account, "claude-sonnet-4-5"); twice != wrapped {
		t.Fatal("transport was wrapped twice")
	}
	request, err := http.NewRequest(http.MethodPost, "https://example.invalid", nil)
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		response, err := wrapped.Do(request, "", account.ID, 2)
		if err != nil || response.StatusCode != http.StatusTooManyRequests {
			t.Fatal("upstream response was changed")
		}
		_ = response.Body.Close()
	}
	if transport.calls != 2 || len(recorder.statuses) != 2 || recorder.models[0] != "claude-sonnet-4-5" {
		t.Fatalf("calls=%d events=%v models=%v", transport.calls, recorder.statuses, recorder.models)
	}
}

func TestAntigravityPriorityGatewayDoesNotTreatHeadersAsSuccess(t *testing.T) {
	for _, status := range []int{200, 400, 401, 403, 500, 503} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			recorder := &priorityGatewayRecorder{}
			gateway := &AntigravityGatewayService{priorityPlugin: recorder}
			account := &Account{ID: 1, Platform: PlatformAntigravity, Type: AccountTypeOAuth}
			wrapped := gateway.priorityUpstream(&priorityGatewayTransport{status: status}, account, "gemini-3-pro")
			request, err := http.NewRequest(http.MethodPost, "https://example.invalid", nil)
			if err != nil {
				t.Fatal(err)
			}
			response, err := wrapped.DoWithTLS(request, "", 1, 2, nil)
			if err != nil {
				t.Fatal(err)
			}
			_ = response.Body.Close()
			if len(recorder.statuses) != 0 {
				t.Fatalf("non-429 headers emitted events: %v", recorder.statuses)
			}
		})
	}
}

func TestAntigravityPriorityGatewayTerminalSuccessAndFailure(t *testing.T) {
	recorder := &priorityGatewayRecorder{}
	gateway := &AntigravityGatewayService{priorityPlugin: recorder}
	account := &Account{ID: 1, Platform: PlatformAntigravity, Type: AccountTypeOAuth}
	result := &ForwardResult{}
	streamError := errors.New("stream failed")
	gateway.finishPriorityRequest(context.Background(), nil, account, "gemini-3-pro", &result, &streamError)
	if len(recorder.statuses) != 0 {
		t.Fatal("failed stream was marked successful")
	}
	var successError error
	result.ClientDisconnect = true
	gateway.finishPriorityRequest(context.Background(), nil, account, "gemini-3-pro", &result, &successError)
	result.ClientDisconnect = false
	c := &gin.Context{}
	c.Set(OpsStreamErrorKey, OpsStreamError{})
	gateway.finishPriorityRequest(context.Background(), c, account, "gemini-3-pro", &result, &successError)
	if len(recorder.statuses) != 0 {
		t.Fatal("interrupted or failed stream was marked successful")
	}
	gateway.finishPriorityRequest(context.Background(), nil, account, "gemini-3-pro", &result, &successError)
	if len(recorder.statuses) != 1 || recorder.statuses[0] != http.StatusOK {
		t.Fatalf("success events=%v", recorder.statuses)
	}
}

func TestAntigravityPriorityGatewayIgnoresNonOAuthAndPreservesObservedResult(t *testing.T) {
	recorder := &priorityGatewayRecorder{}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for _, account := range []*Account{nil, {Platform: PlatformAntigravity, Type: AccountTypeUpstream}, {Platform: PlatformOpenAI, Type: AccountTypeOAuth}} {
		reportAntigravityPriority(recorder, ctx, account, "gemini", 429)
	}
	if len(recorder.statuses) != 0 {
		t.Fatal("unrelated account reported")
	}
	reportAntigravityPriority(recorder, ctx, &Account{Platform: PlatformAntigravity, Type: AccountTypeOAuth}, "gemini", 429)
	if len(recorder.statuses) != 1 || recorder.canceled {
		t.Fatal("observed response was lost to request cancellation")
	}
}

func TestAntigravityPriorityCountTokensDoesNotRecoverCooldown(t *testing.T) {
	recorder := &priorityGatewayRecorder{}
	gateway := &AntigravityGatewayService{priorityPlugin: recorder}
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1beta/models/gemini-test:countTokens", nil)
	account := &Account{ID: 1, Name: "test", Platform: PlatformAntigravity, Type: AccountTypeOAuth}
	result, err := gateway.ForwardGemini(context.Background(), c, account, "gemini-test", "countTokens", false, []byte(`{"contents":[]}`), false)
	if err != nil || result == nil {
		t.Fatal("countTokens local behavior changed")
	}
	if len(recorder.statuses) != 0 {
		t.Fatal("local token counting was reported as upstream business success")
	}
}
