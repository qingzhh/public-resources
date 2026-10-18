'use client';

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Activity, CheckCircle2, CircleX, GripVertical, Layers, LoaderCircle, Trash2, X } from 'lucide-react';
import {
    DragDropContext,
    Draggable,
    Droppable,
    type DraggableProvided,
    type DropResult,
} from '@hello-pangea/dnd';
import { motion, AnimatePresence } from 'motion/react';
import { cn } from '@/lib/utils';
import { getModelIcon } from '@/lib/model-icons';
import type { LLMChannel } from '@/api/endpoints/model';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/animate-ui/components/animate/tooltip';
import { useTranslations } from 'next-intl';
import { type GroupRoutingItem, useProbeGroupRoutingItem } from '@/api/endpoints/group';
import { toast } from '@/components/common/Toast';
import { getGroupRoutingState, memberKey, modelChannelKey } from './utils';

export interface SelectedMember extends LLMChannel {
    id: string;
    item_id?: number;
    weight?: number;
    manual_rank?: number | null;
    price_multiplier?: number;
    speed_ms?: number;
    health_state?: 'unknown' | 'healthy' | 'degraded';
}

function reorderList<T>(list: T[], startIndex: number, endIndex: number): T[] {
    const result = [...list];
    const [removed] = result.splice(startIndex, 1);
    result.splice(endIndex, 0, removed);
    return result;
}

type MemberItemDnd = {
    innerRef: DraggableProvided['innerRef'];
    draggableProps: DraggableProvided['draggableProps'];
    dragHandleProps: DraggableProvided['dragHandleProps'];
    isDragging: boolean;
};

type ProbeOutcome = {
    status: 'success' | 'failed';
    recovered?: number;
    attempted?: number;
};

