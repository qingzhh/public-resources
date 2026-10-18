'use client';

import { useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ListRestart, LoaderCircle, Timer } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { GroupMode, type Group, useRefreshGroupRouting } from '@/api/endpoints/group';
import { toast } from '@/components/common/Toast';
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

function formatCountdown(totalSeconds: number) {
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const minuteSecond = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
    return hours > 0 ? `${hours}:${minuteSecond}` : minuteSecond;
}

export function SmartReorderControls({
    group,
    className,
    disabled = false,
    onRefresh,
}: {
    group: Group;
    className?: string;
    disabled?: boolean;
    onRefresh?: (clearManualRank: boolean) => Promise<void>;
}) {
    const t = useTranslations('group.routing');
    const queryClient = useQueryClient();
    const refreshRouting = useRefreshGroupRouting();
    const [confirmOpen, setConfirmOpen] = useState(false);
    const [externalPending, setExternalPending] = useState(false);
    const [now, setNow] = useState(() => Date.now());

    const nextAtMS = useMemo(() => {
        if (!group.smart_reorder_next_at) return null;
        const value = new Date(group.smart_reorder_next_at).getTime();
        return Number.isFinite(value) ? value : null;
    }, [group.smart_reorder_next_at]);
    const remainingSeconds = nextAtMS === null ? null : Math.max(0, Math.ceil((nextAtMS - now) / 1000));
    const isDue = nextAtMS !== null && remainingSeconds === 0;
    const hasManualOrder = group.items?.some((item) => typeof item.manual_rank === 'number' && item.manual_rank > 0) ?? false;

    useEffect(() => {
        if (nextAtMS === null) return;
        const timer = window.setInterval(() => setNow(Date.now()), 1000);
        return () => window.clearInterval(timer);
    }, [nextAtMS]);

    useEffect(() => {
        if (!isDue || !group.id) return;
        const refresh = () => queryClient.invalidateQueries({ queryKey: ['groups', 'list'] });
        refresh();
        const timer = window.setInterval(refresh, 5000);
        return () => window.clearInterval(timer);
    }, [group.id, isDue, queryClient]);

    if (group.mode !== GroupMode.SmartFailover || !group.id) return null;

    const runRefresh = async (clearManualRank: boolean) => {
        if (onRefresh) {
            setExternalPending(true);
            try {
                await onRefresh(clearManualRank);
                toast.success(t('refreshDone'));
            } catch (error) {
                const message = error instanceof Error ? error.message : String(error);
                toast.error(t('actionFailed'), { description: message });
            } finally {
                setExternalPending(false);
            }
            return;
        }
        refreshRouting.mutate(
            { groupId: group.id!, clearManualRank },
            {
                onSuccess: () => toast.success(t('refreshDone')),
                onError: (error) => toast.error(t('actionFailed'), { description: error.message }),
            },
        );
    };

    const isPending = refreshRouting.isPending || externalPending;

    const countdownLabel = group.smart_reorder_interval && group.smart_reorder_interval > 0
        ? nextAtMS === null
            ? t('scheduleIdle', { minutes: group.smart_reorder_interval })
            : isDue
                ? t('scheduleUpdating')
                : t('scheduleCountdown', { time: formatCountdown(remainingSeconds ?? 0) })
        : t('scheduleDisabled');

    return (
        <div className={cn('flex min-w-0 flex-wrap items-center gap-2', className)}>
            <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-8 rounded-lg px-2 text-xs"
                disabled={disabled || isPending}
                onClick={() => hasManualOrder ? setConfirmOpen(true) : runRefresh(false)}
            >
                {isPending ? <LoaderCircle className="size-3.5 animate-spin" /> : <ListRestart className="size-3.5" />}
                {t('refreshNow')}
            </Button>
            <span className="inline-flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
                <Timer className="size-3.5 shrink-0" />
                <span className="truncate tabular-nums">{countdownLabel}</span>
            </span>

            <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>{t('refreshConfirmTitle')}</AlertDialogTitle>
                        <AlertDialogDescription>{t('refreshConfirmDescription')}</AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                        <AlertDialogCancel>{t('refreshCancel')}</AlertDialogCancel>
                        <AlertDialogAction onClick={() => runRefresh(false)}>{t('refreshKeepManual')}</AlertDialogAction>
                        <AlertDialogAction
                            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                            onClick={() => runRefresh(true)}
                        >
                            {t('refreshClearManual')}
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>
        </div>
    );
}
