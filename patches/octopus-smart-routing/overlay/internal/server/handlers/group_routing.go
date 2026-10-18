package handlers

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/bestruirui/octopus/internal/grouphealth"
	"github.com/bestruirui/octopus/internal/model"
	"github.com/bestruirui/octopus/internal/op"
	"github.com/bestruirui/octopus/internal/relay/balancer"
	"github.com/bestruirui/octopus/internal/server/resp"
	"github.com/gin-gonic/gin"
)

func getGroupRouting(c *gin.Context) {
	groupID, ok := parsePositiveID(c.Param("id"))
	if !ok {
		resp.InvalidParam(c)
		return
	}
	view, err := buildGroupRoutingView(c.Request.Context(), groupID)
	if err != nil {
		resp.Error(c, http.StatusInternalServerError, err.Error())
		return
	}
	resp.Success(c, view)
}

func refreshGroupRouting(c *gin.Context) {
	groupID, ok := parsePositiveID(c.Param("id"))
	if !ok {
		resp.InvalidParam(c)
		return
	}
	var req model.GroupRoutingRefreshRequest
	if err := c.ShouldBindJSON(&req); err != nil && !errors.Is(err, io.EOF) {
		resp.InvalidJSON(c)
		return
	}
	if err := op.SmartRoutingRefreshGroupNow(c.Request.Context(), groupID, req.ClearManualRank); err != nil {
		status := http.StatusInternalServerError
		if errors.Is(err, op.ErrSmartRoutingModeRequired) {
			status = http.StatusBadRequest
		}
		resp.Error(c, status, err.Error())
		return
	}
	view, err := buildGroupRoutingView(c.Request.Context(), groupID)
	if err != nil {
		resp.Error(c, http.StatusInternalServerError, err.Error())
		return
	}
	resp.Success(c, view)
}

func probeGroupRoutingItem(c *gin.Context) {
	groupID, groupOK := parsePositiveID(c.Param("id"))
	itemID, itemOK := parsePositiveID(c.Param("itemId"))
	if !groupOK || !itemOK {
		resp.InvalidParam(c)
		return
	}
	group, err := op.GroupGet(groupID, c.Request.Context())
	if err != nil {
		resp.Error(c, http.StatusNotFound, err.Error())
		return
	}
	var item *model.GroupItem
	for index := range group.Items {
		if group.Items[index].ID == itemID {
			item = &group.Items[index]
			break
		}
	}
	if item == nil {
		resp.Error(c, http.StatusNotFound, "group item not found")
		return
	}
	channel, err := op.ChannelGet(item.ChannelID, c.Request.Context())
	if err != nil {
		resp.Error(c, http.StatusNotFound, err.Error())
		return
	}

	tripped := make(map[int]balancer.CircuitSnapshot)
	for _, snapshot := range balancer.ListCircuitSnapshots() {
		if snapshot.ChannelID == item.ChannelID && snapshot.ModelName == item.ModelName && snapshot.State != balancer.StateClosed {
			tripped[snapshot.ChannelKeyID] = snapshot
		}
	}
	probeKeys := selectProbeKeys(channel.Keys, tripped)
	if len(probeKeys) == 0 {
		resp.Error(c, http.StatusConflict, "group item has no enabled keys")
		return
	}
	accountKey := -channel.ID
	if binding, bindingErr := op.SiteChannelBindingGetByChannelID(channel.ID, c.Request.Context()); bindingErr == nil {
		accountKey = binding.SiteAccountID
	}
	release, acquired := grouphealth.AcquireProbeSlot(c.Request.Context(), accountKey)
	if !acquired {
		resp.Error(c, http.StatusRequestTimeout, "probe canceled")
		return
	}
	defer release()

	prober := grouphealth.NewProber()
	successCount := 0
	attemptedCount := 0
	bestDurationMS := int64(0)
	lastDurationMS := int64(0)
	for _, key := range probeKeys {
		attemptedCount++
		result := prober.RunCandidate(c.Request.Context(), *channel, key, item.ModelName)
		lastDurationMS = result.DurationMS
		if result.Success {
			successCount++
			if bestDurationMS == 0 || result.DurationMS < bestDurationMS {
				bestDurationMS = result.DurationMS
			}
			balancer.RecordSuccess(channel.ID, key.ID, item.ModelName)
		} else {
			balancer.RecordProbeFailure(channel.ID, key.ID, item.ModelName)
		}
	}
	probeDurationMS := lastDurationMS
	if successCount > 0 {
		probeDurationMS = bestDurationMS
	}
	if attemptedCount > 0 {
		if err := op.SmartRoutingRecordProbe(c.Request.Context(), channel.ID, item.ModelName, probeDurationMS, successCount > 0); err != nil {
			resp.Error(c, http.StatusInternalServerError, err.Error())
			return
		}
	}
	if err := op.SmartRoutingRefreshGroup(c.Request.Context(), groupID); err != nil {
		resp.Error(c, http.StatusInternalServerError, err.Error())
		return
	}
	view, err := buildGroupRoutingView(c.Request.Context(), groupID)
	if err != nil {
		resp.Error(c, http.StatusInternalServerError, err.Error())
		return
	}
	c.JSON(http.StatusOK, resp.ResponseStruct{Code: http.StatusOK, Message: "success", Data: gin.H{
		"attempted": attemptedCount,
		"recovered": successCount,
		"routing":   view,
	}})
}

