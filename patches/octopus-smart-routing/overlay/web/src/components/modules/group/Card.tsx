'use client';

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { GripVertical, Pencil, Pin, PinOff, Trash2, X } from 'lucide-react';
import { AnimatePresence, motion } from 'motion/react';
import { useTranslations } from 'next-intl';
import {
    GroupMode,
    type Group,
    type GroupRoutingItem,
    type GroupUpdateRequest,
    useDeleteGroup,
    useGroupRouting,
    useToggleGroupPin,
    useUpdateGroup,
    useRefreshGroupRouting,
} from '@/api/endpoints/group';
import { useModelChannelList } from '@/api/endpoints/model';
import { toast } from '@/components/common/Toast';
import { CopyIconButton } from '@/components/common/CopyButton';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/animate-ui/components/animate/tooltip';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { SelectedMember } from './ItemList';
import { MemberList } from './ItemList';
import { GroupEditor, type GroupEditorValues } from './Editor';
import { GroupHealthBadge } from './health';
import { PresetPopover } from './PresetPopover';
import { modelChannelKey, MODE_LABELS } from './utils';
import { SmartReorderControls } from './smart-reorder';

export type GroupEditorOverride = Partial<Omit<GroupEditorValues, 'members' | 'name' | 'match_regex'>>;

interface GroupCardProps {
    group: Group;
    initialOverride?: GroupEditorOverride;
    onDone?: () => void;
    sidePanel?: ReactNode;
    routingItems?: GroupRoutingItem[];
}

function orderedGroupItems(group: Group) {
    const items = [...(group.items ?? [])];
    if (group.mode !== GroupMode.SmartFailover) return items.sort((a, b) => a.priority - b.priority);
    const healthRank = (state?: string) => state === 'degraded' ? 1 : 0;
    const effectiveRank = (item: (typeof items)[number]) => item.manual_rank && item.manual_rank > 0
        ? item.manual_rank
        : item.auto_rank && item.auto_rank > 0
            ? item.auto_rank
            : item.priority > 0 ? item.priority : Number.MAX_SAFE_INTEGER;
    return items.sort((a, b) => {
        const healthDifference = healthRank(a.health_state) - healthRank(b.health_state);
        if (healthDifference !== 0) return healthDifference;
        const rankDifference = effectiveRank(a) - effectiveRank(b);
        if (rankDifference !== 0) return rankDifference;
        return (b.composite_score ?? 0) - (a.composite_score ?? 0) || a.priority - b.priority;
    });
}

