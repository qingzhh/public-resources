import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '../client';
import { logger } from '@/lib/logger';
import { AutoGroupType } from './channel';

/**
 * 分组项信息
 */
export interface GroupItem {
    id?: number;
    group_id?: number;
    channel_id: number;
    model_name: string;
    priority: number;
    weight: number;
    manual_rank?: number | null;
    auto_rank?: number;
    expected_cost?: number;
    speed_ms?: number;
    success_rate?: number;
    price_score?: number;
    speed_score?: number;
    composite_score?: number;
    health_state?: 'unknown' | 'healthy' | 'degraded';
    price_multiplier?: number;
    price_known?: boolean;
    speed_source?: 'ttft' | 'probe' | 'head' | 'unknown';
    sample_count?: number;
    last_request_at?: string | null;
    last_probe_at?: string | null;
    last_probe_ms?: number;
    last_probe_success?: boolean;
    score_updated_at?: string | null;
}

/**
 * 分组模式
 */
export enum GroupMode {
    RoundRobin = 1,
    Random = 2,
    Failover = 3,
    Weighted = 4,
    SmartFailover = 5,
}

/**
 * 分组信息
 */
export interface Group {
    id?: number;
    name: string;
    mode: GroupMode;
    match_regex: string;
    first_token_time_out?: number;
    session_keep_time?: number;
    retry_enabled?: boolean;
    max_retries?: number;
    price_weight?: number;
    speed_weight?: number;
    smart_reorder_interval?: number;
    smart_reorder_clear_manual?: boolean;
    smart_reorder_started_at?: string | null;
    smart_reorder_next_at?: string | null;
    zero_cost_takeover_enabled?: boolean;
    zero_cost_takeover_interval?: number;
    sort_order?: number;
    pinned?: boolean;
    pinned_at?: string | null;
    active_preset_id?: number | null;
    items?: GroupItem[];
}

/**
 * 预设中的渠道-模型条目（JSON 快照内容）
 */
export interface GroupPresetItem {
    channel_id: number;
    model_name: string;
    priority: number;
    weight: number;
    manual_rank?: number | null;
}

/**
 * 分组预设：命名快照，包含 Mode/超时/重试/regex + items
 */
export interface GroupPreset {
    id: number;
    group_id: number;
    name: string;
    mode: GroupMode;
    match_regex: string;
    first_token_time_out: number;
    session_keep_time: number;
    retry_enabled: boolean;
    max_retries: number;
    price_weight: number;
    speed_weight: number;
    zero_cost_takeover_enabled: boolean;
    zero_cost_takeover_interval: number;
    items: GroupPresetItem[];
    created_at: string;
    updated_at: string;
}

/**
 * 预设直接编辑请求（仅非活动预设可调）
 * Items 为整体替换语义
 */
export interface GroupPresetUpdateRequest {
    name?: string;
    mode?: GroupMode;
    match_regex?: string;
    first_token_time_out?: number;
    session_keep_time?: number;
    retry_enabled?: boolean;
    max_retries?: number;
    price_weight?: number;
    speed_weight?: number;
    zero_cost_takeover_enabled?: boolean;
    zero_cost_takeover_interval?: number;
    items?: GroupPresetItem[];
}

/**
 * 新增 item 请求
 */
export interface GroupItemAddRequest {
    channel_id: number;
    model_name: string;
    priority: number;
    weight: number;
    manual_rank?: number;
}

/**
 * 更新 item 请求 (仅 priority)
 */
export interface GroupItemUpdateRequest {
    id: number;
    priority: number;
    weight: number;
    manual_rank?: number;
    clear_manual_rank?: boolean;
}

/**
 * 分组更新请求 - 仅包含变更的数据
 */
export interface GroupUpdateRequest {
    id: number;
    name?: string;                        // 仅在名称变更时发送
    mode?: GroupMode;                     // 仅在模式变更时发送
    match_regex?: string;                 // 仅在匹配正则变更时发送
    first_token_time_out?: number;        // 仅在超时变更时发送
    session_keep_time?: number;           // 仅在会话保持时间变更时发送
    retry_enabled?: boolean;              // 仅在同通道重试开关变更时发送
    max_retries?: number;                 // 仅在最大重试次数变更时发送
    price_weight?: number;
    speed_weight?: number;
    smart_reorder_interval?: number;
    smart_reorder_clear_manual?: boolean;
    zero_cost_takeover_enabled?: boolean;
    zero_cost_takeover_interval?: number;
    items_to_add?: GroupItemAddRequest[];    // 新增的 items
    items_to_update?: GroupItemUpdateRequest[]; // 更新的 items (priority 变更)
    items_to_delete?: number[];              // 删除的 item IDs
}

