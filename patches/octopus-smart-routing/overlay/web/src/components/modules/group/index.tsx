'use client';

import { useMemo, useState } from 'react';
import { LoaderCircle, Play } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { useGroupList, useReorderGroups } from '@/api/endpoints/group';
import { useRunAllGroupHealth } from '@/api/endpoints/group-health';
import { useGroupHealthEnabled } from '@/api/endpoints/setting';
import { VirtualizedGrid } from '@/components/common/VirtualizedGrid';
import { useSearchStore } from '@/components/modules/toolbar';
import { toast } from '@/components/common/Toast';
import { Button } from '@/components/ui/button';
import { GroupOverviewCard } from './Card';

function GlobalHealthActions() {
    const t = useTranslations('home.groupHealth');
    const { enabled } = useGroupHealthEnabled();
    const runAllGroupHealth = useRunAllGroupHealth();

    if (!enabled) return null;

    const isFullPending = runAllGroupHealth.isPending
        && runAllGroupHealth.variables?.probeMode === 'full';
    const isStandardPending = runAllGroupHealth.isPending && !isFullPending;

    return (
        <div className="flex shrink-0 flex-wrap justify-end gap-2 pb-2">
            <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-8 rounded-lg text-xs"
                disabled={runAllGroupHealth.isPending}
                onClick={() => runAllGroupHealth.mutate({})}
            >
                {isStandardPending ? <LoaderCircle className="size-4 animate-spin" /> : <Play className="size-4" />}
                {t('runAll')}
            </Button>
            <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-8 rounded-lg text-xs"
                disabled={runAllGroupHealth.isPending}
                onClick={() => runAllGroupHealth.mutate({ probeMode: 'full' })}
            >
                {isFullPending ? <LoaderCircle className="size-4 animate-spin" /> : <Play className="size-4" />}
                {t('runAllFull')}
            </Button>
        </div>
    );
}

export function Group() {
    const t = useTranslations('group');
    const { data: groups = [] } = useGroupList();
    const searchTerm = useSearchStore((state) => state.getSearchTerm('group'));
    const reorderGroups = useReorderGroups();
    const [draggedID, setDraggedID] = useState<number | null>(null);

    const visibleGroups = useMemo(() => {
        const term = searchTerm.trim().toLowerCase();
        return groups.filter((group) => !term || group.name.toLowerCase().includes(term));
    }, [groups, searchTerm]);

    const handleDrop = (targetID?: number) => {
        if (!draggedID || !targetID || draggedID === targetID) return;
        const dragged = groups.find((group) => group.id === draggedID);
        const target = groups.find((group) => group.id === targetID);
        if (!dragged || !target || Boolean(dragged.pinned) !== Boolean(target.pinned)) {
            setDraggedID(null);
            toast.error(t('routing.reorderPinnedBoundary'));
            return;
        }

        const ids = groups.map((group) => group.id).filter((id): id is number => typeof id === 'number');
        const sourceIndex = ids.indexOf(draggedID);
        const targetIndex = ids.indexOf(targetID);
        if (sourceIndex < 0 || targetIndex < 0) return;
        ids.splice(targetIndex, 0, ids.splice(sourceIndex, 1)[0]);
        setDraggedID(null);
        reorderGroups.mutate(ids, {
            onSuccess: () => toast.success(t('routing.reorderSaved')),
            onError: (error) => toast.error(t('routing.reorderFailed'), { description: error.message }),
        });
    };

    if (!visibleGroups.length) {
        return <div className="flex min-h-64 items-center justify-center text-sm text-muted-foreground">{t('empty')}</div>;
    }

    return (
        <div className="flex h-full min-h-0 flex-col">
            <GlobalHealthActions />
            <div className="min-h-0 flex-1">
                <VirtualizedGrid
                    items={visibleGroups}
                    columns={{ default: 1, md: 2 }}
                    estimateItemHeight={960}
                    getItemKey={(group, index) => group.id ?? `group-${index}`}
                    renderItem={(group) => (
                        <div
                            onDragOver={(event) => event.preventDefault()}
                            onDrop={(event) => {
                                event.preventDefault();
                                handleDrop(group.id);
                            }}
                        >
                            <GroupOverviewCard
                                group={group}
                                onGroupDragStart={() => group.id && setDraggedID(group.id)}
                            />
                        </div>
                    )}
                />
            </div>
        </div>
    );
}