function MemberItem({
    member,
    onRemove,
    onWeightChange,
    isRemoving,
    index,
    showWeight = false,
    showConfirmDelete = true,
    layoutScope,
    routingItem,
    probeItemId,
    onProbe,
    isProbing,
    probeDisabled,
    probeOutcome,
    compact = false,
    dnd,
}: {
    member: SelectedMember;
    onRemove: (id: string) => void;
    onWeightChange?: (id: string, weight: number) => void;
    isRemoving?: boolean;
    index: number;
    showWeight?: boolean;
    showConfirmDelete?: boolean;
    layoutScope?: string;
    routingItem?: GroupRoutingItem;
    probeItemId?: number;
    onProbe?: (itemId: number) => void;
    isProbing?: boolean;
    probeDisabled?: boolean;
    probeOutcome?: ProbeOutcome;
    compact?: boolean;
    dnd: MemberItemDnd;
}) {
    const t = useTranslations('group');
    const { Avatar: ModelAvatar } = getModelIcon(member.name);
    const [confirmDelete, setConfirmDelete] = useState(false);
    const isDisabled = member.enabled === false;
    const routingState = routingItem
        ? getGroupRoutingState(routingItem)
        : member.health_state === 'degraded' ? 'degraded' : member.health_state === 'healthy' ? 'healthy' : 'unknown';
    const needsProbe = routingState === 'degraded' || routingState === 'tripped';
    const persistedProbeStatus = routingItem?.last_probe_at
        ? routingItem.last_probe_success ? 'success' : 'failed'
        : undefined;
    const probeStatus = isProbing ? 'running' : (probeOutcome?.status ?? persistedProbeStatus);
    const priceMultiplier = routingItem?.price_multiplier ?? member.price_multiplier ?? 1;
    const priceMultiplierLabel = Number(priceMultiplier.toFixed(4)).toString();
    const speedMS = routingItem?.speed_ms ?? member.speed_ms;
    const isSiteChannel = member.site_id != null;
    const sourceLabel = [member.channel_name, isSiteChannel ? null : member.endpoint_type?.trim()]
        .filter(Boolean)
        .join(' · ');

    return (
        <div
            // DnD libraries provide imperative refs/props; the hook lint rule (`react-hooks/refs`)
            // flags this pattern, but it's safe and required for correct drag behavior.
            // eslint-disable-next-line react-hooks/refs
            ref={dnd.innerRef}
            // eslint-disable-next-line react-hooks/refs
            {...dnd.draggableProps}
            className={cn('rounded-lg grid transition-[grid-template-rows] duration-200', isRemoving ? 'grid-rows-[0fr]' : 'grid-rows-[1fr]')}
            // eslint-disable-next-line react-hooks/refs
            style={{
                /* eslint-disable-next-line react-hooks/refs */
                ...(dnd.draggableProps?.style ?? {}),
                /* eslint-disable-next-line react-hooks/refs */
                ...(dnd.isDragging ? { zIndex: 50, boxShadow: '0 8px 32px rgba(0,0,0,0.15)' } : null),
            }}
        >
            <div className={cn(
                'flex items-center gap-2 rounded-lg bg-background border border-border/50 select-none transition-opacity duration-200 relative overflow-hidden',
                compact ? 'px-2 py-1.5' : 'px-2.5 py-2',
                isRemoving && 'opacity-0',
                isDisabled && 'opacity-60',
                routingState === 'tripped' && 'opacity-60 grayscale'
            )}>
                <span className={cn(
                    'size-5 rounded-md text-xs font-bold grid place-items-center shrink-0',
                    isDisabled ? 'bg-muted text-muted-foreground' : 'bg-primary/10 text-primary'
                )}>
                    {index + 1}
                </span>

                <div
                    className={cn(
                        'p-0.5 rounded touch-none transition-colors',
                        isDisabled
                            ? 'cursor-grab active:cursor-grabbing hover:bg-muted/60'
                            : 'cursor-grab active:cursor-grabbing hover:bg-muted'
                    )}
                    // eslint-disable-next-line react-hooks/refs
                    {...dnd.dragHandleProps}
                >
                    <GripVertical className="size-3.5 text-muted-foreground" />
                </div>

                <span className={cn(isDisabled && 'opacity-70')}>
                    <ModelAvatar size={18} />
                </span>

                <div className="flex min-w-0 flex-1 flex-col">
                    <Tooltip side="top" sideOffset={10} align="start">
                        <TooltipTrigger className={cn(
                            'min-w-0 truncate text-left text-sm font-medium leading-tight',
                            isDisabled && 'text-muted-foreground'
                        )}>
                            {member.name}
                        </TooltipTrigger>
                        <TooltipContent key={member.name}>{member.name}</TooltipContent>
                    </Tooltip>
                    <span className="truncate text-[10px] leading-tight text-muted-foreground">{sourceLabel}</span>
                    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] leading-tight text-muted-foreground">
                        <span className="shrink-0 whitespace-nowrap text-[10px] text-muted-foreground">
                            {t('routing.selectedPriceMultiplier', { value: priceMultiplierLabel })}
                        </span>
                        <span className="shrink-0 whitespace-nowrap text-[10px] text-muted-foreground">
                            {t('routing.selectedSpeed', { value: speedMS ? `${Math.round(speedMS)}ms` : '-' })}
                        </span>
                        {needsProbe && (
                            <span className={cn(
                                'shrink-0 whitespace-nowrap rounded border px-1 py-0.5 text-[9px]',
                                routingState === 'tripped'
                                    ? 'border-destructive/30 text-destructive'
                                    : 'border-amber-500/30 text-amber-700 dark:text-amber-300'
                            )}>
                                {t(`routing.state.${routingState}`)}
                            </span>
                        )}
                        {isDisabled && !needsProbe && (
                            <span className="shrink-0 whitespace-nowrap text-[9px] text-muted-foreground">
                                {t('routing.channelDisabled')}
                            </span>
                        )}
                        {probeStatus === 'running' && (
                            <span className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded border border-sky-500/30 px-1 py-0.5 text-[9px] text-sky-700 dark:text-sky-300">
                                <LoaderCircle className="size-2.5 animate-spin" />
                                {t('routing.probeRunning')}
                            </span>
                        )}
                        {probeStatus === 'success' && (
                            <span className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded border border-emerald-500/30 px-1 py-0.5 text-[9px] text-emerald-700 dark:text-emerald-300">
                                <CheckCircle2 className="size-2.5" />
                                {probeOutcome?.attempted != null
                                    ? t('routing.probeSuccessCount', { recovered: probeOutcome.recovered ?? 0, attempted: probeOutcome.attempted })
                                    : t('routing.probeSuccess')}
                            </span>
                        )}
                        {probeStatus === 'failed' && (
                            <span className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded border border-destructive/30 px-1 py-0.5 text-[9px] text-destructive">
                                <CircleX className="size-2.5" />
                                {probeOutcome?.attempted != null
                                    ? t('routing.probeFailureCount', { attempted: probeOutcome.attempted })
                                    : t('routing.probeFailure')}
                            </span>
                        )}
                    </div>
                </div>

                {probeItemId && onProbe && (
                    <button
                        type="button"
                        className="flex h-6 shrink-0 items-center gap-1 rounded-md border border-border px-1.5 text-[10px] text-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
                        disabled={probeDisabled}
                        aria-label={`${t('routing.probe')} ${member.name}`}
                        title={t('routing.probe')}
                        onClick={() => onProbe(probeItemId)}
                    >
                        {isProbing ? <LoaderCircle className="size-3 animate-spin" /> : <Activity className="size-3" />}
                        {t('routing.probe')}
                    </button>
                )}

                {showWeight && (
                    <input
                        type="number"
                        min={1}
                        value={member.weight ?? 1}
                        onChange={(e) => onWeightChange?.(member.id, Math.max(1, parseInt(e.target.value) || 1))}
                        className={cn(
                            'w-12 h-6 text-xs text-center rounded border border-border bg-muted/50 focus:outline-none focus:ring-1 focus:ring-primary',
                            isDisabled && 'text-muted-foreground'
                        )}
                    />
                )}

                {(!showConfirmDelete || !confirmDelete) && (
                    <motion.button
                        layoutId={`delete-btn-member-${layoutScope ?? 'default'}-${member.id}`}
                        type="button"
                        onClick={() => showConfirmDelete ? setConfirmDelete(true) : onRemove(member.id)}
                        className="p-1 rounded hover:bg-destructive/10 hover:text-destructive transition-colors"
                        initial={false}
                        animate={{ opacity: 1, x: 0 }}
                        transition={{ duration: 0.15 }}
                        style={{ pointerEvents: 'auto' }}
                    >
                        <X className="size-3" />
                    </motion.button>
                )}

                <AnimatePresence>
                    {showConfirmDelete && confirmDelete && (
                        <motion.div
                            layoutId={`delete-btn-member-${layoutScope ?? 'default'}-${member.id}`}
                            className="absolute inset-0 flex items-center justify-center gap-2 bg-destructive p-1.5 rounded-lg"
                            transition={{ type: 'spring', stiffness: 400, damping: 30 }}
                        >
                            <button
                                type="button"
                                onClick={() => setConfirmDelete(false)}
                                className="flex h-6 w-6 items-center justify-center rounded-md bg-destructive-foreground/20 text-destructive-foreground transition-all hover:bg-destructive-foreground/30 active:scale-95"
                            >
                                <X className="h-3 w-3" />
                            </button>
                            <button
                                type="button"
                                onClick={() => onRemove(member.id)}
                                className="flex-1 h-6 flex items-center justify-center gap-1.5 rounded-md bg-destructive-foreground text-destructive text-xs font-semibold transition-all hover:bg-destructive-foreground/90 active:scale-[0.98]"
                            >
                                <Trash2 className="h-3 w-3" />
                            </button>
                        </motion.div>
                    )}
                </AnimatePresence>
            </div>
        </div>
    );
}