export interface GroupAutoGroupSource {
    channel_id: number;
    channel_name: string;
    enabled: boolean;
    managed: boolean;
    auto_group: AutoGroupType;
    effective_auto_group: AutoGroupType;
    global_override: boolean;
    model_count: number;
    models: string[];
    site_id?: number | null;
    site_name?: string;
    site_account_id?: number | null;
    site_account_name?: string;
    site_group_key?: string;
    site_group_name?: string;
    endpoint_type?: string;
}

export interface GroupAutoGroupConfig {
    projected_global_auto_group: AutoGroupType;
    sources: GroupAutoGroupSource[];
}

export interface GroupAutoGroupSourceUpdateRequest {
    channel_id: number;
    auto_group: AutoGroupType;
}

export interface GroupAutoGroupConfigUpdateRequest {
    projected_global_auto_group?: AutoGroupType;
    items?: GroupAutoGroupSourceUpdateRequest[];
    run_now?: boolean;
}

export interface GroupAutoGroupRunRequest {
    channel_ids?: number[];
}

/**
 * 获取分组列表 Hook
 * 
 * @example
 * const { data: groups, isLoading, error } = useGroupList();
 * 
 * if (isLoading) return <Loading />;
 * if (error) return <Error message={error.message} />;
 * 
 * groups?.forEach(group => console.log(group.name, group.items));
 */
export function useGroupList() {
    return useQuery({
        queryKey: ['groups', 'list'],
        queryFn: async () => {
            return apiClient.get<Group[]>('/api/v1/group/list');
        },
        refetchInterval: 30000,
    });
}

export function useReorderGroups() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: async (ids: number[]) => apiClient.post<Group[]>('/api/v1/group/reorder', { ids }),
        onMutate: (ids) => {
            queryClient.cancelQueries({ queryKey: ['groups', 'list'] });
            const previous = queryClient.getQueryData<Group[]>(['groups', 'list']);
            const order = new Map(ids.map((id, index) => [id, index + 1]));
            queryClient.setQueryData<Group[]>(['groups', 'list'], (groups) =>
                groups?.map((group) => ({ ...group, sort_order: group.id ? (order.get(group.id) ?? group.sort_order) : group.sort_order })),
            );
            return { previous };
        },
        onError: (error, _ids, context) => {
            if (context?.previous) queryClient.setQueryData(['groups', 'list'], context.previous);
            logger.error('分组排序失败:', error);
        },
        onSuccess: (groups) => queryClient.setQueryData(['groups', 'list'], groups),
        onSettled: () => queryClient.invalidateQueries({ queryKey: ['groups', 'list'] }),
    });
}

/**
 * 创建分组 Hook
 * 
 * @example
 * const createGroup = useCreateGroup();
 * 
 * createGroup.mutate({
 *   name: 'my-group',
 *   items: [
 *     { channel_id: 1, model_name: 'gpt-4', priority: 1 },
 *   ],
 * });
 */
export function useCreateGroup() {
    const queryClient = useQueryClient();

    return useMutation({
        mutationFn: async (data: Group) => {
            return apiClient.post<Group>('/api/v1/group/create', data);
        },
        onSuccess: (data) => {
            logger.log('分组创建成功:', data);
            queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
        },
        onError: (error) => {
            logger.error('分组创建失败:', error);
        },
    });
}

/**
 * 更新分组 Hook - 仅发送变更的数据
 * 
 * @example
 * const updateGroup = useUpdateGroup();
 * 
 * updateGroup.mutate({
 *   id: 1,
 *   name: 'updated-group',  // 可选，仅在名称变更时发送
 *   items_to_add: [{ channel_id: 1, model_name: 'gpt-4', priority: 1 }],
 *   items_to_update: [{ id: 1, priority: 2 }],
 *   items_to_delete: [2, 3],
 * });
 */
/**
 * 把 GroupUpdateRequest 应用到一个 Group 上，用于乐观更新
 * 拖拽重排序场景需要立刻反映新顺序，否则 drop 动画结束后会先短暂回到旧顺序、再等服务器响应后跳到新顺序，造成两次视觉变动
 */
