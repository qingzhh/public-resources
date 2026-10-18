package balancer

import (
	"context"
	"fmt"
	"time"

	"github.com/bestruirui/octopus/internal/model"
	"github.com/bestruirui/octopus/internal/op"
)

// Iterator 统一的负载均衡迭代器
// 内部编排：策略排序 + 粘性优先 + 决策追踪
type Iterator struct {
	candidates        []model.GroupItem
	index             int
	stickyIdx         int // 粘性通道在 candidates 中的位置，-1 表示无
	stickyKeyID       int
	failOpenChannelID int
	failOpenKeyID     int
	modelName         string // 请求模型名（用于熔断检查）
	zeroCostClaim     *zeroCostTakeoverClaim
	zeroCostItemID    int

	// 内嵌追踪
	attempts []model.ChannelAttempt
	count    int
}

// NewIterator 创建负载均衡迭代器
// 自动处理：策略排序 + 粘性通道提前
func NewIterator(group model.Group, apiKeyID int, requestModel string) *Iterator {
	return NewIteratorWithPreference(group, apiKeyID, requestModel, nil)
}

// NewIteratorWithPreference 创建带优先通道偏好的负载均衡迭代器。
// preferred 非空时，会优先把指定通道提前到候选列表最前面。
func NewIteratorWithPreference(group model.Group, apiKeyID int, requestModel string, preferred *SessionEntry) *Iterator {
	b := GetBalancer(group.Mode)
	candidates := b.Candidates(group)

	stickyIdx := -1
	stickyKeyID := 0
	if preferred != nil && preferred.ChannelID > 0 {
		for i, item := range candidates {
			if item.ChannelID == preferred.ChannelID && stickyCandidateHealthy(group, item) {
				if i > 0 {
					preferredItem := candidates[i]
					copy(candidates[1:i+1], candidates[0:i])
					candidates[0] = preferredItem
				}
				stickyIdx = 0
				stickyKeyID = preferred.ChannelKeyID
				break
			}
		}
	}
	var zeroCostClaim *zeroCostTakeoverClaim
	zeroCostItemID := 0
	if stickyIdx < 0 && group.Mode == model.GroupModeSmartFailover && group.ZeroCostTakeoverEnabled {
		var sticky *SessionEntry
		if group.SessionKeepTime > 0 {
			stickyTTL := time.Duration(group.SessionKeepTime) * time.Second
			sticky = GetSticky(apiKeyID, requestModel, stickyTTL)
		}
		zeroItems := make([]model.GroupItem, 0)
		paidItems := make([]model.GroupItem, 0, len(candidates))
		stickyZeroIndex := -1
		for _, item := range candidates {
			if item.PriceMultiplier == 0 {
				if sticky != nil && item.ChannelID == sticky.ChannelID {
					stickyZeroIndex = len(zeroItems)
				}
				zeroItems = append(zeroItems, item)
			} else {
				paidItems = append(paidItems, item)
			}
		}
		if claim, selected := claimZeroCostTakeover(group, apiKeyID, requestModel, len(zeroItems), stickyZeroIndex); claim != nil {
			zeroCostClaim = claim
			selectedItem := zeroItems[selected]
			zeroCostItemID = selectedItem.ID
			candidates = append([]model.GroupItem{selectedItem}, paidItems...)
			if stickyZeroIndex >= 0 {
				stickyIdx = 0
				stickyKeyID = sticky.ChannelKeyID
			}
		} else if len(zeroItems) > 0 {
			candidates = paidItems
		}
	}
	if stickyIdx < 0 && zeroCostClaim == nil && group.SessionKeepTime > 0 {
		stickyTTL := time.Duration(group.SessionKeepTime) * time.Second
		if sticky := GetSticky(apiKeyID, requestModel, stickyTTL); sticky != nil {
			for i, item := range candidates {
				if item.ChannelID == sticky.ChannelID && stickyCandidateHealthy(group, item) {
					if i > 0 {
						// 将粘性通道移到最前面
						stickyItem := candidates[i]
						copy(candidates[1:i+1], candidates[0:i])
						candidates[0] = stickyItem
					}
					stickyIdx = 0
					stickyKeyID = sticky.ChannelKeyID
					break
				}
			}
		}
	}
	failOpenChannelID, failOpenKeyID := 0, 0
	if group.Mode == model.GroupModeSmartFailover {
		failOpenChannelID, failOpenKeyID = selectFailOpenKey(group)
	}

	return &Iterator{
		candidates:        candidates,
		index:             -1,
		stickyIdx:         stickyIdx,
		stickyKeyID:       stickyKeyID,
		failOpenChannelID: failOpenChannelID,
		failOpenKeyID:     failOpenKeyID,
		modelName:         requestModel,
		zeroCostClaim:     zeroCostClaim,
		zeroCostItemID:    zeroCostItemID,
	}
}

func stickyCandidateHealthy(group model.Group, item model.GroupItem) bool {
	return group.Mode != model.GroupModeSmartFailover || item.HealthState != model.GroupItemHealthDegraded
}

// Next 移动到下一个候选，返回 false 表示遍历完成
func (it *Iterator) Next() bool {
	it.index++
	return it.index < len(it.candidates)
}

// Item 返回当前候选的 GroupItem
func (it *Iterator) Item() model.GroupItem {
	return it.candidates[it.index]
}

// IsSticky 当前候选是否为粘性通道
func (it *Iterator) IsSticky() bool {
	return it.stickyIdx >= 0 && it.index == it.stickyIdx
}

func (it *Iterator) IsZeroCostTakeover() bool {
	return it.index >= 0 && it.index < len(it.candidates) && it.zeroCostItemID != 0 && it.candidates[it.index].ID == it.zeroCostItemID
}

