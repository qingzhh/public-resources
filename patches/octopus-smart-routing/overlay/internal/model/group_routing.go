package model

import "time"

type GroupRoutingCircuit struct {
	ChannelKeyID        int        `json:"channel_key_id"`
	KeyRemark           string     `json:"key_remark"`
	State               string     `json:"state"`
	ConsecutiveFailures int64      `json:"consecutive_failures"`
	TripCount           int        `json:"trip_count"`
	RemainingMS         int64      `json:"remaining_ms"`
	LastFailureAt       *time.Time `json:"last_failure_at,omitempty"`
}

type GroupRoutingItemView struct {
	GroupItem
	ChannelName     string                `json:"channel_name"`
	ChannelEnabled  bool                  `json:"channel_enabled"`
	SiteID          *int                  `json:"site_id,omitempty"`
	SiteName        string                `json:"site_name,omitempty"`
	SiteAccountID   *int                  `json:"site_account_id,omitempty"`
	SiteAccountName string                `json:"site_account_name,omitempty"`
	SiteGroupKey    string                `json:"site_group_key,omitempty"`
	SiteGroupName   string                `json:"site_group_name,omitempty"`
	EnabledKeyCount int                   `json:"enabled_key_count"`
	TrippedKeyCount int                   `json:"tripped_key_count"`
	Circuits        []GroupRoutingCircuit `json:"circuits"`
}

type GroupRoutingView struct {
	GroupID     int                    `json:"group_id"`
	GroupName   string                 `json:"group_name"`
	Mode        GroupMode              `json:"mode"`
	PriceWeight int                    `json:"price_weight"`
	SpeedWeight int                    `json:"speed_weight"`
	Items       []GroupRoutingItemView `json:"items"`
}
