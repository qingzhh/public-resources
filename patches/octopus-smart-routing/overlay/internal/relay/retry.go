package relay

import (
	"math/rand/v2"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/bestruirui/octopus/internal/model"
	"github.com/bestruirui/octopus/internal/relay/balancer"
)

// isRetryableStatus 判断 HTTP 状态码是否可重试
// 429(限流)、503(服务不可用)、>=500(服务端错误)、0(连接错误) 可重试
// 400/401/403/404 等客户端错误不可重试
func isRetryableStatus(code int) bool {
	return code == 0 || code == 429 || code >= 500
}

// isPassthroughStatus 判断是否应透传给下游客户端
// 429 和 503 透传，让客户端 SDK 的重试机制接管
func isPassthroughStatus(code int) bool {
	return code == 429 || code == 503
}

func sameChannelAttemptLimit(group model.Group, zeroCostTakeover bool) int {
	if zeroCostTakeover {
		return 1
	}
	if group.RetryEnabled {
		if group.MaxRetries > 0 {
			return group.MaxRetries
		}
		return 3
	}
	return 1
}

func isZeroCostCapacityFailure(statusCode int, err error) bool {
	if statusCode == http.StatusTooManyRequests || statusCode == http.StatusServiceUnavailable {
		return true
	}
	if statusCode < http.StatusInternalServerError || err == nil {
		return false
	}
	message := strings.ToLower(err.Error())
	return strings.Contains(message, "get_channel_failed") ||
		strings.Contains(message, "负载已经达到上限") ||
		strings.Contains(message, "load reached limit") ||
		strings.Contains(message, "no available channel") ||
		strings.Contains(message, "no available account")
}

func circuitFailureKind(retryEnabled bool, statusCode int, err error, zeroCostTakeover bool) balancer.FailureKind {
	if zeroCostTakeover && isZeroCostCapacityFailure(statusCode, err) {
		return balancer.FailureSoftRateLimit
	}
	if retryEnabled && isPassthroughStatus(statusCode) {
		return balancer.FailureSoftRateLimit
	}
	return balancer.FailureHard
}

// parseRetryAfter 解析 Retry-After 响应头（仅支持秒数格式），上限 60s
func parseRetryAfter(header string) time.Duration {
	if header == "" {
		return 0
	}
	secs, err := strconv.Atoi(header)
	if err != nil || secs <= 0 {
		return 0
	}
	d := time.Duration(secs) * time.Second
	if d > 60*time.Second {
		d = 60 * time.Second
	}
	return d
}

// computeBackoff 计算退避时间
// 优先使用 retryAfter（上游指定的等待时间），否则使用指数退避 + jitter
// retryNum 从 1 开始（第1次重试）
func computeBackoff(retryNum int, retryAfter time.Duration) time.Duration {
	if retryAfter > 0 {
		return retryAfter
	}

	// 指数退避: 1s * 2^(retryNum-1)
	base := time.Second
	shift := retryNum - 1
	if shift > 5 {
		shift = 5
	}
	delay := base << shift

	if delay > 60*time.Second {
		delay = 60 * time.Second
	}

	// 添加 10%-50% 的 jitter 防止惊群
	jitter := time.Duration(float64(delay) * (0.1 + rand.Float64()*0.4))
	return delay + jitter
}
