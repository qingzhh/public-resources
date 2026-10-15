package bridge

import (
	"errors"
	"strings"
)

// Keep host-specific priority presentation outside the original management UI.
const sub2apiPriorityScript = `        function sub2apiNativePriority(state) {
            if (!state) return null;
            if (state.priority_missing) return 1000;
            if (!Number.isSafeInteger(state.priority)) return null;
            const native = 1000 - state.priority;
            return Number.isSafeInteger(native) ? native : null;
        }
        function sub2apiPriorityMarkup(item, isPredictedView) {
            const zh = currentLang === "zh-CN";
            const current = item.current || item;
            const target = item.target || current;
            const format = function(state) {
                const value = sub2apiNativePriority(state);
                if (value === null) return "—";
                return String(value) + (state.disabled ? (zh ? " [已禁用]" : " [Disabled]") : "");
            };
            const currentLabel = zh ? "当前" : "Current";
            const targetLabel = isPredictedView ? (zh ? "预测" : "Predicted") : (zh ? "目标" : "Target");
            const explanation = zh ? "Sub2API 同步值 = 1000 − 插件优先级，排序保持一致。" : "Sub2API sync value = 1000 minus plugin priority; preference is preserved.";
            const summary = zh ? "Sub2API 同步值" : "Sub2API sync values";
            return '<details class="sub2api-priority-values" style="font-size:11px; color:var(--text-secondary);" title="' + explanation + '">' +
                '<summary style="cursor:pointer; color:var(--text-muted);">' + summary + '</summary>' +
                '<div style="display:flex; flex-wrap:wrap; gap:4px 10px; margin-top:4px;">' +
                '<span>' + currentLabel + ': <strong data-sub2api-priority="current">' + format(current) + '</strong></span>' +
                '<span>' + targetLabel + ': <strong data-sub2api-priority="target">' + format(target) + '</strong></span></div></details>';
        }

`

func adaptPriorityDisplay(page string) (string, error) {
	const scriptAnchor = "        function renderDashboard() {"
	const priorityAnchor = `                    "<div class=\"cred-priority\">" +`
	const markupAnchor = `                        "<div style=\"font-size:11px; color:var(--text-secondary);\">" + escapeHTML(formattedReason) + "</div>" +`
	if strings.Count(page, scriptAnchor) != 1 || strings.Count(page, priorityAnchor) != 1 || strings.Count(page, markupAnchor) != 1 {
		return "", errors.New("original priority UI score display changed")
	}
	page = strings.Replace(page, scriptAnchor, sub2apiPriorityScript+scriptAnchor, 1)
	guidance := priorityAnchor + "\n" + `                        "<div class=\"plugin-priority-guidance\" style=\"font-size:11px; color:var(--text-muted);\">" + (currentLang === "zh-CN" ? "插件优先级 · 越大越优先" : "Plugin priority · higher values first") + "</div>" +`
	page = strings.Replace(page, priorityAnchor, guidance, 1)
	return strings.Replace(page, markupAnchor, "                        sub2apiPriorityMarkup(item, isPredictedView) +\n"+markupAnchor, 1), nil
}