function buildGroupUpdateRequest(group: Group, values: GroupEditorValues): GroupUpdateRequest {
    const originalItems = orderedGroupItems(group);
    const originalByID = new Map<number, { priority: number; weight: number }>();
    const originalIDs = new Set<number>();

    originalItems.forEach((item) => {
        if (typeof item.id !== 'number') return;
        originalIDs.add(item.id);
        originalByID.set(item.id, { priority: item.priority, weight: item.weight });
    });

    const retainedIDs = new Set<number>();
    values.members.forEach((member) => {
        if (typeof member.item_id === 'number') retainedIDs.add(member.item_id);
    });

    const itemsToDelete = Array.from(originalIDs).filter((id) => !retainedIDs.has(id));
    const originalOrder = originalItems.flatMap((item) => typeof item.id === 'number' ? [item.id] : []);
    const nextOrder = values.members.flatMap((member) => typeof member.item_id === 'number' ? [member.item_id] : []);
    const orderingChanged = originalOrder.length !== nextOrder.length || originalOrder.some((id, index) => nextOrder[index] !== id);
    const hasNewItems = values.members.some((member) => typeof member.item_id !== 'number');
    const persistManualOrder = values.mode === GroupMode.SmartFailover
        && (orderingChanged || hasNewItems || itemsToDelete.length > 0);
    const forcePriorityOrder = values.mode === GroupMode.Failover && values.smart_order_imported;
    const itemsToAdd = values.members
        .map((member, index) => ({ member, priority: index + 1 }))
        .filter(({ member }) => typeof member.item_id !== 'number')
        .map(({ member, priority }) => ({
            channel_id: member.channel_id,
            model_name: member.name,
            priority,
            weight: member.weight ?? 1,
            ...(persistManualOrder ? { manual_rank: priority } : {}),
        }));
    const itemsToUpdate = values.members
        .map((member, index) => ({ member, priority: index + 1 }))
        .filter(({ member }) => typeof member.item_id === 'number')
        .map(({ member, priority }) => {
            const id = member.item_id!;
            const original = originalByID.get(id);
            const weight = member.weight ?? 1;
            if (!persistManualOrder && !forcePriorityOrder && (!original || (original.priority === priority && original.weight === weight))) return null;
            return { id, priority, weight, ...(persistManualOrder ? { manual_rank: priority } : {}) };
        })
        .filter((item): item is NonNullable<typeof item> => item !== null);

    const payload: GroupUpdateRequest = { id: group.id! };
    const nextName = values.name.trim();
    const nextRegex = values.match_regex.trim();

    if (nextName && nextName !== group.name) payload.name = nextName;
    if (values.mode !== group.mode) payload.mode = values.mode;
    if (nextRegex !== (group.match_regex ?? '')) payload.match_regex = nextRegex;
    if (values.first_token_time_out !== (group.first_token_time_out ?? 0)) payload.first_token_time_out = values.first_token_time_out;
    if (values.session_keep_time !== (group.session_keep_time ?? 0)) payload.session_keep_time = values.session_keep_time;
    if (values.retry_enabled !== (group.retry_enabled ?? false)) payload.retry_enabled = values.retry_enabled;
    if (values.max_retries !== (group.max_retries ?? 3)) payload.max_retries = values.max_retries;
    if (values.price_weight !== (group.price_weight ?? 70)) payload.price_weight = values.price_weight;
    if (values.speed_weight !== (group.speed_weight ?? 30)) payload.speed_weight = values.speed_weight;
    if (values.smart_reorder_interval !== (group.smart_reorder_interval ?? 0)) payload.smart_reorder_interval = values.smart_reorder_interval;
    if (values.smart_reorder_clear_manual !== (group.smart_reorder_clear_manual ?? false)) payload.smart_reorder_clear_manual = values.smart_reorder_clear_manual;
    if (values.zero_cost_takeover_enabled !== (group.zero_cost_takeover_enabled ?? false)) payload.zero_cost_takeover_enabled = values.zero_cost_takeover_enabled;
    if (values.zero_cost_takeover_interval !== (group.zero_cost_takeover_interval ?? 5)) payload.zero_cost_takeover_interval = values.zero_cost_takeover_interval;
    if (itemsToAdd.length) payload.items_to_add = itemsToAdd;
    if (itemsToUpdate.length) payload.items_to_update = itemsToUpdate;
    if (itemsToDelete.length) payload.items_to_delete = itemsToDelete;

    return payload;
}

function useGroupMembers(group: Group) {
    const { data: modelChannels = [] } = useModelChannelList();

    return useMemo<SelectedMember[]>(() => {
        const modelChannelByKey = new Map(modelChannels.map((channel) => [modelChannelKey(channel.channel_id, channel.name), channel]));
        return orderedGroupItems(group)
            .map((item) => {
                const key = modelChannelKey(item.channel_id, item.model_name);
                const modelChannel = modelChannelByKey.get(key);
                return {
                    ...(modelChannel ?? {}),
                    id: key,
                    name: item.model_name,
                    enabled: modelChannel?.enabled ?? true,
                    channel_id: item.channel_id,
                    channel_name: modelChannel?.channel_name ?? `Channel ${item.channel_id}`,
                    item_id: item.id,
                    weight: item.weight,
                    manual_rank: item.manual_rank,
                    price_multiplier: item.price_multiplier,
                    speed_ms: item.speed_ms,
                    health_state: item.health_state,
                };
            });
    }, [group, modelChannels]);
}

