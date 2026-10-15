package bridge

import (
	"errors"
	"strings"
)

// Keep host-specific priority presentation outside the original management UI.
const sub2apiPriorityScript = `        function sub2apiNativePriority(state) {
            if (!state) return null;
            if (state.priority_missing) return 0;
            if (!Number.isSafeInteger(state.priority)) return null;
            return state.priority === 0 ? 0 : -state.priority;
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
            const currentLabel = zh ? "Sub2API 当前" : "Sub2API current";
            const targetLabel = isPredictedView ? (zh ? "Sub2API 预测" : "Sub2API predicted") : (zh ? "Sub2API 目标" : "Sub2API target");
            const explanation = zh ? "插件数值越大越优先，Sub2API 数值越小越优先。" : "Higher plugin values and lower Sub2API values are preferred.";
            return '<div class="sub2api-priority-values" style="display:flex; flex-wrap:wrap; gap:4px 10px; font-size:11px; color:var(--text-secondary);" title="' + explanation + '">' +
                '<span>' + currentLabel + ': <strong data-sub2api-priority="current">' + format(current) + '</strong></span>' +
                '<span>' + targetLabel + ': <strong data-sub2api-priority="target">' + format(target) + '</strong></span></div>';
        }

`

func adaptPriorityDisplay(page string) (string, error) {
	const scriptAnchor = "        function renderDashboard() {"
	const markupAnchor = `                        "<div style=\"font-size:11px; color:var(--text-secondary);\">" + escapeHTML(formattedReason) + "</div>" +`
	if strings.Count(page, scriptAnchor) != 1 || strings.Count(page, markupAnchor) != 1 {
		return "", errors.New("original priority UI score display changed")
	}
	page = strings.Replace(page, scriptAnchor, sub2apiPriorityScript+scriptAnchor, 1)
	return strings.Replace(page, markupAnchor, "                        sub2apiPriorityMarkup(item, isPredictedView) +\n"+markupAnchor, 1), nil
}
