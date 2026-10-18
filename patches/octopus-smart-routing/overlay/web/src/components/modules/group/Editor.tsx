'use client';

import { useCallback, useMemo, useState, type FormEvent, type ReactNode } from 'react';
import { Check, ChevronDownIcon, ListRestart, Plus, Search, Sparkles, Trash2 } from 'lucide-react';
import { useTranslations } from 'next-intl';
import * as AccordionPrimitive from '@radix-ui/react-accordion';
import { useModelChannelList, type LLMChannel } from '@/api/endpoints/model';
import { toast } from '@/components/common/Toast';
import { Button } from '@/components/ui/button';
import { Field, FieldGroup, FieldLabel } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Accordion, AccordionContent, AccordionItem } from '@/components/ui/accordion';
import { cn } from '@/lib/utils';
import { getModelIcon } from '@/lib/model-icons';
import { GroupMode, type Group, type GroupRoutingItem } from '@/api/endpoints/group';
import type { SelectedMember } from './ItemList';
import { MemberList } from './ItemList';
import { getGroupRoutingState, matchesGroupName, memberKey, modelChannelKey, normalizeKey, MODE_LABELS } from './utils';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/animate-ui/components/animate/tooltip';
import { HelpCircle } from 'lucide-react';
import { SmartReorderControls } from './smart-reorder';



export type GroupEditorValues = {
    name: string;
    match_regex: string;
    mode: GroupMode;
    first_token_time_out: number;
    session_keep_time: number;
    retry_enabled: boolean;
    max_retries: number;
    price_weight: number;
    speed_weight: number;
    smart_reorder_interval: number;
    smart_reorder_clear_manual: boolean;
    zero_cost_takeover_enabled: boolean;
    zero_cost_takeover_interval: number;
    smart_order_imported: boolean;
    members: SelectedMember[];
};