function applyGroupUpdate(group: Group, req: GroupUpdateRequest): Group {
    const next: Group = { ...group };
    if (req.name !== undefined) next.name = req.name;
    if (req.mode !== undefined) next.mode = req.mode;
    if (req.match_regex !== undefined) next.match_regex = req.match_regex;
    if (req.first_token_time_out !== undefined) next.first_token_time_out = req.first_token_time_out;
    if (req.session_keep_time !== undefined) next.session_keep_time = req.session_keep_time;
    if (req.retry_enabled !== undefined) next.retry_enabled = req.retry_enabled;
    if (req.max_retries !== undefined) next.max_retries = req.max_retries;
    if (req.price_weight !== undefined) next.price_weight = req.price_weight;
    if (req.speed_weight !== undefined) next.speed_weight = req.speed_weight;
    if (req.smart_reorder_interval !== undefined) {
        next.smart_reorder_interval = req.smart_reorder_interval;
        next.smart_reorder_started_at = null;
        next.smart_reorder_next_at = null;
    }
    if (req.smart_reorder_clear_manual !== undefined) next.smart_reorder_clear_manual = req.smart_reorder_clear_manual;
    if (req.zero_cost_takeover_enabled !== undefined) next.zero_cost_takeover_enabled = req.zero_cost_takeover_enabled;
    if (req.zero_cost_takeover_interval !== undefined) next.zero_cost_takeover_interval = req.zero_cost_takeover_interval;
    if (req.mode !== undefined && req.mode !== group.mode) {
        next.smart_reorder_started_at = null;
        next.smart_reorder_next_at = null;
    }

    let items = [...(group.items ?? [])];
    if (req.items_to_delete?.length) {
        const ids = new Set(req.items_to_delete);
        items = items.filter((it) => it.id === undefined || !ids.has(it.id));
    }
    if (req.items_to_update?.length) {
        const updateById = new Map(req.items_to_update.map((u) => [u.id, u] as const));
        items = items.map((it) => {
            if (it.id === undefined) return it;
            const u = updateById.get(it.id);
            if (!u) return it;
            return {
                ...it,
                priority: u.priority,
                weight: u.weight,
                manual_rank: u.clear_manual_rank ? null : (u.manual_rank ?? it.manual_rank),
            };
        });
    }
    if (req.items_to_add?.length) {
        // 临时负数 id 给乐观新增项占位，服务器响应后会被真实 id 替换
        let tempId = -Date.now();
        for (const a of req.items_to_add) {
            items.push({
                id: tempId--,
                group_id: group.id,
                channel_id: a.channel_id,
                model_name: a.model_name,
                priority: a.priority,
                weight: a.weight,
                manual_rank: a.manual_rank,
            });
        }
    }
    next.items = items;
    return next;
}

export interface GroupRoutingCircuit {
    channel_key_id: number;
    key_remark: string;
    state: 'open' | 'half_open' | 'closed';
    consecutive_failures: number;
    trip_count: number;
    remaining_ms: number;
    last_failure_at?: string | null;
}

export interface GroupRoutingItem extends GroupItem {
    channel_name: string;
    channel_enabled: boolean;
    site_id?: number | null;
    site_name?: string;
    site_account_id?: number | null;
    site_account_name?: string;
    site_group_key?: string;
    site_group_name?: string;
    enabled_key_count: number;
    tripped_key_count: number;
    circuits: GroupRoutingCircuit[];
}

export interface GroupRoutingView {
    group_id: number;
    group_name: string;
    mode: GroupMode;
    price_weight: number;
    speed_weight: number;
    items: GroupRoutingItem[];
}

export function useGroupRouting(groupId?: number, active = true) {
    return useQuery({
        queryKey: ['groups', 'routing', groupId],
        queryFn: async () => apiClient.get<GroupRoutingView>(`/api/v1/group/${groupId}/routing`),
        enabled: active && typeof groupId === 'number' && groupId > 0,
        refetchInterval: (query) => {
            const data = query.state.data as GroupRoutingView | undefined;
            return data?.items.some((item) => item.tripped_key_count > 0) ? 5000 : 30000;
        },
    });
}

export function useRefreshGroupRouting() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: async ({ groupId, clearManualRank = false }: { groupId: number; clearManualRank?: boolean }) =>
            apiClient.post<GroupRoutingView>(`/api/v1/group/${groupId}/routing/refresh`, { clear_manual_rank: clearManualRank }),
        onSuccess: (data, { groupId }) => {
            queryClient.setQueryData(['groups', 'routing', groupId], data);
            queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
        },
    });
}

export function useProbeGroupRoutingItem() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: async ({ groupId, itemId }: { groupId: number; itemId: number }) =>
            apiClient.post<{ attempted: number; recovered: number; routing: GroupRoutingView }>(`/api/v1/group/${groupId}/items/${itemId}/probe`, {}),
        onSuccess: (data, variables) => {
            queryClient.setQueryData(['groups', 'routing', variables.groupId], data.routing);
            queryClient.invalidateQueries({ queryKey: ['group-health', 'list'] });
            queryClient.invalidateQueries({ queryKey: ['group-health', 'detail', variables.groupId] });
            queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
        },
    });
}