export function GroupCard({ group, initialOverride, onDone, sidePanel, routingItems }: GroupCardProps) {
    const t = useTranslations('group');
    const updateGroup = useUpdateGroup();
    const deleteGroup = useDeleteGroup();
    const togglePin = useToggleGroupPin();
    const refreshRouting = useRefreshGroupRouting();
    const [confirmDelete, setConfirmDelete] = useState(false);
    const displayMembers = useGroupMembers(group);

    const initial = useMemo<Partial<GroupEditorValues>>(() => ({
        name: group.name,
        match_regex: group.match_regex ?? '',
        mode: group.mode,
        first_token_time_out: group.first_token_time_out ?? 0,
        session_keep_time: group.session_keep_time ?? 0,
        retry_enabled: group.retry_enabled ?? false,
        max_retries: group.max_retries ?? 3,
        price_weight: group.price_weight ?? 70,
        speed_weight: group.speed_weight ?? 30,
        smart_reorder_interval: group.smart_reorder_interval ?? 0,
        smart_reorder_clear_manual: group.smart_reorder_clear_manual ?? false,
        zero_cost_takeover_enabled: group.zero_cost_takeover_enabled ?? false,
        zero_cost_takeover_interval: group.zero_cost_takeover_interval ?? 5,
        ...initialOverride,
        members: displayMembers,
    }), [displayMembers, group, initialOverride]);

    const handleSubmit = useCallback((values: GroupEditorValues) => {
        if (!group.id) return;
        const payload = buildGroupUpdateRequest(group, values);
        if (Object.keys(payload).length === 1) {
            onDone?.();
            return;
        }
        updateGroup.mutate(payload, {
            onSuccess: () => {
                toast.success(t('toast.updated'));
                onDone?.();
            },
            onError: (error) => toast.error(t('toast.updateFailed'), { description: error.message }),
        });
    }, [group, onDone, t, updateGroup]);

    const handleImmediateSmartRefresh = useCallback(async (values: GroupEditorValues, clearManualRank: boolean) => {
        if (!group.id) return;
        const payload = buildGroupUpdateRequest(group, values);
        if (Object.keys(payload).length > 1) {
            await updateGroup.mutateAsync(payload);
        }
        await refreshRouting.mutateAsync({ groupId: group.id, clearManualRank });
    }, [group, refreshRouting, updateGroup]);

    return (
        <GroupEditor
            initial={initial}
            submitText={t('detail.actions.save')}
            submittingText={t('create.submitting')}
            isSubmitting={updateGroup.isPending}
            onCancel={onDone}
            onSubmit={handleSubmit}
            sidePanel={sidePanel}
            routingGroupId={group.id}
            routingItems={routingItems}
            smartReorderGroup={group}
            onImmediateSmartRefresh={handleImmediateSmartRefresh}
            enableSmartScheduling
            operations={group.id ? (
                <div className="flex flex-col gap-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                        <CopyIconButton text={group.name} className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground" />
                        <PresetPopover group={group} />
                        <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            className="h-8 rounded-lg px-2 text-xs"
                            disabled={togglePin.isPending}
                            onClick={() => togglePin.mutate(
                                { groupID: group.id!, pinned: !group.pinned },
                                {
                                    onSuccess: () => toast.success(group.pinned ? t('toast.unpinned') : t('toast.pinned')),
                                    onError: (error) => toast.error(t('toast.pinFailed'), { description: error.message }),
                                },
                            )}
                        >
                            {group.pinned ? <Pin className="size-3.5 fill-current" /> : <PinOff className="size-3.5" />}
                            {group.pinned ? t('pin.unpin') : t('pin.pin')}
                        </Button>
                        <Button type="button" size="sm" variant="destructive" className="h-8 rounded-lg px-2 text-xs" onClick={() => setConfirmDelete(true)}>
                            <Trash2 className="size-3.5" />
                            {t('detail.actions.delete')}
                        </Button>
                    </div>
                    <GroupHealthBadge groupId={group.id} />
                    <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
                        <AlertDialogContent>
                            <AlertDialogHeader>
                                <AlertDialogTitle>{t('detail.actions.confirmDelete')}</AlertDialogTitle>
                                <AlertDialogDescription>{t('detail.actions.deleteDescription', { name: group.name })}</AlertDialogDescription>
                            </AlertDialogHeader>
                            <AlertDialogFooter>
                                <AlertDialogCancel>{t('detail.actions.cancel')}</AlertDialogCancel>
                                <AlertDialogAction
                                    className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                                    disabled={deleteGroup.isPending}
                                    onClick={() => deleteGroup.mutate(group.id!, {
                                        onSuccess: () => {
                                            toast.success(t('toast.deleted'));
                                            onDone?.();
                                        },
                                        onError: (error) => toast.error(t('toast.deleteFailed'), { description: error.message }),
                                    })}
                                >
                                    {t('detail.actions.confirmDelete')}
                                </AlertDialogAction>
                            </AlertDialogFooter>
                        </AlertDialogContent>
                    </AlertDialog>
                </div>
            ) : undefined}
        />
    );
}