function ModelPickerSection({
    modelChannels,
    selectedMembers,
    onAdd,
    onAutoAdd,
    autoAddDisabled,
}: {
    modelChannels: LLMChannel[];
    selectedMembers: SelectedMember[];
    onAdd: (channel: LLMChannel) => void;
    onAutoAdd: () => void;
    autoAddDisabled: boolean;
}) {
    const t = useTranslations('group');
    const [searchKeyword, setSearchKeyword] = useState('');

    const selectedKeys = useMemo(() => new Set(selectedMembers.map(memberKey)), [selectedMembers]);
    const normalizedSearch = searchKeyword.trim().toLowerCase();

    const channels = useMemo(() => {
        const byId = new Map<number, { id: number; name: string; models: LLMChannel[] }>();
        modelChannels.forEach((mc) => {
            const existing = byId.get(mc.channel_id);
            if (existing) existing.models.push(mc);
            else byId.set(mc.channel_id, { id: mc.channel_id, name: mc.channel_name, models: [mc] });
        });

        return Array.from(byId.values())
            .map((c) => ({ ...c, models: [...c.models].sort((a, b) => a.name.localeCompare(b.name)) }))
            .sort((a, b) => a.id - b.id);
    }, [modelChannels]);

    const filteredChannels = useMemo(() => {
        if (!normalizedSearch) return channels;
        return channels.reduce<typeof channels>((acc, channel) => {
            if (channel.name.toLowerCase().includes(normalizedSearch)) {
                acc.push(channel);
                return acc;
            }

            const models = channel.models.filter((model) => model.name.toLowerCase().includes(normalizedSearch));
            if (models.length > 0) acc.push({ ...channel, models });
            return acc;
        }, []);
    }, [channels, normalizedSearch]);

    const modelSourceLabel = (model: LLMChannel) => [model.site_name, model.site_account_name, model.site_group_name]
        .map((value) => value?.trim())
        .filter(Boolean)
        .join(' / ');

    return (
        <div className="flex min-h-64 flex-col rounded-lg border border-border/50 bg-muted/30 md:min-h-0">
            <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-2 border-b border-border/30 bg-muted/50 px-2.5 py-1.5">
                <span className="min-w-0 justify-self-start text-sm font-medium text-foreground">
                    {t('form.addItem')}
                </span>

                <div className="relative justify-self-center w-30">
                    <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                    <Input
                        value={searchKeyword}
                        onChange={(event) => setSearchKeyword(event.target.value)}
                        className="h-6 rounded-lg border-border/60 bg-background/70 pl-7 pr-2 text-xs shadow-none focus-visible:border-border/60 focus-visible:ring-0"
                        aria-label="search"
                    />
                </div>

                <button
                    type="button"
                    onClick={onAutoAdd}
                    className={cn(
                        'justify-self-end shrink-0 flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium transition-colors',
                        autoAddDisabled
                            ? 'text-muted-foreground/50 cursor-not-allowed'
                            : 'hover:bg-muted text-muted-foreground hover:text-foreground'
                    )}
                    disabled={autoAddDisabled}
                    title={t('form.autoAdd')}
                >
                    <Sparkles className="size-3.5" />
                    <span>{t('form.autoAdd')}</span>
                </button>
            </div>

            <div className="flex-1 min-h-0 overflow-y-auto p-1.5">
                <Accordion type="multiple" className="w-full space-y-1">
                    {filteredChannels.map((channel) => {
                        const total = channel.models.length;
                        const selectedCount = channel.models.reduce(
                            (acc, m) => acc + (selectedKeys.has(memberKey(m)) ? 1 : 0),
                            0
                        );
                        const available = total - selectedCount;

                        return (
                            <AccordionItem key={channel.id} value={`channel-${channel.id}`}>
                                <AccordionPrimitive.Header className="sticky top-0 z-10 flex overflow-hidden rounded-md bg-muted px-2">
                                    <AccordionPrimitive.Trigger className="flex min-w-0 flex-1 items-center gap-2 py-2 text-left text-sm transition-all outline-none focus-visible:ring-[3px] disabled:pointer-events-none disabled:opacity-50 [&[data-state=open]>svg]:rotate-180">
                                        <span className="truncate">{channel.name}</span>
                                        <span className="text-xs text-muted-foreground shrink-0">
                                            {available}/{total}
                                        </span>
                                        <ChevronDownIcon className="text-muted-foreground pointer-events-none size-4 shrink-0 transition-transform duration-200" />
                                    </AccordionPrimitive.Trigger>
                                </AccordionPrimitive.Header>
                                <AccordionContent className="px-1.5 pt-1.5">
                                    <div className="flex flex-col gap-1">
                                        {channel.models.map((m) => {
                                            const isSelected = selectedKeys.has(memberKey(m));
                                            const { Avatar } = getModelIcon(m.name);
                                            const sourceLabel = modelSourceLabel(m);
                                            const isSiteChannel = m.site_id != null;
                                            const suffix = [sourceLabel, isSiteChannel ? null : m.endpoint_type?.trim()]
                                                .filter(Boolean)
                                                .join(' · ');
                                            return (
                                                <button
                                                    key={memberKey(m)}
                                                    type="button"
                                                    onClick={() => !isSelected && onAdd(m)}
                                                    disabled={isSelected}
                                                    className={cn(
                                                        'flex w-full items-center justify-between gap-2 rounded-md border border-border/50 bg-background px-2 py-1.5 text-left transition-colors',
                                                        isSelected ? 'opacity-60 cursor-not-allowed' : 'hover:bg-muted'
                                                    )}
                                                >
                                                    <span className="flex items-center gap-2 min-w-0">
                                                        <Avatar size={16} />
                                                        <span className="min-w-0 flex flex-col">
                                                            <span className="text-sm font-medium truncate">{m.name}</span>
                                                            {suffix && <span className="text-[10px] text-muted-foreground truncate">{suffix}</span>}
                                                        </span>
                                                    </span>

                                                    <span className="shrink-0 text-muted-foreground">
                                                        {isSelected ? (
                                                            <Check className="size-4 text-primary" />
                                                        ) : (
                                                            <Plus className="size-4" />
                                                        )}
                                                    </span>
                                                </button>
                                            );
                                        })}
                                    </div>
                                </AccordionContent>
                            </AccordionItem>
                        );
                    })}
                </Accordion>
            </div>
        </div>
    );
}