export function useUpdateGroup() {
    const queryClient = useQueryClient();

    return useMutation({
        mutationFn: async (data: GroupUpdateRequest) => {
            return apiClient.post<Group>('/api/v1/group/update', data);
        },
        onMutate: (data) => {
            // 不 await cancelQueries：await 会把 setQueryData 推到 microtask，
            // React 用 setMembers(next) + setIsDragging(false) 渲染时 cache 还没更，
            // 会看到一帧旧顺序的 effectiveDisplayMembers，造成肉眼可见的"文本变动"。
            // cancelQueries 本身是 fire-and-forget 安全的——它会同步标记进行中的请求
            // 为 canceled，即使响应回来也会被丢弃。
            queryClient.cancelQueries({ queryKey: ['groups', 'list'] });
            const previous = queryClient.getQueryData<Group[]>(['groups', 'list']);
            queryClient.setQueryData<Group[]>(['groups', 'list'], (old) => {
                if (!old) return old;
                return old.map((g) => (g.id === data.id ? applyGroupUpdate(g, data) : g));
            });
            return { previous };
        },
        onError: (error, _vars, context) => {
            if (context?.previous) {
                queryClient.setQueryData(['groups', 'list'], context.previous);
            }
            logger.error('分组更新失败:', error);
        },
        onSuccess: (data) => {
            logger.log('分组更新成功:', data);
        },
        onSettled: (_data, _error, variables) => {
            queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
            queryClient.invalidateQueries({ queryKey: ['groups', 'routing', variables.id] });
        },
    });
}

/**
 * 删除分组 Hook
 * 
 * @example
 * const deleteGroup = useDeleteGroup();
 * 
 * deleteGroup.mutate(1); // 删除 ID 为 1 的分组
 */
export function useDeleteGroup() {
    const queryClient = useQueryClient();

    return useMutation({
        mutationFn: async (id: number) => {
            return apiClient.delete<null>(`/api/v1/group/delete/${id}`);
        },
        onSuccess: () => {
            logger.log('分组删除成功');
            queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
        },
        onError: (error) => {
            logger.error('分组删除失败:', error);
        },
    });
}

export function useGroupAutoGroupConfig() {
    return useQuery({
        queryKey: ['groups', 'auto-group', 'config'],
        queryFn: async () => apiClient.get<GroupAutoGroupConfig>('/api/v1/group/auto-group/config'),
        refetchInterval: 30000,
    });
}

function invalidateAutoGroupRelated(queryClient: ReturnType<typeof useQueryClient>) {
    queryClient.invalidateQueries({ queryKey: ['groups', 'auto-group', 'config'] });
    queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
    queryClient.invalidateQueries({ queryKey: ['channels', 'list'] });
    queryClient.invalidateQueries({ queryKey: ['models', 'channel'] });
    queryClient.invalidateQueries({ queryKey: ['site-channel', 'list'] });
    queryClient.invalidateQueries({ queryKey: ['settings', 'list'] });
}

export function useUpdateGroupAutoGroupConfig() {
    const queryClient = useQueryClient();

    return useMutation({
        mutationFn: async (data: GroupAutoGroupConfigUpdateRequest) =>
            apiClient.put<GroupAutoGroupConfig>('/api/v1/group/auto-group/config', data),
        onSuccess: (data) => {
            logger.log('自动分组配置已更新:', data);
            queryClient.setQueryData(['groups', 'auto-group', 'config'], data);
            invalidateAutoGroupRelated(queryClient);
        },
        onError: (error) => {
            logger.error('自动分组配置更新失败:', error);
        },
    });
}

export function useRunGroupAutoGroup() {
    const queryClient = useQueryClient();

    return useMutation({
        mutationFn: async (data: GroupAutoGroupRunRequest = {}) =>
            apiClient.post<null>('/api/v1/group/auto-group/run', data),
        onSuccess: () => {
            logger.log('自动分组执行成功');
            invalidateAutoGroupRelated(queryClient);
        },
        onError: (error) => {
            logger.error('自动分组执行失败:', error);
        },
    });
}

/**
 * 自动添加分组 item Hook
 *
 * 后端路由: POST /api/v1/group/auto-add-item
 * Body: { id: number }
 *
 * @example
 * const autoAdd = useAutoAddGroupItem();
 * autoAdd.mutate(1); // 为 groupId=1 自动添加匹配的 items
 */