func selectProbeKeys(keys []model.ChannelKey, tripped map[int]balancer.CircuitSnapshot) []model.ChannelKey {
	selected := make([]model.ChannelKey, 0, len(keys))
	for _, key := range keys {
		if !key.Enabled || strings.TrimSpace(key.ChannelKey) == "" {
			continue
		}
		if len(tripped) > 0 {
			if _, ok := tripped[key.ID]; !ok {
				continue
			}
		}
		selected = append(selected, key)
	}
	return selected
}

func buildGroupRoutingView(ctx context.Context, groupID int) (*model.GroupRoutingView, error) {
	group, err := op.GroupGet(groupID, ctx)
	if err != nil {
		return nil, err
	}
	snapshots := balancer.ListCircuitSnapshots()
	view := &model.GroupRoutingView{
		GroupID: group.ID, GroupName: group.Name, Mode: group.Mode,
		PriceWeight: group.PriceWeight, SpeedWeight: group.SpeedWeight,
		Items: make([]model.GroupRoutingItemView, 0, len(group.Items)),
	}
	items := group.Items
	if group.Mode == model.GroupModeSmartFailover {
		items = (&balancer.SmartFailover{}).Candidates(*group)
	}
	for _, item := range items {
		itemView := model.GroupRoutingItemView{GroupItem: item, Circuits: make([]model.GroupRoutingCircuit, 0)}
		channel, getErr := op.ChannelGet(item.ChannelID, ctx)
		if getErr == nil {
			itemView.ChannelName = channel.Name
			itemView.ChannelEnabled = channel.Enabled
			for _, key := range channel.Keys {
				if key.Enabled && strings.TrimSpace(key.ChannelKey) != "" {
					itemView.EnabledKeyCount++
				}
			}
			if channel.ManagedSource != nil {
				itemView.SiteID = &channel.ManagedSource.SiteID
				itemView.SiteAccountID = &channel.ManagedSource.SiteAccountID
				itemView.SiteGroupKey = channel.ManagedSource.GroupKey
				if site, siteErr := op.SiteGet(channel.ManagedSource.SiteID, ctx); siteErr == nil {
					itemView.SiteName = site.Name
				}
				if account, accountErr := op.SiteAccountGet(channel.ManagedSource.SiteAccountID, ctx); accountErr == nil {
					itemView.SiteAccountName = account.Name
				}
			}
		}
		for _, snapshot := range snapshots {
			if snapshot.ChannelID != item.ChannelID || snapshot.ModelName != item.ModelName || snapshot.State == balancer.StateClosed {
				continue
			}
			circuit := model.GroupRoutingCircuit{
				ChannelKeyID: snapshot.ChannelKeyID, State: balancer.CircuitStateName(snapshot.State),
				ConsecutiveFailures: snapshot.ConsecutiveFailures, TripCount: snapshot.TripCount,
				RemainingMS: snapshot.RemainingMS,
			}
			if !snapshot.LastFailureAt.IsZero() {
				at := snapshot.LastFailureAt
				circuit.LastFailureAt = &at
			}
			if channel != nil {
				for _, key := range channel.Keys {
					if key.ID == snapshot.ChannelKeyID {
						circuit.KeyRemark = key.Remark
						break
					}
				}
			}
			itemView.Circuits = append(itemView.Circuits, circuit)
			itemView.TrippedKeyCount++
		}
		view.Items = append(view.Items, itemView)
	}
	return view, nil
}

func parsePositiveID(value string) (int, bool) {
	id, err := strconv.Atoi(value)
	return id, err == nil && id > 0
}