function SortSection({
    members,
    onReorder,
    onRemove,
    onWeightChange,
    removingIds,
    showWeight,
    onClear,
    routingGroupId,
    routingItems,
}: {
    members: SelectedMember[];
    onReorder: (members: SelectedMember[]) => void;
    onRemove: (id: string) => void;
    onWeightChange: (id: string, weight: number) => void;
    removingIds: Set<string>;
    showWeight: boolean;
    onClear: () => void;
    routingGroupId?: number;
    routingItems?: GroupRoutingItem[];
}) {
    const t = useTranslations('group');

    return (
        <div className="flex min-h-64 flex-col rounded-lg border border-border/50 bg-muted/30 md:min-h-0">
            <div className="flex items-center justify-between border-b border-border/30 bg-muted/50 px-2.5 py-1.5">
                <span className="text-sm font-medium text-foreground">
                    {t('form.items')}
                    {members.length > 0 && (
                        <span className="ml-1.5 text-xs text-muted-foreground font-normal">
                            ({members.length})
                        </span>
                    )}
                </span>
                <button
                    type="button"
                    onClick={onClear}
                    disabled={members.length === 0}
                    className={cn(
                        'flex items-center gap-1 px-2 py-1 rounded-lg text-xs font-medium transition-colors',
                        members.length === 0
                            ? 'text-muted-foreground/50 cursor-not-allowed'
                            : 'hover:bg-muted text-muted-foreground hover:text-foreground'
                    )}
                    title={t('form.clear')}
                >
                    <Trash2 className="size-3.5" />
                    <span>{t('form.clear')}</span>
                </button>
            </div>

            <div className="flex-1 min-h-0">
                <MemberList
                    members={members}
                    onReorder={onReorder}
                    onRemove={onRemove}
                    onWeightChange={onWeightChange}
                    removingIds={removingIds}
                    showWeight={showWeight}
                    showConfirmDelete={false}
                    routingGroupId={routingGroupId}
                    routingItems={routingItems}
                    compact
                />
            </div>
        </div>
    );
}

