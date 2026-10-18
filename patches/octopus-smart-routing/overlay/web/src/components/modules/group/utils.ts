import type { LLMChannel } from '@/api/endpoints/model';
import { GroupMode, type GroupRoutingItem } from '@/api/endpoints/group';

export const MODE_LABELS: Record<GroupMode, string> = {
    [GroupMode.RoundRobin]: 'roundRobin',
    [GroupMode.Random]: 'random',
    [GroupMode.Failover]: 'failover',
    [GroupMode.Weighted]: 'weighted',
    [GroupMode.SmartFailover]: 'smartFailover',
} as const;

export function normalizeKey(value: string) {
    return value.trim().toLowerCase();
}

export function modelChannelKey(channelId: number, modelName: string) {
    return `${channelId}-${modelName}`;
}

export function memberKey(member: Pick<LLMChannel, 'channel_id' | 'name'>) {
    return modelChannelKey(member.channel_id, member.name);
}

export function getGroupRoutingState(item: GroupRoutingItem) {
    if (item.enabled_key_count > 0 && item.tripped_key_count >= item.enabled_key_count) return 'tripped' as const;
    if (item.tripped_key_count > 0 || item.health_state === 'degraded') return 'degraded' as const;
    if (item.health_state === 'healthy') return 'healthy' as const;
    return 'unknown' as const;
}

export function matchesGroupName(modelName: string, groupKey: string) {
    if (!groupKey) return false;
    return modelName.toLowerCase().includes(groupKey);
}

export function buildChannelNameByModelKey(modelChannels: LLMChannel[]) {
    const map = new Map<string, string>();
    modelChannels.forEach((mc) => {
        map.set(modelChannelKey(mc.channel_id, mc.name), mc.channel_name);
    });
    return map;
}
