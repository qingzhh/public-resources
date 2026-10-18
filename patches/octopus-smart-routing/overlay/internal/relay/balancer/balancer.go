package balancer

import (
	"fmt"
	"math/rand"
	"sort"
	"sync"

	"github.com/bestruirui/octopus/internal/model"
)

var roundRobinCounters sync.Map // group key -> *roundRobinState
var weightedStates sync.Map     // group key -> *smoothWeightedState

type roundRobinState struct {
	mu      sync.Mutex
	counter uint64
}

type smoothWeightedState struct {
	mu      sync.Mutex
	current map[string]int
}

// Balancer 根据负载均衡模式选择通道
type Balancer interface {
	// Candidates 返回按策略排序的候选列表
	// 调用方在遍历候选列表时自行检查熔断状态
	Candidates(group model.Group) []model.GroupItem
}

// GetBalancer 根据模式返回对应的负载均衡器
func GetBalancer(mode model.GroupMode) Balancer {
	switch mode {
	case model.GroupModeRoundRobin:
		return &RoundRobin{}
	case model.GroupModeRandom:
		return &Random{}
	case model.GroupModeFailover:
		return &Failover{}
	case model.GroupModeWeighted:
		return &Weighted{}
	case model.GroupModeSmartFailover:
		return &SmartFailover{}
	default:
		return &RoundRobin{}
	}
}

// RoundRobin 轮询：从上次位置开始轮转排列
type RoundRobin struct{}

func (b *RoundRobin) Candidates(group model.Group) []model.GroupItem {
	items := group.Items
	n := len(items)
	if n == 0 {
		return nil
	}
	value, _ := roundRobinCounters.LoadOrStore(groupStateKey(group), &roundRobinState{})
	state := value.(*roundRobinState)
	state.mu.Lock()
	idx := int(state.counter % uint64(n))
	state.counter++
	state.mu.Unlock()
	result := make([]model.GroupItem, n)
	for i := 0; i < n; i++ {
		result[i] = items[(idx+i)%n]
	}
	return result
}

// Random 随机：随机打乱所有 items
type Random struct{}

func (b *Random) Candidates(group model.Group) []model.GroupItem {
	items := group.Items
	n := len(items)
	if n == 0 {
		return nil
	}
	result := make([]model.GroupItem, n)
	copy(result, items)
	rand.Shuffle(n, func(i, j int) {
		result[i], result[j] = result[j], result[i]
	})
	return result
}

// Failover 故障转移：按优先级排序
type Failover struct{}

func (b *Failover) Candidates(group model.Group) []model.GroupItem {
	items := group.Items
	if len(items) == 0 {
		return nil
	}
	return sortByPriority(items)
}

// Weighted 平滑加权轮询：长期首选次数严格趋近配置权重。
type Weighted struct{}

func (b *Weighted) Candidates(group model.Group) []model.GroupItem {
	items := group.Items
	n := len(items)
	if n == 0 {
		return nil
	}
	value, _ := weightedStates.LoadOrStore(groupStateKey(group), &smoothWeightedState{current: make(map[string]int)})
	state := value.(*smoothWeightedState)
	state.mu.Lock()
	defer state.mu.Unlock()

	active := make(map[string]struct{}, n)
	totalWeight := 0
	selected := 0
	selectedCurrent := 0
	for i, item := range items {
		w := item.Weight
		if w <= 0 {
			w = 1
		}
		key := groupItemStateKey(item)
		active[key] = struct{}{}
		state.current[key] += w
		totalWeight += w
		if i == 0 || state.current[key] > selectedCurrent {
			selected = i
			selectedCurrent = state.current[key]
		}
	}
	for key := range state.current {
		if _, ok := active[key]; !ok {
			delete(state.current, key)
		}
	}
	selectedKey := groupItemStateKey(items[selected])
	state.current[selectedKey] -= totalWeight

	result := append([]model.GroupItem(nil), items...)
	result[0], result[selected] = result[selected], result[0]
	if len(result) > 2 {
		sort.SliceStable(result[1:], func(i, j int) bool {
			left := state.current[groupItemStateKey(result[i+1])]
			right := state.current[groupItemStateKey(result[j+1])]
			if left != right {
				return left > right
			}
			return result[i+1].Priority < result[j+1].Priority
		})
	}
	return result
}

// SmartFailover 使用后台计算的排名，请求失败时按顺序降级。
type SmartFailover struct{}

func (b *SmartFailover) Candidates(group model.Group) []model.GroupItem {
	result := append([]model.GroupItem(nil), group.Items...)
	sort.SliceStable(result, func(i, j int) bool {
		if healthRank(result[i].HealthState) != healthRank(result[j].HealthState) {
			return healthRank(result[i].HealthState) < healthRank(result[j].HealthState)
		}
		leftRank := effectiveRank(result[i])
		rightRank := effectiveRank(result[j])
		if leftRank != rightRank {
			return leftRank < rightRank
		}
		leftManual := result[i].ManualRank != nil && *result[i].ManualRank > 0
		rightManual := result[j].ManualRank != nil && *result[j].ManualRank > 0
		if leftManual != rightManual {
			return leftManual
		}
		if result[i].CompositeScore != result[j].CompositeScore {
			return result[i].CompositeScore > result[j].CompositeScore
		}
		return result[i].Priority < result[j].Priority
	})
	return result
}

func sortByPriority(items []model.GroupItem) []model.GroupItem {
	sorted := make([]model.GroupItem, len(items))
	copy(sorted, items)
	sort.Slice(sorted, func(i, j int) bool {
		return sorted[i].Priority < sorted[j].Priority
	})
	return sorted
}

func healthRank(state model.GroupItemHealthState) int {
	switch state {
	case model.GroupItemHealthHealthy, model.GroupItemHealthUnknown:
		return 0
	default:
		return 1
	}
}

func effectiveRank(item model.GroupItem) int {
	if item.ManualRank != nil && *item.ManualRank > 0 {
		return *item.ManualRank
	}
	if item.AutoRank > 0 {
		return item.AutoRank
	}
	if item.Priority > 0 {
		return item.Priority
	}
	return int(^uint(0) >> 1)
}

func groupStateKey(group model.Group) string {
	return fmt.Sprintf("%d:%s", group.ID, group.Name)
}

func groupItemStateKey(item model.GroupItem) string {
	if item.ID > 0 {
		return fmt.Sprintf("id:%d", item.ID)
	}
	return fmt.Sprintf("channel:%d:model:%s", item.ChannelID, item.ModelName)
}

// Reset clears in-memory balancer state for tests.
func Reset() {
	roundRobinCounters = sync.Map{}
	weightedStates = sync.Map{}
	globalBreaker = sync.Map{}
	globalSession = sync.Map{}
	globalZeroCostTakeover = sync.Map{}
}