func (it *Iterator) releaseZeroCostTakeover(failed bool) {
	if it.zeroCostClaim != nil {
		it.zeroCostClaim.release(failed)
		it.zeroCostClaim = nil
	}
}

func (it *Iterator) Close() {
	it.releaseZeroCostTakeover(false)
}

func (it *Iterator) StickyKeyID() int {
	if !it.IsSticky() {
		return 0
	}
	return it.stickyKeyID
}

// Len 返回候选列表长度
func (it *Iterator) Len() int {
	return len(it.candidates)
}

// Index 返回当前迭代位置（0-based）
func (it *Iterator) Index() int {
	return it.index
}

// Skip 记录当前通道被跳过（通道禁用、无Key、类型不兼容等）
func (it *Iterator) Skip(channelID, channelKeyID int, channelName, msg string) {
	it.count++
	it.attempts = append(it.attempts, model.ChannelAttempt{
		ChannelID:       channelID,
		ChannelKeyID:    channelKeyID,
		ChannelName:     channelName,
		ModelName:       it.candidates[it.index].ModelName,
		PriceMultiplier: it.currentPriceMultiplier(),
		AttemptNum:      it.count,
		Status:          model.AttemptSkipped,
		Sticky:          it.IsSticky(),
		Msg:             msg,
	})
	if it.IsZeroCostTakeover() {
		it.releaseZeroCostTakeover(true)
	}
}

// SkipCircuitBreak 检查熔断状态，若已熔断自动记录（含剩余冷却时间）并返回 true
func (it *Iterator) SkipCircuitBreak(channelID, channelKeyID int, channelName string) bool {
	modelName := it.candidates[it.index].ModelName
	if channelID == it.failOpenChannelID && channelKeyID == it.failOpenKeyID {
		return false
	}
	tripped, remaining := IsTripped(channelID, channelKeyID, modelName)
	if !tripped {
		return false
	}
	msg := "circuit breaker tripped"
	if remaining > 0 {
		msg = fmt.Sprintf("circuit breaker tripped, remaining cooldown: %ds", int(remaining.Seconds()))
	}
	it.count++
	it.attempts = append(it.attempts, model.ChannelAttempt{
		ChannelID:       channelID,
		ChannelKeyID:    channelKeyID,
		ChannelName:     channelName,
		ModelName:       modelName,
		PriceMultiplier: it.currentPriceMultiplier(),
		AttemptNum:      it.count,
		Status:          model.AttemptCircuitBreak,
		Sticky:          it.IsSticky(),
		Msg:             msg,
	})
	if it.IsZeroCostTakeover() {
		it.releaseZeroCostTakeover(true)
	}
	return true
}

func selectFailOpenKey(group model.Group) (int, int) {
	usable := 0
	tripped := 0
	selectedChannelID := 0
	selectedKeyID := 0
	var selectedRemaining time.Duration
	for _, item := range group.Items {
		channel, err := op.ChannelGet(item.ChannelID, context.Background())
		if err != nil || !channel.Enabled {
			continue
		}
		for _, key := range channel.Keys {
			if !key.Enabled || key.ChannelKey == "" {
				continue
			}
			usable++
			isTripped, remaining := PeekTripped(channel.ID, key.ID, item.ModelName)
			if !isTripped {
				continue
			}
			tripped++
			if selectedKeyID == 0 || remaining < selectedRemaining {
				selectedChannelID = channel.ID
				selectedKeyID = key.ID
				selectedRemaining = remaining
			}
		}
	}
	if usable > 0 && usable == tripped {
		return selectedChannelID, selectedKeyID
	}
	return 0, 0
}

// StartAttempt 开始一次真实转发尝试，返回 Span 用于记录结果
func (it *Iterator) StartAttempt(channelID, channelKeyID int, channelName string) *AttemptSpan {
	it.count++
	return &AttemptSpan{
		attempt: model.ChannelAttempt{
			ChannelID:       channelID,
			ChannelKeyID:    channelKeyID,
			ChannelName:     channelName,
			ModelName:       it.candidates[it.index].ModelName,
			PriceMultiplier: it.currentPriceMultiplier(),
			AttemptNum:      it.count,
			Sticky:          it.IsSticky(),
		},
		startTime:        time.Now(),
		iter:             it,
		zeroCostTakeover: it.IsZeroCostTakeover(),
	}
}

func (it *Iterator) currentPriceMultiplier() *float64 {
	multiplier := it.candidates[it.index].PriceMultiplier
	if multiplier < 0 {
		multiplier = 1
	}
	return &multiplier
}

// Attempts 返回所有决策记录（交给日志模块持久化）
func (it *Iterator) Attempts() []model.ChannelAttempt {
	return it.attempts
}

// AttemptSpan 管理单次通道尝试的生命周期（计时、状态、结果）
type AttemptSpan struct {
	attempt          model.ChannelAttempt
	startTime        time.Time
	iter             *Iterator
	ended            bool
	zeroCostTakeover bool
}

// End 结束尝试：设置状态，自动计算耗时，追加到 Iterator
func (s *AttemptSpan) End(status model.AttemptStatus, statusCode int, msg string) {
	if s.ended {
		return
	}
	s.ended = true
	s.attempt.Status = status
	s.attempt.Duration = int(time.Since(s.startTime).Milliseconds())
	s.attempt.Msg = msg
	s.iter.attempts = append(s.iter.attempts, s.attempt)
	if s.zeroCostTakeover {
		s.iter.releaseZeroCostTakeover(status != model.AttemptSuccess)
	}
}

// Duration 返回从开始到现在的耗时
func (s *AttemptSpan) Duration() time.Duration {
	return time.Since(s.startTime)
}