export function GroupOverviewCard({
    group,
    onGroupDragStart,
}: {
    group: Group;
    onGroupDragStart?: () => void;
}) {
    const t = useTranslations('group');
    const updateGroup = useUpdateGroup();
    const deleteGroup = useDeleteGroup();
    const togglePin = useToggleGroupPin();
    const routing = useGroupRouting(group.id, true);
    const displayMembers = useGroupMembers(group);
    const [editorOpen, setEditorOpen] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState(false);
    const [isDragging, setIsDragging] = useState(false);
    const [members, setMembers] = useState<SelectedMember[]>([]);
    const [weightOverrides, setWeightOverrides] = useState<Record<string, number>>({});
    const membersRef = useRef<SelectedMember[]>([]);
    const weightTimerRef = useRef<NodeJS.Timeout | null>(null);

    const effectiveDisplayMembers = useMemo(
        () => displayMembers.map((member) => {
            const weight = weightOverrides[member.id];
            return weight === undefined ? member : { ...member, weight };
        }),
        [displayMembers, weightOverrides],
    );
    const renderedMembers = isDragging ? members : effectiveDisplayMembers;

    useEffect(() => {
        membersRef.current = renderedMembers;
    }, [renderedMembers]);

    useEffect(() => () => {
        if (weightTimerRef.current) clearTimeout(weightTimerRef.current);
    }, []);

    const priorityByItemID = useMemo(() => {
        const priorities = new Map<number, number>();
        for (const item of group.items ?? []) {
            if (typeof item.id === 'number') priorities.set(item.id, item.priority);
        }
        return priorities;
    }, [group.items]);

    const onUpdateSuccess = useCallback(() => toast.success(t('toast.updated')), [t]);
    const onUpdateError = useCallback(
        (error: Error) => toast.error(t('toast.updateFailed'), { description: error.message }),
        [t],
    );

    const clearWeightOverride = useCallback((id: string) => {
        setWeightOverrides((previous) => {
            if (!(id in previous)) return previous;
            const next = { ...previous };
            delete next[id];
            return next;
        });
    }, []);

    const handleMemberDragStart = useCallback(() => {
        setMembers([...effectiveDisplayMembers]);
        setIsDragging(true);
    }, [effectiveDisplayMembers]);

    const handleMemberDrop = useCallback((nextMembers: SelectedMember[]) => {
        if (!group.id) return;
        const itemsToUpdate = group.mode === GroupMode.SmartFailover
            ? nextMembers
                .map((member, index) => ({ member, priority: index + 1 }))
                .filter(({ member }) => typeof member.item_id === 'number')
                .map(({ member, priority }) => ({
                    id: member.item_id!,
                    priority,
                    weight: member.weight ?? 1,
                    manual_rank: priority,
                }))
            : nextMembers
                .map((member, index) => ({ member, priority: index + 1 }))
                .filter(({ member, priority }) => (
                    typeof member.item_id === 'number'
                    && priorityByItemID.get(member.item_id) !== priority
                ))
                .map(({ member, priority }) => ({
                    id: member.item_id!,
                    priority,
                    weight: member.weight ?? 1,
                }));
        if (!itemsToUpdate.length) return;
        updateGroup.mutate(
            { id: group.id, items_to_update: itemsToUpdate },
            { onSuccess: onUpdateSuccess, onError: onUpdateError },
        );
    }, [group.id, group.mode, onUpdateError, onUpdateSuccess, priorityByItemID, updateGroup]);

    const handleRemoveMember = useCallback((id: string) => {
        if (!group.id) return;
        const member = membersRef.current.find((item) => item.id === id);
        clearWeightOverride(id);
        if (typeof member?.item_id !== 'number') return;
        updateGroup.mutate(
            { id: group.id, items_to_delete: [member.item_id] },
            { onSuccess: onUpdateSuccess, onError: onUpdateError },
        );
    }, [clearWeightOverride, group.id, onUpdateError, onUpdateSuccess, updateGroup]);

    const handleWeightChange = useCallback((id: string, weight: number) => {
        setWeightOverrides((previous) => ({ ...previous, [id]: weight }));
        if (isDragging) {
            setMembers((previous) => previous.map((member) => (
                member.id === id ? { ...member, weight } : member
            )));
        }
        if (weightTimerRef.current) clearTimeout(weightTimerRef.current);
        weightTimerRef.current = setTimeout(() => {
            if (!group.id) return;
            const member = membersRef.current.find((item) => item.id === id);
            if (typeof member?.item_id !== 'number') return;
            const priority = priorityByItemID.get(member.item_id);
            if (priority === undefined) return;
            updateGroup.mutate(
                { id: group.id, items_to_update: [{ id: member.item_id, priority, weight }] },
                {
                    onSuccess: () => {
                        clearWeightOverride(id);
                        onUpdateSuccess();
                    },
                    onError: onUpdateError,
                },
            );
        }, 500);
    }, [clearWeightOverride, group.id, isDragging, onUpdateError, onUpdateSuccess, priorityByItemID, updateGroup]);

    const routingItems = routing.data?.items ?? [];
    const modes = [
        GroupMode.RoundRobin,
        GroupMode.Random,
        GroupMode.Failover,
        GroupMode.Weighted,
        GroupMode.SmartFailover,
    ] as const;

    return (
        <article className="relative flex flex-col rounded-xl border border-border bg-card p-3 text-card-foreground shadow-sm">
            <header className="mb-2 flex min-w-0 items-center gap-1">
                <button
                    type="button"
                    draggable
                    className="cursor-grab rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted active:cursor-grabbing"
                    aria-label={t('routing.reorder')}
                    title={t('routing.reorder')}
                    onDragStart={(event) => {
                        event.dataTransfer.effectAllowed = 'move';
                        onGroupDragStart?.();
                    }}
                >
                    <GripVertical className="size-4" />
                </button>
                <Tooltip side="top" sideOffset={8} align="start">
                    <TooltipTrigger className="min-w-0 flex-1 truncate text-left text-base font-semibold">
                        {group.name}
                    </TooltipTrigger>
                    <TooltipContent>{group.name}</TooltipContent>
                </Tooltip>
                <Tooltip side="top" sideOffset={8} align="center">
                    <TooltipTrigger asChild>
                        <CopyIconButton
                            text={group.name}
                            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                            copyIconClassName="size-4"
                            checkIconClassName="size-4 text-primary"
                        />
                    </TooltipTrigger>
                    <TooltipContent>{t('detail.actions.copyName')}</TooltipContent>
                </Tooltip>
                <PresetPopover group={group} />
                <Tooltip side="top" sideOffset={8} align="center">
                    <TooltipTrigger asChild>
                        <button
                            type="button"
                            className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                            aria-label={t('detail.actions.edit')}
                            onClick={() => setEditorOpen(true)}
                        >
                            <Pencil className="size-4" />
                        </button>
                    </TooltipTrigger>
                    <TooltipContent>{t('detail.actions.edit')}</TooltipContent>
                </Tooltip>
                <Tooltip side="top" sideOffset={8} align="center">
                    <TooltipTrigger asChild>
                        <button
                            type="button"
                            className={cn(
                                'rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50',
                                group.pinned && 'text-primary',
                            )}
                            disabled={!group.id || togglePin.isPending}
                            aria-label={group.pinned ? t('pin.unpin') : t('pin.pin')}
                            onClick={() => group.id && togglePin.mutate(
                                { groupID: group.id, pinned: !group.pinned },
                                {
                                    onSuccess: () => toast.success(group.pinned ? t('toast.unpinned') : t('toast.pinned')),
                                    onError: (error) => toast.error(t('toast.pinFailed'), { description: error.message }),
                                },
                            )}
                        >
                            {group.pinned ? <Pin className="size-4 fill-current" /> : <PinOff className="size-4" />}
                        </button>
                    </TooltipTrigger>
                    <TooltipContent>{group.pinned ? t('pin.unpin') : t('pin.pin')}</TooltipContent>
                </Tooltip>
                <Tooltip side="top" sideOffset={8} align="center">
                    <TooltipTrigger asChild>
                        <button
                            type="button"
                            className="inline-flex items-center gap-1 rounded-md px-1.5 py-1.5 text-destructive transition-colors hover:bg-destructive/10"
                            aria-label={t('detail.actions.delete')}
                            onClick={() => setConfirmDelete(true)}
                        >
                            <Trash2 className="size-4" />
                            <span className="hidden text-xs sm:inline">{t('detail.actions.delete')}</span>
                        </button>
                    </TooltipTrigger>
                    <TooltipContent>{t('detail.actions.delete')}</TooltipContent>
                </Tooltip>
            </header>

            <AnimatePresence initial={false}>
                {confirmDelete ? (
                    <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        className="mb-2 overflow-hidden rounded-lg bg-destructive p-2 text-destructive-foreground"
                    >
                        <div className="flex items-center gap-2">
                            <button
                                type="button"
                                className="flex size-7 items-center justify-center rounded-md bg-destructive-foreground/20 hover:bg-destructive-foreground/30"
                                aria-label={t('detail.actions.cancel')}
                                onClick={() => setConfirmDelete(false)}
                            >
                                <X className="size-4" />
                            </button>
                            <button
                                type="button"
                                className="flex h-7 flex-1 items-center justify-center gap-1.5 rounded-md bg-destructive-foreground px-2 text-xs font-semibold text-destructive disabled:opacity-50"
                                disabled={!group.id || deleteGroup.isPending}
                                onClick={() => group.id && deleteGroup.mutate(group.id, {
                                    onSuccess: () => toast.success(t('toast.deleted')),
                                    onError: (error) => toast.error(t('toast.deleteFailed'), { description: error.message }),
                                })}
                            >
                                <Trash2 className="size-3.5" />
                                {t('detail.actions.confirmDelete')}
                            </button>
                        </div>
                    </motion.div>
                ) : null}
            </AnimatePresence>

            <div className="mb-2 grid grid-cols-2 gap-1 sm:grid-cols-3">
                {modes.map((mode) => (
                    <button
                        key={mode}
                        type="button"
                        disabled={!group.id || updateGroup.isPending}
                        onClick={() => {
                            if (!group.id || group.mode === mode) return;
                            updateGroup.mutate(
                                { id: group.id, mode },
                                { onSuccess: onUpdateSuccess, onError: onUpdateError },
                            );
                        }}
                        className={cn(
                            'min-h-7 rounded-md px-1 py-1 text-[11px] leading-tight transition-colors disabled:cursor-not-allowed disabled:opacity-50',
                            group.mode === mode
                                ? 'bg-primary text-primary-foreground'
                                : 'bg-muted text-muted-foreground hover:bg-muted/80 hover:text-foreground',
                        )}
                    >
                        {t(`mode.${MODE_LABELS[mode]}`)}
                    </button>
                ))}
            </div>

            <SmartReorderControls group={group} className="mb-2" disabled={updateGroup.isPending} />

            <GroupHealthBadge groupId={group.id} />

            <section className="relative min-h-24 rounded-lg border border-border/50 bg-muted/30">
                <MemberList
                    members={renderedMembers}
                    onReorder={setMembers}
                    onRemove={handleRemoveMember}
                    onWeightChange={handleWeightChange}
                    onDragStart={handleMemberDragStart}
                    onDrop={handleMemberDrop}
                    onDragFinish={() => setIsDragging(false)}
                    autoScrollOnAdd={false}
                    showWeight={group.mode === GroupMode.Weighted}
                    layoutScope={`card-${group.id ?? 'unknown'}`}
                    routingGroupId={group.id}
                    routingItems={routingItems}
                    expandToFit
                />
            </section>

            <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
                <DialogContent
                    aria-describedby={undefined}
                    className="flex h-[calc(100vh-0.5rem)] max-h-[calc(100vh-0.5rem)] max-w-[calc(100vw-0.5rem)] flex-col gap-2 overflow-hidden rounded-lg p-3 sm:h-[calc(100vh-1rem)] sm:max-h-[calc(100vh-1rem)] sm:max-w-6xl"
                >
                    <DialogHeader className="shrink-0 gap-0">
                        <DialogTitle className="text-base">{t('detail.actions.edit')} · {group.name}</DialogTitle>
                    </DialogHeader>
                    <div className="min-h-0 flex-1">
                        <GroupCard
                            group={group}
                            onDone={() => setEditorOpen(false)}
                            routingItems={routingItems}
                        />
                    </div>
                </DialogContent>
            </Dialog>
        </article>
    );
}