export interface MemberListProps {
    members: SelectedMember[];
    onReorder: (members: SelectedMember[]) => void;
    onRemove: (id: string) => void;
    onWeightChange?: (id: string, weight: number) => void;
    /**
     * When true, auto-scroll the list to bottom when a *new visible* member appears
     * (i.e. a new member id is added). Useful in "editor" flows. Defaults to true.
     */
    autoScrollOnAdd?: boolean;
    onDragStart?: () => void;
    /**
     * Called only when a drop results in a different order (i.e. commit reorder).
     * Useful for persisting the new order.
     */
    onDrop?: (members: SelectedMember[]) => void;
    /**
     * Called whenever a drag ends (including cancel / same-index drop).
     * Useful for lifecycle cleanup (e.g. clearing "isDragging" flags).
     */
    onDragFinish?: () => void;
    removingIds?: Set<string>;
    showWeight?: boolean;
    /**
     * When true, show a confirmation overlay before removing an item.
     * When false, clicking the delete button removes the item immediately.
     * Defaults to true.
     */
    showConfirmDelete?: boolean;
    layoutScope?: string;
    routingGroupId?: number;
    routingItems?: GroupRoutingItem[];
    expandToFit?: boolean;
    compact?: boolean;
}

export function MemberList({
    members,
    onReorder,
    onRemove,
    onWeightChange,
    autoScrollOnAdd = true,
    onDragStart,
    onDrop,
    onDragFinish,
    removingIds = new Set(),
    showWeight = false,
    showConfirmDelete = true,
    layoutScope: externalLayoutScope,
    routingGroupId,
    routingItems = [],
    expandToFit = false,
    compact = false,
}: MemberListProps) {
    const internalLayoutScope = useId();
    const layoutScope = externalLayoutScope ?? internalLayoutScope;

    const scrollContainerRef = useRef<HTMLDivElement | null>(null);
    const prevMemberCountRef = useRef<number>(0);
    const hasMountedRef = useRef(false);

    const visibleCount = members.filter((m) => !removingIds.has(m.id)).length;
    const isEmpty = visibleCount === 0;
    const t = useTranslations('group');
    const probe = useProbeGroupRoutingItem();
    const [probeOutcomes, setProbeOutcomes] = useState<Record<number, ProbeOutcome>>({});
    const routingByMember = useMemo(
        () => new Map(routingItems.map((item) => [modelChannelKey(item.channel_id, item.model_name), item])),
        [routingItems],
    );

    const handleProbe = (itemId: number) => {
        if (!routingGroupId || !itemId) return;
        setProbeOutcomes((previous) => {
            const next = { ...previous };
            delete next[itemId];
            return next;
        });
        probe.mutate({ groupId: routingGroupId, itemId }, {
            onSuccess: (result) => {
                const status = result.recovered > 0 ? 'success' : 'failed';
                setProbeOutcomes((previous) => ({
                    ...previous,
                    [itemId]: { status, recovered: result.recovered, attempted: result.attempted },
                }));
                if (status === 'success') {
                    toast.success(t('routing.probeResult', { recovered: result.recovered, attempted: result.attempted }));
                } else {
                    toast.error(t('routing.probeFailed'));
                }
            },
            onError: (error) => {
                setProbeOutcomes((previous) => ({ ...previous, [itemId]: { status: 'failed' } }));
                toast.error(t('routing.probeFailed'), { description: error.message });
            },
        });
    };

    useEffect(() => {
        // Skip the initial mount so we don't auto-scroll on first render / initial data load.
        if (!hasMountedRef.current) {
            hasMountedRef.current = true;
            prevMemberCountRef.current = members.length;
            return;
        }

        if (!autoScrollOnAdd) {
            prevMemberCountRef.current = members.length;
            return;
        }

        const hasNewMember = members.length > prevMemberCountRef.current;

        // Auto-scroll only when member count increases (i.e. added; not reorder / not "unhide").
        if (hasNewMember) {
            // Wait a tick for DOM/placeholder/layout to settle.
            requestAnimationFrame(() => {
                const el = scrollContainerRef.current;
                if (!el) return;
                el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
            });
        }

        prevMemberCountRef.current = members.length;
    }, [members.length, autoScrollOnAdd]);

    const handleDragEnd = (result: DropResult) => {
        try {
            const { destination, source } = result;
            if (!destination) return;
            if (destination.index === source.index) return;

            const next = reorderList(members, source.index, destination.index);
            onReorder(next);
            onDrop?.(next);
        } finally {
            // Ensure drag lifecycle always finishes, even when drop is canceled.
            onDragFinish?.();
        }
    };

    return (
        <div className={cn('relative min-h-0', expandToFit ? 'min-h-24' : 'h-full')}>
            <div
                className={cn(
                    'absolute inset-0 flex flex-col items-center justify-center gap-2 text-muted-foreground',
                    'transition-opacity duration-200 ease-out',
                    isEmpty ? 'opacity-100' : 'opacity-0 pointer-events-none'
                )}
            >
                <Layers className="size-10 opacity-40" />
                <span className="text-sm">{t('card.empty')}</span>
            </div>

            <div
                className={cn(
                    'transition-opacity duration-200',
                    expandToFit ? 'overflow-visible' : 'h-full overflow-y-auto',
                    isEmpty ? 'opacity-0' : 'opacity-100'
                )}
                ref={scrollContainerRef}
            >
                <DragDropContext
                    onDragStart={() => onDragStart?.()}
                    onDragEnd={handleDragEnd}
                >
                    <Droppable droppableId={`members-${layoutScope}`}>
                        {(droppableProvided) => (
                            <div
                                ref={droppableProvided.innerRef}
                                {...droppableProvided.droppableProps}
                                className={cn('flex flex-col', compact ? 'space-y-1 p-1.5' : 'space-y-1.5 p-2')}
                            >
                                {members.map((member, index) => {
                                    const routingItem = routingByMember.get(memberKey(member));
                                    const probeItemId = routingItem?.id ?? member.item_id;
                                    return (
                                        <Draggable
                                            key={member.id}
                                            draggableId={member.id}
                                            index={index}
                                            isDragDisabled={removingIds.has(member.id)}
                                        >
                                            {(draggableProvided, snapshot) => (
                                                <MemberItem
                                                    member={member}
                                                    onRemove={onRemove}
                                                    onWeightChange={onWeightChange}
                                                    isRemoving={removingIds.has(member.id)}
                                                    index={index}
                                                    showWeight={showWeight}
                                                    showConfirmDelete={showConfirmDelete}
                                                    layoutScope={layoutScope}
                                                    routingItem={routingItem}
                                                    probeItemId={probeItemId}
                                                    onProbe={handleProbe}
                                                    isProbing={probe.isPending && probe.variables?.itemId === probeItemId}
                                                    probeDisabled={probe.isPending}
                                                    probeOutcome={probeItemId ? probeOutcomes[probeItemId] : undefined}
                                                    compact={compact}
                                                    dnd={{
                                                        innerRef: draggableProvided.innerRef,
                                                        draggableProps: draggableProvided.draggableProps,
                                                        dragHandleProps: draggableProvided.dragHandleProps,
                                                        isDragging: snapshot.isDragging,
                                                    }}
                                                />
                                            )}
                                        </Draggable>
                                    );
                                })}
                                {droppableProvided.placeholder}
                            </div>
                        )}
                    </Droppable>
                </DragDropContext>
            </div>
        </div>
    );
}