export function GroupEditor({
    initial,
    submitText,
    submittingText,
    isSubmitting,
    onSubmit,
    onCancel,
    nameLabel,
    sidePanel,
    routingGroupId,
    routingItems,
    smartReorderGroup,
    operations,
    onImmediateSmartRefresh,
    enableSmartScheduling = false,
}: {
    initial?: Partial<GroupEditorValues>;
    submitText: string;
    submittingText: string;
    isSubmitting: boolean;
    onSubmit: (values: GroupEditorValues) => void;
    onCancel?: () => void;
    nameLabel?: string;
    sidePanel?: ReactNode;
    routingGroupId?: number;
    routingItems?: GroupRoutingItem[];
    smartReorderGroup?: Group;
    operations?: ReactNode;
    onImmediateSmartRefresh?: (values: GroupEditorValues, clearManualRank: boolean) => Promise<void>;
    enableSmartScheduling?: boolean;
}) {
    const t = useTranslations('group');
    const { data: modelChannels = [] } = useModelChannelList();

    const [groupName, setGroupName] = useState(initial?.name ?? '');
    const [matchRegex, setMatchRegex] = useState(initial?.match_regex ?? '');
    const [mode, setMode] = useState<GroupMode>((initial?.mode ?? 1) as GroupMode);
    const [firstTokenTimeOut, setFirstTokenTimeOut] = useState<number>(initial?.first_token_time_out ?? 0);
    const [sessionKeepTime, setSessionKeepTime] = useState<number>(initial?.session_keep_time ?? 0);
    const [retryEnabled, setRetryEnabled] = useState<boolean>(initial?.retry_enabled ?? false);
    const [maxRetries, setMaxRetries] = useState<number>(initial?.max_retries ?? 3);
    const [priceWeight, setPriceWeight] = useState<number>(initial?.price_weight ?? 70);
    const [speedWeight, setSpeedWeight] = useState<number>(initial?.speed_weight ?? 30);
    const [smartReorderInterval, setSmartReorderInterval] = useState<number>(initial?.smart_reorder_interval ?? 0);
    const [smartReorderClearManual, setSmartReorderClearManual] = useState<boolean>(initial?.smart_reorder_clear_manual ?? false);
    const [zeroCostTakeoverEnabled, setZeroCostTakeoverEnabled] = useState<boolean>(initial?.zero_cost_takeover_enabled ?? false);
    const [zeroCostTakeoverInterval, setZeroCostTakeoverInterval] = useState<number>(initial?.zero_cost_takeover_interval ?? 5);
    const [smartOrderImported, setSmartOrderImported] = useState(false);
    const [selectedMembers, setSelectedMembers] = useState<SelectedMember[]>(initial?.members ?? []);
    const [removingIds, setRemovingIds] = useState<Set<string>>(new Set());

    const groupKey = normalizeKey(groupName);
    const regexKey = matchRegex.trim();

    const { matchedModelChannels, regexError } = useMemo(() => {
        const parseRegex = (input: string): RegExp => {
            const inlineMatch = input.match(/^\(\?([ism]+)\)(.+)$/);
            if (inlineMatch) {
                const flagMap: Record<string, string> = { i: 'i', s: 's', m: 'm' };
                const flags = inlineMatch[1].split('').map(f => flagMap[f] || '').join('');
                return new RegExp(inlineMatch[2], flags);
            }

            return new RegExp(input);
        };

        if (regexKey) {
            try {
                const re = parseRegex(regexKey);
                return { matchedModelChannels: modelChannels.filter((mc) => re.test(mc.name)), regexError: '' };
            } catch (e) {
                return { matchedModelChannels: [], regexError: (e as Error)?.message ?? 'Invalid regex' };
            }
        }
        if (!groupKey) return { matchedModelChannels: [], regexError: '' };
        return { matchedModelChannels: modelChannels.filter((mc) => matchesGroupName(mc.name, groupKey)), regexError: '' };
    }, [groupKey, regexKey, modelChannels]);

    const handleAddMember = useCallback((channel: LLMChannel) => {
        const key = memberKey(channel);
        setSelectedMembers((prev) => {
            if (prev.some((m) => m.id === key)) return prev;
            return [...prev, { ...channel, id: key, weight: 1 }];
        });
    }, []);

    const autoAddDisabled = useMemo(() => {
        if ((!regexKey && !groupKey) || regexError || matchedModelChannels.length === 0) return true;
        const existing = new Set(selectedMembers.map((m) => m.id));
        return matchedModelChannels.every((mc) => existing.has(memberKey(mc)));
    }, [groupKey, regexKey, regexError, matchedModelChannels, selectedMembers]);

    const handleAutoAdd = useCallback(() => {
        if (matchedModelChannels.length === 0) return;
        setSelectedMembers((prev) => {
            const existing = new Set(prev.map((m) => m.id));
            const toAdd = matchedModelChannels
                .filter((mc) => !existing.has(memberKey(mc)))
                .map((mc) => ({ ...mc, id: memberKey(mc), weight: 1 }));
            return toAdd.length ? [...prev, ...toAdd] : prev;
        });
    }, [matchedModelChannels]);

    const handleWeightChange = useCallback((id: string, weight: number) => {
        setSelectedMembers((prev) => prev.map((m) => m.id === id ? { ...m, weight } : m));
    }, []);

    const handleRemoveMember = useCallback((id: string) => {
        setRemovingIds((prev) => new Set(prev).add(id));
        setTimeout(() => {
            setSelectedMembers((prev) => prev.filter((m) => m.id !== id));
            setRemovingIds((prev) => { const n = new Set(prev); n.delete(id); return n; });
        }, 200);
    }, []);

    const handleClearMembers = useCallback(() => {
        setSelectedMembers([]);
        setRemovingIds(new Set());
    }, []);

    const smartOrderByMemberKey = useMemo(() => {
        const healthRank = (item: GroupRoutingItem) => {
            const state = getGroupRoutingState(item);
            return state === 'degraded' ? 1 : 0;
        };
        const effectiveRank = (item: GroupRoutingItem) => {
            if (typeof item.manual_rank === 'number' && item.manual_rank > 0) return item.manual_rank;
            if (typeof item.auto_rank === 'number' && item.auto_rank > 0) return item.auto_rank;
            return Number.MAX_SAFE_INTEGER;
        };
        const ranked = [...(routingItems ?? [])]
            .filter((item) => typeof item.auto_rank === 'number' && item.auto_rank > 0)
            .sort((a, b) => (
                healthRank(a) - healthRank(b)
                || effectiveRank(a) - effectiveRank(b)
                || (b.composite_score ?? 0) - (a.composite_score ?? 0)
                || a.priority - b.priority
            ));
        return new Map(ranked.map((item, index) => [modelChannelKey(item.channel_id, item.model_name), index]));
    }, [routingItems]);

    const canImportSmartOrder = selectedMembers.some((member) => smartOrderByMemberKey.has(memberKey(member)));

    const handleImportSmartOrder = useCallback(() => {
        if (!canImportSmartOrder) return;
        setSelectedMembers((previous) => previous
            .map((member, index) => ({ member, index, smartIndex: smartOrderByMemberKey.get(memberKey(member)) }))
            .sort((a, b) => {
                if (a.smartIndex === undefined && b.smartIndex === undefined) return a.index - b.index;
                if (a.smartIndex === undefined) return 1;
                if (b.smartIndex === undefined) return -1;
                return a.smartIndex - b.smartIndex;
            })
            .map(({ member }) => member));
        setSmartOrderImported(true);
        toast.success(t('toast.smartOrderImported'));
    }, [canImportSmartOrder, smartOrderByMemberKey, t]);

    const smartWeightsValid = priceWeight >= 0 && speedWeight >= 0 && priceWeight + speedWeight === 100;
    const isValid = groupKey.length > 0 && selectedMembers.length > 0 && !regexError
        && (mode !== GroupMode.SmartFailover || smartWeightsValid);

    const buildValues = useCallback((): GroupEditorValues => ({
            name: groupName,
            match_regex: regexKey,
            mode,
            first_token_time_out: firstTokenTimeOut,
            session_keep_time: sessionKeepTime,
            retry_enabled: retryEnabled,
            max_retries: maxRetries,
            price_weight: priceWeight,
            speed_weight: speedWeight,
            smart_reorder_interval: smartReorderInterval,
            smart_reorder_clear_manual: smartReorderClearManual,
            zero_cost_takeover_enabled: zeroCostTakeoverEnabled,
            zero_cost_takeover_interval: zeroCostTakeoverInterval,
            smart_order_imported: smartOrderImported,
            members: selectedMembers,
    }), [firstTokenTimeOut, groupName, maxRetries, mode, priceWeight, regexKey, retryEnabled, selectedMembers, sessionKeepTime, smartOrderImported, smartReorderClearManual, smartReorderInterval, speedWeight, zeroCostTakeoverEnabled, zeroCostTakeoverInterval]);

    const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
        event.preventDefault();
        if (!isValid) return;
        onSubmit(buildValues());
    };


    return (
        <form onSubmit={handleSubmit} className="flex h-full min-h-0 flex-col">
            <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-1 md:overflow-hidden">
                <FieldGroup className="flex min-h-full flex-col gap-2 md:h-full">
                    {smartReorderGroup ? (
                        <SmartReorderControls
                            group={smartReorderGroup}
                            disabled={isSubmitting}
                            onRefresh={onImmediateSmartRefresh ? (clearManualRank) => onImmediateSmartRefresh(buildValues(), clearManualRank) : undefined}
                        />
                    ) : null}
                    {operations}
                    <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-4">
                        <Field className="gap-1">
                            <FieldLabel htmlFor="group-name">{nameLabel ?? t('form.name')}</FieldLabel>
                            <Input
                                id="group-name"
                                value={groupName}
                                onChange={(e) => setGroupName(e.target.value)}
                                className="h-8 rounded-lg"
                            />
                        </Field>
                        <Field className="gap-1">
                            <FieldLabel htmlFor="group-match-regex">{t('form.matchRegex')}</FieldLabel>
                            <Input
                                id="group-match-regex"
                                value={matchRegex}
                                onChange={(e) => setMatchRegex(e.target.value)}
                                className="h-8 rounded-lg"
                                placeholder={t('form.matchRegexPlaceholder')}
                            />
                            {regexError && (
                                <p className="mt-1 text-xs text-destructive">
                                    {t('form.matchRegexInvalid')}: {regexError}
                                </p>
                            )}
                        </Field>

                        <Field className="gap-1">
                            <FieldLabel htmlFor="group-first-token-time-out">
                                {t('form.firstTokenTimeOut')}
                                <TooltipProvider>
                                    <Tooltip>
                                        <TooltipTrigger asChild>
                                            <HelpCircle className="size-3.5 text-muted-foreground cursor-help" />
                                        </TooltipTrigger>
                                        <TooltipContent>
                                            {t('form.firstTokenTimeOutHint')}
                                        </TooltipContent>
                                    </Tooltip>
                                </TooltipProvider>
                            </FieldLabel>
                            <Input
                                id="group-first-token-time-out"
                                type="number"
                                inputMode="numeric"
                                min={0}
                                step={1}
                                value={String(firstTokenTimeOut)}
                                onChange={(e) => {
                                    const raw = e.target.value;
                                    if (raw.trim() === '') {
                                        setFirstTokenTimeOut(0);
                                        return;
                                    }
                                    const n = Number.parseInt(raw, 10);
                                    setFirstTokenTimeOut(Number.isFinite(n) && n > 0 ? n : 0);
                                }}
                                className="h-8 rounded-lg"
                            />
                        </Field>

                        <Field className="gap-1">
                            <FieldLabel htmlFor="group-session-keep-time">
                                {t('form.sessionKeepTime')}
                                <TooltipProvider>
                                    <Tooltip>
                                        <TooltipTrigger asChild>
                                            <HelpCircle className="size-3.5 text-muted-foreground cursor-help" />
                                        </TooltipTrigger>
                                        <TooltipContent>
                                            {t('form.sessionKeepTimeHint')}
                                        </TooltipContent>
                                    </Tooltip>
                                </TooltipProvider>
                            </FieldLabel>
                            <Input
                                id="group-session-keep-time"
                                type="number"
                                inputMode="numeric"
                                min={0}
                                step={1}
                                value={String(sessionKeepTime)}
                                onChange={(e) => {
                                    const raw = e.target.value;
                                    if (raw.trim() === '') {
                                        setSessionKeepTime(0);
                                        return;
                                    }
                                    const n = Number.parseInt(raw, 10);
                                    setSessionKeepTime(Number.isFinite(n) && n > 0 ? n : 0);
                                }}
                                className="h-8 rounded-lg"
                            />
                        </Field>
                    </div>

                    {/* Mode + Retry Toggle */}
                    <div className="flex flex-col gap-1.5 lg:flex-row lg:items-center">
                        <div className="flex min-w-0 flex-1 gap-1">
                            {([GroupMode.RoundRobin, GroupMode.Random, GroupMode.Failover, GroupMode.Weighted, GroupMode.SmartFailover] as const).map((m) => (
                                <button
                                    key={m}
                                    type="button"
                                    onClick={() => {
                                        setMode(m);
                                        if (m === GroupMode.SmartFailover && sessionKeepTime <= 0) setSessionKeepTime(30 * 60);
                                    }}
                                    className={cn(
                                        'flex min-h-8 flex-1 items-center justify-center rounded-md px-1 py-1 text-xs leading-tight transition-colors',
                                        mode === m ? 'bg-primary text-primary-foreground' : 'bg-muted hover:bg-muted/80'
                                    )}
                                >
                                    {t(`mode.${MODE_LABELS[m]}`)}
                                </button>
                            ))}
                        </div>
                        <TooltipProvider>
                            <Tooltip>
                                <TooltipTrigger asChild>
                                    <label className="flex items-center gap-1.5 shrink-0 cursor-pointer">
                                        <Switch
                                            checked={retryEnabled}
                                            onCheckedChange={setRetryEnabled}
                                        />
                                        <span className="text-xs text-muted-foreground">{t('form.retryEnabled')}</span>
                                    </label>
                                </TooltipTrigger>
                                <TooltipContent>
                                    {t('form.retryEnabledHint')}
                                </TooltipContent>
                            </Tooltip>
                        </TooltipProvider>
                        {retryEnabled && (
                            <TooltipProvider>
                                <Tooltip>
                                    <TooltipTrigger asChild>
                                        <label className="flex items-center gap-1.5 shrink-0">
                                            <Input
                                                type="number"
                                                inputMode="numeric"
                                                min={1}
                                                step={1}
                                                value={String(maxRetries)}
                                                onChange={(e) => {
                                                    const raw = e.target.value;
                                                    if (raw.trim() === '') { setMaxRetries(1); return; }
                                                    const n = Number.parseInt(raw, 10);
                                                    setMaxRetries(Number.isFinite(n) && n > 0 ? n : 1);
                                                }}
                                                className="w-16 h-7 rounded-lg text-xs text-center"
                                            />
                                            <span className="text-xs text-muted-foreground">{t('form.maxRetries')}</span>
                                        </label>
                                    </TooltipTrigger>
                                    <TooltipContent>
                                        {t('form.maxRetriesHint')}
                                    </TooltipContent>
                                </Tooltip>
                            </TooltipProvider>
                        )}
                    </div>

                    {mode === GroupMode.Failover && typeof routingGroupId === 'number' && (
                        <div className="flex justify-end">
                            <TooltipProvider>
                                <Tooltip>
                                    <TooltipTrigger asChild>
                                        <span className="inline-flex">
                                            <Button
                                                type="button"
                                                size="sm"
                                                variant="outline"
                                                className="h-8 rounded-lg px-2 text-xs"
                                                disabled={!canImportSmartOrder}
                                                onClick={handleImportSmartOrder}
                                            >
                                                <ListRestart className="size-3.5" />
                                                {t('form.importSmartFailover')}
                                            </Button>
                                        </span>
                                    </TooltipTrigger>
                                    <TooltipContent>
                                        {t(canImportSmartOrder ? 'form.importSmartFailoverHint' : 'form.importSmartFailoverUnavailable')}
                                    </TooltipContent>
                                </Tooltip>
                            </TooltipProvider>
                        </div>
                    )}

                    {mode === GroupMode.SmartFailover && (
                        <div className={cn(
                            'grid grid-cols-1 gap-2 rounded-lg border border-border/60 bg-muted/20 p-2 md:grid-cols-2',
                            enableSmartScheduling && 'lg:grid-cols-4',
                        )}>
                            <Field className="gap-1">
                                <FieldLabel htmlFor="group-price-weight">{t('form.priceWeight')}</FieldLabel>
                                <Input
                                    id="group-price-weight"
                                    type="number"
                                    inputMode="numeric"
                                    min={0}
                                    max={100}
                                    value={String(priceWeight)}
                                    onChange={(event) => setPriceWeight(Math.min(100, Math.max(0, Number.parseInt(event.target.value, 10) || 0)))}
                                    className="h-8 rounded-lg"
                                />
                            </Field>
                            <Field className="gap-1">
                                <FieldLabel htmlFor="group-speed-weight">{t('form.speedWeight')}</FieldLabel>
                                <Input
                                    id="group-speed-weight"
                                    type="number"
                                    inputMode="numeric"
                                    min={0}
                                    max={100}
                                    value={String(speedWeight)}
                                    onChange={(event) => setSpeedWeight(Math.min(100, Math.max(0, Number.parseInt(event.target.value, 10) || 0)))}
                                    className="h-8 rounded-lg"
                                />
                            </Field>
                            {enableSmartScheduling ? (
                                <>
                                    <Field className="gap-1">
                                        <FieldLabel htmlFor="group-smart-reorder-interval">{t('form.smartReorderInterval')}</FieldLabel>
                                        <Input
                                            id="group-smart-reorder-interval"
                                            type="number"
                                            inputMode="numeric"
                                            min={0}
                                            step={1}
                                            value={String(smartReorderInterval)}
                                            onChange={(event) => {
                                                const value = Number.parseInt(event.target.value, 10);
                                                setSmartReorderInterval(Number.isFinite(value) && value > 0 ? value : 0);
                                            }}
                                            className="h-8 rounded-lg"
                                        />
                                        <span className="text-[10px] leading-tight text-muted-foreground">{t('form.smartReorderIntervalHint')}</span>
                                    </Field>
                                    <Field className="gap-1">
                                        <FieldLabel>{t('form.smartReorderClearManual')}</FieldLabel>
                                        <label className="flex min-h-8 cursor-pointer items-center gap-2 rounded-md border border-border/60 bg-background px-2 py-1 text-xs">
                                            <Switch checked={smartReorderClearManual} onCheckedChange={setSmartReorderClearManual} />
                                            <span className="min-w-0 leading-tight text-muted-foreground">{t('form.smartReorderClearManualHint')}</span>
                                        </label>
                                    </Field>
                                </>
                            ) : null}
                            {!smartWeightsValid && (
                                <p className={cn('text-xs text-destructive md:col-span-2', enableSmartScheduling && 'lg:col-span-4')}>{t('form.weightTotalError')}</p>
                            )}
                            <Field className={cn('gap-1 md:col-span-2', enableSmartScheduling && 'lg:col-span-4')}>
                                <FieldLabel>{t('form.zeroCostTakeover')}</FieldLabel>
                                <div className="flex min-h-8 flex-wrap items-center gap-2">
                                    <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                                        <Switch checked={zeroCostTakeoverEnabled} onCheckedChange={setZeroCostTakeoverEnabled} />
                                        <span>{t('form.zeroCostTakeoverEnabled')}</span>
                                    </label>
                                    {zeroCostTakeoverEnabled ? (
                                        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                                            <Input
                                                type="number"
                                                inputMode="numeric"
                                                min={0}
                                                step={1}
                                                value={String(zeroCostTakeoverInterval)}
                                                onChange={(event) => {
                                                    const value = Number.parseInt(event.target.value, 10);
                                                    setZeroCostTakeoverInterval(Number.isFinite(value) && value > 0 ? value : 0);
                                                }}
                                                className="h-7 w-16 rounded-lg text-center text-xs"
                                            />
                                            <span>{t('form.zeroCostTakeoverInterval')}</span>
                                        </label>
                                    ) : null}
                                </div>
                                <span className="text-[10px] leading-tight text-muted-foreground">{t('form.zeroCostTakeoverHint')}</span>
                            </Field>
                        </div>
                    )}

                    <div className="min-h-0 md:flex-1">
                        <div className={cn(
                            'grid grid-cols-1 gap-2 md:h-full md:min-h-0 md:grid-cols-2',
                            sidePanel && 'lg:grid-cols-3',
                        )}>
                            <ModelPickerSection
                                modelChannels={modelChannels}
                                selectedMembers={selectedMembers}
                                onAdd={handleAddMember}
                                onAutoAdd={handleAutoAdd}
                                autoAddDisabled={autoAddDisabled}
                            />
                            <SortSection
                                members={selectedMembers}
                                onReorder={setSelectedMembers}
                                onRemove={handleRemoveMember}
                                onWeightChange={handleWeightChange}
                                removingIds={removingIds}
                                showWeight={mode === 4}
                                onClear={handleClearMembers}
                                routingGroupId={routingGroupId}
                                routingItems={routingItems}
                            />
                            {sidePanel ? <div className="min-h-64 md:min-h-0">{sidePanel}</div> : null}
                        </div>
                    </div>
                </FieldGroup>
            </div>

            <div className="mt-auto shrink-0 px-1 pt-2">
                <div className="flex gap-2">
                    {onCancel && (
                        <Button type="button" variant="secondary" className="h-9 flex-1 rounded-lg" onClick={onCancel}>
                            {t('detail.actions.cancel')}
                        </Button>
                    )}
                    <Button
                        type="submit"
                        disabled={!isValid || isSubmitting}
                        className="h-9 flex-1 rounded-lg"
                    >
                        {isSubmitting ? submittingText : submitText}
                    </Button>
                </div>
            </div>
        </form>
    );
}