// export function useAutoAddGroupItem() {
//     const queryClient = useQueryClient();

//     return useMutation({
//         mutationFn: async (groupId: number) => {
//             return apiClient.post<null>(`/api/v1/group/auto-add-item`, { id: groupId });
//         },
//         onSuccess: () => {
//             logger.log('自动添加分组 item 成功');
//             queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
//         },
//         onError: (error) => {
//             logger.error('自动添加分组 item 失败:', error);
//         },
//     });
// }

/**
 * 获取某个分组的预设列表
 */
export function useGroupPresetList(groupID: number | undefined) {
    return useQuery({
        queryKey: ['groups', 'presets', groupID],
        queryFn: async () => apiClient.get<GroupPreset[]>(`/api/v1/group/preset/list/${groupID}`),
        enabled: typeof groupID === 'number' && groupID > 0,
        refetchInterval: 30000,
    });
}

/**
 * 创建预设：服务端从分组当前实时状态取快照
 */
export function useCreateGroupPreset() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: async ({ groupID, name }: { groupID: number; name: string }) =>
            apiClient.post<GroupPreset>(`/api/v1/group/preset/create/${groupID}`, { name }),
        onSuccess: (_, vars) => {
            queryClient.invalidateQueries({ queryKey: ['groups', 'presets', vars.groupID] });
            queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
        },
        onError: (error) => logger.error('预设创建失败:', error),
    });
}

/**
 * 创建空白预设：使用默认 Mode + 空 items，不读取分组当前状态
 */
export function useCreateBlankGroupPreset() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: async ({ groupID, name }: { groupID: number; name: string }) =>
            apiClient.post<GroupPreset>(`/api/v1/group/preset/create-blank/${groupID}`, { name }),
        onSuccess: (_, vars) => {
            queryClient.invalidateQueries({ queryKey: ['groups', 'presets', vars.groupID] });
        },
        onError: (error) => logger.error('空白预设创建失败:', error),
    });
}

/**
 * 克隆已有预设为副本
 */
export function useCloneGroupPreset() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: async ({ presetID, name }: { presetID: number; groupID?: number; name: string }) =>
            apiClient.post<GroupPreset>(`/api/v1/group/preset/clone/${presetID}`, { name }),
        onSuccess: (_, vars) => {
            if (vars.groupID) {
                queryClient.invalidateQueries({ queryKey: ['groups', 'presets', vars.groupID] });
            }
        },
        onError: (error) => logger.error('预设克隆失败:', error),
    });
}

/**
 * 激活预设：用预设覆盖分组的实时配置
 */
export function useActivateGroupPreset() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: async ({ presetID }: { presetID: number; groupID?: number }) =>
            apiClient.post<string>(`/api/v1/group/preset/activate/${presetID}`, {}),
        onSuccess: (_, vars) => {
            queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
            if (vars.groupID) {
                queryClient.invalidateQueries({ queryKey: ['groups', 'presets', vars.groupID] });
            }
        },
        onError: (error) => logger.error('预设激活失败:', error),
    });
}

/**
 * 直接编辑预设内容；若是 active 预设，后端会同步镜像到所属分组（live binding）
 */
export function useUpdateGroupPreset() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: async ({ presetID, data }: { presetID: number; groupID?: number; data: GroupPresetUpdateRequest }) =>
            apiClient.put<GroupPreset>(`/api/v1/group/preset/update/${presetID}`, data),
        onSuccess: (_, vars) => {
            queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
            if (vars.groupID) {
                queryClient.invalidateQueries({ queryKey: ['groups', 'presets', vars.groupID] });
            }
        },
        onError: (error) => logger.error('预设编辑失败:', error),
    });
}

/**
 * 删除预设（active 预设会被后端拒绝，需先激活其他预设）
 */
export function useDeleteGroupPreset() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: async ({ presetID }: { presetID: number; groupID?: number }) =>
            apiClient.delete<string>(`/api/v1/group/preset/delete/${presetID}`),
        onSuccess: (_, vars) => {
            if (vars.groupID) {
                queryClient.invalidateQueries({ queryKey: ['groups', 'presets', vars.groupID] });
            }
        },
        onError: (error) => logger.error('预设删除失败:', error),
    });
}

/**
 * 切换分组置顶
 */
export function useToggleGroupPin() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: async ({ groupID, pinned }: { groupID: number; pinned: boolean }) =>
            apiClient.post<string>(`/api/v1/group/pin/${groupID}`, { pinned }),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
        },
        onError: (error) => logger.error('置顶切换失败:', error),
    });
}
