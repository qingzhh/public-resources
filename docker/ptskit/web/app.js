'use strict';
const $ = id => document.getElementById(id);
let status = null, tasks = [], ptsData = null, page = 'dashboard';
let candidatePage = 1, timer = null, refreshing = false, ptsRefreshing = false, statusForceRefreshing = false;
let settingsDirty = false, busy = false, settingsRevision = 0, busyEpoch = 0, statusRefreshEpoch = 0;
let queuedPtsRefresh = null, queuedRefresh = null;
let ptsStatisticsVersion = 0, ptsCacheRequest = 0;
const ptsStatisticsFields = ['task','has_task','has_record','current','target','missing','seeders_max','synced_at','fetched_at','attempted_at','available','stale','error','cache_expired','restored','cache_error','local_target'];
let displayTimezone = 'Asia/Shanghai';
let queryGeneration = 0;
let transferRuleDocument = null, transferRuleDirty = false, transferRuleConflict = false;
let transferRuleEditVersion = 0, transferRuleRequestVersion = 0, transferRuleLoading = null, transferRulePending = '';
let transferRulePreview = null, transferRuleNoticeKey = null;
let unifiedSaving = false, settingsReadBusy = false, settingsReadEpoch = 0;
let instanceLabelsDocument = null, instanceLabelsConflict = false, instanceLabelsVersion = 0;
const instanceLabelDrafts = new Map();
let settingsPendingDocument = null, settingsPendingLoading = null;
let logRecords = [];
const titles = {dashboard:'仪表盘', tasks:'任务', candidates:'候选', logs:'日志', settings:'设置', downloads:'下载器设置'};
const eventNames = {started:'开始运行', progress:'拉取进度', finished:'本轮完成', failed:'运行失败',
  web_started:'网页服务启动', scheduled_run_deferred:'本轮调度延后', scheduler_error:'调度异常',
  automatic_enabled:'自动拉取已开启', automatic_disabled:'自动拉取已关闭', already_running:'已有一轮正在运行',
  completed:'本轮完成', target_already_satisfied:'当前已满额', run_complete:'本轮完成',
  candidate_pool:'读取候选任务', download_failed:'种子下载失败', inventory_summary:'本地保种库存', inventory_unavailable:'本地库存不可用', refill_check:'补量检查', refill_check_deferred:'补量检查延后', management_job_finished:'管理作业完成', transfer_task_completed:'转种任务完成', transfer_started:'转种开始'};
eventNames.transfer_progress = '转种进度';
const logCounterLabels = {seeding_total:'策略库存',seeding_valid:'有效',seeding_invalid:'失效',seeding_unknown:'未知',seeding_inactive:'暂停／排队',seeding_downloading:'未完成分类',site_current:'站端有效',refill_allowance:'可补额度',added_this_run:'本轮新增',transfer_total:'转种总数',transfer_completed:'转种完成',transfer_failed:'转种失败',transfer_skipped:'转种跳过',transfer_cancelled:'转种取消',transfer_waiting:'转种等待',processed:'已处理',errors:'错误'};
function numericSummary(item, labels = logCounterLabels) { return Object.entries(labels).filter(([key]) => Number.isInteger(item?.[key]) && item[key] >= 0).map(([key,label]) => label + ' ' + item[key]).join(' · '); }
function logPublicMessage(item,value) { let message = safeMessage(value).replace(/\b(?:[a-f\d]{64}|[a-f\d]{40})\b/gi,'[种子标识已隐藏]'); for (const name of [item.name,item.torrent_name,item.torrent?.name]) if (typeof name === 'string' && name) message = message.split(name).join('[种子名称已隐藏]'); return message; }
const reasonNames = {target_reached:'已达到目标', target_or_pending_reservation:'已达到目标或待确认预留数量',
  run_budget_reached:'达到本轮上限', time_budget_reached:'达到时间上限', candidate_pool_empty:'暂无合格候选',
  no_candidates:'暂无合格候选', pool_exhausted:'候选已用完'};
function text(id, value) { $(id).textContent = String(value ?? '—'); }
function notice(message, error = false) {
  $('notice').hidden = !message;
  text('notice', message);
  $('notice').classList.toggle('error', error);
}
function date(value) {
  if (!value) return '—';
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  try { return d.toLocaleString('zh-CN', {timeZone:displayTimezone, hour12:false}); }
  catch { return d.toLocaleString('zh-CN', {hour12:false}); }
}
function size(bytes) { return bytes >= 1073741824 ? (bytes / 1073741824).toFixed(2) + ' GiB' : (bytes / 1048576).toFixed(2) + ' MiB'; }
function showLogin() {
  queryGeneration++; settingsRevision++; busyEpoch++; busy = false; configurationBusy = false; tokenLoading = false;
  settingsReadEpoch++; settingsReadBusy = false; unifiedSaving = false; instanceLabelsVersion++; instanceLabelsDocument = null; instanceLabelsConflict = false; instanceLabelDrafts.clear(); $('instanceLabelsRows').replaceChildren();
  settingsPendingDocument = null; settingsPendingLoading = null; $('settingsPendingFeedback').hidden = true;
  clearInterval(timer);
  timer = null;
  pollingSeconds = null;
  resetConfiguration();
  resetTransferRules();
  resetCategoryCleanup();
  settingsDirty = false;
  status = null;
  ptsData = null;
  ptsStatisticsVersion++; ptsCacheRequest++;
  tasks = [];
  logRecords = []; $('logRows').replaceChildren(); $('logSearch').value = ''; $('logLevel').value = ''; $('logTime').value = '';
  resetDownloads();
  $('loginView').hidden = false;
  $('appView').hidden = true;
}
async function api(path, payload) {
  const generation = queryGeneration;
  const options = {credentials:'same-origin', cache:'no-store'};
  if (payload !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type':'application/json', 'X-Seedkeep-Request':'1'};
    options.body = JSON.stringify(payload);
  }
  let response;
  try { response = await fetch('/api/' + path, options); }
  catch { throw Error('无法连接服务，请检查网络后刷新'); }
  const result = await response.json();
  if (generation !== queryGeneration) { const error = Error('会话已变更'); error.stale = true; throw error; }
  if (!response.ok) {
    if (response.status === 401) showLogin();
    const sensitive = /^(configuration|instances|fleet\/cleanup)/.test(path);
    const error = Error(sensitive ? (response.status === 409 ? '配置冲突或作业繁忙，草稿已保留。' : '操作失败，请核对设置或稍后重试。') : safeMessage(result.error || '操作失败，请稍后重试'));
    error.status = response.status;
    throw error;
  }
  return result;
}
function dot(id, ok) { $(id).className = 'dot ' + (ok ? 'ok' : 'bad'); }
function compactDate(value, timeOnly = false) {
  if (!value) return '—';
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(d.getTime())) return '时间未知';
  try {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:displayTimezone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(d).map(part => [part.type,part.value]));
    const time = `${parts.hour}:${parts.minute}`; return timeOnly ? time : `${parts.year}-${parts.month}-${parts.day} ${time}`;
  } catch { return date(value); }
}
function iconNode(name) {
  const node = document.createElementNS('http://www.w3.org/2000/svg','svg'), use = document.createElementNS('http://www.w3.org/2000/svg','use');
  node.setAttribute('class','icon'); node.setAttribute('aria-hidden','true'); use.setAttribute('href','#icon-' + name); node.append(use); return node;
}
function downloaderIdentity(item, type = item?.type) {
  const node = element('span',undefined,'downloader-identity'), logo = element('span',undefined,'client-logo ' + (type === 'tr' ? 'tr' : 'qb')), label = element('span',undefined,'client-label');
  logo.setAttribute('aria-hidden','true'); logo.append(type === 'tr' ? iconNode('transmission') : document.createTextNode('qb'));
  label.append(element('b',item ? downloaderName(item) : type === 'qb' ? '来源未设置' : '目的未设置'),element('small',`${type === 'tr' ? '（保种）' : '（下载）'}${item?.enabled === false ? ' · 停用' : ''}`)); node.append(logo,label); return node;
}
const refillStateNames = {idle:'等待检查', refilling:'补量中', waiting_downloads:'等待下载 / 转种', waiting_sync:'等待站端同步', at_target:'已达维持目标', below_trigger:'低于安全触发线', unknown:'有效数量未知', disabled:'自动补量关闭', busy:'作业繁忙', running:'本轮运行中'};
function siteEffective(settings = status?.settings) { return settings?.refill_count_basis === 'site_effective'; }
function refillDefaults(settings) {
  const target = Number(settings.target) || 1200;
  const trigger = Math.min(1100,Math.max(0,target - 100));
  return {refill_count_basis:'managed_tasks',refill_trigger:trigger,refill_floor:Math.min(1000,Math.max(0,trigger - 100)),refill_check_minutes:5,refill_max_inflight:500,refill_retry_seconds:60,refill_site_max_age_minutes:120,refill_reservation_hours:72};
}
function publicDownloaders() { return status?.downloaders || downloadData?.instances || instancesData?.items || []; }
function downloaderName(item) { return safeMessage(item?.name || (item?.type === 'qb' ? 'qBittorrent' : item?.type === 'tr' ? 'Transmission' : '未命名下载器')); }
function downloaderLabel(item) { return downloaderName(item) + (item?.type === 'qb' ? ' · qB' : item?.type === 'tr' ? ' · TR' : ''); }
function taskDownloaderName(item) { return downloaderName(publicDownloaders().find(d => d.id === item.instance_id) || {name:item.instance_name || item.location || '历史'}); }
function displayCount(value) { return Number.isInteger(value) && value >= 0 ? value : null; }
function renderSeedkeepInstances() {
  const display = status?.seedkeep_display, registered = publicDownloaders();
  const summaries = new Map((Array.isArray(display?.instances) ? display.instances : []).map(item => [item.instance_id,item]));
  const items = registered.length ? registered.map(item => summaries.get(item.id) || ({instance_id:item.id,name:item.name,type:item.type,enabled:item.enabled,connected:null})) : [...summaries.values()];
  const rows = items.map(item => {
    const row = element('tr'), info = element('td'), registeredItem = registered.find(entry => entry.id === item.instance_id);
    info.append(downloaderIdentity(item)); row.append(info);
    for (const key of ['completed_total','valid','invalid','downloading']) {
      const cell = element('td',item.enabled !== false && item.connected === true ? displayCount(item[key]) : null);
      cell.classList.add('stat-' + key);
      if (key === 'downloading') cell.classList.add('download-divider');
      if (key === 'valid') cell.classList.add('success'); if (key === 'invalid') cell.classList.add('error'); row.append(cell);
    }
    const connection = element('td'), caption = item.enabled === false ? '已停用' : item.connected === true ? '连接正常' : item.connected === false ? '连接失败' : '未知';
    const badge = element('span',undefined,'pill' + (item.enabled === false ? '' : item.connected === true ? ' enabled' : ' unavailable')); badge.append(element('i',undefined,'dot ' + (item.connected === true ? 'ok' : item.connected === false ? 'bad' : '')),document.createTextNode(caption)); connection.append(badge); row.append(connection);
    const settings = element('td'), button = element('button',undefined,'text-button instance-settings'); button.type = 'button'; button.append(iconNode('settings')); button.setAttribute('aria-label','管理下载器：'+downloaderName(item)); button.title = '管理下载器：' + downloaderName(item); button.addEventListener('click',() => openDownloaderSettings(item.instance_id)); settings.append(button); row.append(settings); return row;
  });
  if (!rows.length) { const row = element('tr'), cell = element('td',display ? '尚无已登记下载器实例' : '实例统计尚未取得','empty'); cell.colSpan = 7; row.append(cell); rows.push(row); }
  $('seedkeepInstanceRows').replaceChildren(...rows);
}
async function openDownloaderSettings(id) {
  navigate('downloads'); await (instancesLoading || refreshInstances());
  if (!status || page !== 'downloads') return;
  const item = instancesData?.items.find(entry => entry.id === id); if (item) openInstance(item);
}
function renderSiteProgress() {
  const s = status?.settings || {}, p = ptsData, trusted = p?.available && !p.stale;
  const current = trusted || p?.stale ? displayCount(p.current) : null, target = displayCount(s.target ?? p?.local_target);
  const trigger = siteEffective(s) ? displayCount(s.refill_trigger) : null, floor = siteEffective(s) ? displayCount(s.refill_floor) : null;
  const axis = Math.max(0,...[current,target,trigger,floor,trusted || p?.stale ? displayCount(p.target) : null].filter(value => value !== null));
  $('siteProgress').classList.toggle('unknown',current === null || axis === 0);
  const position = current !== null && axis > 0 ? current / axis * 100 + '%' : '0%';
  $('targetProgress').style.width = position; $('progressCurrent').style.left = position; $('progressCurrent').hidden = current === null || axis === 0;
  text('progressCurrentValue',current); text('progressTargetValue',target); text('dashboardTargetGap',trusted && current !== null && target !== null && siteEffective(s) ? Math.max(0,target-current) : null);
  $('progressCurrentValue').title = p?.stale ? `上次成功查询 ${date(p.fetched_at)}；当前显示旧记录。` : `站端成功查询 ${date(p?.fetched_at)}`;
  $('dashboardTargetGap').parentNode.title = p?.stale ? '站端缓存已过期或查询失败；不以旧记录计算当前缺额。' : siteEffective(s) ? '已保存的长期维持目标减去可信站端有效数量；不使用在途预留。' : '当前采用本地数量模式；站端有效与本地目标不能直接计算缺额。';
  text('progressAxisMax',axis > 0 ? axis : '—');
  const caption = current === null ? '站端有效未知' : p?.stale ? p.cache_expired ? '旧记录 · 等待后台更新' : '旧记录 · 查询失败' : target === null ? '本地目标未知' : target === 0 ? '本地目标为 0 · 不计算比例' : `维持目标进度 ${(current / target * 100).toFixed(1)}%`;
  text('progressCaption',caption); text('progressUnknown',caption); $('progressUnknown').hidden = current !== null && target !== null && target > 0;
  $('siteProgress').setAttribute('aria-label',`${caption}；站端有效 ${current ?? '未知'}，本地维持目标 ${target ?? '未知'}，统一数量轴 0 至 ${axis || '未知'}；触发线 ${trigger ?? '不适用'}，警戒线 ${floor ?? '不适用'}。`);
  for (const [id,value] of [['progressFloor',floor],['progressTrigger',trigger],['progressTarget',target]]) { const marker = $(id); marker.hidden = value === null || axis === 0; marker.style.left = value !== null && axis > 0 ? value / axis * 100 + '%' : '0%'; }
}
function renderDashboardDownloaders() {
  const items = publicDownloaders(), rule = transferRuleDocument;
  const source = items.find(item => item.id === rule?.values.source_instance_id), target = items.find(item => item.id === rule?.values.target_instance_id);
  const flow = [source,target].map((item,index) => { const button = element('button',undefined,'downloader-chip'); button.type = 'button'; button.append(downloaderIdentity(item,index ? 'tr' : 'qb')); button.disabled = !item; if (item) button.addEventListener('click',() => openDownloaderSettings(item.id)); return button; });
  const arrow = iconNode('arrow'); arrow.classList.add('flow-arrow'); $('dashboardDownloaders').replaceChildren(flow[0],arrow,flow[1]);
  renderDashboardTransferSummary();
  if (!rule) { text('dashboardTransfer','尚未读取规则'); text('settingsTransfer','尚未读取转种规则'); text('dashboardTransferRoute','尚未读取规则'); text('dashboardTransferNext',transferRuleLoading ? '读取中…' : '计划未知 · 尚未取得规则'); text('taskSavedRuleSummary','尚未读取已保存规则'); return; }
  const route = `${source ? downloaderName(source) : '来源未设置'} → ${target ? downloaderName(target) : '目的未设置'}`;
  const state = !rule.saved ? '尚未配置 · 规则未保存' : rule.values.enabled ? '已启用自动转种' : '自动转种已关闭';
  const next = !rule.saved ? '尚未配置' : !rule.values.enabled ? '自动转种已关闭' : rule.runtime?.next_run_at == null ? '时间未知' : compactDate(rule.runtime.next_run_at);
  text('dashboardTransferRoute',route); text('dashboardTransfer',state); text('dashboardTransferNext',next); $('dashboardTransferNext').title = `${state} · ${date(rule.runtime?.next_run_at)} · ${displayTimezone}`;
  const summary = `${state} · ${route} · ${rule.values.cron || '计划未知'}`; text('settingsTransfer',summary); text('taskSavedRuleSummary',`${summary} · 下次转种：${next} · ${displayTimezone}${transferRuleDirty ? '；设置有未保存草稿，执行使用已保存版本。' : ''}`);
}
function renderRefillStrategy() {
  const settings = status.settings, r = status.refill_strategy, site = siteEffective(settings), checked = r?.last_checked_at != null;
  const state = r?.status && refillStateNames[r.status] ? r.status : 'unknown';
  text('strategyBasis',site ? 'PTS 站端有效保种 · 预留不计入确认有效' : '本地标签保种总数 · 包含下载中');
  text('strategyTargetLabel','本地维持目标');
  text('strategyTarget',settings.target); text('strategyTrigger',site ? settings.refill_trigger : '不适用'); text('strategyFloor',site ? settings.refill_floor : '不适用');
  for (const [id,key] of [['strategyReserved','reserved'],['strategyInflight','inflight'],['strategyAllowance','allowance']]) text(id,checked ? r?.[key] : null);
  const stateText = r ? (checked ? refillStateNames[state] : '尚未检查') : '尚未检查';
  const stateCaption = r?.warning ? `低于警戒线 · ${status.running || state === 'refilling' ? '自动补量运行中' : stateText}` : stateText; $('strategyState').replaceChildren(...(r?.warning ? [iconNode('warning-filled')] : []),element('span',stateCaption));
  $('strategyState').className = 'pill' + (r?.warning ? ' warning' : r?.active || ['refilling','running'].includes(state) ? ' running' : state === 'at_target' ? ' enabled' : '');
  $('strategyTitle').closest('.card').classList.toggle('warning',r?.warning === true);
  const descriptions = {idle:'等待下一次检查。',refilling:'已进入补量阶段，逐轮补至维持目标。',waiting_downloads:'已有任务等待下载或转种完成，在途数量会限制继续补量。',waiting_sync:'新增任务待站端同步确认，请勿将预留当作有效保种。',at_target:'已达到维持目标，本轮无需继续补量。',below_trigger:'低于安全触发线，是否启动仍由在途、同步与作业状态决定。',unknown:'尚无可信的有效数量，本次不推测缺额。',disabled:'自动调度关闭，可手动检查一轮。',busy:'已有作业运行，等待作业结束后再检查。',running:'本轮正在运行，实际新增以最近一轮结果为准。'};
  text('strategySummary',checked ? `${r.warning ? '警戒提醒 · ' : ''}${site ? '检查所用站端有效 ' + (r.site_current ?? '未知') + '。' : ''}${descriptions[state]}` : '尚未取得策略检查结果；不推测站端有效数量、在途预留或补量额度。');
  updateDashboardHelp('dashboard_strategy',$('strategyBasis').textContent + '。' + $('strategySummary').textContent + ' 在途预留用于避免重复补量，安全触发线与警戒线仅站端有效模式生效。进度条使用 PTS 当前有效值与本地维持目标，数量轴统一包含站端目标、本地目标、触发线、警戒线与当前值；超过目标仍显示真实比例。未知不绘为零，目标为零不计算百分比。');
  feedback('strategyWarning',r?.warning ? '警戒提醒：低于警戒线，请检查补量与下载器状态。' : '', 'error');
  text('strategyChecked','最近检查：' + date(r?.last_checked_at)); text('strategyNext','下次检查：' + date(r?.next_check_at));
  text('strategySynced','检查所用站端同步：' + (r?.site_synced_at || '未知'));
  renderDashboardDownloaders();
}
function renderStatus() {
  if (status?.timezone) displayTimezone = status.timezone;
  const s = status.settings;
  const inventoryCount = kind => !status.connection.error && Number.isInteger(status[`${kind}_total`]) && status[`${kind}_total`] >= 0 ? status[`${kind}_total`] : null;
  const qbTotal = inventoryCount('qb'), trTotal = inventoryCount('tr');
  const inventoryCaption = (kind, total) => total === null ? '任务数未知，请检查下载器连接' : !status.connection[kind] ? '未启用该类型下载器' : `启用实例全部任务（去重） · 受管 ${status[`managed_${kind}`] ?? '—'}`;
  const local = status.seedkeep_display, localCount = key => local?.connected === true ? displayCount(local[key]) : null;
  const localTotal = localCount('completed_total');
  const values = {managedCount:localTotal, seedkeepValidCount:localCount('valid'), seedkeepInvalidCount:localCount('invalid'), seedkeepUnknownCount:localCount('unknown'), strategyDownloading:localCount('downloading'), qbCount:qbTotal, trCount:trTotal,
    seedkeepTag:'管理标签：' + (local?.tag ?? s.managed_tag ?? '—'), seedkeepChecked:'本地检查：' + date(local?.checked_at),seedkeepState:localTotal === null ? '完成数量未知' : '已确认完成范围' + (localCount('completion_unknown') ? ` · 完成状态待确认 ${local.completion_unknown}` : ''),
    qbCountCaption:inventoryCaption('qb', qbTotal), trCountCaption:inventoryCaption('tr', trTotal),
    remainingCount:siteEffective(s) ? status.refill_strategy?.last_checked_at != null ? status.refill_strategy.allowance : null : status.seedkeep?.connected !== true ? null : status.remaining, taskBadge:downloadData?.items.length ?? '—',
    targetCaption:'本地显示按已完成任务统计；补量仍沿用原库存口径（包含下载中）。', pendingCaption:`待下载器确认 ${status.pending_total ?? '—'} · 未确认 ${status.unconfirmed_total ?? '—'}`,
    nextRun:status.automatic_enabled ? compactDate(siteEffective(s) ? status.refill_strategy?.next_check_at : status.next_run_at,true) : '已关闭',
    scheduleText:siteEffective(s) ? `${s.refill_check_minutes ?? '—'} 分钟` : `${s.interval_hours} 小时`, budgetText:`${s.max_per_run} 个`,
    filterSeeders:`${s.min_seeders}–${s.max_seeders} 人`, filterSize:`< ${s.max_bytes / 1048576} MiB`,
    acceptedText:`${status.accepted_total ?? '—'} 个`,
    destinationText:[status.destination?.category, status.destination?.tag].filter(Boolean).join(' / ') || '—'};
  for (const [id, value] of Object.entries(values)) text(id, value);
  $('nextRun').title = status.automatic_enabled ? `${date(siteEffective(s) ? status.refill_strategy?.next_check_at : status.next_run_at)} · ${displayTimezone}` : '自动补量已关闭';
  $('scheduleText').title = siteEffective(s) ? '已保存的补量检查间隔' : `每 ${s.interval_hours} 小时 · 第 ${s.cron_minute} 分钟`;
  renderSiteProgress(); renderSeedkeepInstances();
  text('remainingLabel',siteEffective(s) ? '本轮可补额度' : '本地待补');
  text('automationCaption',siteEffective(s) ? '低于安全线开始补量，达到目标后停止；在途预留避免重复添加。' : '按精确管理标签下的全部任务补齐保种数量目标，包含下载中；使用本地数量模式运行计划。');
  feedback('seedkeepError',localTotal === null ? '已完成保种统计无法确认；请检查实例连接与完成状态。未知不会计作 0。' : '', 'error');
  $('seedkeepTag').title = $('seedkeepTag').textContent;
  updateDashboardHelp('dashboard_seedkeep',fieldHelp.dashboard_seedkeep + ' ' + numericSummary(local,{unknown:'有效性待确认',inactive:'已完成暂停／排队',downloading:'真实下载中',completion_unknown:'完成状态待确认'}) + ' · 人数上限 ' + (local?.seeders_max ?? '未知'));
  updateDashboardHelp('dashboard_remaining',fieldHelp.dashboard_remaining + ' ' + $('targetCaption').textContent + '；' + $('pendingCaption').textContent);
  for (const kind of ['qb','tr']) updateDashboardHelp('dashboard_' + kind,fieldHelp['dashboard_' + kind] + ' ' + $(kind + 'CountCaption').textContent);
  updateDashboardHelp('dashboard_automation',$('automationCaption').textContent + ' 关闭后停止后续调度，当前一轮会继续完成。');
  renderRefillStrategy();
  renderCategoryCleanupRuntime();
  text('autoBadge', status.automatic_enabled ? '已开启' : '已关闭');
  $('autoBadge').classList.toggle('enabled', status.automatic_enabled);
  $('autoButton').setAttribute('aria-label',status.automatic_enabled ? '关闭自动补量' : '开启自动补量'); $('autoButton').title = status.automatic_enabled ? '点击关闭自动补量' : '点击开启自动补量'; $('autoButton').classList.toggle('enabled',status.automatic_enabled); $('autoButton').setAttribute('aria-pressed',String(status.automatic_enabled));
  $('autoButton').disabled = status.running || busy;
  $('runButton').disabled = status.running || busy || activeDownloadJob();
  updateSettingsAvailability();
  renderConfigurationAvailability();
  dot('qbDot', status.connection.qb);
  dot('trDot', status.connection.tr);
  text('connectionText', status.connection.error ? '部分下载器连接异常，请在任务页查看实例状态。' : '连接正常');
  text('stateText', status.running ? '正在拉取' : status.connection.error ? '连接异常' : status.automatic_enabled ? '自动运行已开启' : '空闲');
  $('stateDot').className = 'dot ' + (status.running ? 'busy' : status.connection.error ? 'bad' : 'ok');
  const last = status.last_run;
  const outcome = last.run_outcome === 'failed' ? '失败' : last.run_outcome ? '完成' : '暂无记录';
  text('lastRun', last.updated_at ? `${date(last.updated_at)} · ${outcome} · 新增 ${last.added_this_run ?? 0} 个` : '暂无记录');
  text('updatedTime', '更新于 ' + date(Date.now() / 1000));
  if (!settingsDirty) {
    fillRuntimeSettings(pendingRuntimeSettings(s));
  }
  text('logLimitCaption', `最近 ${settingNumber('log_limit', 100)} 条`);
  syncPolling();
}
function renderPts() {
  updateRecommendationAvailability();
  const p = ptsData || {items:[], filter:status?.settings || {}, available:false, error:null};
  text('ptsCurrent', p.current);
  text('ptsTarget', p.target);
  text('ptsMissing', p.missing);
  renderSiteProgress();
  const connection = p.available && !p.stale ? 'PTS 已连接' : p.stale ? p.cache_expired ? '旧记录 · 等待更新' : '旧记录 · 查询失败' : p.error ? 'PTS 未连接' : '站端尚未查询';
  text('ptsBadge', connection);
  $('ptsBadge').className = 'pill ' + (p.available && !p.stale ? 'enabled' : 'unavailable');
  $('ptsBadge').hidden = p.available === true && !p.stale;
  text('candidateConnection', `API ${p.available ? '可用' : p.stale ? '旧值' : '未知'} · RSS ${p.rss_available ? '可用' : p.rss_stale ? '旧值' : '未知'}`);
  $('candidateConnection').className = 'pill ' + (p.available || p.rss_available ? 'enabled' : 'unavailable');
  text('ptsCurrentCaption', (p.stale ? p.cache_expired ? '显示上次成功查询的站端记录；缓存已过期，等待后台更新' : '显示上次成功查询的站端记录；最近查询失败' : p.available ? '显示最近成功查询的站端统计' : '无法确认站端有效数量') + (p.cache_error ? '；' + safeMessage(p.cache_error) : ''));
  updateDashboardHelp('dashboard_site',fieldHelp.dashboard_site + ' 当前回执：' + $('ptsCurrentCaption').textContent);
  text('ptsTask', p.has_task === undefined ? '任务：未知' : p.has_task ? '任务：' + (p.task || '已建立') : '任务：尚未建立');
  text('ptsRecord', p.has_record === undefined ? '记录：未知' : p.has_record ? '记录：已有保种记录' : '记录：暂无保种记录');
  text('ptsSeederLimit', '站点做种人数上限：' + (p.seeders_max === null || p.seeders_max === undefined ? '未知' : p.seeders_max + ' 人'));
  text('ptsSynced', '站端同步：' + (p.synced_at ? p.synced_at + '（UTC+8）' : '未知'));
  text('ptsFetched', '查询时间：' + date(p.fetched_at));
  feedback('ptsError', safeMessage(p.error), 'error');
  feedback('candidateError', [p.error && 'API：' + safeMessage(p.error), p.rss_error && 'RSS：' + safeMessage(p.rss_error)].filter(Boolean).join('；'), 'error');
  for (const [id, value] of Object.entries({candidateTotal:p.candidates_total,candidateReturned:p.returned_candidates,candidateApiValid:p.api_valid_candidates,candidateRssTotal:p.rss_total,candidateRssReturned:p.rss_returned_candidates,candidateMerged:p.merged_candidates,candidateMatched:p.filter_matches})) text(id, value);
  const sourceState = (available, stale, expired = false) => stale ? expired ? '旧值 · 等待后台更新' : '旧值 · 本次查询失败' : available ? '查询成功' : '未取得可用数据';
  text('candidateApiStatus', `API：${sourceState(p.available,p.stale,p.cache_expired)} · 成功查询 ${date(p.fetched_at)}${p.error ? ' · ' + safeMessage(p.error) : ''}`);
  text('candidateRssStatus', `RSS：${sourceState(p.rss_available,p.rss_stale)} · 成功查询 ${date(p.rss_fetched_at)} · 最近尝试 ${date(p.rss_attempted_at)}${p.rss_error ? ' · ' + safeMessage(p.rss_error) : ''}`);
  $('candidateApiStatus').classList.toggle('error', !!p.error || !!p.stale);
  $('candidateRssStatus').classList.toggle('error', !!p.rss_error || !!p.rss_stale);
  const truncated = value => value === true ? '是' : value === false ? '否' : '未知';
  text('candidateTruncated', `截断：API ${truncated(p.candidates_truncated)} / RSS ${truncated(p.rss_truncated)}`);
  const s = p.filter || status?.settings || {};
  text('candidateCriteria', s.min_seeders == null || s.max_seeders == null || s.max_bytes == null ? '筛选参数尚未取得，匹配结果以服务端为准。' : `当前保存的筛选：${s.min_seeders}–${s.max_seeders} 人（含上下限），体积严格小于 ${s.max_bytes / 1048576} MiB；匹配按服务端返回结果。`);
  const category = $('candidateCategory');
  const selected = category.value;
  const all = document.createElement('option');
  all.value = '';
  all.textContent = '全部分类';
  const options = [all];
  for (const value of [...new Set(p.items.map(item => item.category).filter(Boolean))].sort()) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    options.push(option);
  }
  category.replaceChildren(...options);
  if (options.some(option => option.value === selected)) category.value = selected;
  renderCandidates();
}
function mergePtsStatistics(result) {
  ptsData = {...(ptsData || {items:[],filter:status?.settings || {}}), ...Object.fromEntries(ptsStatisticsFields.map(key => [key,result[key]]))};
  ptsStatisticsVersion++;
}
async function refreshCachedPts() {
  const generation = queryGeneration, revision = settingsRevision, version = ptsStatisticsVersion, request = ++ptsCacheRequest;
  try {
    const result = await api('pts/statistics');
    if (generation !== queryGeneration || revision !== settingsRevision || version !== ptsStatisticsVersion || request !== ptsCacheRequest) return;
    mergePtsStatistics(result);
    if (!status) {
      $('loginView').hidden = true; $('appView').hidden = false;
      text('connectionText','正在读取本地库存…'); text('stateText','正在读取');
      syncControls();
    }
    renderPts();
  } catch (error) {
    if (generation === queryGeneration && revision === settingsRevision && !error.stale) feedback('ptsError',safeMessage(error.message),'error');
  }
}
async function refreshPts(mode = 'normal') {
  if (!status) return;
  if (mode === true) mode = 'full';
  if (mode === false) mode = 'normal';
  if (ptsRefreshing) {
    const priority = {normal:0,stats:1,full:2};
    if (queuedPtsRefresh === null || priority[mode] > priority[queuedPtsRefresh]) queuedPtsRefresh = mode;
    return;
  }
  ptsRefreshing = true;
  const generation = queryGeneration, settingsVersion = settingsRevision;
  const button = $(mode === 'full' ? 'candidateRefreshButton' : 'ptsRefreshButton');
  button.disabled = true; button.textContent = '刷新中…'; button.setAttribute('aria-busy','true');
  try {
    const result = await api(mode === 'full' ? 'pts/refresh' : mode === 'stats' ? 'pts/statistics/refresh' : 'pts', mode === 'normal' ? undefined : {});
    if (!status || generation !== queryGeneration || settingsVersion !== settingsRevision) return;
    if (mode === 'stats') {
      mergePtsStatistics(result);
      feedback('ptsRefreshFeedback',result.available && !result.stale ? '站端统计已更新。' : result.stale ? '本次查询失败；当前仍显示上次成功的站端记录。' : '本次查询失败；站端统计未知。', result.available && !result.stale ? 'success' : 'error');
    } else {
      const retained = result.available !== true && result.current == null && ptsData?.fetched_at != null
        ? Object.fromEntries(['task','has_task','has_record','current','target','missing','seeders_max','synced_at','fetched_at'].map(key => [key,ptsData[key]])) : null;
      ptsData = {...result,...(retained || {})};
      if (retained) { ptsData.available = false; ptsData.stale = true; }
      ptsStatisticsVersion++;
    }
    renderPts();
  } catch (error) {
    if (status && generation === queryGeneration && settingsVersion === settingsRevision && !error.stale) {
      if (mode === 'stats') {
        ptsData = {...(ptsData || {items:[],filter:status.settings}),available:false,stale:!!ptsData?.fetched_at,error:error.message};
        feedback('ptsRefreshFeedback',ptsData.stale ? `刷新失败；当前仍显示旧记录（${error.message}）。` : `刷新失败；站端统计未知（${error.message}）。`,'error');
      } else {
        ptsData = {...(ptsData || {items:[],filter:status.settings}), available:false, stale:!!ptsData?.fetched_at, error:error.message, rss_available:false, rss_stale:!!ptsData?.rss_fetched_at, rss_error:error.message};
      }
      ptsStatisticsVersion++;
      renderPts();
    }
  } finally {
    ptsRefreshing = false;
    button.disabled = false; button.textContent = mode === 'full' ? '刷新 API / RSS' : '刷新站端统计'; button.setAttribute('aria-busy','false');
    if (queuedPtsRefresh !== null && status) { const queued = queuedPtsRefresh; queuedPtsRefresh = null; refreshPts(queued); }
  }
}
async function refreshLocalSeedkeep() {
  if (!status || statusForceRefreshing) return;
  if (busy || unifiedSaving || settingsReadBusy) { feedback('seedkeepRefreshFeedback','当前操作期间无法刷新；本地数据未更新。','error'); return; }
  statusForceRefreshing = true;
  const generation = queryGeneration, revision = settingsRevision, epoch = ++statusRefreshEpoch;
  const button = $('seedkeepRefreshButton');
  button.disabled = true; button.textContent = '刷新中…'; button.setAttribute('aria-busy','true');
  feedback('seedkeepRefreshFeedback','正在强制查询本地保种统计…');
  try {
    const result = await api('status/refresh',{});
    if (!status || generation !== queryGeneration || epoch !== statusRefreshEpoch) return;
    if (revision !== settingsRevision) { feedback('seedkeepRefreshFeedback','设置在刷新期间发生变化；本次结果未应用，本地数据仍是旧记录。','error'); return; }
    status = result;
    renderStatus();
    feedback('seedkeepRefreshFeedback',result.seedkeep_display?.connected ? '本地保种统计已更新。' : '本次查询未取得可信完成统计；请检查连接与完成状态。',result.seedkeep_display?.connected ? 'success' : 'error');
  } catch (error) {
    if (status && generation === queryGeneration && !error.stale) feedback('seedkeepRefreshFeedback',`刷新失败；当前仍显示旧记录（${error.message}），检查时间保持为旧时间。`,'error');
  } finally {
    if (epoch === statusRefreshEpoch) {
      statusForceRefreshing = false;
      button.disabled = false; button.textContent = '刷新本地统计'; button.setAttribute('aria-busy','false');
      if (queuedRefresh !== null) { const queued = queuedRefresh; queuedRefresh = null; refresh(queued); }
    }
  }
}
function renderCandidates() {
  const query = $('candidateSearch').value.trim().toLowerCase();
  const category = $('candidateCategory').value;
  const onlyMatched = $('candidateOnlyMatched').checked, source = $('candidateSource').value;
  const items = (ptsData?.items || []).filter(item => (!onlyMatched || item.matches_filter === true)
    && (!source || item.source === source || item.source === 'both' || (!item.source && source === 'api'))
    && (!category || item.category === category)
    && (!query || String(item.name || '').toLowerCase().includes(query) || String(item.small_descr || '').toLowerCase().includes(query) || String(item.id).includes(query)));
  const perPage = pageSize(), totalPages = Math.max(1, Math.ceil(items.length / perPage));
  candidatePage = Math.min(candidatePage, totalPages);
  const rows = document.createDocumentFragment();
  for (const item of items.slice((candidatePage - 1) * perPage, candidatePage * perPage)) {
    const row = document.createElement('tr');
    const name = document.createElement('td');
    name.textContent = safeMessage(item.name);
    const subtitle = document.createElement('small');
    subtitle.textContent = '#' + item.id + (item.small_descr && item.small_descr !== item.name ? ' · ' + safeMessage(item.small_descr) : '');
    name.append(subtitle);
    row.append(name);
    const knownSize = Number.isFinite(item.size) && item.size > 0, knownSeeders = displayCount(item.seeders) !== null, criteria = ptsData?.filter || status?.settings || {};
    const matched = item.matches_filter === true, unknown = !knownSize || !knownSeeders || typeof item.matches_filter !== 'boolean';
    const reasons = [];
    if (!knownSize) reasons.push('体积未知'); if (!knownSeeders) reasons.push('做种人数未知');
    if (!matched && !unknown) { if (item.seeders < criteria.min_seeders) reasons.push('低于最低人数'); if (item.seeders > criteria.max_seeders) reasons.push('超过最高人数'); if (item.size >= criteria.max_bytes) reasons.push('达到或超过体积上限'); }
    for (const value of [({api:'API',rss:'RSS',both:'API + RSS'})[item.source || 'api'] || '未知',item.category || '—',knownSize ? size(item.size) : '—',`${displayCount(item.seeders) ?? '—'} / ${displayCount(item.leechers) ?? '—'}`]) {
      const cell = element('td',safeMessage(value)); cell.dataset.label = ['来源','分类','体积','做种 / 下载'][row.children.length - 1]; if (row.children.length === 1) cell.className = 'candidate-source'; row.append(cell);
    }
    const matchCell = element('td'); matchCell.dataset.label = '补量要求'; matchCell.append(element('span',unknown ? '信息未知' : matched ? '符合' : '被排除','validity-badge ' + (unknown ? 'unknown' : matched ? 'valid' : 'invalid')));
    if (reasons.length) matchCell.append(element('small',reasons.join(' · '))); else if (!matched && !unknown) matchCell.append(element('small','不满足服务端当前条件'));
    row.append(matchCell); rows.append(row);
    rows.append(row);
  }
  $('candidateRows').replaceChildren(rows);
  $('candidateEmpty').hidden = items.length > 0;
  const hasData = !!(ptsData?.items?.length || ptsData?.fetched_at || ptsData?.rss_fetched_at);
  text('candidateEmpty', !hasData ? '尚未取得 API / RSS 候选，请刷新后重试。' : onlyMatched && ptsData?.filter_matches === 0
    ? `合并返回列表没有符合当前人数／体积条件的候选。${$('candidateCriteria').textContent} API 返回 ${ptsData.returned_candidates ?? '未知'}，RSS 导入 ${ptsData.rss_returned_candidates ?? '未知'}，合并 ${ptsData.merged_candidates ?? '未知'}；取消“仅看”可查看不符合项。` : '当前来源、分类、搜索或人数／体积条件下没有候选；可清除筛选查看其他来源。');
  text('candidateSummary', `当前视图 ${items.length} 个 · 第 ${candidatePage} / ${totalPages} 页 · 每页 ${perPage} 个（上方为完整返回统计）`);
  $('candidatePrevious').disabled = candidatePage === 1;
  $('candidateNext').disabled = candidatePage === totalPages;
}
function renderTasks() { renderDownloads(); }
function logRecordLevel(item) {
  if (/error|failed/.test(item.event || '') || ['error','failed','critical'].includes(String(item.level || '').toLowerCase()) || ['error','failed'].includes(item.status) || item.transfer_failed > 0 || item.errors > 0) return 'error';
  if (['warning','warn'].includes(String(item.level || '').toLowerCase()) || ['waiting','deferred'].includes(item.status) || item.transfer_waiting > 0 || /deferred/.test(item.event || '')) return 'warning';
  return 'info';
}
function renderLogs(items = logRecords) {
  logRecords = items; const rows = document.createDocumentFragment(), query = $('logSearch').value.trim().toLowerCase(), levelFilter = $('logLevel').value, hours = Number($('logTime').value), after = Date.now() - hours * 3600000;
  let shown = 0;
  for (const item of items) {
    const level = logRecordLevel(item), counters = numericSummary(item), legacyCount = !Number.isInteger(item.seeding_total) && Number.isInteger(item.managed_active) && item.managed_active >= 0 ? '策略库存 ' + item.managed_active : '';
    const statusLabel = item.status && (jobStatusNames[item.status] || ({deferred:'已延后',error:'错误'})[item.status]);
    const event = logPublicMessage(item,eventNames[item.event] || item.event || '未标记事件'), evidence = logPublicMessage(item,[statusLabel,item.reason && (reasonNames[item.reason] || item.reason),item.message,item.error,counters,legacyCount].filter(Boolean).join(' · '));
    const stampValue = typeof item.time === 'number' ? item.time * 1000 : new Date(item.time).getTime();
    if ((levelFilter && level !== levelFilter) || (hours && (!Number.isFinite(stampValue) || stampValue < after)) || (query && !(event + ' ' + evidence).toLowerCase().includes(query))) continue;
    const row = element('div',undefined,'log-row' + (level === 'error' ? ' error' : level === 'warning' ? ' waiting' : '')), stamp = element('time',date(item.time));
    if (Number.isFinite(stampValue)) stamp.dateTime = new Date(stampValue).toISOString();
    const badge = element('span',({info:'信息',warning:'提醒',error:'错误'})[level],'pill' + (level === 'error' ? ' log-error' : level === 'warning' ? ' warning' : ''));
    const content = element('div'); content.append(element('b',event));
    if (evidence.length > 180) { const details = element('details',undefined,'log-details'); details.append(element('summary','展开完整内容与错误证据'),element('p',evidence)); content.append(details); }
    else if (evidence) content.append(element('p',evidence));
    row.append(stamp,badge,content); rows.append(row); shown++;
  }
  $('logRows').replaceChildren(rows); $('logEmpty').hidden = shown > 0;
  text('logEmpty',items.length ? '当前搜索、级别或时间范围内没有日志；筛选仅作用于已读取记录。' : '暂无运行日志');
}
async function refresh(forcePts = false) {
  if (unifiedSaving || settingsReadBusy) return;
  if (statusForceRefreshing) { queuedRefresh = forcePts || queuedRefresh === true; return; }
  const settingsVersion = settingsRevision, generation = queryGeneration, cleanupEpoch = categoryCleanupRequest, statusEpoch = statusRefreshEpoch;
  if (refreshing) { queuedRefresh = forcePts || queuedRefresh === true; return; }
  refreshing = true;
  const firstConnection = !status;
  refreshCachedPts();
  try {
    const snapshot = await api('status');
    if (generation !== queryGeneration || statusEpoch !== statusRefreshEpoch) return;
    if (settingsVersion !== settingsRevision) { queuedRefresh = forcePts || queuedRefresh === true; return; }
    status = snapshot;
    if (cleanupEpoch !== categoryCleanupRequest && categoryCleanupDocument) status.cleanup = categoryCleanupDocument.runtime;
    $('loginView').hidden = true;
    $('appView').hidden = false;
    renderStatus();
    syncPolling();
    if (page === 'settings') refreshPendingSettings();
    if (page === 'tasks') refreshDownloads();
    refreshTransferRules();
    refreshCategoryCleanup();
    if (page === 'settings') { refreshCleanupStorage(); refreshConfiguration(); refreshPolicy(); refreshLogPolicy(); }
    if (page === 'logs') { refreshLogs(); refreshLogPolicy(); }
    if (forcePts || firstConnection || page === 'candidates') refreshPts(forcePts ? 'full' : 'normal');
    if (firstConnection && page === 'downloads') refreshInstances();
    if (firstConnection && page === 'settings') { refreshConfiguration(); refreshPolicy(); }
  } catch (error) {
    if (generation === queryGeneration && !error.stale) {
      if (status || $('loginView').hidden) notice(error.message, true);
      else if (!$('loginView').hidden) text('loginError', error.message);
    }
  } finally {
    refreshing = false;
    if (queuedRefresh !== null && !statusForceRefreshing) { const queued = queuedRefresh; queuedRefresh = null; refresh(queued); }
  }
}
function navigate(next) {
  if (page === 'settings' && next !== 'settings') clearConfigurationSecrets();
  if (page === 'settings' && next !== 'settings' && $('cleanupConfirmDialog').open) finishCleanupConfirmation(false);
  if (page === 'downloads' && next !== 'downloads') clearInstanceSecret();
  page = next;
  document.body.dataset.page = next;
  closeHelp();
  for (const id of Object.keys(titles)) $(id).hidden = id !== next;
  for (const button of document.querySelectorAll('.nav-item')) {
    button.classList.toggle('active', button.dataset.page === next);
    if (button.dataset.page === next) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  }
  text('pageTitle', titles[next]);
  updateSettingsAvailability();
  notice('');
  if (next === 'tasks') { renderDownloads(); refreshDownloads(); }
  if (next === 'downloads') refreshInstances();
  if (next === 'settings') { refreshAllSettings(); if (!downloadData) refreshDownloads(); }
  if (next === 'candidates') renderCandidates();
  if (next === 'logs') { refreshLogs(); refreshLogPolicy(); }
  refresh();
}
async function action(button, request, message) {
  if (busy || (button.id === 'runButton' && (status?.running || activeDownloadJob()))) return;
  const generation = queryGeneration, owner = ++busyEpoch;
  busy = true; settingsRevision++; downloadRevision++; syncControls();
  button.disabled = true;
  try { const result = await request(); if (generation !== queryGeneration) return; notice(typeof message === 'function' ? message(result) : message); await refresh(); }
  catch (error) { if (generation === queryGeneration && !error.stale) notice(error.message, true); }
  finally { if (owner === busyEpoch) { busy = false; settingsRevision++; downloadRevision++; if (generation === queryGeneration) { button.disabled = false; syncControls(); } } }
}
$('loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  text('loginError', '');
  try {
    await api('login', {username:form.elements.username.value, password:form.elements.password.value});
    form.elements.password.value = '';
    await refresh();
  } catch (error) { text('loginError', error.message); }
  finally { button.disabled = false; }
});
for (const button of document.querySelectorAll('button[data-page]')) button.addEventListener('click', () => { navigate(button.dataset.page); if (button.dataset.settingsGroup) $(button.dataset.settingsGroup)?.scrollIntoView({block:'start'}); });
for (const button of document.querySelectorAll('[data-settings-group]:not([data-page])')) button.addEventListener('click',() => $(button.dataset.settingsGroup)?.scrollIntoView({block:'start'}));
$('seedkeepTagEdit').addEventListener('click',() => { $('managedTagInput').scrollIntoView({block:'center'}); $('managedTagInput').focus({preventScroll:true}); });
$('refreshButton').addEventListener('click', () => { notice(''); if (page === 'settings') { refreshAllSettings(true); return; } refresh(true); if (page === 'tasks') refreshDownloads(true); if (page === 'downloads') refreshInstances(true); if (page === 'logs') refreshLogs(); });
for (const id of ['ptsRefreshButton', 'candidateRefreshButton']) $(id).addEventListener('click', () => refreshPts(id === 'ptsRefreshButton' ? 'stats' : 'full'));
$('seedkeepRefreshButton').addEventListener('click',refreshLocalSeedkeep);
$('runButton').addEventListener('click', () => action($('runButton'), () => api('run', {}), result => result?.started === false ? '本次未启动补量，请查看策略状态、在途预留和最近一轮结果。' : '已提交本轮检查请求；是否启动与实际新增数量以策略状态和最近一轮结果为准。'));
$('autoButton').addEventListener('click', () => action($('autoButton'), () => api('automation', {enabled:!status.automatic_enabled}), status.automatic_enabled ? '已关闭未来自动调度。' : '已开启自动拉取。'));
$('logoutButton').addEventListener('click', () => {
  const request = api('logout', {}); showLogin();
  request.catch(error => { if (!error.stale) text('loginError', '已清除本地会话；服务退出失败，请重新登录。'); });
});
$('settingsForm').addEventListener('input',markRuntimeDirty);
$('settingsForm').addEventListener('submit', event => { event.preventDefault(); saveAllSettings(); });
$('candidateSearch').addEventListener('input', () => { candidatePage = 1; renderCandidates(); });
for (const id of ['candidateCategory', 'candidateOnlyMatched', 'candidateSource']) $(id).addEventListener('change', () => { candidatePage = 1; renderCandidates(); });
$('candidatePrevious').addEventListener('click', () => { candidatePage--; renderCandidates(); });
$('candidateNext').addEventListener('click', () => { candidatePage++; renderCandidates(); });

// Fleet rows use an instance/hash composite identity; policies and limits have independent drafts.
let downloadData = null, downloadFresh = false, downloadRevision = 0;
let fleetLoading = null, fleetQueued = null, historyLoaded = false;
let cleanupDirty = false, cleanupVersion = 0, cleanupPolicy = null, policyLoading = null;
let logPolicy = null, logPolicyDirty = false, logPolicyVersion = 0, logPolicyLoading = null, logsVersion = 0;
let instancesData = null, instancesLoading = null, instancesConflict = false, instancesVersion = 0;
let instanceDirty = false, instanceEditVersion = 0, editingInstanceId = null, editorRevision = null;
let limitsRevision = 0;
const limitDrafts = new Map(), downloadSelection = new Set();
const validityNames = {valid:'有效', invalid:'人数失效', unknown:'未知', inactive:'暂停／排队', downloading:'未完成', other:'其他'};
const taskStateNames = {completed:'已完成',downloading:'下载中（含排队／暂停）',seeding:'做种中',paused:'已暂停',queued:'排队中',checking:'校验中',stalled:'等待传输',running:'运行中',active:'活动中',inactive:'无活动',moving:'移动文件中',error:'错误／文件缺失',unknown:'状态未知'};
const jobStatusNames = {running:'运行中', waiting:'等待中', completed:'已完成', failed:'失败', cancelled:'已取消', interrupted:'已中断', deferred:'暂缓执行'};
const phaseNames = {pending:'等待处理', waiting:'等待处理', queued:'等待处理', prepared:'恢复资料已保存', pausing:'暂停 qB', paused:'qB 已暂停', exporting:'导出种子', adding:'添加到 TR', added:'TR 已添加', waiting_target:'等待 TR 原生接管', verify_requested:'请求 TR 校验', checking:'TR 校验中', verifying:'TR 校验中', verified:'完整数据已确认', source_removed:'来源已移除', restoring_source:'恢复来源运行状态', source_kept:'来源任务已保留', recovering:'恢复原运行状态', transferring:'转种中', removing:'移除任务', deleting:'移除任务', completed:'已完成', done:'已完成', transferred:'转种完成', deleted:'已移除', failed:'失败', cancelled:'已取消', skipped:'已跳过', interrupted:'已中断'};
phaseNames.removing_source = '等待确认来源移除结果';
const policyDefaults = {cleanup_enabled:false,cleanup_wait_hours:24,cleanup_max_per_run:20,unregistered_enabled:false,unregistered_wait_hours:24,unregistered_interval_hours:24,unregistered_max_per_run:20,unregistered_scope:'pts',unregistered_instances:[]};
function element(tag, value, className) { const node = document.createElement(tag); if (value !== undefined) node.textContent = String(value ?? '—'); if (className) node.className = className; return node; }
function safeMessage(value) {
  let message = String(value ?? '').replace(/https?:\/\/[^\s，；]+/gi,'[连接地址已隐藏]').replace(/\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b/g,'[连接地址已隐藏]').replace(/["']?\b(password|token|passkey|api[_-]?key|authorization|cookie|username)["']?\s*[:=]\s*["']?[^\s,，；;"'}]+/gi,'$1=[已隐藏]');
  for (const item of instancesData?.items || []) for (const secret of [item.url,item.username,item.proxy_url]) if (secret && secret.length >= 3) message = message.split(secret).join('[连接资料已隐藏]');
  for (const secret of [configurationData?.values.api_base,configurationData?.values.site_proxy_url,$('configurationForm')?.elements.token?.value,$('instanceForm')?.elements.password?.value]) if (secret && secret.length >= 3) message = message.split(secret).join('[秘密或连接资料已隐藏]');
  return message;
}
function feedback(id, message, state = '') { text(id, message); $(id).hidden = !message; $(id).classList.toggle('error', state === 'error'); $(id).classList.toggle('success', state === 'success'); }
function speed(bytes) { const value = Math.max(0, Number(bytes) || 0); return value >= 1048576 ? (value / 1048576).toFixed(2) + ' MiB/s' : (value / 1024).toFixed(1) + ' KiB/s'; }
function limitText(value) { return Number(value) === 0 ? '不限速' : value + ' KiB/s'; }
function validHash(hash) { return typeof hash === 'string' && /^(?:[a-f\d]{40}|[a-f\d]{64})$/i.test(hash); }
function taskKey(item) { return item.instance_id + ':' + item.hash; }
function activeDownloadJob() { return (!!downloadData?.job && ['running','waiting'].includes(downloadData.job.status)) || cleanupJobRunning(); }
function instanceConnected(item) { return downloadData?.instances?.some(instance => instance.id === item.instance_id && instance.connected && instance.enabled !== false); }
function selectable(item) { return !item.history && validHash(item.hash) && instanceConnected(item); }
function deletionEligible(item) { return selectable(item) && ((item.pts === true && item.validity === 'invalid' && item.delete_ready === true) || (item.unregistered === true && item.unregistered_ready === true)); }
function transferEligible(item) { return selectable(item) && item.client === 'qb' && item.can_transfer === true && item.progress >= 1 && (status?.settings?.transfer_path_mappings ?? defaultMappings).length > 0; }
function selectedDownloads() { return (downloadData?.items || []).filter(item => downloadSelection.has(taskKey(item))); }
function option(value, caption) { const node = element('option', caption); node.value = value; return node; }
function fillOptions(id, items, first) { const select = $(id), current = select.value; select.replaceChildren(option('',first), ...items.map(item => option(item.value,item.label))); if ([...select.options].some(node => node.value === current)) select.value = current; }
function filteredDownloads() {
  const query = $('downloadSearch').value.trim().toLowerCase(), category = $('downloadCategory').value.trim().toLowerCase(), tag = $('downloadTag').value.trim().toLowerCase();
  const scope = $('downloadScope').value, client = $('downloadClient').value, validity = $('downloadValidity').value, state = $('downloadState').value, instance = $('downloadInstance').value;
  const source = scope === 'history' ? tasks.map(item => ({...item,history:true,instance_name:item.location,progress:item.progress ?? 0})) : downloadData?.items || [];
  return source.filter(item => (!instance || item.instance_id === instance) && (!client || item.client === client) && (!state || (item.state_groups || []).includes(state))
    && (scope !== 'pts' || item.pts) && (scope !== 'managed' || item.managed) && (!validity || (validity === 'unregistered' ? item.unregistered : item.validity === validity))
    && (!$('downloadCleanupRule').value || (item.cleanup_rules || []).includes($('downloadCleanupRule').value))
    && (!$('downloadCleanupStatus').value || ($('downloadCleanupStatus').value === 'none' ? !(item.cleanup_rules || []).length : item.cleanup_status === $('downloadCleanupStatus').value))
    && (!query || [item.name,item.hash,item.id].some(value => String(value ?? '').toLowerCase().includes(query)))
    && (!category || String(item.category ?? '').toLowerCase().includes(category)) && (!tag || String(item.tag ?? '').toLowerCase().includes(tag)));
}
// UI preferences contain allowlisted column keys and sorting only; inventory stays in memory.
const TASK_ROW_HEIGHT = 30, TASK_HEADER_HEIGHT = 32, TASK_OVERSCAN = 8;
const taskColumns = [
  {key:'name',label:'名称',width:320,value:item => item.name || '未命名'},
  {key:'instance',label:'下载器名称',width:144,value:taskDownloaderName},
  {key:'client',label:'类型',width:72,value:item => item.client === 'qb' ? 'qB' : item.client === 'tr' ? 'TR' : '历史'},
  {key:'state',label:'状态',width:156,value:item => item.state_text || (item.history ? item.location || '历史记录' : '状态未知')},
  {key:'progress',label:'进度',width:84,value:item => (Math.max(0,Math.min(1,Number(item.progress)||0))*100).toFixed(1)+'%',sort:item => Number(item.progress)||0},
  {key:'size',label:'体积',width:100,value:item => size(item.size || 0),sort:item => Number(item.size)||0},
  {key:'seeders',label:'做种人数',width:84,value:item => item.history ? item.seeders_at_pull ?? '未知' : item.seeders ?? '未知',sort:item => Number(item.history ? item.seeders_at_pull : item.seeders)},
  {key:'validity',label:'本地判定',width:128,value:item => item.history ? '历史记录' : item.unregistered ? 'Tracker 未注册' : item.validity_text || validityNames[item.validity] || '未知'},
  {key:'upload',label:'上传速度',width:112,value:item => speed(item.upload_speed),sort:item => Number(item.upload_speed)||0},
  {key:'download',label:'下载速度',width:112,value:item => speed(item.download_speed),sort:item => Number(item.download_speed)||0},
  {key:'category',label:'分类',width:140,value:item => item.category || '—'},
  {key:'tag',label:'标签',width:140,value:item => item.tag || '—'},
  {key:'hash',label:'Hash',width:290,value:item => item.hash || '#'+item.id},
  {key:'cleanup',label:'清理等待',width:156,value:item => item.history ? '历史不操作' : item.delete_ready || item.unregistered_ready ? '已到期 · 删除前复核' : item.invalid_since || item.unregistered_since ? '连续等待中' : '不累计等待',sort:item => Number(item.delete_after || item.unregistered_since || item.invalid_since)||0}
  ,{key:'category_cleanup',label:'分类清理',width:140,value:item => cleanupTaskStateNames[item.cleanup_status] || '未归入规则'}
];
const defaultTaskColumns = ['instance','state','progress','size','seeders','validity','upload','download','cleanup'];
const taskPreferenceKey = 'ptskit.taskColumns.v1';
let visibleTaskColumns = [...defaultTaskColumns], taskSort = {key:'',direction:'asc'}, virtualDownloads = [], downloadDetailKey = null, detailReturnKey = null, virtualFrame = null;
try {
  const saved = JSON.parse(localStorage.getItem(taskPreferenceKey));
  if (Array.isArray(saved?.columns)) visibleTaskColumns = [...new Set(saved.columns.filter(key => key !== 'name' && taskColumns.some(column => column.key === key)))];
  if (taskColumns.some(column => column.key === saved?.sort?.key) && ['asc','desc'].includes(saved.sort.direction)) taskSort = {key:saved.sort.key,direction:saved.sort.direction};
} catch { /* A disabled or malformed browser store uses defaults. */ }
function saveTaskPreferences() { try { localStorage.setItem(taskPreferenceKey,JSON.stringify({columns:visibleTaskColumns,sort:taskSort})); } catch { /* Preferences remain usable for this session. */ } }
function displayedTaskColumns() { return taskColumns.filter(column => column.key === 'name' || visibleTaskColumns.includes(column.key)); }
function detailKey(item) { return item.history ? 'history:'+item.id : taskKey(item); }
function resetDownloadPosition() { virtualDownloads = []; $('downloadViewport').scrollTop = 0; }
function sortedDownloads(items) {
  const column = taskColumns.find(column => column.key === taskSort.key); if (!column) return items;
  const sign = taskSort.direction === 'desc' ? -1 : 1;
  return [...items].sort((a,b) => {
    const av = (column.sort || column.value)(a), bv = (column.sort || column.value)(b);
    const compared = typeof av === 'number' && typeof bv === 'number' ? (Number.isFinite(av) ? av : -1) - (Number.isFinite(bv) ? bv : -1) : String(av).localeCompare(String(bv),'zh-CN',{numeric:true});
    return compared * sign || detailKey(a).localeCompare(detailKey(b));
  });
}
function renderDownloadHead() {
  const columnWidth = column => column.key === 'name' && innerWidth <= 760 ? 196 : column.width;
  const columns = displayedTaskColumns(), group = [element('col')], row = element('tr'); group[0].style.width = '36px';
  row.append(element('th','选择')); row.firstChild.scope = 'col';
  for (const column of columns) {
    const col = element('col'); col.style.width = columnWidth(column)+'px'; group.push(col);
    const th = element('th'), button = element('button',column.label + (taskSort.key === column.key ? taskSort.direction === 'asc' ? ' ↑' : ' ↓' : ''),'column-sort'); button.type = 'button'; button.dataset.sort = column.key;
    th.scope = 'col'; th.setAttribute('aria-sort',taskSort.key === column.key ? taskSort.direction === 'asc' ? 'ascending' : 'descending' : 'none');
    button.addEventListener('click',() => { taskSort = {key:column.key,direction:taskSort.key === column.key && taskSort.direction === 'asc' ? 'desc' : 'asc'}; saveTaskPreferences(); resetDownloadPosition(); renderDownloads(); $('downloadHead').querySelector(`[data-sort="${column.key}"]`).focus({preventScroll:true}); }); th.append(button); row.append(th);
  }
  $('downloadColgroup').replaceChildren(...group); $('downloadHead').replaceChildren(row);
  document.querySelector('.download-table').style.width = 36 + columns.reduce((sum,column) => sum+columnWidth(column),0)+'px';
}
function renderTaskColumnChoices() {
  $('downloadColumnChoices').replaceChildren(...taskColumns.map(column => {
    const label = element('label',column.label,'checkbox-label'), input = element('input'); input.type = 'checkbox'; input.dataset.column = column.key; input.checked = column.key === 'name' || visibleTaskColumns.includes(column.key); input.disabled = column.key === 'name';
    input.addEventListener('change',() => { if (input.checked) visibleTaskColumns.push(column.key); else visibleTaskColumns = visibleTaskColumns.filter(key => key !== column.key); saveTaskPreferences(); renderDownloads(); }); label.prepend(input); return label;
  }));
}
function renderVirtualDownloads() {
  const viewport = $('downloadViewport'), columns = displayedTaskColumns(), total = virtualDownloads.length;
  const first = Math.min(Math.max(0,total-1),Math.floor(Math.max(0,viewport.scrollTop-TASK_HEADER_HEIGHT)/TASK_ROW_HEIGHT));
  const start = Math.max(0,first-TASK_OVERSCAN), end = Math.min(total,first+Math.ceil((viewport.clientHeight || 420)/TASK_ROW_HEIGHT)+TASK_OVERSCAN);
  const focused = document.activeElement.closest?.('#downloadRows tr'), focusKey = focused?.dataset.key, focusPick = document.activeElement.type === 'checkbox';
  const rows = document.createDocumentFragment();
  const spacer = height => { const row = element('tr',undefined,'virtual-spacer'), cell = element('td'); row.setAttribute('aria-hidden','true'); cell.colSpan = columns.length+1; cell.style.height = height+'px'; row.append(cell); return row; };
  if (start) rows.append(spacer(start*TASK_ROW_HEIGHT));
  for (let index=start; index<end; index++) {
    const item = virtualDownloads[index], key = detailKey(item), row = element('tr',undefined,'task-row'), pick = element('td'), checkbox = element('input');
    row.dataset.key = key; row.dataset.index = index; row.setAttribute('aria-rowindex',index+2); row.classList.toggle('selected',downloadSelection.has(taskKey(item)));
    checkbox.type = 'checkbox'; checkbox.checked = downloadSelection.has(taskKey(item)); checkbox.disabled = !selectable(item); checkbox.setAttribute('aria-label','选择 '+taskDownloaderName(item)+' 中的 '+safeMessage(item.name));
    checkbox.addEventListener('click',event => event.stopPropagation());
    checkbox.addEventListener('change',() => { if (checkbox.checked) downloadSelection.add(taskKey(item)); else downloadSelection.delete(taskKey(item)); row.classList.toggle('selected',checkbox.checked); renderSelection(); }); pick.append(checkbox); row.append(pick);
    for (const column of columns) {
      const cell = element('td'); cell.dataset.column = column.key;
      if (column.key === 'name') { const button = element('button',safeMessage(column.value(item)),'task-name'); button.type = 'button'; button.setAttribute('aria-label','查看详情：'+safeMessage(item.name)); button.addEventListener('click',event => { event.stopPropagation(); openDownloadDetail(item); }); cell.append(button); }
      else { const content = element('span',safeMessage(column.value(item)),'cell-value'); if (column.key === 'state' || column.key === 'validity') { const stateClass = column.key === 'validity' ? item.unregistered || item.validity === 'invalid' ? 'invalid' : item.validity === 'valid' ? 'valid' : 'unknown' : item.completed === true ? 'valid' : /error|failed/i.test(item.state || '') ? 'invalid' : 'neutral'; content.classList.add('task-state',stateClass); } cell.append(content); }
      if (column.key === 'validity') cell.classList.add(['valid','invalid','unknown'].includes(item.validity) ? item.validity : 'unknown');
      if (column.key === 'instance' && !item.history && !instanceConnected(item)) cell.classList.add('error');
      row.append(cell);
    }
    row.addEventListener('click',() => openDownloadDetail(item)); rows.append(row);
  }
  if (end<total) rows.append(spacer((total-end)*TASK_ROW_HEIGHT));
  $('downloadRows').replaceChildren(rows); document.querySelector('.download-table').setAttribute('aria-rowcount',total+1);
  if (focusKey) { const row = [...$('downloadRows').querySelectorAll('.task-row')].find(row => row.dataset.key === focusKey); if (row) row.querySelector(focusPick ? 'input' : '.task-name').focus({preventScroll:true}); else viewport.focus({preventScroll:true}); }
}
function openDownloadDetail(item) { downloadDetailKey = detailKey(item); detailReturnKey = downloadDetailKey; renderDownloadDetail(); if (!$('downloadDetail').open) $('downloadDetail').showModal(); }
function renderDownloadDetail() {
  if (!downloadDetailKey) return;
  const item = (downloadDetailKey.startsWith('history:') ? tasks.map(item => ({...item,history:true,instance_name:item.location})) : downloadData?.items || []).find(item => detailKey(item) === downloadDetailKey);
  if (!item) { closeDownloadDetail(false); return; }
  text('downloadDetailTitle',safeMessage(item.name || '任务详情'));
  const fields = [['名称',item.name],...taskColumns.filter(column => column.key !== 'name').map(column => [column.label,column.value(item)]),['完成分类',item.history ? '历史资料' : item.completed ? '已完成（下载器分类）' : '未列入已完成'],['范围',item.history ? '受管历史资料' : (item.pts ? 'PTS' : '非 PTS')+(item.managed ? ' · 受管' : '')],['数据状态',item.history ? '历史资料，不可操作' : downloadFresh && instanceConnected(item) ? '本次查询 · 实例已连接' : '旧数据，仅供参考，不可操作'],['判定说明',item.reason || '—'],['分类清理规则',(item.cleanup_rules || []).map(id => cleanupRuleName(id)).join(' / ') || '—'],['分类清理说明',cleanupPublicText(item.cleanup_reason) || '—'],['人数失效起点',date(item.invalid_since)],['人数删除资格时间',date(item.delete_after)],['未注册起点',date(item.unregistered_since)],['转种资格',transferEligible(item) ? '满足本地资格，提交前仍复核' : '当前不满足本地资格']];
  $('downloadDetailFields').replaceChildren(...fields.flatMap(([label,value]) => [element('dt',label),element('dd',safeMessage(value))]));
}
function closeDownloadDetail(restore = true) { const key = detailReturnKey; downloadDetailKey = null; detailReturnKey = null; if ($('downloadDetail').open) $('downloadDetail').close(); $('downloadDetailFields').replaceChildren(); text('downloadDetailTitle','任务详情'); if (restore && page === 'tasks') { const row = [...$('downloadRows').querySelectorAll('.task-row')].find(row => row.dataset.key === key); (row?.querySelector('.task-name') || $('downloadViewport')).focus({preventScroll:true}); } }
$('downloadDetailClose').addEventListener('click',() => closeDownloadDetail());
$('downloadDetail').addEventListener('cancel',event => { event.preventDefault(); closeDownloadDetail(); });
$('downloadDetail').addEventListener('click',event => { if (event.target !== event.currentTarget) return; const r = event.currentTarget.getBoundingClientRect(); if (event.clientX<r.left || event.clientX>r.right || event.clientY<r.top || event.clientY>r.bottom) closeDownloadDetail(); });
$('downloadResetColumns').addEventListener('click',() => { visibleTaskColumns = [...defaultTaskColumns]; taskSort = {key:'',direction:'asc'}; saveTaskPreferences(); renderTaskColumnChoices(); resetDownloadPosition(); renderDownloads(); });
function clearDownloadFilters() { for (const id of ['downloadInstance','downloadClient','downloadState','downloadValidity','downloadSearch','downloadCategory','downloadTag','downloadCleanupRule','downloadCleanupStatus']) $(id).value = ''; $('downloadScope').value = 'all'; }
$('downloadResetFilters').addEventListener('click',() => { clearDownloadFilters(); resetDownloadPosition(); renderDownloads(); });
$('downloadQbCompleted').addEventListener('click',() => { clearDownloadFilters(); $('downloadClient').value = 'qb'; $('downloadState').value = 'completed'; downloadSelection.clear(); closeDownloadDetail(false); resetDownloadPosition(); $('downloadViewport').scrollLeft = 0; renderDownloads(); });
$('downloadViewport').addEventListener('scroll',() => { if (virtualFrame !== null) return; virtualFrame = requestAnimationFrame(() => { virtualFrame = null; renderVirtualDownloads(); }); },{passive:true});
$('downloadViewport').addEventListener('keydown',event => {
  const row = event.target.closest('.task-row'); if (!row || !['ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
  event.preventDefault(); const index = Math.max(0,Math.min(virtualDownloads.length-1,event.key === 'Home' ? 0 : event.key === 'End' ? virtualDownloads.length-1 : Number(row.dataset.index)+(event.key === 'ArrowDown' ? 1 : -1)));
  const viewport = $('downloadViewport'), top = TASK_HEADER_HEIGHT+index*TASK_ROW_HEIGHT;
  if (top<viewport.scrollTop+TASK_HEADER_HEIGHT) viewport.scrollTop = top-TASK_HEADER_HEIGHT; else if (top+TASK_ROW_HEIGHT>viewport.scrollTop+viewport.clientHeight) viewport.scrollTop = top+TASK_ROW_HEIGHT-viewport.clientHeight;
  const pick = event.target.type === 'checkbox'; renderVirtualDownloads(); const next = [...$('downloadRows').querySelectorAll('.task-row')].find(row => Number(row.dataset.index) === index); next?.querySelector(pick ? 'input' : '.task-name').focus({preventScroll:true});
});
new ResizeObserver(() => renderVirtualDownloads()).observe($('downloadViewport'));
renderTaskColumnChoices();
function resetDownloads() {
  downloadRevision++; instancesVersion++; cleanupVersion++; logPolicyVersion++; limitsRevision++; logsVersion++;
  downloadData = null; downloadFresh = false; downloadSelection.clear(); tasks = []; historyLoaded = false; virtualDownloads = []; closeDownloadDetail(false); $('downloadViewport').scrollTop = 0;
  cleanupDirty = false; cleanupPolicy = null; logPolicyDirty = false; logPolicy = null; instancesData = null; instancesConflict = false;
  instanceDirty = false; editorRevision = null; editingInstanceId = null; instanceEditVersion++;
  $('instanceForm').reset(); $('instanceForm').hidden = true; $('instanceList').replaceChildren(); $('instanceLimits').replaceChildren(); limitDrafts.clear();
  $('cleanupForm').reset(); $('logPolicyForm').reset(); $('cleanupInstances').replaceChildren(); $('downloadClients').replaceChildren(); $('downloadRows').replaceChildren(); $('logRows').replaceChildren(); $('downloadJob').hidden = true;
  for (const id of ['downloadError','limitsError','downloadFeedback']) $(id).hidden = true;
  renderDownloads(); renderCleanup(); renderLogPolicy(); renderInstances();
}
function syncControls() {
  updateSettingsAvailability(); renderConfigurationAvailability(); renderSelection(); renderDownloadJob(); renderCleanup(); renderLogPolicy(); renderInstanceAvailability();
  renderCategoryCleanupAvailability();
  for (const draft of limitDrafts.values()) updateLimitAvailability(draft);
  for (const id of ['runButton','autoButton','checkButton','logDeleteErrors','logCleanupExpired','instanceAdd']) $(id).disabled = busy || !status || (['runButton','instanceAdd'].includes(id) && (activeDownloadJob() || !!status?.running));
}
async function transaction(callback, target) {
  if (busy || !status) return;
  const generation = queryGeneration, owner = ++busyEpoch; busy = true; settingsRevision++; downloadRevision++; limitsRevision++; syncControls();
  try { await callback(); }
  catch (error) { if (generation === queryGeneration && status && !error.stale) feedback(target, error.message, 'error'); }
  finally { if (owner === busyEpoch) { busy = false; settingsRevision++; downloadRevision++; limitsRevision++; if (generation === queryGeneration && status) { syncControls(); renderInstances(); refresh(); } } }
}
async function refreshDownloads(force = false) {
  if (!status) return;
  if (fleetLoading) { fleetQueued = force || fleetQueued === true; return fleetLoading; }
  const generation = queryGeneration;
  fleetLoading = (async () => {
    let next = force;
    do {
      fleetQueued = null; const version = downloadRevision;
      $('downloadRefresh').disabled = true; text('downloadRefresh','查询中…');
      try {
        const result = await api(next && !busy ? 'fleet/tasks/refresh' : 'fleet/tasks', next && !busy ? {} : undefined);
        if (!status || generation !== queryGeneration || version !== downloadRevision) { next = fleetQueued; continue; }
        downloadData = result; downloadFresh = true;
        const failed = (result.instances || []).filter(item => item.enabled !== false && !item.connected);
        feedback('downloadError',failed.length ? '部分实例查询失败；保留的旧行仅供参考，不可操作。' : '', 'error');
        const present = new Set(result.items.map(taskKey)); for (const key of downloadSelection) if (!present.has(key)) downloadSelection.delete(key);
        if (!cleanupDirty && result.policy) cleanupPolicy = result.policy;
        fillOptions('downloadInstance',(result.instances || []).map(item => ({value:item.id,label:downloaderLabel(item)})),'全部下载器');
        renderDashboardDownloaders();
        fillOptions('downloadState',Object.entries(taskStateNames).map(([value,label]) => ({value,label})),'全部状态');
        fillOptions('transferTarget',(result.instances || []).filter(item => item.type === 'tr' && item.enabled && item.connected).map(item => ({value:item.id,label:downloaderLabel(item)})),'选择启用且已连接的 TR');
        renderDownloads(); renderCleanup();
      } catch (error) {
        if (generation === queryGeneration && status) { downloadFresh = false; feedback('downloadError',error.message + (downloadData ? '；保留上次数据，禁止操作。' : '；请重试。'),'error'); renderDownloads(); }
      }
      next = fleetQueued;
    } while (next !== null && generation === queryGeneration && status);
  })().finally(() => { fleetLoading = null; if (generation === queryGeneration) { $('downloadRefresh').disabled = false; text('downloadRefresh','刷新任务'); } });
  return fleetLoading;
}
async function refreshHistory() { const generation = queryGeneration; try { const result = await api('tasks'); if (generation !== queryGeneration || !status) return; tasks = result.items; historyLoaded = true; renderDownloads(); } catch (error) { if (generation === queryGeneration && status) feedback('downloadFeedback',error.message,'error'); } }
function renderDownloadClients() {
  $('downloadClients').replaceChildren(...(downloadData?.instances || []).map(item => {
    const row = element('p',undefined,'fleet-status');
    row.append(element('i',undefined,'dot ' + (item.enabled === false ? '' : item.connected ? 'ok' : 'bad')),element('span',safeMessage(item.name)),element('small',item.enabled === false ? '停用' : item.connected ? String(item.total ?? '—') : '旧值 / 失败'));
    return row;
  }));
}
function renderSelection() {
  const selected = selectedDownloads(), count = selected.length, blocked = busy || activeDownloadJob() || !downloadFresh || !!status?.running;
  const canOperate = count > 0 && selected.every(selectable), target = $('transferTarget').value;
  text('downloadSelected',`已选 ${count} 个（包含筛选外任务）`); $('downloadClear').disabled = !count;
  for (const id of ['taskStart','taskStop','taskVerify','taskRemove']) $(id).disabled = blocked || !canOperate;
  $('downloadDelete').disabled = blocked || !count || count > deleteBatchSize() || !selected.every(deletionEligible);
  $('downloadTransfer').disabled = blocked || !count || !selected.every(transferEligible) || new Set(selected.map(item => item.instance_id)).size !== 1 || !target;
  text('downloadSelectionHint',!downloadFresh ? '需成功读取任务后才能操作。' : activeDownloadJob() ? '后台作业进行中，完成后可提交下一批。' : `每批最多移除 ${deleteBatchSize()} 项；仅下载完成的同一 qB 来源任务可转种到启用 TR。每项由 TR 原生快速接管并确认完整做种后交接，再处理下一项；移除始终保留文件。未知或断开连接的任务不可删除。`);
  renderTransferRuleAvailability();
}
function renderDownloads() {
  renderDownloadClients(); text('downloadChecked',downloadData ? '任务查询：' + date(downloadData.checked_at) + (downloadFresh ? '' : ' · 旧数据') : '尚未查询任务');
  const qbCompleted = (downloadData?.items || []).filter(item => item.client === 'qb' && item.completed === true).length;
  const completedStale = !downloadFresh || (downloadData?.instances || []).some(item => item.type === 'qb' && item.enabled !== false && !item.connected);
  text('downloadQbCompleted',downloadData ? `qB 已完成（${qbCompleted}）${completedStale ? ' · 旧值' : ''}` : 'qB 已完成（—）');
  $('downloadQbCompleted').disabled = !downloadData;
  $('downloadQbCompleted').setAttribute('aria-pressed',String($('downloadClient').value === 'qb' && $('downloadState').value === 'completed' && $('downloadScope').value === 'all' && ['downloadInstance','downloadValidity','downloadSearch','downloadCategory','downloadTag','downloadCleanupRule','downloadCleanupStatus'].every(id => !$(id).value)));
  const viewport = $('downloadViewport'), oldTop = viewport.scrollTop, oldIndex = Math.floor(Math.max(0,oldTop - TASK_HEADER_HEIGHT) / TASK_ROW_HEIGHT), anchor = virtualDownloads[oldIndex];
  virtualDownloads = sortedDownloads(filteredDownloads());
  if (anchor && oldTop > TASK_HEADER_HEIGHT) {
    const nextIndex = virtualDownloads.findIndex(item => detailKey(item) === detailKey(anchor));
    if (nextIndex >= 0) viewport.scrollTop = TASK_HEADER_HEIGHT + nextIndex * TASK_ROW_HEIGHT + (oldTop - TASK_HEADER_HEIGHT) % TASK_ROW_HEIGHT;
  }
  renderDownloadHead(); renderVirtualDownloads(); renderDownloadDetail();
  $('downloadEmpty').hidden = !!virtualDownloads.length;
  text('downloadEmpty',$('downloadScope').value === 'history' && !historyLoaded ? '正在读取受管历史资料…' : downloadData || historyLoaded ? '没有符合筛选的任务。' : '尚未取得任务，请刷新任务。');
  text('downloadSummary',`筛选 ${virtualDownloads.length} / ${$('downloadScope').value === 'history' ? tasks.length : downloadData?.items.length ?? 0} 项 · 连续滚动`);
  $('downloadSelectFiltered').disabled = !virtualDownloads.some(selectable);
  if (downloadData) text('taskBadge',downloadData.items.length); renderSelection(); renderDownloadJob(); updateSettingsAvailability();
}
function renderDownloadJob() {
  renderDashboardTransferSummary();
  const job = downloadData?.job; $('downloadJob').hidden = !job; if (!job) return;
  const items = job.items || [], finished = job.status === 'completed' ? items.length : items.filter(item => ['completed','done','transferred','deleted','failed','skipped','cancelled','interrupted'].includes(item.phase)).length;
  text('downloadJobTitle',job.kind === 'transfer' ? 'qB → TR 转种作业' : '任务操作作业'); text('downloadJobSummary',`${jobStatusNames[job.status] || job.status} · 已处理 ${finished} / ${items.length}`);
  $('downloadJobProgress').max = Math.max(1,items.length); $('downloadJobProgress').value = finished; text('downloadJobTime','开始 ' + date(job.started_at) + (job.finished_at ? ' · 结束 ' + date(job.finished_at) : ' · 状态自动更新'));
  feedback('downloadJobError',safeMessage(job.error),'error'); $('downloadCancel').hidden = job.kind !== 'transfer' || !['running','waiting'].includes(job.status); $('downloadCancel').disabled = busy; $('downloadCancelHint').hidden = $('downloadCancel').hidden;
  $('downloadJobItems').replaceChildren(...items.map(item => { const row = element('li'); row.append(element('span',item.name || item.hash),element('span',(phaseNames[item.phase] || item.phase || '待处理') + (item.source_kept === true ? ' · 来源任务保留' : ''))); if (item.error) row.append(element('small',safeMessage(item.error))); return row; }));
}
async function fleetAction(actionName) {
  renderSelection(); const button = $(({start:'taskStart',stop:'taskStop',verify:'taskVerify',remove:'taskRemove',delete_expired:'downloadDelete',transfer:'downloadTransfer'})[actionName]); if (button.disabled) return;
  const selected = selectedDownloads(), refs = selected.map(({instance_id,hash}) => ({instance_id,hash}));
  if (['remove','delete_expired','transfer'].includes(actionName) && !window.confirm(actionName === 'transfer' ? `将所选 ${refs.length} 项依序转种到所选 TR？全部须下载完成并符合条件。每项由 TR 原生快速接管并确认完整做种后交接；来源暂停，来源移除只移除任务，保留全部文件。` : `确认移除 ${refs.length} 项任务？保留全部文件；到期删除由服务器再次核查，旧 hash 永久排除。`)) return;
  await transaction(async () => {
    downloadRevision++; const body = actionName === 'transfer' ? {tasks:refs,target_instance_id:$('transferTarget').value,confirm:'MOVE_QB_TO_TR_KEEP_DATA'} : {action:actionName,tasks:refs,...(['remove','delete_expired'].includes(actionName) ? {confirm:'REMOVE_TASKS_KEEP_DATA'} : {})};
    const result = await api(actionName === 'transfer' ? 'fleet/transfer' : 'fleet/actions',body); downloadRevision++;
    if (result.ok === false && !(result.results || []).length) throw Error(safeMessage(result.error || '操作未完成，请刷新任务后重试。'));
    if (result.job) { downloadData = {...downloadData,job:result.job}; renderDownloadJob(); }
    const failures = (result.results || []).filter(item => !item.ok);
    feedback('downloadFeedback',failures.length ? `部分失败 ${failures.length} 项：` + failures.map(item => `${downloadData?.instances?.find(instance => instance.id === item.instance_id)?.name || item.instance_id} / ${item.hash}：${safeMessage(item.error || '操作失败')}`).join('；') : '操作已提交；后台作业状态持续更新。',failures.length ? 'error' : 'success');
    downloadSelection.clear(); await refreshDownloads();
  },'downloadFeedback');
}
$('downloadRefresh').addEventListener('click',() => refreshDownloads(true));
for (const id of ['downloadInstance','downloadClient','downloadScope','downloadState','downloadValidity','downloadSearch','downloadCategory','downloadTag','downloadCleanupRule','downloadCleanupStatus']) $(id).addEventListener($(id).tagName === 'INPUT' ? 'input' : 'change',() => { resetDownloadPosition(); renderDownloads(); if (id === 'downloadScope' && $('downloadScope').value === 'history') refreshHistory(); });
$('transferTarget').addEventListener('change',renderSelection);
$('downloadSelectFiltered').addEventListener('click',() => { for (const item of filteredDownloads()) if (selectable(item)) downloadSelection.add(taskKey(item)); renderDownloads(); });
$('downloadClear').addEventListener('click',() => { downloadSelection.clear(); renderDownloads(); });
for (const [id,name] of [['taskStart','start'],['taskStop','stop'],['taskVerify','verify'],['taskRemove','remove'],['downloadDelete','delete_expired'],['downloadTransfer','transfer']]) $(id).addEventListener('click',() => fleetAction(name));
$('downloadCancel').addEventListener('click',() => { const job = downloadData?.job; if (busy || job?.kind !== 'transfer' || !['running','waiting'].includes(job.status) || !window.confirm('取消未完成转种？恢复原运行状态；新建目的副本暂停保留，已交接项不回退。')) return; transaction(async () => { downloadRevision++; await api('fleet/cancel',{job_id:job.id}); downloadRevision++; feedback('downloadFeedback','取消请求已提交，等待后台恢复。','success'); await refreshDownloads(); },'downloadFeedback'); });
function formValues(form, keys) { const values = {}; for (const key of keys) { const field = form.elements[key]; values[key] = field.type === 'checkbox' ? field.checked : field.type === 'number' ? Number(field.value) : field.value; } return values; }
function fillForm(form,values) { for (const [key,value] of Object.entries(values)) { const field = form.elements[key]; if (!field || !('type' in field)) continue; if (field.type === 'checkbox') field.checked = value === true; else field.value = value ?? ''; } }
function renderCleanup() {
  $('cleanupFields').disabled = !cleanupPolicy || busy;
  text('cleanupPolicyState',cleanupPolicy ? cleanupPolicy.cleanup_enabled || cleanupPolicy.unregistered_enabled ? '有规则启用' : '自动清理关闭' : '尚未查询');
  if (!cleanupPolicy || cleanupDirty) return;
  const values = pendingSettingsValue('policy') || cleanupPolicy;
  fillForm($('cleanupForm'),{...policyDefaults,...values});
  const selected = values.unregistered_instances || [], list = downloadData?.instances || instancesData?.items || [];
  const known = new Set(list.map(item => item.id)); const scope = [...list,...selected.filter(id => !known.has(id)).map(id => ({id,name:'已移除实例 ' + id}))];
  $('cleanupInstances').replaceChildren(...scope.map(item => { const label = element('label',item.name,'checkbox-label'), input = element('input'); input.type = 'checkbox'; input.value = item.id; input.name = 'unregistered_instances'; input.checked = selected.includes(item.id); label.prepend(input); return label; }));
}
async function refreshPolicy() {
  if (!status || policyLoading) return; const generation = queryGeneration, version = cleanupVersion;
  policyLoading = (async () => { try { const result = await api('fleet/policy'); if (generation !== queryGeneration || !status || version !== cleanupVersion) return; if (!cleanupDirty) cleanupPolicy = result.policy || result; renderCleanup(); feedback('cleanupFeedback',cleanupDirty ? '策略已读取；未保存草稿保留。' : '当前清理策略已读取。'); } catch (error) { if (generation === queryGeneration && status) feedback('cleanupFeedback',error.message,'error'); } })().finally(() => { policyLoading = null; }); return policyLoading;
}
$('cleanupRefresh').addEventListener('click',() => { refreshPolicy(); if (!downloadData) refreshDownloads(); });
$('cleanupForm').addEventListener('input',() => { cleanupDirty = true; cleanupVersion++; feedback('cleanupFeedback','有未保存清理策略；刷新保留草稿。'); });
$('cleanupForm').addEventListener('submit',event => { event.preventDefault(); saveAllSettings(); });
async function refreshLogs() {
  if (!status) return; const generation = queryGeneration, version = ++logsVersion;
  try { const result = await api('logs?kind=' + encodeURIComponent($('logKind').value)); if (!status || generation !== queryGeneration || version !== logsVersion) return; renderLogs(result.items || []); feedback('logFeedback','日志已更新。'); } catch (error) { if (status && generation === queryGeneration && version === logsVersion) feedback('logFeedback',error.message,'error'); }
}
function renderLogPolicy() { $('logPolicyFields').disabled = !logPolicy || busy; if (logPolicy && !logPolicyDirty) fillForm($('logPolicyForm'),pendingSettingsValue('logs') || logPolicy); text('logSavedPolicyCaption',!logPolicy ? '保留策略未知' : `已保存策略：保留 ${logPolicy.retention_days ?? '—'} 天 · ${logPolicy.auto_cleanup_enabled ? '每 ' + (logPolicy.cleanup_interval_hours ?? '—') + ' 小时清理过期记录' : '定期清理已关闭'}${logPolicyDirty ? ' · 设置有未保存草稿' : ''}`); }
async function refreshLogPolicy() { if (!status || logPolicyLoading) return; const generation = queryGeneration, version = logPolicyVersion; logPolicyLoading = (async () => { try { const result = await api('logs/settings'); if (!status || generation !== queryGeneration || version !== logPolicyVersion) return; if (!logPolicyDirty) logPolicy = result.policy || result; renderLogPolicy(); feedback('logPolicyFeedback',logPolicyDirty ? '未保存日志策略已保留。' : '当前日志策略已读取。'); } catch (error) { if (status && generation === queryGeneration) feedback('logPolicyFeedback',error.message,'error'); } })().finally(() => { logPolicyLoading = null; }); return logPolicyLoading; }
$('logKind').addEventListener('change',refreshLogs); $('logRefresh').addEventListener('click',refreshLogs); $('logPolicyRefresh').addEventListener('click',refreshLogPolicy);
for (const [id,mode] of [['logDeleteErrors','errors'],['logCleanupExpired','expired']]) $(id).addEventListener('click',() => { if (busy || !window.confirm(mode === 'errors' ? '删除运行与管理日志中的错误记录？恢复资料不受影响。' : '按已保存保留天数清理过期运行与管理日志？')) return; transaction(async () => { await api('logs/cleanup',{mode,confirm:'CLEAR_MANAGED_LOGS'}); await refreshLogs(); feedback('logFeedback','日志清理完成。','success'); },'logFeedback'); });
$('logPolicyForm').addEventListener('input',() => { logPolicyDirty = true; logPolicyVersion++; feedback('logPolicyFeedback','有未保存日志策略；刷新保留输入。'); });
$('logPolicyForm').addEventListener('submit',event => { event.preventDefault(); saveAllSettings(); });
function clearInstanceSecret() { instanceEditVersion++; $('instanceForm').elements.password.value = ''; $('instanceForm').elements.clear_password.checked = false; }
function renderInstanceAvailability() { const fields = $('instanceForm').elements; if (fields.type.value !== 'qb') fields.default.checked = false; $('instanceFields').disabled = busy || !instancesData; $('instanceSave').disabled = busy || !instancesData || instancesConflict || !!status?.running || activeDownloadJob(); $('instanceCheck').disabled = busy || !instancesData; fields.type.disabled = !!editingInstanceId || busy; fields.default.disabled = fields.type.value !== 'qb' || busy; $('instancesRefresh').disabled = busy || !!instancesLoading; }
function renderInstances() {
  $('instanceEmpty').hidden = !instancesData || !!instancesData.items.length;
  const rows = (instancesData?.items || []).map(item => { const card = element('article',undefined,'instance-row'), info = element('div');
    const connection = status?.seedkeep_display?.instances?.find(entry => entry.instance_id === item.id) || downloadData?.instances?.find(entry => entry.id === item.id);
    info.append(element('h3',safeMessage(item.name)),element('p',`${item.type === 'tr' ? 'Transmission · 长期保种' : 'qBittorrent · 下载'} · ${item.enabled ? '启用' : '停用'}${item.default ? ' · 默认补量目的' : ''}`,'hint'));
    info.append(element('span',!item.enabled ? '已停用' : connection?.connected === true ? '连接正常' : connection?.connected === false ? '连接失败' : '连接未知','pill' + (item.enabled && connection?.connected === true ? ' enabled' : item.enabled ? ' unavailable' : '')));
    info.append(element('p',`新任务目录：${safeMessage(item.download_path || '默认')} · 分类：${safeMessage(item.category || '—')} · 标签：${safeMessage(item.tag || '—')}`,'hint'));
    const actions = element('div',undefined,'configuration-actions');
    for (const [caption,handler] of [['改名 / 编辑',() => openInstance(item)],['删除配置',() => deleteInstance(item)]]) { const button = element('button',caption,'button small' + (caption === '删除配置' ? ' danger' : '')); button.type = 'button'; button.disabled = busy || !!status?.running || activeDownloadJob(); button.addEventListener('click',handler); actions.append(button); }
    card.append(info,actions); return card; }); $('instanceList').replaceChildren(...rows); renderInstanceAvailability();
}
function applyInstances(result,acceptRevision = false) {
  const next = {items:(result.items || []).map(item => { const copy = {...item}; delete copy.password; return copy; }),revision:result.revision,login_independent:result.login_independent};
  if ((instanceDirty || instancesConflict) && instancesData && next.revision !== instancesData.revision && !acceptRevision) instancesConflict = true;
  else { instancesData = next; if (acceptRevision && !$('instanceForm').hidden) editorRevision = next.revision; }
  renderInstances(); syncLimitCards();
}
async function refreshInstances(explicit = false) {
  if (!status || busy || instancesLoading) return;
  if (explicit && (instanceDirty || instancesConflict) && !window.confirm('确认刷新实例版本？保留编辑草稿、清除密码输入；读取新版本后请检查再保存。')) return;
  const generation = queryGeneration, version = instancesVersion, edit = instanceEditVersion; instancesLoading = (async () => { feedback('instancesFeedback','正在读取实例配置…'); try { const result = await api('instances'); if (!status || generation !== queryGeneration || version !== instancesVersion) return; if (explicit) { if (edit === instanceEditVersion) clearInstanceSecret(); instancesConflict = false; } applyInstances(result,explicit); feedback('instancesFeedback',instancesConflict ? '实例版本已变更，草稿与旧 revision 保留；请明确刷新实例配置后检查。' : '实例配置已读取。密码不回显。',instancesConflict ? 'error' : ''); refreshLimits(); } catch (error) { if (status && generation === queryGeneration) feedback('instancesFeedback',error.message,'error'); } })().finally(() => { instancesLoading = null; if (generation === queryGeneration) renderInstanceAvailability(); }); renderInstanceAvailability(); return instancesLoading;
}
function openInstance(item = null) {
  if (!instancesData || busy) return; if (instanceDirty && !window.confirm('放弃当前实例未保存草稿并打开其他实例？')) return;
  clearInstanceSecret(); instanceDirty = false; editingInstanceId = item?.id || null; editorRevision = instancesData.revision;
  $('instanceForm').reset(); fillForm($('instanceForm'),item || {type:'qb',enabled:true,default:false,keep_torrent:false,use_proxy:false});
  text('instanceTitle',item ? '编辑下载器 · ' + item.name : '添加下载器'); text('instancePasswordState',item?.password_configured ? '已保存 · 空白保留；密码不回显' : '未保存密码');
  $('instanceForm').hidden = false; feedback('instanceFeedback','只改名称保留稳定 ID，不暂停自动化。身份、端点、默认或启停变化会暂停自动补量与清理；检查草稿不保存。'); renderInstanceAvailability(); $('instanceForm').elements.name.focus();
}
function instancePayload() { const values = formValues($('instanceForm'),['type','name','url','username','password','clear_password','enabled','default','download_path','keep_torrent','use_proxy','proxy_url']); const saved = instancesData?.items.find(item => item.id === editingInstanceId); values.category = saved?.category || ''; values.tag = saved?.tag || ''; for (const key of ['name','url','username','download_path','proxy_url']) values[key] = values[key].trim(); if (editingInstanceId) values.id = editingInstanceId; if (values.type !== 'qb') values.default = false; if (values.password && values.clear_password) throw Error('新密码与明确清空不能同时使用。'); if (values.default && !values.enabled) throw Error('默认补量 qB 必须启用。'); if (values.download_path && !absoluteDirectory(values.download_path)) throw Error('目录必须为绝对目录，不能含 ..；可留空。'); return values; }
async function saveInstance(check = false) {
  if (busy || !instancesData || (!check && (instancesConflict || $('instanceSave').disabled)) || !$('instanceForm').reportValidity()) return;
  let instance; try { instance = instancePayload(); } catch (error) { feedback('instanceFeedback',error.message,'error'); return; }
  const version = instanceEditVersion;
  await transaction(async () => { instancesVersion++; try {
    const result = await api(check ? 'instances/check' : 'instances/save',check ? {instance} : {revision:editorRevision,instance}); instancesVersion++;
    if (check) { if (version === instanceEditVersion) feedback('instanceFeedback',result.connected ? '草稿连接正常，配置未保存。' : '草稿连接失败，请核对地址、凭据和代理。',result.connected ? 'success' : 'error'); return; }
    clearInstanceSecret(); instanceDirty = false; instancesConflict = false; applyInstances(result,true); editorRevision = result.revision; if (!editingInstanceId) $('instanceForm').hidden = true;
    feedback('instancesFeedback',result.automation_paused ? '已保存下载器；自动补量与清理已暂停。' : '下载器已保存；名称立即更新，自动化保持当前状态。','success'); if (result.automation_paused) pauseLocalAutomation(); downloadRevision++; await refreshDownloads(); refreshLimits(); await refresh();
  } catch (error) { if (error.status === 409) { instancesConflict = true; feedback('instanceFeedback','配置冲突或作业繁忙，草稿与旧 revision 保留；请确认刷新实例配置后检查。','error'); } else throw error; } },'instanceFeedback');
}
async function deleteInstance(item) {
  if (busy || !instancesData || activeDownloadJob() || status?.running || instancesConflict) return;
  if (instanceDirty) { feedback('instancesFeedback','请先保存或关闭实例编辑草稿，再删除配置。','error'); return; }
  if (!window.confirm(`删除“${item.name}”的实例配置？\n不删除下载器任务和文件；自动补量与清理会暂停。网页登录凭据不变。`)) return;
  transaction(async () => { instancesVersion++; try { const result = await api('instances/delete',{revision:instancesData.revision,instance_id:item.id,confirm:'REMOVE_INSTANCE_CONFIGURATION'}); instancesVersion++; if (editingInstanceId === item.id) { clearInstanceSecret(); $('instanceForm').hidden = true; editingInstanceId = null; } applyInstances(result,true); pauseLocalAutomation(); downloadRevision++; await refreshDownloads(); feedback('instancesFeedback','实例配置已删除；任务与文件保留。','success'); } catch (error) { if (error.status === 409) instancesConflict = true; throw error; } },'instancesFeedback');
}
function pauseLocalAutomation() { if (status) { status.automatic_enabled = false; renderStatus(); } if (cleanupPolicy) { cleanupPolicy = {...cleanupPolicy,cleanup_enabled:false,unregistered_enabled:false}; renderCleanup(); } }
$('instancesRefresh').addEventListener('click',() => refreshInstances(true)); $('instanceAdd').addEventListener('click',() => openInstance());
$('instanceClose').addEventListener('click',() => { if (instanceDirty && !window.confirm('放弃未保存实例草稿并关闭？')) return; clearInstanceSecret(); instanceDirty = false; $('instanceForm').reset(); $('instanceForm').hidden = true; editingInstanceId = null; });
$('instanceForm').addEventListener('input',() => { instanceDirty = true; instanceEditVersion++; feedback('instanceFeedback','未保存草稿保留；密码不回显。'); renderInstanceAvailability(); });
$('instanceForm').addEventListener('submit',event => { event.preventDefault(); saveInstance(); }); $('instanceCheck').addEventListener('click',() => saveInstance(true));
function syncLimitCards() {
  const items = instancesData?.items || []; for (const [id,draft] of limitDrafts) if (!items.some(item => item.id === id)) { draft.form.remove(); limitDrafts.delete(id); }
  for (const item of items) {
    if (limitDrafts.has(item.id)) { const draft = limitDrafts.get(item.id); draft.heading.textContent = item.name; draft.enabled = item.enabled; updateLimitAvailability(draft); continue; }
    const form = element('form',undefined,'card limits-form'), heading = element('h3',item.name), state = element('span','尚未查询','pill'), head = element('div',undefined,'card-heading'), fields = element('fieldset'), grid = element('div',undefined,'limit-inputs');
    head.append(heading,state); const alternative = element('p','', 'alternative-warning'), schedule = element('p','', 'hint'), message = element('p','先读取限速。','operation-feedback'), save = element('button','保存实例限速','button primary'); save.type = 'submit';
    for (const [key,title] of [['upload_kib','上传（KiB/s）'],['download_kib','下载（KiB/s）']]) { const label = buildControl(key,title,'number',{prefix:'limit_' + item.id + '_',min:0,max:1048576,step:'any',required:true}); grid.append(label); }
    fields.append(grid,buildControl('disable_alternative','同时关闭当前备用限速','checkbox',{prefix:'limit_' + item.id + '_'})); const footer = element('div',undefined,'form-footer'); footer.append(message,save); form.append(head,alternative,schedule,fields,footer);
    const draft = {form,heading,state,fields,alternative,schedule,message,save,dirty:false,version:0,data:null,loading:false,enabled:item.enabled}; limitDrafts.set(item.id,draft); $('instanceLimits').append(form); alternative.hidden = true; updateLimitAvailability(draft);
    form.addEventListener('input',() => { draft.dirty = true; draft.version++; message.textContent = '未保存限速草稿，刷新保留。'; }); form.addEventListener('submit',event => { event.preventDefault(); saveLimit(item.id,draft); }); installFieldHelp(form);
  }
}
function updateLimitAvailability(draft) { draft.fields.disabled = !draft.data?.connected || busy || !draft.enabled; draft.save.disabled = !draft.data?.connected || busy || !draft.enabled; }
function renderLimit(draft,result) {
  const data = result.values; draft.data = data; draft.state.textContent = data.connected ? '已连接' : '未连接'; draft.state.className = 'pill ' + (data.connected ? 'enabled' : 'unavailable');
  draft.alternative.hidden = !data.alternative_enabled; draft.alternative.textContent = `备用限速生效：上传 ${limitText(data.alternative_upload_kib)} · 下载 ${limitText(data.alternative_download_kib)}`;
  draft.schedule.textContent = `备用调度：${data.schedule_enabled ? '开启；后续可能再次切换' : '关闭'} · 回读 ${date(result.checked_at)}。TR 单位以实际回读为准。`;
  if (!draft.dirty) { fillForm(draft.form,{upload_kib:data.upload_kib,download_kib:data.download_kib,disable_alternative:false}); draft.message.textContent = data.connected ? '普通限速已读取；保存后显示实际回读值。' : '连接失败，请检查实例设置。'; }
  updateLimitAvailability(draft);
}
async function refreshLimits() {
  if (!status || !instancesData) return; const generation = queryGeneration, revision = limitsRevision;
  await Promise.allSettled([...limitDrafts].map(async ([id,draft]) => { if (draft.loading) return; draft.loading = true; const version = draft.version; try { const result = await api('fleet/limits?instance_id=' + encodeURIComponent(id)); if (!status || generation !== queryGeneration || revision !== limitsRevision || limitDrafts.get(id) !== draft || version !== draft.version) return; renderLimit(draft,result); } catch (error) { if (status && generation === queryGeneration && limitDrafts.get(id) === draft) { draft.message.textContent = error.message; draft.message.classList.add('error'); } } finally { draft.loading = false; } }));
}
async function saveLimit(id,draft) {
  if (draft.save.disabled || !draft.form.reportValidity()) return; const values = formValues(draft.form,['upload_kib','download_kib','disable_alternative']);
  if ([values.upload_kib,values.download_kib].some(value => value > 0 && value < 1)) { draft.message.textContent = '限速必须为 0 或至少 1 KiB/s。'; return; }
  const generation = queryGeneration;
  transaction(async () => { limitsRevision++; draft.version++; const result = await api('fleet/limits',{instance_id:id,...values}); if (generation !== queryGeneration || limitDrafts.get(id) !== draft) return; limitsRevision++; draft.dirty = false; renderLimit(draft,result.values ? result : {...result,values:result}); draft.message.textContent = '限速已保存并回读验证。'; draft.message.className = 'operation-feedback success'; },'limitsError');
}
$('limitsRefresh').addEventListener('click',refreshLimits);
// Grouped runtime and configuration controls are initialized below before the first request.
let pollingSeconds = null, configurationData = null, configurationDirty = false;
let configurationBusy = false, configurationLoading = null, configurationConflict = false;
let configurationGeneration = 0, configurationEditVersion = 0;
const runtimeGroups = [
  ['补量安全与等待', '在途预留并非确认有效；新鲜度与预留期限可按站端同步频率调整。', [
    ['refill_retry_seconds','补量重试间隔（秒）',60,10,3600,1,'默认 60 秒。补量等待或失败后的重试节奏，不改变每轮上限或在途上限。'],
    ['refill_site_max_age_minutes','站端记录最大年龄（分钟）',120,1,1440,1,'默认 120 分钟。过期站端数据不能作为有效数量进行补量；站端同步时间与网页查询时间不同。'],
    ['refill_reservation_hours','新增预留保留期限（小时）',72,1,720,1,'默认 72 小时。为新增待同步任务保留额度，避免站端延迟期间重复补量；预留不是确认有效。']
  ]],
  ['拉取执行', '默认值适合日常补量。并发和请求间隔会影响站点负载。', [
    ['max_run_seconds','每轮最长运行（秒）',1800,60,86400,1,'默认 1800 秒。限制单轮拉取时长，超时结束本轮；不清理已经添加的任务。'],
    ['pending_grace_seconds','待确认宽限（秒）',300,30,3600,1,'默认 300 秒。给新添加任务留出被下载器确认的时间，避免短暂延迟造成重复补量。'],
    ['download_concurrency','种子下载并发',4,1,32,1,'默认 4。同时下载种子元数据的请求数；遇到站点限流时降低。'],
    ['request_interval_seconds','请求间隔（秒）',1,0.1,60,'any','默认 1 秒。控制站点请求节奏；共享或限流环境建议增大。'],
    ['download_retries','下载尝试次数',3,1,10,1,'默认 3 次。种子下载失败后的尝试上限；不跳过种子验证或永久去重。'],
    ['download_retry_seconds','下载重试间隔（秒）',2,0.5,60,'any','默认 2 秒。失败后等候再重试，站点忙时可适当增加。'],
    ['auto_start','新任务自动开始',true,null,null,'checkbox','默认开启。只决定后续新拉取任务是否自动开始；不改变现有任务的运行状态。']
  ]],
  ['查询与页面', '缓存与页面展示单独调节；任务列表连续滚动，筛选外的选择仍保留。', [
    ['candidate_refresh_seconds','候选刷新间隔（秒）',180,30,3600,1,'默认 180 秒。在补量运行中重新读取 API 和全量 RSS 的间隔；缩短会增加站点负载，闲时不会持续读取全量 RSS。'],
    ['candidate_limit','API 候选返回上限',1000,1,1000,1,'默认 1000。控制 PTS 查询和补量 API 每次请求的候选上限；全量 RSS 不受此数量限制，候选列表不代表最终可拉取数量。'],
    ['pts_cache_seconds','PTS 查询缓存（秒）',60,10,3600,1,'默认 60 秒。站端查询结果缓存时间；强制刷新按钮用于主动重新查询。'],
    ['page_refresh_seconds','网页轮询间隔（秒）',15,5,300,1,'默认 15 秒。保存后立即用于登录会话的状态与下载器轮询，较长间隔降低负载。'],
    ['task_page_size','候选每页数量',50,10,200,1,'默认 50。仅控制候选列表每页数量；任务列表全量读取、连续滚动，不分页。'],
    ['log_limit','日志显示上限',100,20,1000,1,'默认 100。读取和显示最近日志的条数，不删除历史日志。'],
    ['management_cache_seconds','下载器管理缓存（秒）',30,5,300,1,'默认 30 秒。下载器快照缓存时间；强制刷新重新查询，删除资格仍由服务器复核。']
  ]],
  ['有效性与观察', '本地判定独立于站端统计；站点数据不可用或过期时不累计删除等待。', [
    ['monitor_interval_seconds','后台观察间隔（秒）',60,15,600,1,'默认 60 秒。观察下载器及清理资格的周期；只有连续观察到严格超限才累计等待。'],
    ['tracker_fresh_seconds','Tracker 人数有效期（秒）',7200,60,86400,1,'默认 7200 秒。超过此时长的 Tracker 人数不视为新鲜证据；未知不触发删除。'],
    ['seeders_limit_mode','本地人数上限来源','site',null,null,'select','默认跟随 PTS。手动模式只覆盖本地判定上限，不改变站点规则或实际有效保种数；站点查询不可用或过期时，手动模式也不累计删除等待。'],
    ['manual_seeders_max','手动本地人数上限',10,0,100000,1,'默认 10 人，仅手动模式生效。严格大于该人数才算人数失效，等于上限合格。站端统计仍按 PTS 规则计算。']
  ]],
  ['转种与批量操作', '仅已完成任务转种；TR 原生快速接管后确认完整做种再交接，来源文件保留。', [
    ['delete_max_per_job','每批最多移除任务',50,1,200,1,'限制手动及旧自动清理移除任务的批次，只移除任务、保留文件。'],
    ['verify_timeout_seconds','TR 接管最长等待（秒）',21600,60,172800,1,'默认 21600 秒。等待 TR 完整做种回读；兼容旧在途校验作业的等待字段，新流程不额外发起 torrent-verify。'],
    ['pause_timeout_seconds','qB 暂停最长等待（秒）',120,10,600,1,'等待来源暂停确认；超时保留来源。'],
    ['transfer_path_mappings','手动转种路径映射',null,null,null,'textarea','qB 内容目录 => TR 内容目录；两端须挂载同一数据。元数据经 qB 导出 API，无需额外种子缓存目录挂载。清空禁用手动转种。']
  ]],
  ['连接超时', '只调整等待时间；不会绕过身份认证、文件校验或永久去重。', [
    ['site_timeout_seconds','站点查询超时（秒）',15,5,120,1,'默认 15 秒。PTS API 和站点查询的请求等待上限。'],
    ['download_timeout_seconds','种子下载超时（秒）',45,5,300,1,'默认 45 秒。种子元数据下载的单次等待上限；失败后按重试策略处理。'],
    ['qb_timeout_seconds','qB 请求超时（秒）',45,5,120,1,'默认 45 秒。qB 管理请求等待上限，网络较慢时可适当增大。'],
    ['tr_timeout_seconds','TR 请求超时（秒）',20,5,120,1,'Transmission RPC 单次请求等待上限，与接管总等待时限独立。']
  ]]
];
const runtimeFields = runtimeGroups.flatMap(group => group[2]);
const defaultMappings = [{qb:'/downloads',tr:'/downloads'}, {qb:'/media',tr:'/media'}, {qb:'/downloads2',tr:'/downloads2'}];
const configurationGroups = [['PTS 站点', [
  ['api_base','站点 API 地址','url','填写站点 API 基础地址；更改端点或凭据后自动补量和清理暂停。'],
  ['token','站点 Token','password','留空保留。默认遮盖；明确点击显示时才读取已有 Token。离开设置、保存或退出后清除，已有 Token 不轮询。'],
  ['site_use_proxy','站点使用代理','checkbox','只控制站点查询和种子下载代理，不改变下载器实例连接。'],
  ['site_proxy_url','站点代理地址','text','开启且留空时使用系统代理环境；下载器代理在每实例编辑器设置。']
]]];
const fieldHelp = {
  managed_tag:'默认 pts保种组。输入一个非空管理标签，去除首尾空白后最多 128 个字符，不能含控制字符或逗号。精确匹配所有启用实例的标签并跨实例、hash 别名去重；总数包含下载中。修改仅切换管理范围，不改写已有任务标签。由顶部统一保存或取消。',
  dashboard_strategy:'根据已保存的补量口径检查目标、在途预留与可补额度。',
  dashboard_site:'有效数量、账号任务目标和缺额直接读取 PTS 的 current、target、missing。下载中、转种中和新增待同步属于预留，不能当作确认有效；刷新仅查询，站端同步时间可能早于查询时间。',
  dashboard_seedkeep:'精确匹配 settings.managed_tag：qB 使用 tags，TR 使用 labels；同名分类不等于标签。保种总数只计确认已完成任务，包含完成后暂停／上传排队；有效、失效与待确认只在已完成范围判断，不以总数减有效推算失效。真实下载数只计未完成且处于下载／下载队列的任务，未完成暂停、校验或错误不计入任一库存。完成状态无法确认单独列为待确认完成，必要计数显示 —；可信空范围为 0。每实例内部去重，汇总按 hash 及别名跨实例去重；任一已完成副本使该身份归入汇总已完成，其他实例仍按自己的完成状态统计，数字不可直接相加。已完成副本有效性冲突归待确认。人数上限边界与原有效性规则保持。新显示不替代补量、清理库存或在途预留。',
  dashboard_local_validity:'本地有效依据下载器的完成状态、Tracker 回执和人数规则判断；PTS 站端有效由站点返回，两个口径及同步时间独立。',
  dashboard_qb:'qB 全部启用实例库存，包含下载、暂停、排队与做种，同类型同一种子去重；不会仅显示管理标签子集。',
  dashboard_tr:'TR 全部启用实例库存，包含下载、暂停、排队与做种，同类型同一种子去重；不会仅显示管理标签子集。',
  dashboard_remaining:'站端模式显示最近检查的可补额度；本地模式用保种数量目标减去全部标签任务与待确认预留，由服务端计算。总数包含下载中；未知库存不推测缺额。',
  dashboard_automation:'达到触发条件后补至目标，遵守每轮和在途上限。关闭后停止后续调度，当前一轮会继续完成。',
  dashboard_filters:'保种人数上下限均包含；总体积严格小于上限，添加前验证真实种子大小。筛选只影响后续拉取，已有任务保留，已处理 hash 永久排除。',
  dashboard_transfer:'仅下载完成任务可转种。TR 原生快速接管并确认完整做种后逐项交接，移除来源任务始终保留文件。作业数字来自最新已读取规则结果或任务快照，不读取种子名称与 hash。累计已接受是历史新增量，包含已移除任务。',
  dashboard_cleanup:'按分类／标签统计本地库存，与 PTS 站端有效补量分别计算。清理库存沿用原策略口径，包含规则范围内未完成任务，与仪表盘已完成保种总数不同。达到启动线开始清理，降至停止线结束；仅连续失效且通过文件保护核查的任务可处理。自动删除状态和实际检查结果以规则回执为准。',
  min_seeders:'拉取候选最低做种人数，包含此人数。只影响后续拉取筛选，与失效清理的人数上限来源独立。',
  max_seeders:'拉取候选最高做种人数，包含此人数。此筛选不改变 PTS 站点规则，也不决定现有任务是否删除。',
  max_size_mib:'种子真实总大小必须严格小于此值。1 MiB = 1024² 字节；后端保存为 max_bytes，不按文件名估算体积。',
  target:'站端有效模式的本地维持目标，不修改 PTS 站端任务目标；推荐以当前可信站端目标 S 加两档 10% 余量计算。本地数量模式为保种数量目标，按精确管理标签下的全部任务（包含下载中）与待确认预留补齐。',
  max_per_run:'每轮最多新增 1–1000 个，受缺额和在途容量约束。',
  refill_count_basis:'站端有效模式读取 PTS current；本地数量模式按精确管理标签下全部任务计数（包含下载中）与待确认预留补量，有效和失效分别展示。',
  refill_trigger:'低于此线补量；须小于目标且不低于警戒线。',
  refill_floor:'警戒提醒线；必须 0 ≤ 警戒 ≤ 触发 < 目标。',
  refill_check_minutes:'站端有效模式每隔 1–1440 分钟检查，默认 5。该模式不使用旧运行周期；切换回兼容模式仍保留旧周期值。',
  refill_max_inflight:'默认 500，范围 1–10000。限制未完成任务及等待添加回执的新增任务；已完成待站端同步的任务仍占补量预留，不占未完成容量。',
  interval_hours:'仅本地标签数量模式使用的自动运行周期，按服务时区 00 点对齐。站端有效模式不使用，已保存值仍保留。',
  cron_minute:'仅本地数量模式使用，0–59；按服务部署时区计算，切换口径不会丢失已保存值。',
  name:'下载器名称用于任务筛选、转种来源与目的、限速和主界面。建议按用途命名，如“下载 qB”“长期保种 TR”；现有名称可继续使用。只改名保留稳定 ID，不暂停自动化。',
  cleanup_enabled:'默认关闭。开启后只自动移除到期人数失效 PTS 任务，保留文件；未知、暂停或未完成任务不会触发删除，旧 hash 永久排除。',
  cleanup_wait_hours:'单位小时，1–8760。只有连续观察到严格人数超限才累计等待；查询未知或恢复合格会重置。站点数据不可用或过期时不累计。',
  cleanup_max_per_run:'自动清理每轮最多移除 1–50 个任务，实际执行还受运行设置“每批最多删除”的上限限制，取两者较小值。只移除任务，不删除文件。',
  upload_kib:'全局普通上传限速，单位 KiB/s。0 不限速，非零至少 1；1 KiB = 1024 字节。备用模式生效时普通限速不是当前有效值。TR 保存后显示服务器回读的实际值。',
  download_kib:'全局普通下载限速，单位 KiB/s。0 不限速，非零至少 1；备用模式生效时使用备用值。保存后以服务器回读为准。',
  disable_alternative:'默认不勾选，保留备用限速和调度。明确勾选才关闭当前备用模式；已开启的调度仍可在后续再次切换备用模式。',
  web_port:'只读部署信息。修改 Docker WEB_PORT 及对应端口映射并重新创建容器后生效；网页不会代替 Docker 修改部署。',
  timezone:'只读部署信息。修改 Docker TZ 并重新创建容器后生效；自动运行和网页时间显示跟随服务时区。PTS 原始同步记录仍来自站点。',
  username:'使用已有 Transmission 账号登录网页，账号不能在未登录页面读取。',
  password:'输入 Transmission 密码完成登录；密码不会回显，登录成功后清空输入。',
  unregistered_enabled:'默认关闭。只移除可信 Tracker 明确未注册或已删除且满足连续等待的任务，保留文件；候选 API 未返回不构成证据。',
  unregistered_wait_hours:'连续观察未注册报告的等待小时数。认证、通信错误、未知、冲突、恢复注册和监测中断清除等待。',
  unregistered_interval_hours:'定期检查周期，单位小时；删除前重新读取可信 Tracker 报告。',
  unregistered_max_per_run:'每轮自动移除的最大任务数，只移除任务保留文件。',
  unregistered_scope:'默认仅 PTS。选择全部任务会扩大可信 Tracker 未注册清理范围，未知和连接错误仍不删除。',
  auto_cleanup_enabled:'默认关闭。定期清理过期运行与管理日志；不清理操作恢复资料、备份和永久去重记录。',
  retention_days:'保留运行与管理日志的天数；只作用于受管日志。',
  cleanup_interval_hours:'自动日志清理周期，单位小时；与任务清理策略独立。',
  clear_password:'明确勾选才清空此实例已保存密码。空白密码保留旧值，不能同时填写新密码。',
  enabled:'启用此下载器实例。启停变更暂停自动补量与清理；不删除任务与文件。',
  default:'仅 qB 可作为默认补量目的。切换默认会撤销其他 qB 默认，必须同时启用。',
  download_path:'后续新任务的下载器内绝对目录，留空使用默认；不移动已有任务文件。',
  category:'后续新任务分类，只影响之后添加的任务。',
  tag:'后续新任务标签，多个标签用英文逗号分隔；不改变已有任务。',
  keep_torrent:'保留后续下载的种子元数据。关闭不删除已有文件或必要恢复资料。',
  use_proxy:'此实例管理连接使用代理，不设置下载器 peer 或 tracker 代理。',
  proxy_url:'实例管理连接代理地址；启用且留空时使用系统代理环境。仅专用设置页显示。',
  downloadInstance:'默认全部实例。同 hash 在不同实例分别显示和操作，筛选外选择仍保留。',
  downloadState:'参考 qB 的分类，可筛选已完成、下载中、做种中、暂停、排队、校验和错误等。已完成包含排队／暂停做种，不等于本地有效，也不直接授予转种资格。',
  downloadCategory:'包含分类文字的任务，空白表示全部分类。',
  downloadTag:'包含标签文字的任务，空白表示全部标签。',
  candidateSearch:'按名称、说明或 ID 筛选 API 已返回候选。',
  candidateCategory:'只筛选当前返回候选的分类，不修改拉取策略。',
  candidateOnlyMatched:'只展示符合当前已保存人数和体积筛选的候选；仍需要真实种子校验和永久去重。',
  downloadClient:'按下载器类型筛选所有实例，同 hash 在不同实例分别管理。',
  downloadScope:'默认全部任务。可筛选 PTS、管理标签内的保种任务或历史添加资料；标签精确匹配，历史仅用于去重和查看。',
  downloadValidity:'筛选本地有效性，不代表站端逐种认证。未知不会触发删除；暂停／排队、未完成分别显示。',
  downloadSearch:'按名称、ID 或 hash 搜索当前快照，已选任务跨筛选保留。'
};
for (const spec of runtimeFields) fieldHelp[spec[0]] = spec[6];
for (const group of configurationGroups) for (const spec of group[1]) fieldHelp[spec[0]] = spec[3];
function settingNumber(name, fallback) { const value = Number(status?.settings?.[name]); return Number.isFinite(value) && value > 0 ? value : fallback; }
function pageSize() { return settingNumber('task_page_size', 50); }
function deleteBatchSize() { return settingNumber('delete_max_per_job', 50); }
function syncPolling() {
  if (!status) return;
  const seconds = settingNumber('page_refresh_seconds', 15);
  if (timer && pollingSeconds === seconds) return;
  clearInterval(timer); pollingSeconds = seconds;
  timer = setInterval(() => {
    refresh();
    if (page === 'tasks' || activeDownloadJob()) refreshDownloads();
    if (page === 'downloads') refreshLimits();
  }, seconds * 1000);
}
function settingsBlocked() { return busy; }
function pendingRuntimeSettings(settings) { const values = pendingSettingsValue('runtime'); return values ? {...settings,...values,...(values.max_size_mib !== undefined ? {max_bytes:values.max_size_mib*1048576} : {})} : settings; }
function pendingSettingsValue(key) { return ['pending','failed'].includes(settingsPendingDocument?.status) ? settingsPendingDocument.entries?.find(entry => entry.key === key)?.values : null; }
function hasPendingSettings() { return ['pending','failed'].includes(settingsPendingDocument?.status) && settingsPendingDocument.count > 0; }
function renderSettingsPending() {
  const doc = settingsPendingDocument, active = hasPendingSettings(), node = $('settingsPendingFeedback'); node.hidden = !active;
  if (active) { const failed = doc.status === 'failed'; feedback('settingsPendingFeedback',failed ? `已保存的设置尚未生效：${safeMessage(doc.error || '请核对最新版本后重新保存')}。点击顶部刷新核对，再点“保存修改”重试剩余 ${doc.count} 组；已成功分组不会重复提交。` : `已保存 ${doc.count} 组设置，等待当前作业结束后自动生效。本轮继续使用原参数；刷新或离开页面不会丢失已保存设置。` ,failed ? 'error' : ''); }
}
function applyPendingSettings(doc) {
  if (!doc || !Array.isArray(doc.entries) || !Number.isInteger(doc.count)) throw Error('待生效设置响应不完整。');
  settingsPendingDocument = doc; renderSettingsPending();
  if (!status || !hasPendingSettings()) return;
  if (!settingsDirty && pendingSettingsValue('runtime')) fillRuntimeSettings(pendingRuntimeSettings(status.settings));
  if (!configurationDirty && pendingSettingsValue('configuration')) fillForm($('configurationForm'),pendingSettingsValue('configuration').values || {});
  if (!categoryCleanupDirty && pendingSettingsValue('category')) { categoryCleanupDrafts = cleanupClone(pendingSettingsValue('category').rules); for (const rule of categoryCleanupDrafts) ensureCleanupTotals(rule); categoryCleanupSelected = categoryCleanupDrafts.some(rule => rule.id === categoryCleanupSelected) ? categoryCleanupSelected : categoryCleanupDrafts[0]?.id || null; selectCategoryCleanupRule(categoryCleanupSelected,false); }
  if (!cleanupStorageDirty && pendingSettingsValue('storage')) { cleanupStorageDrafts = cleanupClone(pendingSettingsValue('storage').mappings); renderCleanupStorageEditor(); }
  if (!transferRuleDirty && pendingSettingsValue('transfer')) fillTransferRuleForm(pendingSettingsValue('transfer'));
  renderCleanup(); renderLogPolicy(); renderInstanceLabels(); renderCategoryCleanupAvailability();
}
async function refreshPendingSettings() {
  if (!status || settingsPendingLoading || unifiedSaving || settingsReadBusy) return;
  const generation = queryGeneration, revision = settingsRevision;
  const promise = (async () => { try { const doc = await api('settings/pending'); if (!status || generation !== queryGeneration || revision !== settingsRevision) return;
    const wasPending = hasPendingSettings(); applyPendingSettings(doc);
    if (wasPending && !hasPendingSettings() && page === 'settings') await refreshAllSettings(false);
  } catch (error) { if (status && generation === queryGeneration && revision === settingsRevision && !error.stale) feedback('settingsPendingFeedback',error.message,'error'); }
  finally { if (generation === queryGeneration) settingsPendingLoading = null; } })(); settingsPendingLoading = promise; return promise;
}
function updateSettingsAvailability() {
  $('settingsFields').disabled = !status || busy;
  for (const field of document.querySelectorAll('[form="settingsForm"]')) field.disabled = !status || busy;
  $('saveButton').hidden = page !== 'settings'; $('settingsCancel').hidden = page !== 'settings'; document.body.dataset.page = page;
  $('saveButton').disabled = !status || settingsBlocked() || settingsReadBusy;
  $('settingsCancel').disabled = !status || busy || settingsReadBusy;
  $('instanceLabelsFields').disabled = !instanceLabelsDocument || busy || settingsReadBusy;
  text('saveButton',busy && unifiedSaving ? '保存中…' : '保存修改');
  renderSettingsPending();
  updateRecommendationAvailability();
}
function updateManualLimitField() {
  const fields = $('settingsForm').elements;
  fields.manual_seeders_max.disabled = fields.seeders_limit_mode.value !== 'manual';
}
const refillPrimaryFields = [['refill_trigger',0,99999],['refill_floor',0,99999],['refill_check_minutes',1,1440],['refill_max_inflight',1,10000]];
function updateRefillMode() {
  const fields = $('settingsForm').elements, site = fields.refill_count_basis.value === 'site_effective';
  $('managedSchedule').hidden = false;
  text('strategySettingsBadge',settingsDirty ? '未保存草稿' : site ? '站端有效模式' : '本地数量模式');
  $('strategySettingsBadge').className = 'pill' + (settingsDirty ? ' warning' : '');
  text('settingsTargetLabel',site ? '维持目标' : '保种数量目标');
  text('strategyBasisHint',site ? '低于安全线开始补量，补至目标；下载 / 转种 / 待同步占用预留。须满足 0 ≤ 警戒 ≤ 触发 < 目标。' : '按本地标签全部任务（包含下载中）与待确认预留补至保种数量目标。安全线、警戒线及分钟检查不生效，参数仍保留；使用下方本地数量运行计划。');
  for (const key of ['refill_trigger','refill_floor','refill_check_minutes']) fields[key].closest('.help-field').classList.toggle('inactive-field',!site);
}
$('strategyRecommended').addEventListener('click',fillRecommendedSettings);
function fillRuntimeSettings(settings) {
  const fields = $('settingsForm').elements;
  fields.managed_tag.value = settings.managed_tag ?? 'pts保种组';
  for (const key of ['target','max_per_run','min_seeders','max_seeders','interval_hours','cron_minute']) fields[key].value = settings[key];
  const defaults = refillDefaults(settings);
  fields.refill_count_basis.value = settings.refill_count_basis ?? defaults.refill_count_basis;
  for (const [key] of refillPrimaryFields) fields[key].value = settings[key] ?? defaults[key];
  fields.max_size_mib.value = settings.max_bytes / 1048576;
  for (const spec of runtimeFields) {
    const key = spec[0], value = settings[key] ?? (key === 'transfer_path_mappings' ? defaultMappings : spec[2]);
    if (spec[5] === 'checkbox') fields[key].checked = value === true;
    else if (spec[5] === 'textarea') fields[key].value = value.map(mapping => mapping.qb + ' => ' + mapping.tr).join('\n');
    else fields[key].value = value;
  }
  updateManualLimitField();
  updateRefillMode();
}
function absoluteDirectory(value) {
  return !!value && value.length <= 2048 && !/[\x00-\x1f]/.test(value) && value.startsWith('/') && !value.startsWith('//') && !value.split('/').includes('..');
}
function parsePathMappings(value) {
  const lines = value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  if (lines.length > 20) throw Error('路径映射最多 20 行。');
  const seen = new Set();
  return lines.map((line, index) => {
    const pair = line.split('=>').map(part => part.trim());
    if (pair.length !== 2 || !pair.every(absoluteDirectory)) throw Error(`路径映射第 ${index + 1} 行需为容器内绝对目录 => 绝对目录，不能含 .. 或空目录。`);
    const normalized = pair.map(path => '/' + path.split('/').filter(part => part && part !== '.').join('/'));
    if (normalized.includes('/')) throw Error(`路径映射第 ${index + 1} 行不能使用根目录 /。`);
    if (seen.has(normalized[0])) throw Error(`路径映射第 ${index + 1} 行的 qB 目录重复。`);
    seen.add(normalized[0]); return {qb:normalized[0], tr:normalized[1]};
  });
}
function collectRuntimeSettings() {
  const fields = $('settingsForm').elements, values = {};
  values.managed_tag = fields.managed_tag.value.trim();
  if (!values.managed_tag || Array.from(values.managed_tag).length > 128 || /[\x00-\x1f\x7f-\x9f,，]/.test(fields.managed_tag.value)) throw settingsValidationError('管理标签须为单个非空标签，最多 128 个字符，不能含控制字符或逗号。',fields.managed_tag);
  for (const key of ['target','max_per_run','min_seeders','max_seeders','max_size_mib','interval_hours','cron_minute']) values[key] = Number(fields[key].value);
  values.refill_count_basis = fields.refill_count_basis.value;
  if (!['site_effective','managed_tasks'].includes(values.refill_count_basis)) throw Error('请选择有效的补量计数口径。');
  for (const [key,min,max] of refillPrimaryFields) { const input = fields[key], value = Number(input.value); if (!input.value || !Number.isInteger(value) || value < min || value > max) throw Error('长期保种参数超出允许范围，请检查整数与上下限。'); values[key] = value; }
  if (siteEffective(values) && !(values.refill_floor <= values.refill_trigger && values.refill_trigger < values.target)) throw Error('站端有效模式须满足：0 ≤ 警戒线 ≤ 安全触发线 < 维持目标。');
  for (const spec of runtimeFields) {
    const key = spec[0], input = fields[key];
    if (spec[5] === 'checkbox') values[key] = input.checked;
    else if (spec[5] === 'textarea') values[key] = parsePathMappings(input.value);
    else if (spec[5] === 'select') {
      if (!['site','manual'].includes(input.value)) throw Error('本地人数上限来源必须为站点或手动模式。');
      values[key] = input.value;
    }
    else {
      const value = Number(input.value);
      if (!input.value || !Number.isFinite(value) || value < spec[3] || value > spec[4] || (spec[5] === 1 && !Number.isInteger(value))) throw Error(spec[1] + '超出允许范围。');
      values[key] = value;
    }
  }
  return values;
}
function buildControl(name, title, type, options = {}) {
  const label = element('label', title);
  const input = element(type === 'select' ? 'select' : type === 'textarea' ? 'textarea' : 'input');
  input.name = name; input.id = options.prefix + name;
  if (input.tagName === 'INPUT') input.type = type;
  if (type === 'checkbox') label.className = 'checkbox-label';
  if (type === 'password') { input.autocomplete = 'new-password'; input.value = ''; }
  if (type === 'textarea') { input.rows = 5; input.spellcheck = false; label.className = 'wide-field'; }
  if (type === 'select') for (const [value, caption] of [['site','跟随 PTS 站点'], ['manual','手动本地覆盖']]) { const option = element('option', caption); option.value = value; input.append(option); }
  if (options.min != null) { input.min = options.min; input.max = options.max; input.step = options.step; }
  input.required = !!options.required;
  label.append(input);
  if (type === 'password') {
    const state = element('small', '尚未读取保存状态'); state.id = 'secretState_' + name; label.append(state);
  } else if (options.caption) label.append(element('small', options.caption));
  return label;
}
function buildGroupedControls() {
  for (const [title, description, specs] of runtimeGroups) {
    const group = element('section', undefined, 'card settings-group');
    group.append(element('h3', title), element('p', description, 'muted'));
    const grid = element('div', undefined, 'form-grid');
    for (const spec of specs) {
      const type = typeof spec[5] === 'number' || spec[5] === 'any' ? 'number' : spec[5];
      grid.append(buildControl(spec[0], spec[1], type, {prefix:'runtime_', min:spec[3], max:spec[4], step:spec[5], required:type === 'number', caption:type === 'number' ? `默认 ${spec[2]} · 范围 ${spec[3]}–${spec[4]}` : null}));
    }
    group.append(grid); for (const field of grid.querySelectorAll('input,select,textarea')) { field.setAttribute('form','settingsForm'); field.addEventListener('input',markRuntimeDirty); }
    const destination = title === '转种与批量操作' ? 'settingsTransferGroup' : ['补量安全与等待','有效性与观察'].includes(title) ? 'settingsLongTerm' : 'settingsRuntime';
    $(destination).append(group);
    if (title === '转种与批量操作') { const section = element('section',undefined,'card settings-group'), limits = element('div',undefined,'form-grid'); section.append(element('h3','任务移除批次'),element('p','限制旧任务清理与手动移除批次；保留文件。','muted')); limits.append(grid.querySelector('[name="delete_max_per_job"]').closest('label')); section.append(limits); $('settingsLongTerm').append(section); }
    if (title === '连接超时') { const section = element('section',undefined,'card settings-group'), waits = element('div',undefined,'form-grid'); section.append(element('h3','下载器管理请求等待'),element('p','qB 与 TR 单次管理请求超时，接管总等待在上方独立设置。','muted')); for (const name of ['qb_timeout_seconds','tr_timeout_seconds']) waits.append(grid.querySelector(`[name="${name}"]`).closest('label')); section.append(waits); $('settingsTransferGroup').append(section); }
  }
  for (const [title, specs] of configurationGroups) {
    const section = element('section', undefined, 'connection-section'); section.append(element('h3', title));
    const grid = element('div', undefined, 'form-grid connection-grid');
    for (const spec of specs) grid.append(buildControl(spec[0], spec[1], spec[2], {prefix:'config_', required:spec[0] === 'api_base'}));
    section.append(grid); $('configurationGroups').append(section);
  }
}
let tokenGeneration = 0, tokenLoading = false, tokenFetched = false;
function clearConfigurationSecrets() {
  tokenGeneration++; const input = $('configurationForm').elements.token; input.value = ''; input.type = 'password'; tokenFetched = false;
  const button = $('tokenVisibility'); if (button) { button.textContent = '显示'; button.setAttribute('aria-pressed','false'); }
}
function resetConfiguration() {
  configurationGeneration++; configurationEditVersion++; configurationData = null; configurationDirty = false; configurationConflict = false;
  clearConfigurationSecrets(); $('configurationForm').reset(); closeHelp(); $('configurationResults').replaceChildren(); $('configurationResults').hidden = true;
  text('deploymentPort','—'); text('deploymentTimezone','—'); text('secretState_token','尚未读取保存状态'); feedback('configurationFeedback','进入设置页后读取配置。'); renderConfigurationAvailability();
}
function renderConfigurationAvailability() {
  $('configurationFields').disabled = !configurationData || configurationBusy || busy;
  $('configurationCheck').disabled = !configurationData || configurationBusy || busy || !!configurationLoading;
  $('configurationRefresh').disabled = configurationBusy || busy || !!configurationLoading;
  if ($('tokenVisibility')) $('tokenVisibility').disabled = !configurationData || configurationBusy || busy || tokenLoading;
}
function siteConfiguration(result) { const values = {}; for (const [name] of configurationGroups[0][1]) if (name !== 'token') values[name] = result.values?.[name]; return {values,secrets:{token:!!result.secrets?.token},revision:result.revision,deployment:result.deployment || {}}; }
function applyConfiguration(result,preserveDraft = false,acceptRevision = false) {
  const next = siteConfiguration(result);
  if (preserveDraft && configurationData && !acceptRevision) { if (next.revision !== configurationData.revision) configurationConflict = true; }
  else configurationData = next;
  if (!preserveDraft) { fillForm($('configurationForm'),{...next.values,...(pendingSettingsValue('configuration')?.values || {})}); clearConfigurationSecrets(); configurationDirty = false; }
  text('secretState_token',next.secrets.token ? '已保存 · 留空保留' : '未保存'); text('deploymentPort',next.deployment.web_port); text('deploymentTimezone',next.deployment.timezone); renderConfigurationAvailability();
}
async function refreshConfiguration(explicit = false) {
  if (!status || configurationBusy || busy) return; if (configurationLoading) return configurationLoading;
  if (explicit && (configurationDirty || configurationConflict) && !window.confirm('确认读取最新站点版本？保留非秘密草稿，清除 Token 输入。刷新确认后请检查再保存。')) return;
  const session = configurationGeneration, generation = queryGeneration, version = configurationEditVersion;
  configurationLoading = (async () => { feedback('configurationFeedback','正在读取站点配置…'); try {
    const result = await api('configuration'); if (!status || session !== configurationGeneration || generation !== queryGeneration) return;
    const editedSince = version !== configurationEditVersion, preserve = configurationDirty || configurationConflict || editedSince;
    if (explicit && !editedSince) clearConfigurationSecrets(); applyConfiguration(result,preserve,explicit);
    if (explicit || !preserve) configurationConflict = false;
    feedback('configurationFeedback',configurationConflict ? '服务器配置版本已变更。草稿与旧 revision 已保留，请明确刷新站点配置后检查。' : preserve ? '非秘密草稿已保留，站点配置已读取。' : '站点配置已读取，Token 不回显。',configurationConflict ? 'error' : '');
  } catch (error) { if (status && session === configurationGeneration && generation === queryGeneration) feedback('configurationFeedback',error.message,'error'); } })().finally(() => { configurationLoading = null; if (generation === queryGeneration) renderConfigurationAvailability(); }); renderConfigurationAvailability(); return configurationLoading;
}
function configurationPayload() { const values = formValues($('configurationForm'),['api_base','site_use_proxy','site_proxy_url','token']); values.api_base = values.api_base.trim(); values.site_proxy_url = values.site_proxy_url.trim(); if (tokenFetched) values.token = ''; return {values,revision:configurationData.revision}; }
async function configurationAction(checkOnly) {
  if (!configurationData || configurationBusy || busy || configurationLoading || (!checkOnly && configurationConflict) || !$('configurationForm').reportValidity()) return;
  if (!checkOnly && !window.confirm('保存站点配置？端点或凭据变化会暂停自动补量与清理；Token 留空保留。')) return;
  const payload = configurationPayload(), generation = queryGeneration;
  tokenGeneration++; if (!checkOnly) { if (tokenFetched) clearConfigurationSecrets(); else { $('configurationForm').elements.token.type = 'password'; $('tokenVisibility').textContent = '显示'; $('tokenVisibility').setAttribute('aria-pressed','false'); } }
  await transaction(async () => { configurationBusy = true; configurationGeneration++; const session = configurationGeneration; renderConfigurationAvailability(); $('configurationResults').hidden = true;
    feedback('configurationFeedback',checkOnly ? '正在检查站点草稿…' : '正在保存站点配置…');
    try { const result = await api(checkOnly ? 'configuration/check' : 'configuration',payload); if (!status || generation !== queryGeneration || session !== configurationGeneration) return;
      if (checkOnly) { const connected = result.site?.connected; $('configurationResults').replaceChildren(element('li',connected ? 'PTS：草稿连接正常，未保存。' : 'PTS：连接失败，请核对站点地址、Token 与代理。',connected ? 'success' : 'error')); $('configurationResults').hidden = false; feedback('configurationFeedback','站点草稿检查完成，未写入配置。'); }
      else { clearConfigurationSecrets(); configurationConflict = false; applyConfiguration(result.configuration || result); settingsRevision++; downloadRevision++; if (result.automation_paused) pauseLocalAutomation(); feedback('configurationFeedback',result.automation_paused ? '站点配置已保存；自动补量与清理暂停。' : '站点配置已保存。','success'); refresh(); }
    } catch (error) { if (generation !== queryGeneration || !status) return; if (error.status === 409) { configurationConflict = true; feedback('configurationFeedback','配置冲突或作业繁忙，草稿与旧 revision 保留。请确认刷新站点配置后检查再保存。','error'); } else throw error; }
    finally { if (generation === queryGeneration) configurationBusy = false; }
  },'configurationFeedback');
}
function installTokenVisibility() {
  const input = $('configurationForm').elements.token, button = element('button','显示','button small token-visibility'); button.type = 'button'; button.id = 'tokenVisibility'; button.setAttribute('aria-pressed','false'); button.setAttribute('aria-label','显示或隐藏站点 Token'); input.closest('label').after(button);
  button.addEventListener('click',async () => {
    if (tokenLoading || busy || !configurationData || page !== 'settings') return;
    if (input.type === 'text') { input.type = 'password'; button.textContent = '显示'; button.setAttribute('aria-pressed','false'); return; }
    if (input.value) { input.type = 'text'; button.textContent = '隐藏'; button.setAttribute('aria-pressed','true'); return; }
    const generation = queryGeneration, version = tokenGeneration, edit = configurationEditVersion; tokenLoading = true; button.disabled = true;
    try { const result = await api('configuration/token',{}); if (!status || page !== 'settings' || generation !== queryGeneration || version !== tokenGeneration || edit !== configurationEditVersion || input.value) return;
      input.value = result.token || ''; tokenFetched = true; input.type = 'text'; button.textContent = '隐藏'; button.setAttribute('aria-pressed','true');
    } catch (error) { if (status && generation === queryGeneration && page === 'settings') feedback('configurationFeedback','无法读取 Token，请稍后重试。','error'); }
    finally { if (generation === queryGeneration) { tokenLoading = false; renderConfigurationAvailability(); } }
  });
  input.addEventListener('input',() => { tokenGeneration++; tokenFetched = false; });
}
$('configurationForm').addEventListener('input',() => { configurationDirty = true; configurationEditVersion++; $('configurationResults').hidden = true; feedback('configurationFeedback','有未保存站点草稿，刷新保留非秘密输入。'); });
$('configurationForm').addEventListener('submit',event => { event.preventDefault(); saveAllSettings(); });
$('configurationRefresh').addEventListener('click',() => refreshConfiguration(true)); $('configurationCheck').addEventListener('click',() => configurationAction(true));
function updateDashboardHelp(key,message) { const button = document.querySelector(`#dashboard [data-help="${key}"]`), panel = button && $(button.getAttribute('aria-controls')); if (panel) { const copy = panel.querySelector('.help-copy'); if (copy) copy.textContent = message; if (currentHelp?.panel === panel) positionHelp(); } }
function renderDashboardTransferSummary() {
  const job = downloadData?.job?.kind === 'transfer' ? downloadData.job : null, result = transferRuleDocument?.runtime?.last_result;
  const active = job && ['running','waiting'].includes(job.status), useJob = job && (active || !result || Number(job.finished_at || job.started_at) >= Number(result.finished_at || 0));
  let value = '暂无记录', failed = false, waiting = false, completedCount = null, failedCount = null, state = '';
  if (useJob) {
    const items = job.items || [], count = phases => items.filter(item => phases.includes(item.phase)).length;
    value = `${jobStatusNames[job.status] || job.status} · 总数 ${items.length} · 完成 ${count(['completed','done','transferred'])} · 失败 ${count(['failed','interrupted'])} · 跳过 ${count(['skipped'])} · 取消 ${count(['cancelled'])} · 等待 ${items.filter(item => !['completed','done','transferred','failed','interrupted','skipped','cancelled'].includes(item.phase)).length}`;
    completedCount = count(['completed','done','transferred']); failedCount = count(['failed','interrupted']); state = job.status;
    failed = job.status === 'failed' || count(['failed','interrupted']) > 0; waiting = job.status === 'waiting';
  } else if (result) { value = [jobStatusNames[result.status] || result.status, numericSummary(result,{total:'总数',completed:'完成',failed:'失败',skipped:'跳过',cancelled:'取消',waiting:'等待',source_removed:'来源移除',source_kept:'来源保留'}),result.finished_at ? date(result.finished_at) : ''].filter(Boolean).join(' · '); failed = result.status === 'failed' || result.failed > 0; waiting = result.status === 'waiting'; completedCount = displayCount(result.completed); failedCount = displayCount(result.failed); state = result.status; }
  text('dashboardTransferSummary',value); $('dashboardTransferSummary').classList.toggle('error',failed); $('dashboardTransferSummary').classList.toggle('waiting',waiting && !failed);
  text('dashboardTransferCompleted',completedCount); text('dashboardTransferFailed',failedCount); text('dashboardTransferStatus',state ? jobStatusNames[state] || '状态未知' : '暂无记录'); $('dashboardTransferStatus').hidden = !state || state === 'completed'; $('dashboardTransferStatus').parentNode.title = value;
  updateDashboardHelp('dashboard_transfer',fieldHelp.dashboard_transfer + ' 已保存规则：' + $('dashboardTransfer').textContent + '；' + $('dashboardTransferRoute').textContent + '。最近结果：' + value);
}
let currentHelp = null, helpSequence = 0, helpCloseTimer = null;
function closeHelp() { clearTimeout(helpCloseTimer); if (!currentHelp) return; currentHelp.panel.hidden = true; currentHelp.button.setAttribute('aria-expanded','false'); currentHelp.pinned = false; currentHelp = null; }
function positionHelp() {
  if (!currentHelp) return; const {button,panel} = currentHelp, anchor = button.getBoundingClientRect();
  const viewport = window.visualViewport, width = viewport?.width || innerWidth, height = viewport?.height || innerHeight, left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
  panel.style.maxWidth = Math.max(0,width-24) + 'px'; panel.style.maxHeight = Math.max(0,height-24) + 'px'; const box = panel.getBoundingClientRect();
  const x = Math.max(left+12,Math.min(anchor.right-box.width,left+width-box.width-12));
  const below = anchor.bottom+8, y = below+box.height <= top+height-12 ? below : anchor.top-box.height-8;
  panel.style.left = x + 'px'; panel.style.top = Math.max(top+12,Math.min(y,top+height-box.height-12)) + 'px';
}
function wireHelp(button,wrapper,message) {
  const panel = element('div',undefined,'help-panel'); panel.append(element('p',message,'help-copy')); panel.id = 'fieldHelp_' + (++helpSequence); panel.hidden = true; panel.setAttribute('role','tooltip'); panel.tabIndex = 0; document.body.append(panel);
  const actionId = button.dataset.help === 'dashboard_site' ? 'ptsRefreshButton' : button.dataset.help === 'dashboard_seedkeep' ? 'seedkeepRefreshButton' : null;
  if (actionId && $(actionId)?.parentNode.id === 'dashboardRefreshActions') { const actions = element('div',undefined,'help-actions'); actions.append($(actionId)); panel.append(actions); panel.setAttribute('role','region'); panel.setAttribute('aria-label',button.getAttribute('aria-label') || '统计说明与刷新'); }
  button.setAttribute('aria-controls',panel.id); button.setAttribute('aria-describedby',panel.id); button.setAttribute('aria-expanded','false');
  const state = {button,panel,wrapper,pinned:false};
  function open() { clearTimeout(helpCloseTimer); if (currentHelp !== state) closeHelp(); currentHelp = state; panel.hidden = false; button.setAttribute('aria-expanded','true'); positionHelp(); }
  function laterClose() { clearTimeout(helpCloseTimer); helpCloseTimer = setTimeout(() => { if (currentHelp === state && !state.pinned && document.activeElement !== button && !panel.contains(document.activeElement)) closeHelp(); },180); }
  button.addEventListener('pointerenter',event => { if (event.pointerType !== 'touch') open(); }); button.addEventListener('focus',open);
  button.addEventListener('click',() => { if (state.pinned) closeHelp(); else { open(); state.pinned = true; } });
  button.addEventListener('pointerleave',laterClose); panel.addEventListener('pointerenter',() => clearTimeout(helpCloseTimer)); panel.addEventListener('pointerleave',laterClose);
  button.addEventListener('blur',laterClose); panel.addEventListener('focus',open); panel.addEventListener('blur',laterClose);
}
function installFieldHelp(root = document) {
  for (const control of root.querySelectorAll('input, select, textarea')) {
    if (control.closest('#downloadRows') || control.closest('#transferRulesForm') || control.closest('.help-field')) continue;
    const key = control.name || control.id, message = control.closest('#instanceForm') && ['username','password'].includes(key) ? (key === 'password' ? '只设置此实例的密码。空白保留已存值，明确清空需单独勾选；不改变网页登录凭据。' : '此下载器实例的认证用户名，只在专用设置页读取与显示，不改变网页登录账号。') : fieldHelp[key];
    if (!message) continue;
    let label = control.closest('label');
    const wrapper = element('div', undefined, 'help-field');
    if (!label) {
      control.parentNode.insertBefore(wrapper, control); label = element('label');
      const title = control.getAttribute('aria-label') || control.placeholder || key;
      label.append(element('span', title, 'sr-only'), control); wrapper.classList.add('compact-help'); wrapper.append(label);
    } else { label.parentNode.insertBefore(wrapper, label); wrapper.append(label); }
    if (label.classList.contains('wide-field')) wrapper.classList.add('wide-field');
    const title = [...label.childNodes].filter(node => node.nodeType === 3).map(node => node.textContent.trim()).join('') || label.querySelector('.field-title')?.textContent || control.getAttribute('aria-label') || control.placeholder || key;
    const textNodes = [...label.childNodes].filter(node => node.nodeType === 3);
    if (textNodes.length) {
      const caption = element('span', title, 'field-title');
      for (const node of textNodes) node.remove();
      label.insertBefore(caption, control.type === 'checkbox' ? control.nextSibling : control);
    }
    const button = element('button', '?', 'help-button'); button.type = 'button'; button.setAttribute('aria-label', '查看' + title + '说明');
    wrapper.append(button); wireHelp(button, wrapper, message);
  }
  for (const button of root.querySelectorAll('button[data-help]')) if (!button.hasAttribute('aria-controls')) wireHelp(button,button.parentNode,fieldHelp[button.dataset.help]);
}
document.addEventListener('click',event => { if (currentHelp && !currentHelp.wrapper.contains(event.target) && !currentHelp.panel.contains(event.target)) closeHelp(); });
document.addEventListener('keydown',event => { if (event.key === 'Escape' && currentHelp) { event.preventDefault(); const {button,panel} = currentHelp, focused = panel.contains(document.activeElement); closeHelp(); if (focused) button.focus(); closeHelp(); } });
window.addEventListener('resize',positionHelp); document.addEventListener('scroll',positionHelp,true); window.visualViewport?.addEventListener('resize',positionHelp);
window.addEventListener('resize',() => { if (page === 'tasks' && downloadData) renderDownloadHead(); });
// Rules have their own request/edit versions; polling never rebases an unsaved draft.
const transferRuleArrays = ['include_categories','include_tags','exclude_tags','excluded_dirs','target_labels'];
const transferRuleBooleans = ['enabled','notify','include_untagged','start_after_verify','delete_source','delete_duplicate_source'];
function resetTransferRules() {
  transferRuleRequestVersion++; transferRuleEditVersion++;
  transferRuleDocument = null; transferRuleDirty = false; transferRuleConflict = false;
  transferRuleLoading = null; transferRulePending = ''; transferRulePreview = null; transferRuleNoticeKey = null;
  $('transferRulesForm').reset();
  for (const name of ['source_instance_id','target_instance_id']) $('transferRulesForm').elements[name].replaceChildren();
  $('transferRulesCounts').replaceChildren(); $('transferRulesItems').replaceChildren();
  text('transferRulesPreviewStatus','尚未预览。修改草稿后请重新预览。');
  text('transferRulesRuntime',''); feedback('transferRulesNotice',''); feedback('transferRulesFeedback','先读取规则，再预览或保存。');
  renderTransferRuleAvailability();
}
function transferRuleInstances(type) { return (instanceLabelsDocument?.items || downloadData?.instances || publicDownloaders()).filter(item => item.type === type && item.enabled !== false); }
function fillTransferRuleInstanceOptions(values = null) {
  const form = $('transferRulesForm');
  for (const [name,type] of [['source_instance_id','qb'],['target_instance_id','tr']]) {
    const field = form.elements[name], value = values ? values[name] || '' : transferRuleDocument ? field.value : '';
    field.replaceChildren(option('',type === 'qb' ? '选择启用的 qB 来源' : '选择启用的 TR 目的'));
    for (const item of transferRuleInstances(type)) field.append(option(item.id,downloaderLabel(item)));
    if (value && ![...field.options].some(item => item.value === value)) field.append(option(value,'已停用或不可用的实例（' + value + '）'));
    field.value = value;
  }
}
function fillTransferRuleForm(values) {
  fillTransferRuleInstanceOptions(values);
  fillForm($('transferRulesForm'),values);
  for (const name of transferRuleArrays) $('transferRulesForm').elements[name].value = (values[name] || []).join('\n');
  $('transferRulesForm').elements.path_mappings.value = (values.path_mappings || []).map(item => item.qb + ' => ' + item.tr).join('\n');
  syncTransferCronPreset();
}
function collectTransferRuleValues() {
  const form = $('transferRulesForm'), lines = name => form.elements[name].value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  const values = formValues(form,[...transferRuleBooleans,'source_instance_id','target_instance_id','cron']);
  for (const name of transferRuleArrays) values[name] = lines(name);
  const path = value => value.length <= 2048 && value.startsWith('/') && value !== '/' && !value.includes('\\') && !value.split('/').some(part => part === '..' || part === '.') && !/[\x00-\x1f]/.test(value);
  if (values.excluded_dirs.some(value => !path(value))) throw Error('排除目录必须是 Linux 绝对目录，不能是根目录或含 . / ..。');
  values.path_mappings = lines('path_mappings').map((line,index) => {
    const parts = line.split('=>').map(value => value.trim());
    if (parts.length !== 2 || parts.some(value => !path(value))) throw Error(`第 ${index + 1} 行路径映射格式错误，请填写 qB 绝对目录 => TR 绝对目录。`);
    return {qb:parts[0],tr:parts[1]};
  });
  if (values.path_mappings.length > 20) throw Error('路径映射最多 20 行。');
  values.cron = validateTransferCron(values.cron);
  for (const name of ['include_categories','include_tags','exclude_tags','target_labels']) if (values[name].length > 50 || values[name].some(value => value.length > 100 || value.includes(',') || /[\x00-\x1f]/.test(value))) throw settingsValidationError('转种分类/标签每组最多50项，每项最多100字且不含逗号或控制字符。',form.elements[name]);
  if (values.excluded_dirs.length > 50) throw settingsValidationError('排除目录最多50项。',form.elements.excluded_dirs);
  return values;
}
function transferRuleRunnable(document) {
  return document?.saved === true && document.revision != null && !!document.values?.path_mappings?.length && !!document.values.source_instance_id && !!document.values.target_instance_id;
}
function renderTransferRuleAvailability() {
  const pending = !!transferRulePending, loading = !!transferRuleLoading;
  const blocked = !status || busy || pending || !!status?.running || activeDownloadJob() || transferRuleDocument?.busy === true;
  $('transferRulesOpen').disabled = !status;
  $('transferRulesFields').disabled = !status || !transferRuleDocument || busy || !!transferRulePending;
  $('transferRulesRefresh').disabled = !status || loading || pending;
  $('transferRulesPreview').disabled = !status || !transferRuleDocument || pending || loading;
  $('transferRulesRun').disabled = blocked || loading || !transferRuleRunnable(transferRuleDocument);
  $('taskRulesRun').disabled = $('transferRulesRun').disabled;
  const selected = selectedDownloads();
  $('transferRuleSelected').disabled = blocked || loading || !downloadFresh || !transferRuleRunnable(transferRuleDocument) || !selected.length || selected.length !== downloadSelection.size || !selected.every(item => selectable(item) && item.client === 'qb' && item.completed === true && item.instance_id === transferRuleDocument?.values.source_instance_id);
  $('transferRulesPanel').setAttribute('aria-busy',String(pending || loading));
  const label = !transferRuleDocument ? loading ? '读取规则中…' : '尚未读取' : transferRuleConflict ? '版本冲突 · 草稿保留' : transferRuleDirty ? '未保存草稿' : transferRuleDocument.saved ? transferRuleDocument.values.enabled ? '自动转种已开启' : '自动转种关闭' : '自动转种关闭 · 尚未保存';
  text('transferRulesState',label); $('transferRulesState').classList.toggle('enabled',!!transferRuleDocument?.values.enabled && !transferRuleDirty);
  text('transferRulesPreview',transferRulePending === 'preview' ? '预览中…' : '预览草稿');
  text('transferRulesRun',transferRulePending === 'run' ? '核查并提交中…' : '运行已保存规则一次');
  text('taskRulesRun',transferRulePending === 'run' ? '核查并提交中…' : '运行已保存规则一次');
  if (transferRuleDocument) {
    const runtime = transferRuleDocument.runtime || {};
    text('transferRulesRuntime',`已保存版本 ${transferRuleDocument.revision ?? '—'} · 下一轮 ${date(runtime.next_run_at)} · 上次运行 ${date(runtime.last_run_at)} · 累计完成 ${runtime.completed_count ?? 0}${blocked && status ? ' · 当前有作业或操作待完成，保存与运行暂不可用' : ''}`);
    text('transferRulesTimezone',`${transferRuleDocument.timezone || displayTimezone} · 五段：分 时 日 月 星期；0–6 为周一至周日，支持英文星期名。`);
    renderDashboardDownloaders();
  }
  if (!transferRuleDocument) renderDashboardDownloaders();
}
function renderTransferRuleCompletion(runtime) {
  const result = runtime?.last_result;
  if (!result || result.notify !== true) return;
  const key = String(result.job_id) + ':' + String(result.finished_at) + ':' + String(result.status);
  if (key === transferRuleNoticeKey) return;
  transferRuleNoticeKey = key;
  feedback('transferRulesNotice',`转种规则${jobStatusNames[result.status] || result.status}：完成 ${result.completed ?? 0} · 失败 ${result.failed ?? 0} · 跳过 ${result.skipped ?? 0} · 取消 ${result.cancelled ?? 0} · 来源移除 ${result.source_removed ?? 0} · 来源保留 ${result.source_kept ?? 0}。`,result.failed ? 'error' : 'success');
}
function applyTransferRuleDocument(result,explicit = false) {
  if (!result?.values || result.revision == null || result.instances_revision == null) throw Error('规则响应缺少配置或版本资料，未覆盖当前草稿与版本。');
  renderTransferRuleCompletion(result.runtime);
  if (transferRuleDocument && transferRuleDirty) {
    const changed = result.revision !== transferRuleDocument.revision || result.instances_revision !== transferRuleDocument.instances_revision;
    if (changed && !explicit) transferRuleConflict = true;
    transferRuleDocument = {...transferRuleDocument,runtime:result.runtime,busy:result.busy,timezone:result.timezone,...(explicit ? {revision:result.revision,instances_revision:result.instances_revision,saved:result.saved,values:result.values} : {})};
    if (explicit) transferRuleConflict = false;
    if (explicit) fillTransferRuleInstanceOptions();
  } else {
    transferRuleDocument = result; transferRuleConflict = false; fillTransferRuleForm(pendingSettingsValue('transfer') || result.values);
  }
  renderTransferRuleAvailability();
}
async function refreshTransferRules(explicit = false) {
  if (!status || transferRuleLoading || transferRulePending) return transferRuleLoading;
  const generation = queryGeneration, request = transferRuleRequestVersion, edit = transferRuleEditVersion;
  const promise = (async () => {
    try {
      const result = await api('fleet/transfer/settings');
      if (!status || generation !== queryGeneration || request !== transferRuleRequestVersion) return;
      applyTransferRuleDocument(result,explicit && edit === transferRuleEditVersion);
      if ((explicit && edit === transferRuleEditVersion) || !transferRuleDirty) feedback('transferRulesFeedback',transferRuleDirty ? '已明确刷新最新版本；未保存输入保留，请核对后再次保存。' : '规则已读取。预览只读，保存不会启动转种。');
      if (transferRuleConflict) feedback('transferRulesFeedback','已保存规则或实例版本变更；草稿与旧 revision 保留。请明确刷新已保存规则，核对后再保存。','error');
      if (edit !== transferRuleEditVersion && !transferRuleDirty) transferRuleDirty = true;
    } catch (error) { if (status && generation === queryGeneration && request === transferRuleRequestVersion && !error.stale) feedback('transferRulesFeedback',error.message + '；保留输入，请重试。','error'); }
    finally { if (generation === queryGeneration && request === transferRuleRequestVersion) { transferRuleLoading = null; renderTransferRuleAvailability(); } }
  })();
  transferRuleLoading = promise; renderTransferRuleAvailability(); return promise;
}
function clearTransferRulePreview(message) {
  transferRulePreview = null; $('transferRulesCounts').replaceChildren(); $('transferRulesItems').replaceChildren(); text('transferRulesPreviewStatus',message);
}
function renderTransferRulePreview(result) {
  transferRulePreview = result;
  const labels = {source_total:'来源总数',completed:'已完成',matched:'匹配规则',eligible:'可执行',selected:'进入队列',target_existing:'目的已有',already_processed:'已处理',skipped:'跳过'};
  $('transferRulesCounts').replaceChildren(...Object.entries(labels).map(([key,label]) => { const node = element('div'); node.append(element('dt',label),element('dd',result.counts?.[key] ?? 0)); return node; }));
  const items = (result.items || []).slice(0,100);
  $('transferRulesItems').replaceChildren(...items.map((item,index) => {
    const row = element('li'); row.style.setProperty('--rule-delay',Math.min(index,5) * 20 + 'ms');
    row.append(element('b',item.name),element('span',item.eligible ? '可执行' : '跳过','pill ' + (item.eligible ? 'enabled' : '')));
    row.append(element('p',`${item.state_text || '—'}${item.target_exists ? ' · 目的已有同 hash' : ''} · ${item.reason || (item.eligible ? '符合规则' : '不满足执行条件')}`));
    return row;
  }));
  text('transferRulesPreviewStatus',`${date(result.checked_at)} · ${items.length ? `展示 ${items.length} 项` : '没有匹配的预览任务'}${result.truncated ? ' · 结果已截断，最多展示 100 项' : ''} · 只读结果；执行前服务器将重新核查。`);
}
async function previewTransferRules() {
  if ($('transferRulesPreview').disabled || !$('transferRulesForm').reportValidity()) return;
  let values; try { values = collectTransferRuleValues(); } catch (error) { feedback('transferRulesFeedback',error.message,'error'); return; }
  const generation = queryGeneration, edit = transferRuleEditVersion, request = ++transferRuleRequestVersion;
  transferRulePending = 'preview'; clearTransferRulePreview('正在读取两端任务，只读预览…'); renderTransferRuleAvailability();
  try {
    const result = await api('fleet/transfer/preview',{values});
    if (!status || generation !== queryGeneration || request !== transferRuleRequestVersion || edit !== transferRuleEditVersion) return;
    renderTransferRulePreview(result); feedback('transferRulesFeedback','草稿预览完成；没有保存或转种。','success');
  } catch (error) { if (status && generation === queryGeneration && request === transferRuleRequestVersion && edit === transferRuleEditVersion) { clearTransferRulePreview('预览失败，未取得可用结果；请重试。'); feedback('transferRulesFeedback',error.message,'error'); } }
  finally { if (generation === queryGeneration && request === transferRuleRequestVersion) { transferRulePending = ''; renderTransferRuleAvailability(); } }
}
async function runTransferRules(selectedOnly = false) {
  const button = $(selectedOnly ? 'transferRuleSelected' : 'transferRulesRun'); renderTransferRuleAvailability(); if (button.disabled) return;
  const refs = selectedOnly ? selectedDownloads().map(({instance_id,hash}) => ({instance_id,hash})) : null;
  const generation = queryGeneration, request = ++transferRuleRequestVersion;
  transferRulePending = 'run'; renderTransferRuleAvailability();
  const targetFeedback = selectedOnly || page === 'tasks' ? 'downloadFeedback' : 'transferRulesFeedback';
  try {
    const fresh = await api('fleet/transfer/settings');
    if (!status || generation !== queryGeneration || request !== transferRuleRequestVersion) return;
    applyTransferRuleDocument(fresh);
    if (!transferRuleRunnable(fresh)) throw Error('需先保存包含路径映射、启用 qB 来源与 TR 目的的规则。');
    if (fresh.busy || busy || status.running || activeDownloadJob()) throw Error('当前有作业正在执行，请完成后再试。');
    if (refs && refs.some(item => item.instance_id !== fresh.values.source_instance_id)) throw Error('所选任务不符合最新已保存规则的来源；选择保留，请核对规则。');
    const source = downloadData?.instances?.find(item => item.id === fresh.values.source_instance_id), target = downloadData?.instances?.find(item => item.id === fresh.values.target_instance_id);
    const message = `使用最新已保存规则（版本 ${fresh.revision}）${selectedOnly ? `依序转种所选 ${refs.length} 项，全部必须符合规则，否则整批拒绝` : '运行一次，所有符合规则的已完成任务依次进入队列'}？\n来源 ${source?.name || fresh.values.source_instance_id} → ${target?.name || fresh.values.target_instance_id}\n每项由 TR 原生快速接管并确认完整做种后交接，再处理下一项；随后${fresh.values.delete_source ? '移除' : '保留'}来源任务。目的重复时${fresh.values.delete_duplicate_source ? '确认目的完整做种后移除重复来源任务' : '跳过并保留来源'}。全部文件保留。\n未保存草稿不会执行，手动运行不要求开启自动调度。`;
    if (!window.confirm(message)) return;
    const body = {revision:fresh.revision,confirm:'RUN_QB_TO_TR_RULE_KEEP_DATA',...(refs ? {tasks:refs} : {})};
    const result = await api('fleet/transfer/run',body);
    if (!status || generation !== queryGeneration || request !== transferRuleRequestVersion) return;
    if (result.job) { downloadData = {...downloadData,job:result.job}; renderDownloadJob(); }
    feedback(targetFeedback,result.message || (result.job ? '规则转种已提交，后台作业状态持续更新。' : '本轮没有可执行任务；未启动转种作业。'),'success');
    if (refs) { for (const ref of refs) downloadSelection.delete(taskKey(ref)); renderSelection(); }
    await refreshDownloads();
  } catch (error) {
    if (status && generation === queryGeneration && request === transferRuleRequestVersion) {
      if (error.status === 409) { transferRuleConflict = true; transferRuleDirty = true; }
      feedback(targetFeedback,error.status === 409 ? '版本冲突或作业繁忙，未执行；草稿与选择保留，请明确刷新规则后重试。' : error.message,'error');
    }
  } finally { if (generation === queryGeneration && request === transferRuleRequestVersion) { transferRulePending = ''; renderTransferRuleAvailability(); } }
}
function syncTransferCronPreset() { const value = $('transferRulesForm').elements.cron.value.trim(); $('transferCronPreset').value = ['*/5 * * * *','*/15 * * * *','0 * * * *'].includes(value) ? value : 'custom'; }
$('transferCronPreset').addEventListener('change',() => { const preset = $('transferCronPreset').value, cron = $('transferRulesForm').elements.cron; if (preset !== 'custom') { cron.value = preset; cron.dispatchEvent(new Event('input',{bubbles:true})); } else cron.focus(); });
$('transferRulesForm').elements.cron.addEventListener('input',syncTransferCronPreset);
function openTransferRules() { if (!status) return; if (page !== 'settings') navigate('settings'); closeHelp(); fillTransferRuleInstanceOptions(); $('settingsTransferGroup').scrollIntoView({block:'start'}); $('transferRulesRefresh').focus({preventScroll:true}); refreshTransferRules(); }
for (const id of ['transferRulesOpen','dashboardRulesOpen','settingsRulesOpen']) $(id).addEventListener('click',openTransferRules);
$('transferRulesRefresh').addEventListener('click',() => refreshTransferRules(true));
$('transferRulesForm').addEventListener('input',() => { transferRuleDirty = true; transferRuleEditVersion++; clearTransferRulePreview('草稿已变更，旧预览已清除。'); feedback('transferRulesFeedback','未保存草稿；刷新保留输入。执行只使用已保存规则。'); renderTransferRuleAvailability(); });
$('transferRulesForm').addEventListener('submit',event => { event.preventDefault(); saveAllSettings(); });
$('transferRulesPreview').addEventListener('click',previewTransferRules);
$('transferRulesRun').addEventListener('click',() => runTransferRules());
$('transferRuleSelected').addEventListener('click',() => runTransferRules(true));
// Category cleanup drafts and storage paths live in memory only; every mutation uses a saved revision.
let categoryCleanupDocument = null, categoryCleanupDrafts = [], categoryCleanupSelected = null;
let categoryCleanupDirty = false, categoryCleanupConflict = false, categoryCleanupEdit = 0, categoryCleanupRequest = 0;
let categoryCleanupLoading = null, categoryCleanupPending = '', cleanupStorageDocument = null, cleanupStorageDrafts = [];
let cleanupStorageDirty = false, cleanupStorageConflict = false, cleanupStorageEdit = 0, cleanupStorageRequest = 0, cleanupStorageLoading = null;
let cleanupConfirmation = null;
const cleanupTaskStateNames = {waiting:'连续等待中',ready:'证据已到期',shared:'共享文件',unknown:'未知',conflict:'规则冲突',protected:'受保护'};
const categoryCleanupStateNames = {idle:'等待检查',observing:'仅观察',cleaning:'清理中',waiting:'等待证据',unknown:'库存 / 检查未知',no_candidates:'无可删除项',disabled:'已暂停',busy:'作业繁忙'};
const cleanupJobStateNames = {running:'运行中',completed:'已完成',cancelled:'已取消',needs_review:'需人工复核',failed:'失败'};
const cleanupJobPhaseNames = {queued:'等待处理',preparing:'准备元数据',verified:'文件核查通过',waiting_result:'等待删除回执',completed:'已删除',needs_review:'需人工复核',failed:'失败',cancelled:'已取消',skipped:'已跳过',restoring:'恢复任务状态'};
const categoryCleanupDefaults = {name:'新清理规则',enabled:false,observe_only:true,scopes:[],target_mode:'follow',target:1,start_margin:200,stop_margin:100,check_minutes:5,seeders_enabled:true,unregistered_enabled:true,seeders_mode:'site',seeders_max:10,seeders_wait_hours:24,unregistered_wait_hours:24,max_per_run:20,retry_seconds:60,sort:'earliest',protected_categories:[],protected_tags:[]};
const cleanupNumberFields = ['target','start_total','stop_total','check_minutes','seeders_max','seeders_wait_hours','unregistered_wait_hours','max_per_run','retry_seconds'];
const cleanupBooleanFields = ['seeders_enabled','unregistered_enabled'];
function cleanupClone(value) { return JSON.parse(JSON.stringify(value)); }
function cleanupLines(value) { return [...new Set(String(value || '').replace(/\r/g,'').split('\n').filter(line => line.length > 0))]; }
function cleanupPublicText(value) { return safeMessage(value).replace(/(?:^|[\s（(])\/(?:[^\s，；）)]+\/)?[^\s，；）)]*/g,' [目录已隐藏]'); }
function cleanupRuleName(id) { return categoryCleanupDocument?.values.rules.find(rule => rule.id === id)?.name || '已移除 / 未知规则'; }
function selectedCleanupRule() { return categoryCleanupDrafts.find(rule => rule.id === categoryCleanupSelected); }
function cleanupInstancesList() { return publicDownloaders(); }
function cleanupAllCanInspect() { const instances = cleanupInstancesList(), caps = categoryCleanupDocument?.capabilities || []; return instances.length > 0 && caps.length > 0 && caps.every(cap => cap.can_inspect === true) && instances.every(instance => caps.some(cap => cap.instance_id === instance.id && cap.can_inspect === true)); }
function cleanupJob() { return categoryCleanupDocument?.runtime?.job || null; }
function cleanupJobRunning() { return cleanupJob()?.status === 'running'; }
function cleanupBlocked() { return busy || !!status?.running || activeDownloadJob() || cleanupJobRunning() || !!categoryCleanupPending; }
function cleanupSavedReady() { return !!categoryCleanupDocument?.saved && !!selectedCleanupRule() && !categoryCleanupDirty && !categoryCleanupConflict && !cleanupStorageDirty && !cleanupStorageConflict && !categoryCleanupLoading && !cleanupStorageLoading && !hasPendingSettings(); }
function cleanupActivationReason() {
  const rule = selectedCleanupRule(); if (!rule) return '先添加规则，选择明确分类范围，再点击顶部保存。';
  if (categoryCleanupDirty || cleanupStorageDirty) return '规则或文件映射有未保存修改；先点击顶部“保存修改”，再启用自动删除。';
  if (categoryCleanupConflict || cleanupStorageConflict) return '规则或映射版本变化；请刷新已保存版本，核对草稿后保存。';
  if (hasPendingSettings()) return '设置已保存，等待生效；生效后再启用此规则，避免使用旧范围或旧目录。';
  if (categoryCleanupLoading || cleanupStorageLoading) return '正在读取启用条件，请稍候。';
  if (!categoryCleanupDocument?.saved) return '先点击顶部“保存修改”；保存新规则后仅观察，不会自动删除。';
  const missing = cleanupInstancesList().filter(instance => !categoryCleanupDocument?.capabilities?.some(cap => cap.instance_id === instance.id && cap.can_inspect === true));
  if (missing.length || !cleanupAllCanInspect()) return `自动删除尚未开启：${missing.map(downloaderName).join('、') || '已登记下载器'}缺少可用的只读文件检查映射。点击“配置文件检查”，保存并刷新覆盖情况。`;
  if (cleanupJob()?.status === 'needs_review') return '上次删除结果待核验；请先“重查删除结果”，确认后重新启用。';
  if (cleanupBlocked()) return '文件检查已覆盖；当前有后台作业，结束后可启用。设置修改仍可随时保存。';
  if (rule.enabled) return '自动删除已开启。达到启动线且失效证据到期后执行，降至停止线结束；有效、未知和受保护任务保留。';
  return '规则已保存且文件检查已覆盖。点击“启用自动删除…”并确认，即可从仅观察切换为自动删除；服务端还会核对实际文件路径。';
}
function openCleanupStorage() { $('cleanupStorageCard').scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'}); ($('cleanupStorageRows').querySelector('input') || $('cleanupStorageAdd')).focus({preventScroll:true}); }
function clearCategoryCleanupPreview(message = '草稿已变更，请重新只读预览。') { $('categoryCleanupCounts').replaceChildren(); $('categoryCleanupItems').replaceChildren(); $('categoryCleanupPreviewRules').replaceChildren(); text('categoryCleanupPreviewStatus',message); }
function cleanupMakeButton(label,callback,className = 'button small') { const button = element('button',label,className); button.type = 'button'; button.addEventListener('click',callback); return button; }
function cleanupMakeLabel(label,control) { const node = element('label',label); node.append(control); return node; }
function cleanupInstanceSelect(value) { const node = element('select'); node.required = true; node.append(option('','选择已登记实例'),...cleanupInstancesList().map(instance => option(instance.id,downloaderLabel(instance)))); if (value && !cleanupInstancesList().some(instance => instance.id === value)) node.append(option(value,'已移除实例 · 请重新选择')); node.value = value || ''; return node; }
function cleanupSummaryNode(summary,name) {
  const node = element('div',undefined,'cleanup-summary'), heading = element('div',undefined,'cleanup-summary-heading');
  heading.append(element('b',name),element('span',categoryCleanupStateNames[summary.status] || '尚未检查','pill' + (summary.status === 'cleaning' ? ' warning' : ''))); node.append(heading);
  node.append(element('p',`本地库存 ${summary.inventory ?? '未知'} · 目标 ${summary.target ?? '—'} · 启动 ≥ ${summary.start_line ?? '—'} / 停止 ≤ ${summary.stop_line ?? '—'}`));
  node.append(element('small',`已到期 ${summary.mature ?? '—'} · 等待 ${summary.waiting ?? '—'} · 本轮额度 ${summary.budget ?? '—'}${summary.active ? ' · 清理阶段已激活' : ''}`));
  node.append(element('small',`人数 ${summary.seeders ?? '—'} · 未注册 ${summary.unregistered ?? '—'} · 共享 ${summary.shared ?? '—'} · 未知 ${summary.unknown ?? '—'} · 冲突 ${summary.conflicts ?? '—'}`));
  node.append(element('small',`检查 ${date(summary.last_checked_at)} · 下次 ${date(summary.next_check_at)}${summary.last_result ? ' · '+cleanupPublicText(summary.last_result) : ''}`)); return node;
}
function cleanupDashboardNode(summary, showName) {
  const node = element('section',undefined,'cleanup-dashboard-item');
  if (showName) node.append(element('b',cleanupRuleName(summary.id),'cleanup-dashboard-name'));
  const row = element('div',undefined,'cleanup-status-row'), state = element('div',undefined,'cleanup-state');
  const unavailable = summary.status === 'unknown', caption = unavailable ? categoryCleanupStateNames.unknown : summary.status === 'disabled' ? categoryCleanupStateNames.disabled : summary.status === 'observe_only' || summary.observe_only === true ? '仅观察' : categoryCleanupStateNames[summary.status] || '尚未检查';
  state.append(iconNode('eye'),element('span','当前状态'),element('span',caption,'pill' + (summary.enabled && !unavailable ? ' enabled' : ' warning'))); row.append(state);
  for (const [label,key,sign] of [['启动阈值','start_line','≥'],['停止阈值','stop_line','≤']]) { const field = element('div',undefined,'cleanup-threshold'); field.append(element('span',label),element('b',displayCount(summary[key]) === null ? '—' : `${sign} ${summary[key]}`)); row.append(field); }
  const counts = element('div',undefined,'cleanup-counts');
  for (const [label,key,icon] of [['等待处理','waiting','database'],['本轮额度','budget','candidates']]) { const field = element('div'); field.append(iconNode(icon),element('span',label),element('b',displayCount(summary[key]))); counts.append(field); }
  node.append(row,counts); node.title = `库存 ${summary.inventory ?? '未知'} · 已到期 ${summary.mature ?? '未知'} · 检查 ${date(summary.last_checked_at)}`; return node;
}
function renderCategoryCleanupRuntime() {
  if (status?.cleanup && categoryCleanupDocument && !categoryCleanupPending) categoryCleanupDocument.runtime = status.cleanup;
  const runtime = categoryCleanupDocument?.runtime || status?.cleanup, summaries = runtime?.rules || [];
  $('cleanupDashboardRules').replaceChildren(...(summaries.length ? summaries.map(summary => cleanupDashboardNode(summary,summaries.length > 1)) : [element('p',categoryCleanupDocument ? '无规则 · 自动删除关闭' : '尚未读取规则','muted')]));
  updateDashboardHelp('dashboard_cleanup',fieldHelp.dashboard_cleanup + ' ' + summaries.map(summary => `${cleanupRuleName(summary.id)}：${categoryCleanupStateNames[summary.status] || '尚未检查'}；库存 ${summary.inventory ?? '未知'}，已到期 ${summary.mature ?? '未知'}，等待 ${summary.waiting ?? '未知'}，本轮额度 ${summary.budget ?? '未知'}；检查 ${date(summary.last_checked_at)}。`).join(' '));
  for (const button of $('categoryCleanupRules').querySelectorAll('button')) { const rule = categoryCleanupDrafts.find(rule => rule.id === button.dataset.ruleId), summary = summaries.find(item => item.id === rule?.id); const note = button.querySelector('small'); if (note) note.textContent = `${rule?.enabled ? '自动删除开启' : rule?.observe_only ? '仅观察 · 删除关闭' : '已暂停'} · 库存 ${summary?.inventory ?? '未知'} · 开始总数 ${rule?.start_total ?? summary?.start_line ?? '—'} / 停止总数 ${rule?.stop_total ?? summary?.stop_line ?? '—'}${categoryCleanupDirty ? '（草稿）' : ''} · 成熟 ${summary?.mature ?? '—'} · ${categoryCleanupStateNames[summary?.status] || '尚未检查'} · 最近 ${date(summary?.last_checked_at)}${summary?.last_result ? ' · '+cleanupPublicText(summary.last_result) : ''}`; }
  renderCleanupStorageCoverage(); renderCategoryCleanupJob(); renderCategoryCleanupAvailability();
}
function renderCategoryCleanupList() {
  $('categoryCleanupRules').replaceChildren(...(categoryCleanupDrafts.length ? categoryCleanupDrafts.map(rule => {
    const button = cleanupMakeButton('',() => selectCategoryCleanupRule(rule.id),'cleanup-rule-choice'); button.dataset.ruleId = rule.id; button.setAttribute('aria-pressed',String(rule.id === categoryCleanupSelected)); const scopeText = rule.scopes.map(scope => { const instance = cleanupInstancesList().find(item => item.id === scope.instance_id); return `${instance ? downloaderLabel(instance) : '已移除实例'} · ${instance?.type === 'tr' ? 'TR 标签' : 'qB 分类'} ${[...scope.values,...(scope.include_empty ? ['空分类 / 无标签'] : [])].join('、') || '未配置'}`; }).join('；'); button.append(element('b',rule.name),element('small',''),element('small',scopeText)); return button;
  }) : [element('p','没有分类规则。添加后先设置范围并只读预览；新规则自动删除关闭。','empty')])); renderCategoryCleanupRuntime();
}
function renderCleanupScopes() {
  const rule = selectedCleanupRule(); $('categoryCleanupScopes').replaceChildren(...(rule?.scopes || []).map(scope => {
    const row = element('div',undefined,'cleanup-scope-row'), select = cleanupInstanceSelect(scope.instance_id), values = element('textarea'), empty = element('input');
    select.dataset.scopeField = 'instance_id'; values.dataset.scopeField = 'values'; values.rows = 2; values.value = scope.values.join('\n'); values.placeholder = '每行一个精确分类 / 标签'; empty.type = 'checkbox'; empty.dataset.scopeField = 'include_empty'; empty.checked = scope.include_empty;
    const label = cleanupMakeLabel('包含空分类 / 无标签',empty); label.className = 'checkbox-label'; label.prepend(empty);
    const remove = cleanupMakeButton('移除范围',() => { row.remove(); markCategoryCleanupDirty(); });
    row.append(cleanupMakeLabel('下载器实例',select),cleanupMakeLabel('匹配任一精确值',values),label,remove); return row;
  }));
}
function readCleanupEditor() {
  const rule = selectedCleanupRule(); if (!rule || $('categoryCleanupForm').hidden) return;
  const fields = $('categoryCleanupForm').elements;
  for (const key of ['name','target_mode','seeders_mode','sort']) rule[key] = fields[key].value;
  for (const key of cleanupNumberFields) rule[key] = Number(fields[key].value);
  for (const key of cleanupBooleanFields) rule[key] = fields[key].checked;
  for (const key of ['protected_categories','protected_tags']) rule[key] = cleanupLines(fields[key].value);
  rule.scopes = [...$('categoryCleanupScopes').children].map(row => ({instance_id:row.querySelector('[data-scope-field="instance_id"]').value,values:cleanupLines(row.querySelector('[data-scope-field="values"]').value),include_empty:row.querySelector('[data-scope-field="include_empty"]').checked}));
}
function markCategoryCleanupDirty() {
  readCleanupEditor(); categoryCleanupDirty = true; categoryCleanupEdit++; clearCategoryCleanupPreview();
  const button = [...$('categoryCleanupRules').querySelectorAll('button')].find(node => node.dataset.ruleId === categoryCleanupSelected); if (button) button.querySelector('b').textContent = selectedCleanupRule().name;
  feedback('categoryCleanupFeedback','未保存草稿；轮询、切换规则和明确刷新均保留输入。保存不会开启自动删除。'); renderCategoryCleanupRuntime();
}
function selectCategoryCleanupRule(id,readCurrent = true) {
  if (readCurrent) readCleanupEditor(); categoryCleanupSelected = id; const rule = selectedCleanupRule(); $('categoryCleanupForm').hidden = false;
  if (rule) { ensureCleanupTotals(rule); fillForm($('categoryCleanupForm'),rule); for (const key of ['protected_categories','protected_tags']) $('categoryCleanupForm').elements[key].value = rule[key].join('\n'); renderCleanupScopes(); }
  else { $('categoryCleanupForm').reset(); $('categoryCleanupScopes').replaceChildren(); text('categoryCleanupThreshold','未配置规则；请添加规则后编辑。'); }
  renderCategoryCleanupList();
}
function renderCategoryCleanupAvailability() {
  const rule = selectedCleanupRule(), loading = !!categoryCleanupLoading, pending = !!categoryCleanupPending, blocked = cleanupBlocked(), ready = cleanupSavedReady();
  $('categoryCleanupCancel').disabled = !cleanupJobRunning() || cleanupJob()?.cancel_requested || pending || busy;
  $('categoryCleanupRecheck').disabled = cleanupJob()?.status !== 'needs_review' || pending || busy;
  $('categoryCleanupFields').disabled = !status || !rule || pending || busy;
  $('categoryCleanupForm').elements.name.disabled = !status || !rule || pending || busy;
  $('categoryCleanupAdd').disabled = !status || !categoryCleanupDocument || loading || pending || busy;
  $('categoryCleanupRefresh').disabled = !status || loading || pending;
  $('categoryCleanupPreview').disabled = !status || !categoryCleanupDocument || !rule || pending;
  for (const id of ['categoryCleanupObserve','categoryCleanupPause']) $(id).disabled = !ready || busy || pending;
  $('categoryCleanupEnable').disabled = !ready || blocked || cleanupJob()?.status === 'needs_review';
  $('categoryCleanupRun').disabled = !ready || blocked || !cleanupAllCanInspect() || cleanupJob()?.status === 'needs_review';
  $('categoryCleanupEnable').disabled ||= rule?.enabled === true;
  $('categoryCleanupRemove').disabled = !rule || pending || busy;
  for (const button of $('categoryCleanupRules').querySelectorAll('button')) button.disabled = pending || busy;
  const state = loading ? '读取中…' : categoryCleanupConflict ? '版本冲突 · 草稿保留' : categoryCleanupDirty ? '未保存草稿' : hasPendingSettings() ? '已保存 · 等待生效' : !categoryCleanupDocument?.saved ? '尚未保存' : rule?.enabled ? '自动删除开启' : rule?.observe_only ? '仅观察 · 删除关闭' : '已暂停'; text('categoryCleanupState',state);
  text('categoryCleanupActivation',cleanupActivationReason()); $('categoryCleanupSetupStorage').disabled = !status || pending || busy;
  text('categoryCleanupPreview',categoryCleanupPending === 'preview' ? '预览中…' : '只读预览草稿');
  $('categoryCleanupCard').setAttribute('aria-busy',String(loading || pending));
  text('categoryCleanupEditorFeedback',$('categoryCleanupFeedback').textContent);
  if (rule) {
    const fields = $('categoryCleanupForm').elements, target = effectiveCleanupTarget(rule);
    fields.target.disabled = pending || busy || rule.target_mode === 'follow'; fields.seeders_max.disabled = pending || busy || rule.seeders_mode === 'site' || !rule.seeders_enabled;
    fields.seeders_mode.disabled = pending || busy || !rule.seeders_enabled; fields.seeders_wait_hours.disabled = pending || busy || !rule.seeders_enabled; fields.unregistered_wait_hours.disabled = pending || busy || !rule.unregistered_enabled;
    const valid = Number.isInteger(target) && target >= 1 && target <= 100000 && target <= rule.stop_total && rule.stop_total < rule.start_total;
    text('categoryCleanupThreshold',`有效目标 ${target || '未知'}${rule.target_mode === 'follow' ? '（跟随当前长期目标草稿）' : '（独立）'} · 开始总数 ${rule.start_total ?? '—'} / 停止总数 ${rule.stop_total ?? '—'}。${valid ? '保存时转换为余量；修改目标保留总数输入。' : '必须满足：目标 ≤ 停止总数 < 开始总数。'}${!cleanupAllCanInspect() ? ' 检查覆盖不足，删除不可用。' : ''}`); $('categoryCleanupThreshold').classList.toggle('error',!valid);
  }
  const storagePending = pending || !!cleanupStorageLoading;
  $('cleanupStorageFields').disabled = !status || !cleanupStorageDocument || storagePending || busy;
  $('cleanupStorageAdd').disabled = !status || !cleanupStorageDocument || storagePending || busy;
  $('cleanupStorageRefresh').disabled = !status || storagePending;
}
function validateCleanupRules() {
  readCleanupEditor();
  const ranges = {target:[1,100000],check_minutes:[1,1440],seeders_max:[1,100000],seeders_wait_hours:[1,8760],unregistered_wait_hours:[1,8760],max_per_run:[1,50],retry_seconds:[10,3600]};
  if (categoryCleanupDrafts.length > 100 || new Set(categoryCleanupDrafts.map(rule => rule.id)).size !== categoryCleanupDrafts.length) throw settingsValidationError('分类清理规则最多100条，标识须唯一。',$('categoryCleanupAdd'));
  const rules = categoryCleanupDrafts.map(rule => {
    ensureCleanupTotals(rule); const fail = (message,key = 'name') => { selectCategoryCleanupRule(rule.id,false); throw settingsValidationError(`规则「${rule.name || '未命名'}」：${message}`,$('categoryCleanupForm').elements[key]); };
    if (!rule.name.trim() || rule.name.length > 80) fail('名称需为 1–80 个字符。');
    if (/[\x00-\x1f]/.test(rule.name)) fail('名称不能包含控制字符。');
    if (!rule.seeders_enabled && !rule.unregistered_enabled) fail('至少选择一种明确失效条件。','seeders_enabled');
    const texts = values => Array.isArray(values) && values.length <= 100 && values.every(value => typeof value === 'string' && value.length > 0 && value.length <= 256 && !/[\x00-\x1f]/.test(value));
    if (!texts(rule.protected_categories) || !texts(rule.protected_tags) || rule.scopes.some(scope => !texts(scope.values))) fail('分类/标签每组最多100项，每项1–256字且不含控制字符。','protected_categories');
    if (!['follow','independent'].includes(rule.target_mode) || !['site','manual'].includes(rule.seeders_mode) || !['earliest','largest'].includes(rule.sort)) fail('请选择有效模式。','target_mode');
    for (const [key,[min,max]] of Object.entries(ranges)) if (!Number.isFinite(rule[key]) || rule[key] < min || rule[key] > max || (!key.endsWith('_hours') && !Number.isSafeInteger(rule[key]))) fail('数字超出范围或不是整数。',key);
    const target = effectiveCleanupTarget(rule), start = rule.start_total - target, stop = rule.stop_total - target;
    if (!Number.isSafeInteger(rule.start_total) || !Number.isSafeInteger(rule.stop_total) || !Number.isSafeInteger(target) || target < 1 || target > 100000 || start < 1 || start > 100000 || stop < 0 || stop > 99999 || stop >= start) fail('需满足目标 ≤ 停止总数 < 开始总数，开始余量 1–100000，停止余量 0–99999。','stop_total');
    if (!rule.scopes.length || new Set(rule.scopes.map(scope => scope.instance_id)).size !== rule.scopes.length) fail('需设置范围且每实例仅一次。','name');
    if (rule.scopes.some(scope => !cleanupInstancesList().some(instance => instance.id === scope.instance_id) || (!scope.values.length && !scope.include_empty))) fail('每个范围需已登记实例及精确值，或明确包含空值。','name');
    const result = {...cleanupClone(rule),start_margin:start,stop_margin:stop}; delete result.start_total; delete result.stop_total; return result;
  });
  return {rules};
}
function applyCategoryCleanupDocument(result,explicit = false) {
  if (!Array.isArray(result?.values?.rules) || !/^[a-f\d]{64}$/i.test(result.revision || '') || !/^[a-f\d]{64}$/i.test(result.instances_revision || '')) throw Error('分类规则响应缺少有效版本，当前草稿已保留。');
  if (status) status.cleanup = result.runtime;
  const previous = categoryCleanupDocument;
  if (previous && (categoryCleanupDirty || categoryCleanupConflict) && !explicit) {
    if (previous.revision !== result.revision || previous.instances_revision !== result.instances_revision) categoryCleanupConflict = true;
    categoryCleanupDocument = {...previous,runtime:result.runtime,target:result.target,capabilities:result.capabilities};
  } else { categoryCleanupDocument = result; categoryCleanupConflict = false; if (!categoryCleanupDirty) { categoryCleanupDrafts = cleanupClone(pendingSettingsValue('category')?.rules || result.values.rules); for (const rule of categoryCleanupDrafts) ensureCleanupTotals(rule,rule.target_mode === 'follow' ? result.target : rule.target); categoryCleanupSelected = categoryCleanupDrafts.some(rule => rule.id === categoryCleanupSelected) ? categoryCleanupSelected : categoryCleanupDrafts[0]?.id || null; selectCategoryCleanupRule(categoryCleanupSelected,false); } }
  const current = $('downloadCleanupRule').value;
  fillOptions('downloadCleanupRule',result.values.rules.map(rule => ({value:rule.id,label:rule.name})),'全部规则');
  if (current && !result.values.rules.some(rule => rule.id === current)) $('downloadCleanupRule').append(option(current,'已移除规则（保留筛选）')); $('downloadCleanupRule').value = current;
  renderCategoryCleanupRuntime();
}
async function refreshCategoryCleanup(explicit = false) {
  if (!status || categoryCleanupLoading || categoryCleanupPending) return categoryCleanupLoading;
  const generation = queryGeneration, request = categoryCleanupRequest, edit = categoryCleanupEdit;
  const promise = (async () => {
    try { const result = await api('fleet/cleanup/settings'); if (!status || generation !== queryGeneration || request !== categoryCleanupRequest) return;
      applyCategoryCleanupDocument(result,explicit && edit === categoryCleanupEdit);
      if (categoryCleanupConflict) feedback('categoryCleanupFeedback','规则或实例版本已变更；草稿与旧版本保留。请明确刷新已保存版本，核对后重新保存。','error');
      else if (explicit || !categoryCleanupDirty) feedback('categoryCleanupFeedback',categoryCleanupDirty ? '已接受最新版本，未保存草稿保留；请核对后保存。' : '规则已读取。新规则默认仅观察；保存不会开启自动删除。');
    } catch (error) { if (status && generation === queryGeneration && request === categoryCleanupRequest && !error.stale) feedback('categoryCleanupFeedback',error.message,'error'); }
    finally { if (generation === queryGeneration && request === categoryCleanupRequest) { categoryCleanupLoading = null; renderCategoryCleanupAvailability(); } }
  })(); categoryCleanupLoading = promise; renderCategoryCleanupAvailability(); return promise;
}
async function cleanupRequest(kind,path,body,callback,target = 'categoryCleanupFeedback') {
  if (!status || categoryCleanupPending) return;
  const generation = queryGeneration, request = ++categoryCleanupRequest; cleanupStorageRequest++; categoryCleanupLoading = null; cleanupStorageLoading = null; categoryCleanupPending = kind; renderCategoryCleanupAvailability();
  try { const result = await api(path,body); if (!status || generation !== queryGeneration || request !== categoryCleanupRequest) return; if (result.ok === false) throw Error('操作未完成，请刷新版本或作业状态后重试。'); await callback(result); }
  catch (error) { if (status && generation === queryGeneration && request === categoryCleanupRequest && !error.stale) { if (kind === 'preview') clearCategoryCleanupPreview('预览失败，未取得可用结果；请重试。'); const conflict = error.status === 409 && /版本|其他页面/.test(error.message); if (conflict) { if (target === 'cleanupStorageFeedback') cleanupStorageConflict = true; else categoryCleanupConflict = true; } feedback(target,error.message + (conflict ? ' 输入和旧版本已保留；请刷新版本并核对后重试。' : ' 自动删除状态未变更。'),'error'); } }
  finally { if (generation === queryGeneration && request === categoryCleanupRequest) { categoryCleanupPending = ''; renderCategoryCleanupAvailability(); } }
}
async function previewCategoryCleanup() {
  if ($('categoryCleanupPreview').disabled || !$('categoryCleanupForm').reportValidity()) return;
  let values; try { values = validateCleanupRules(); } catch (error) { feedback('categoryCleanupFeedback',error.message,'error'); return; }
  const edit = categoryCleanupEdit; clearCategoryCleanupPreview('正在只读检查库存、证据与文件保护…');
  await cleanupRequest('preview','fleet/cleanup/preview',{values,rule_id:categoryCleanupSelected},result => {
    if (edit !== categoryCleanupEdit) return;
    const labels = {seeders:'人数失效',unregistered:'未注册',waiting:'连续等待',shared:'共享',unknown:'未知',conflicts:'冲突',mature:'已到期',protected:'受保护'};
    $('categoryCleanupCounts').replaceChildren(...Object.entries(labels).map(([key,label]) => { const node = element('div'); node.append(element('dt',label),element('dd',result.counts?.[key] ?? '—')); return node; }));
    $('categoryCleanupPreviewRules').replaceChildren(...(result.rules || []).map(summary => cleanupSummaryNode(summary,categoryCleanupDrafts.find(rule => rule.id === summary.id)?.name || '未知规则')));
    const items = (result.items || []).slice(0,100); $('categoryCleanupItems').replaceChildren(...items.map((item,index) => { const row = element('li'); row.style.setProperty('--rule-delay',Math.min(index,5)*20+'ms'); row.append(element('b',item.name || '未命名任务'),element('span',cleanupTaskStateNames[item.status] || categoryCleanupStateNames[item.status] || (item.status === 'mature' ? '证据已到期' : '待核查'),'pill')); row.append(element('p',cleanupPublicText(item.reason) || '没有公开说明'),element('small',`${downloaderName(cleanupInstancesList().find(instance => instance.id === item.instance_id))} · ${categoryCleanupDrafts.find(rule => rule.id === item.rule_id)?.name || '未知规则'} · 连续起点 ${date(item.since)} · 已等待 ${(Number(item.elapsed_seconds || 0)/3600).toFixed(1)} 小时 · 逻辑体积 ${size(item.size || 0)}`)); return row; }));
    text('categoryCleanupPreviewStatus',`${date(result.checked_at)} · ${items.length ? `展示 ${items.length} 项` : '没有匹配的预览项'}${result.truncated || (result.items || []).length > 100 ? ' · 结果已截断，最多展示 100 项' : ''} · 只读；不保存观察，不执行删除。`); feedback('categoryCleanupFeedback','草稿只读预览完成；实际删除前服务端会重新核查。','success');
  });
}
function askCleanupConfirmation(title,description,label,destructive = true) {
  if (cleanupConfirmation || !status) return Promise.resolve(false);
  text('cleanupConfirmTitle',title); text('cleanupConfirmDescription',description); text('cleanupConfirmAccept',label); $('cleanupConfirmAcknowledgement').hidden = !destructive; $('cleanupConfirmCheck').checked = false; $('cleanupConfirmAccept').disabled = destructive;
  const focus = document.activeElement; categoryCleanupPending = 'confirm'; renderCategoryCleanupAvailability();
  return new Promise(resolve => { cleanupConfirmation = {resolve,focus,destructive}; $('cleanupConfirmDialog').showModal(); $('cleanupConfirmBack').focus(); });
}
function finishCleanupConfirmation(accepted) {
  const pending = cleanupConfirmation; if (!pending) return;
  if (accepted && pending.destructive && !$('cleanupConfirmCheck').checked) return;
  cleanupConfirmation = null; $('cleanupConfirmDialog').close(); $('cleanupConfirmCheck').checked = false; text('cleanupConfirmDescription',''); categoryCleanupPending = ''; renderCategoryCleanupAvailability(); if (pending.focus?.isConnected && !$('appView').hidden) pending.focus.focus(); pending.resolve(accepted);
}
async function changeCategoryCleanupMode(actionName) {
  const button = $(({observe:'categoryCleanupObserve',pause:'categoryCleanupPause',enable:'categoryCleanupEnable'})[actionName]); if (button.disabled) return;
  if (actionName === 'enable' && !cleanupAllCanInspect()) { feedback('categoryCleanupFeedback',cleanupActivationReason(),'error'); openCleanupStorage(); return; }
  const id = categoryCleanupSelected, revision = categoryCleanupDocument.revision, edit = categoryCleanupEdit;
  if (actionName === 'enable' && !await askCleanupConfirmation('启用自动删除任务与数据',`规则「${selectedCleanupRule().name}」将按已保存阈值和周期自动删除任务及其数据文件。数据删除不可逆，种子元数据备份不是内容备份。后续自动轮次不会再次询问。全部已登记实例必须可只读检查；每项仍需通过证据和文件保护。`,'启用自动删除')) return;
  if (!status || !cleanupSavedReady() || id !== categoryCleanupSelected || revision !== categoryCleanupDocument.revision || edit !== categoryCleanupEdit || (actionName === 'enable' && !cleanupAllCanInspect())) { feedback('categoryCleanupFeedback','确认期间规则或版本变化，请核对后重新操作。','error'); return; }
  await cleanupRequest(actionName,'fleet/cleanup/mode',{revision,rule_id:id,action:actionName,...(actionName === 'enable' ? {confirm:'ENABLE_AUTOMATIC_DELETE_TASKS_AND_DATA'} : {})},result => { categoryCleanupEdit++; applyCategoryCleanupDocument(result,true); feedback('categoryCleanupFeedback',actionName === 'enable' ? '服务端已接受启用；后续自动轮次不再询问。' : actionName === 'observe' ? '规则已切换为仅观察；自动删除关闭。' : '规则已暂停；如有运行作业，请单独停止后续项。','success'); });
}
async function runCategoryCleanup() {
  if ($('categoryCleanupRun').disabled) return; const id = categoryCleanupSelected, revision = categoryCleanupDocument.revision, edit = categoryCleanupEdit;
  if (!await askCleanupConfirmation('删除任务与数据一轮',`使用规则「${selectedCleanupRule().name}」的已保存版本，核查库存阈值、连续证据和文件保护后删除一轮任务及数据。自动删除无需启用。数据删除不可逆；元数据备份无法恢复内容。提交请求不代表已有任务被删除，实际结果以后台作业回执为准。`,'核查并删除一轮')) return;
  if (!status || !cleanupSavedReady() || !cleanupAllCanInspect() || cleanupBlocked() || id !== categoryCleanupSelected || revision !== categoryCleanupDocument.revision || edit !== categoryCleanupEdit) { feedback('categoryCleanupFeedback','确认期间状态变化，请核对后重新运行。','error'); return; }
  await cleanupRequest('run','fleet/cleanup/run',{revision,rule_id:id,confirm:'RUN_DELETE_TASKS_AND_DATA'},result => { if (result.values) applyCategoryCleanupDocument(result,true); else if (result.job) { categoryCleanupDocument.runtime = {...categoryCleanupDocument.runtime,job:result.job}; if (status) status.cleanup = categoryCleanupDocument.runtime; } renderCategoryCleanupJob(); feedback('categoryCleanupFeedback',result.job ? '已提交清理作业，请查看逐项阶段与实际回执。' : '服务端未返回清理作业；请刷新状态核对本轮结果。','success'); });
}
function renderCategoryCleanupJob() {
  const job = cleanupJob(); $('categoryCleanupJob').hidden = !job; if (!job) return;
  const items = job.items || []; text('categoryCleanupJobSummary',`${cleanupJobStateNames[job.status] || '状态未知'} · 已删除 ${job.completed ?? 0} / ${items.length} · 逻辑体积 ${size(job.logical_bytes || 0)}${job.cancel_requested ? ' · 已请求停止后续项' : ''}`);
  text('categoryCleanupJobTime',`开始 ${date(job.started_at)} · 结束 ${date(job.finished_at)} · ${cleanupRuleName(job.rule_id)}`); $('categoryCleanupJobProgress').max = Math.max(1,items.length); $('categoryCleanupJobProgress').value = job.completed || 0;
  $('categoryCleanupCancel').disabled = job.status !== 'running' || job.cancel_requested || !!categoryCleanupPending || busy;
  $('categoryCleanupRecheck').disabled = job.status !== 'needs_review' || !!categoryCleanupPending || busy;
  $('categoryCleanupJobItems').replaceChildren(...items.slice(0,100).map(item => { const row = element('li'); row.append(element('span',item.name || '未命名任务'),element('span',cleanupJobPhaseNames[item.phase] || '阶段未知')); if (item.reason) row.append(element('small',cleanupPublicText(item.reason))); return row; }));
  if (items.length > 100) $('categoryCleanupJobItems').append(element('li',`共 ${items.length} 项，紧凑列表仅展示前 100 项。`));
}
async function recheckCategoryCleanup() {
  if ($('categoryCleanupRecheck').disabled) return;
  const id = cleanupJob().id;
  await cleanupRequest('recheck','fleet/cleanup/recheck',{job_id:id},result => {
    if (!result.job || result.job.id !== id) throw Error('重查回执不完整，请刷新作业状态。');
    categoryCleanupDocument.runtime.job = result.job;
    if (status) status.cleanup = categoryCleanupDocument.runtime;
    renderCategoryCleanupJob();
    feedback('categoryCleanupFeedback',result.job.status === 'needs_review' ? '任务或文件结果仍无法完整确认；已保留待核验状态。' : '已只读重查删除结果；没有重复删除，自动执行保持暂停。','success');
  });
}
async function cancelCategoryCleanup() {
  if ($('categoryCleanupCancel').disabled) return; const id = cleanupJob().id;
  if (!await askCleanupConfirmation('停止清理后续项','仅停止作业中尚未开始的后续项；当前项可能继续完成。已删除数据不可恢复，种子元数据备份无法恢复文件内容。','停止后续项',false)) return;
  if (!status || cleanupJob()?.id !== id || !cleanupJobRunning()) return;
  await cleanupRequest('cancel','fleet/cleanup/cancel',{job_id:id},result => { if (result.job) { categoryCleanupDocument.runtime.job = result.job; if (status) status.cleanup = categoryCleanupDocument.runtime; } renderCategoryCleanupJob(); feedback('categoryCleanupFeedback','已提交停止请求；以后端作业状态为准，已删除项不回退。','success'); });
}
function renderCleanupStorageCoverage() {
  const caps = categoryCleanupDocument?.capabilities || [], instances = cleanupInstancesList();
  $('cleanupStorageCoverage').replaceChildren(...(instances.length ? instances.map(instance => { const available = caps.some(cap => cap.instance_id === instance.id && cap.can_inspect === true), node = element('span',`${downloaderName(instance)} · ${available ? '可只读检查' : '检查未覆盖'}`,'pill'+(available ? ' enabled' : ' warning')); return node; }) : [element('p','尚未取得已登记实例；无法确认文件检查覆盖。','hint')]));
}
function readCleanupStorageEditor() { cleanupStorageDrafts = [...$('cleanupStorageRows').children].map(row => ({instance_id:row.querySelector('[data-storage-field="instance_id"]').value,download_root:row.querySelector('[data-storage-field="download_root"]').value,inspect_root:row.querySelector('[data-storage-field="inspect_root"]').value,protected_paths:cleanupLines(row.querySelector('[data-storage-field="protected_paths"]').value)})); }
function renderCleanupStorageEditor() {
  $('cleanupStorageRows').replaceChildren(...cleanupStorageDrafts.map(mapping => {
    const row = element('div',undefined,'cleanup-storage-row'), select = cleanupInstanceSelect(mapping.instance_id); select.dataset.storageField = 'instance_id'; row.append(cleanupMakeLabel('下载器实例',select));
    for (const [key,label] of [['download_root','下载器根目录（Linux 绝对路径）'],['inspect_root','只读检查目录（/inspect 下的子目录）'],['protected_paths','保护目录（每行一个远端绝对路径）']]) { const field = element(key === 'protected_paths' ? 'textarea' : 'input'); field.dataset.storageField = key; field.value = key === 'protected_paths' ? mapping[key].join('\n') : mapping[key]; field.spellcheck = false; if (key === 'protected_paths') field.rows = 2; else field.required = true; row.append(cleanupMakeLabel(label,field)); }
    const remove = cleanupMakeButton('移除映射',() => { row.remove(); markCleanupStorageDirty(); }); row.append(remove); return row;
  })); text('cleanupStorageRoots','允许的只读挂载根：'+(cleanupStorageDocument?.allowed_roots || []).join(' · ')); renderCategoryCleanupAvailability();
}
function markCleanupStorageDirty() { readCleanupStorageEditor(); cleanupStorageDirty = true; cleanupStorageEdit++; feedback('cleanupStorageFeedback','目录映射有未保存修改；刷新保留输入。保存后全部分类规则暂停并重置观察。'); renderCategoryCleanupAvailability(); }
async function refreshCleanupStorage(explicit = false) {
  if (!status || page !== 'settings' || cleanupStorageLoading || categoryCleanupPending) return cleanupStorageLoading;
  const generation = queryGeneration, request = cleanupStorageRequest, edit = cleanupStorageEdit;
  const promise = (async () => { try {
    const result = await api('fleet/cleanup/storage'); if (!status || generation !== queryGeneration || request !== cleanupStorageRequest) return;
    if (!Array.isArray(result?.values?.mappings) || !/^[a-f\d]{64}$/i.test(result.revision || '')) throw Error('目录映射响应缺少有效版本，草稿已保留。');
    if (cleanupStorageDocument && (cleanupStorageDirty || cleanupStorageConflict) && !(explicit && edit === cleanupStorageEdit)) { if (cleanupStorageDocument.revision !== result.revision) cleanupStorageConflict = true; }
    else { cleanupStorageDocument = result; cleanupStorageConflict = false; if (!cleanupStorageDirty) { cleanupStorageDrafts = cleanupClone(pendingSettingsValue('storage')?.mappings || result.values.mappings); renderCleanupStorageEditor(); } }
    if (cleanupStorageConflict) feedback('cleanupStorageFeedback','目录映射版本变更；草稿和旧版本保留。请明确刷新映射版本后核对。','error'); else if (explicit || !cleanupStorageDirty) feedback('cleanupStorageFeedback',cleanupStorageDirty ? '已接受最新目录版本，草稿保留；请核对后再保存。' : '目录映射已读取；这里只使用预先只读挂载目录。');
  } catch (error) { if (status && generation === queryGeneration && request === cleanupStorageRequest && !error.stale) feedback('cleanupStorageFeedback',error.message,'error'); }
  finally { if (generation === queryGeneration && request === cleanupStorageRequest) { cleanupStorageLoading = null; renderCategoryCleanupAvailability(); } }
  })(); cleanupStorageLoading = promise; renderCategoryCleanupAvailability(); return promise;
}
function resetCategoryCleanup() {
  categoryCleanupRequest++; cleanupStorageRequest++; categoryCleanupEdit++; cleanupStorageEdit++; finishCleanupConfirmation(false);
  categoryCleanupDocument = null; categoryCleanupDrafts = []; categoryCleanupSelected = null; categoryCleanupDirty = false; categoryCleanupConflict = false; categoryCleanupLoading = null; categoryCleanupPending = '';
  cleanupStorageDocument = null; cleanupStorageDrafts = []; cleanupStorageDirty = false; cleanupStorageConflict = false; cleanupStorageLoading = null;
  $('categoryCleanupForm').reset(); $('categoryCleanupForm').hidden = false; $('categoryCleanupJob').hidden = true; $('cleanupStorageForm').reset();
  for (const id of ['categoryCleanupRules','categoryCleanupScopes','categoryCleanupJobItems','cleanupStorageRows','cleanupStorageCoverage','cleanupDashboardRules']) $(id).replaceChildren(); text('cleanupStorageRoots',''); text('categoryCleanupJobSummary',''); text('categoryCleanupJobTime',''); text('categoryCleanupThreshold',''); text('cleanupConfirmTitle','确认操作');
  $('downloadCleanupRule').replaceChildren(option('','全部规则')); $('downloadCleanupStatus').value = ''; clearCategoryCleanupPreview('尚未预览。'); feedback('categoryCleanupFeedback','登录后读取分类规则。'); feedback('cleanupStorageFeedback','登录后读取目录映射。');
}
$('categoryCleanupForm').addEventListener('input',markCategoryCleanupDirty);
$('categoryCleanupForm').addEventListener('change',event => { if (event.target.tagName === 'SELECT') markCategoryCleanupDirty(); });
$('categoryCleanupForm').addEventListener('submit',event => { event.preventDefault(); saveAllSettings(); });
$('categoryCleanupForm').addEventListener('invalid',event => { const details = event.target.closest('details'); if (details) details.open = true; },true);
$('categoryCleanupRefresh').addEventListener('click',() => refreshCategoryCleanup(true));
$('categoryCleanupAdd').addEventListener('click',() => { if ($('categoryCleanupAdd').disabled) return; addCleanupDraft(); $('categoryCleanupForm').elements.name.focus(); feedback('categoryCleanupFeedback','新规则默认关闭、仅观察；请填写明确范围。'); });
$('categoryCleanupScopeAdd').addEventListener('click',() => { readCleanupEditor(); const rule = selectedCleanupRule(), instance = cleanupInstancesList().find(item => !rule.scopes.some(scope => scope.instance_id === item.id)); if (!instance) { feedback('categoryCleanupFeedback','每个实例只能添加一个范围；请先登记实例或修改已有范围。','error'); return; } rule.scopes.push({instance_id:instance.id,values:[],include_empty:false}); renderCleanupScopes(); markCategoryCleanupDirty(); $('categoryCleanupScopes').lastChild.querySelector('textarea').focus(); });
$('categoryCleanupRemove').addEventListener('click',async () => { if ($('categoryCleanupRemove').disabled) return; const id = categoryCleanupSelected; if (!await askCleanupConfirmation('从规则草稿移除',`移除规则「${selectedCleanupRule().name}」？此操作仅修改规则集合，不删除下载任务或文件。保存全部规则后生效。`,'移除规则',false)) return; if (!status) return; categoryCleanupDrafts = categoryCleanupDrafts.filter(rule => rule.id !== id); categoryCleanupDirty = true; categoryCleanupEdit++; categoryCleanupSelected = null; selectCategoryCleanupRule(categoryCleanupDrafts[0]?.id || null); clearCategoryCleanupPreview(); feedback('categoryCleanupFeedback','规则已从草稿移除；请保存全部规则使其生效。任务与文件保留。'); });
$('categoryCleanupPreview').addEventListener('click',previewCategoryCleanup);
for (const actionName of ['observe','pause','enable']) $(({observe:'categoryCleanupObserve',pause:'categoryCleanupPause',enable:'categoryCleanupEnable'})[actionName]).addEventListener('click',() => changeCategoryCleanupMode(actionName));
$('categoryCleanupRun').addEventListener('click',runCategoryCleanup); $('categoryCleanupCancel').addEventListener('click',cancelCategoryCleanup);
$('categoryCleanupRecheck').addEventListener('click',recheckCategoryCleanup);
$('cleanupDashboardOpen').addEventListener('click',() => { navigate('settings'); $('categoryCleanupCard').scrollIntoView({block:'start'}); $('categoryCleanupRefresh').focus({preventScroll:true}); });
$('categoryCleanupSetupStorage').addEventListener('click',openCleanupStorage);
$('cleanupStorageForm').addEventListener('input',markCleanupStorageDirty); $('cleanupStorageForm').addEventListener('change',event => { if (event.target.tagName === 'SELECT') markCleanupStorageDirty(); });
$('cleanupStorageForm').addEventListener('submit',event => { event.preventDefault(); saveAllSettings(); }); $('cleanupStorageRefresh').addEventListener('click',() => refreshCleanupStorage(true));
$('cleanupStorageAdd').addEventListener('click',() => { if ($('cleanupStorageAdd').disabled) return; readCleanupStorageEditor(); const instance = cleanupInstancesList().find(item => !cleanupStorageDrafts.some(mapping => mapping.instance_id === item.id)); if (!instance) { feedback('cleanupStorageFeedback','每个实例只允许一条根目录映射。','error'); return; } cleanupStorageDrafts.push({instance_id:instance.id,download_root:'',inspect_root:'',protected_paths:[]}); renderCleanupStorageEditor(); markCleanupStorageDirty(); $('cleanupStorageRows').lastChild.querySelector('input').focus(); });
$('cleanupConfirmForm').addEventListener('submit',event => { event.preventDefault(); finishCleanupConfirmation(true); }); $('cleanupConfirmBack').addEventListener('click',() => finishCleanupConfirmation(false)); $('cleanupConfirmCheck').addEventListener('change',() => { $('cleanupConfirmAccept').disabled = !$('cleanupConfirmCheck').checked; }); $('cleanupConfirmDialog').addEventListener('cancel',event => { event.preventDefault(); finishCleanupConfirmation(false); });
$('cleanupConfirmDialog').addEventListener('keydown',event => { if (event.key !== 'Tab') return; const controls = [...event.currentTarget.querySelectorAll('input,button')].filter(node => !node.disabled && node.getClientRects().length); const first = controls[0], last = controls[controls.length-1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } });
// All settings drafts remain in memory; validate every dirty source before the first POST.
function arrangeSettingsSections() {
  $('settingsLongTerm').querySelector('.settings-section-heading').after($('settingsForm'));
  const tagCard = element('article',undefined,'card'), tagHeading = element('div',undefined,'card-heading'), tagGrid = $('managedTagInput').closest('.form-grid'); tagHeading.append(element('h2','全局管理标签')); tagCard.append(tagHeading,tagGrid,element('p','精确标签用于本地保种与真实下载统计；每实例后续分类与标签在下方管理。','hint')); $('settingsCategories').querySelector('.settings-section-heading').after(tagCard); $('managedTagInput').setAttribute('form','settingsForm'); $('managedTagInput').addEventListener('input',markRuntimeDirty);
  $('settingsLongTerm').append($('categoryCleanupCard'));
  const legacy = element('details',undefined,'cleanup-legacy'); legacy.id = 'cleanupLegacySettings'; legacy.append(element('summary','兼容设置 · 仅移除任务，保留数据文件'),$('cleanupForm').closest('article')); $('categoryCleanupCard').append(legacy);
  $('settingsTransferGroup').append($('transferRulesPanel'));
  $('settingsConnection').append($('configurationForm').closest('article'));
  $('settingsConnection').append($('cleanupStorageCard'));
  $('settingsRuntime').append($('logPolicyForm'));
  $('advancedSettings').remove();
}
function markRuntimeDirty(event) {
  settingsDirty = true; settingsRevision++;
  if (event?.target?.name === 'target' && categoryCleanupDrafts.some(rule => rule.target_mode === 'follow')) { readCleanupEditor(); categoryCleanupDirty = true; categoryCleanupEdit++; clearCategoryCleanupPreview(); }
  feedback('settingsFeedback','有未保存修改；顶部保存全部草稿，刷新保留输入。'); updateManualLimitField(); updateRefillMode(); renderCategoryCleanupAvailability();
}
function effectiveCleanupTarget(rule) { return rule.target_mode === 'independent' ? Number(rule.target) : Number($('settingsForm').elements.target.value || categoryCleanupDocument?.target || status?.settings?.target); }
function ensureCleanupTotals(rule,target = effectiveCleanupTarget(rule)) { if (rule.start_total === undefined) rule.start_total = target + rule.start_margin; if (rule.stop_total === undefined) rule.stop_total = target + rule.stop_margin; }
function addCleanupDraft(scopes = []) {
  readCleanupEditor(); const bytes = new Uint8Array(16); crypto.getRandomValues(bytes); const id = [...bytes].map(value => value.toString(16).padStart(2,'0')).join('');
  const rule = {...cleanupClone(categoryCleanupDefaults),id,target:effectiveCleanupTarget({target_mode:'follow'}),scopes}; ensureCleanupTotals(rule);
  categoryCleanupDrafts.push(rule); categoryCleanupDirty = true; categoryCleanupEdit++; selectCategoryCleanupRule(id,false); clearCategoryCleanupPreview(); return rule;
}
function validateTransferCron(expression) {
  const parts = String(expression).trim().toLowerCase().split(/\s+/), bounds = [[0,59],[0,23],[1,31],[1,12],[0,6]];
  if (expression.length > 200 || parts.length !== 5) throw settingsValidationError('Cron 需为五段、最多200字：分 时 日 月 星期。',$('transferRulesForm').elements.cron);
  const names = [{},{},{},Object.fromEntries(['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'].map((name,index) => [name,index+1])),Object.fromEntries(['mon','tue','wed','thu','fri','sat','sun'].map((name,index) => [name,index]))];
  const selected = parts.map((part,index) => {
    const [low,high] = bounds[index], set = new Set(), fail = () => { throw settingsValidationError('Cron 数值、范围或步长无效；星期一为0，星期日为6。',$('transferRulesForm').elements.cron); };
    const number = value => { if (value in names[index]) return names[index][value]; if (!/^[0-9]{1,2}$/.test(value)) fail(); return Number(value); };
    for (const entry of part.split(',')) {
      const chunks = entry.split('/'); if (chunks.length > 2 || !chunks[0] || (chunks.length === 2 && !/^[0-9]{1,2}$/.test(chunks[1]))) fail();
      const step = chunks.length === 2 ? Number(chunks[1]) : 1; if (step < 1 || step > high-low+1) fail(); let start,end;
      if (chunks[0] === '*') { start = low; end = high; }
      else if (chunks[0].includes('-')) { const limits = chunks[0].split('-'); if (limits.length !== 2) fail(); start = number(limits[0]); end = number(limits[1]); }
      else { start = number(chunks[0]); end = chunks.length === 2 ? high : start; }
      if (!(low <= start && start <= end && end <= high)) fail(); for (let value = start; value <= end; value += step) set.add(value);
    } if (!set.size) fail(); return set;
  });
  for (let year = 2000; year < 2400; year++) for (const month of selected[3]) for (const day of selected[2]) { const date = new Date(Date.UTC(year,month-1,day)); if (date.getUTCMonth() === month-1 && date.getUTCDate() === day && selected[4].has((date.getUTCDay()+6)%7)) return parts.join(' '); }
  throw settingsValidationError('Cron 日期组合没有可执行时间，请核对月份、日期和星期。',$('transferRulesForm').elements.cron);
}
function recommendedStrategy(data = ptsData) {
  const S = data?.target;
  if (!data?.available || data.stale || data.has_task !== true || !Number.isInteger(S) || S <= 0) return {reason:'推荐不可用：未取得可信且未过期的 PTS 站端任务目标。请刷新 PTS；不会猜测目标。'};
  const c = Math.max(1,Math.ceil(S * .1));
  if (S + 2*c > 100000) return {reason:'推荐不可用：站端目标加安全余量超过 100000，请手动设置合法目标。'};
  return {S,c,values:{refill_count_basis:'site_effective',target:S+2*c,refill_trigger:S+c,refill_floor:S,refill_check_minutes:5,max_per_run:50,refill_max_inflight:500,refill_retry_seconds:60,refill_site_max_age_minutes:120,refill_reservation_hours:72}};

}
function updateRecommendationAvailability() {
  const recipe = recommendedStrategy(); $('strategyRecommended').disabled = !status || busy || settingsReadBusy || !!recipe.reason;
  text('recommendationReason',recipe.reason || `站端目标 ${recipe.S} · 每档余量 ${recipe.c}；推荐维持 ${recipe.values.target} / 补量 ${recipe.values.refill_trigger} / 警戒 ${recipe.S}。仅填草稿，自动化开关保持原值。`);
}
function fillRecommendedSettings() {
  const recipe = recommendedStrategy(); if (recipe.reason || busy || settingsReadBusy) { feedback('settingsFeedback',recipe.reason || '正在处理设置，请稍后再试。','error'); return; }
  const fields = $('settingsForm').elements; readCleanupEditor();
  for (const spec of runtimeFields) if (!['checkbox','textarea','select'].includes(spec[5]) && !['delete_max_per_job','verify_timeout_seconds','pause_timeout_seconds'].includes(spec[0])) fields[spec[0]].value = spec[2];
  fillForm($('settingsForm'),recipe.values); fields.verify_timeout_seconds.value = 21600; fields.pause_timeout_seconds.value = 120; markRuntimeDirty({target:fields.target});
  const notes = [];
  if (cleanupPolicy) { fillForm($('cleanupForm'),{cleanup_wait_hours:24,cleanup_max_per_run:20,unregistered_wait_hours:24,unregistered_max_per_run:20}); cleanupDirty = true; cleanupVersion++; }
  if (categoryCleanupDocument) {
    if (!categoryCleanupDrafts.length) {
      const scopes = (instanceLabelsDocument?.items || []).filter(item => item.enabled).map(item => ({instance_id:item.id,values:item.type === 'qb' ? (item.category ? [item.category] : []) : [...new Set((item.category+','+item.tag).split(',').map(value => value.trim()).filter(Boolean))],include_empty:false})).filter(scope => scope.values.length);
      if (scopes.length) addCleanupDraft(scopes); else notes.push('清理规则未生成：已保存实例没有明确分类/标签；请手动添加并明确勾选空分类/无标签范围。');
    }
    for (const rule of categoryCleanupDrafts) { rule.seeders_wait_hours = 24; rule.unregistered_wait_hours = 24; rule.check_minutes = 5; rule.max_per_run = 20; rule.retry_seconds = 60; }
    if (categoryCleanupDrafts.length) { categoryCleanupDirty = true; categoryCleanupEdit++; selectCategoryCleanupRule(categoryCleanupSelected,false); }
  } else notes.push('清理规则尚未读取，请刷新后再填推荐。');
  if (transferRuleDocument && !transferRuleDocument.saved && !transferRuleDirty) {
    const items = instanceLabelsDocument?.items || [], source = items.find(item => item.type === 'qb' && item.enabled && item.default), target = items.find(item => item.type === 'tr' && item.enabled);
    if (source && target && (source.category || source.tag)) {
      const saved = transferRuleDocument.values, labels = [...new Set((target.category+','+target.tag).split(',').map(value => value.trim()).filter(Boolean))];
      fillTransferRuleForm({...saved,source_instance_id:source.id,target_instance_id:target.id,include_categories:source.category ? [source.category] : [],include_tags:source.tag ? source.tag.split(',').map(value => value.trim()).filter(Boolean) : [],include_untagged:!source.tag,target_labels:labels,path_mappings:Array.isArray(status.settings.transfer_path_mappings) ? status.settings.transfer_path_mappings : [],enabled:false,cron:'*/5 * * * *'});
      transferRuleDirty = true; transferRuleEditVersion++;
      if (!$('transferRulesForm').elements.path_mappings.value) notes.push('转种路径映射未配置；未猜测宿主路径。');
    } else notes.push(source && target ? '转种规则未生成：来源没有明确分类或标签；请手动确定范围后再保存。' : '转种来源/目的未填：需要启用的默认 qB 与启用 TR。');
  } else if (transferRuleDocument) notes.push('保留已有转种规则与未保存范围、映射及开关。');
  renderTransferRuleAvailability(); renderCategoryCleanupAvailability(); updateRecommendationAvailability();
  feedback('settingsFeedback',`已填推荐草稿：S=${recipe.S}，c=${recipe.c}，目标 S+2c、补量 S+c、警戒 S；等待24小时、清理每轮20、检查5分钟。自动化开关保持原值。${notes.join(' ')}`);
}
function renderInstanceLabels() {
  const rows = (instanceLabelsDocument?.items || []).map(item => {
    const row = element('section',undefined,'instance-label-row'); row.dataset.instanceId = item.id; const values = instanceLabelDrafts.get(item.id) || pendingSettingsValue('labels:'+item.id) || item;
    row.append(element('h3',item.name),element('p',`${item.type === 'qb' ? 'qBittorrent · 精确分类及逗号分隔标签' : 'Transmission · 分类字段与标签合并为 TR 标签'} · ${item.enabled ? '启用' : '停用'}`,'hint'));
    const grid = element('div',undefined,'form-grid connection-grid');
    for (const [key,title,max] of [['category',item.type === 'qb' ? '后续 qB 分类' : 'TR 标签组（兼容分类字段）',100],['tag',item.type === 'qb' ? '后续 qB 标签' : '后续 TR 标签',500]]) {
      const input = element('input'); input.name = key + '_' + item.id; input.dataset.labelField = key; input.maxLength = max; input.value = values[key] || ''; input.placeholder = key === 'tag' ? '多个标签用英文逗号分隔' : '留空不设置'; grid.append(cleanupMakeLabel(title,input));
    } row.append(grid); return row;
  }); $('instanceLabelsRows').replaceChildren(...(rows.length ? rows : [element('p','尚无已登记下载器，请先在下载器设置添加实例。','hint')])); updateSettingsAvailability();
}
function applyInstanceLabels(result,explicit = false) {
  if (!Array.isArray(result?.items) || !/^[a-f\d]{64}$/i.test(result.revision || '')) throw Error('实例响应缺少有效版本，分类草稿保留。');
  if (instanceLabelsDocument && instanceLabelDrafts.size && result.revision !== instanceLabelsDocument.revision && !explicit) instanceLabelsConflict = true;
  else { instanceLabelsDocument = result; instanceLabelsConflict = false; }
  renderInstanceLabels(); feedback('instanceLabelsFeedback',instanceLabelsConflict ? '实例版本已变更；分类草稿与旧版本保留。请点击顶部刷新，确认最新版本后核对。' : instanceLabelDrafts.size ? '分类与标签草稿保留；只影响后续任务。' : '分类与标签已读取；稳定实例 ID 保留。',instanceLabelsConflict ? 'error' : '');
}
function settingsValidationError(message,field) { const error = Error(message); error.field = field; return error; }
function focusSettingsError(error,form) {
  feedback('settingsFeedback',safeMessage(error.message),'error'); notice(safeMessage(error.message),true);
  const field = error.field || [...(form?.elements || [])].find(node => node.willValidate && !node.validity.valid) || form?.querySelector('input:not(:disabled),select:not(:disabled),textarea:not(:disabled)');
  if (field) { field.classList.add('invalid-field'); field.setAttribute('aria-invalid','true'); field.scrollIntoView({block:'center'}); field.focus({preventScroll:true}); }
}
function requireDirtyForm(id,collector,label) {
  const form = $(id), invalid = [...form.elements].find(field => field.willValidate && !field.validity.valid);
  if (invalid) throw settingsValidationError(label+'：'+invalid.validationMessage,invalid);
  try { return collector(); } catch (error) { if (!error.field) error.field = form.querySelector('input:not(:disabled),select:not(:disabled),textarea:not(:disabled)'); throw error; }
}
function collectSettingsDraft() {
  readCleanupEditor(); const entries = [], add = (key,label,form,values,feedbackId,document) => entries.push({key,label,form:$(form),values,feedbackId,document:document ? cleanupClone(document) : null});
  const conflict = (flag,label,field) => { if (flag) throw settingsValidationError(label+'存在版本冲突；点击顶部刷新接受最新版本，核对草稿后重试。',field); };
  if (configurationDirty) { conflict(configurationConflict,'站点配置',$('configurationRefresh')); if (!configurationData) throw Error('站点配置尚未读取。'); add('configuration','站点配置','configurationForm',requireDirtyForm('configurationForm',configurationPayload,'站点配置'),'configurationFeedback',configurationData); }
  if (settingsDirty) {
    const values = requireDirtyForm('settingsForm',collectRuntimeSettings,'运行参数');
    if (values.min_seeders > values.max_seeders) throw settingsValidationError('最低保种人数不能大于最高人数。',$('settingsForm').elements.min_seeders);
    add('runtime','运行参数','settingsForm',values,'settingsFeedback');
  }
  if (instanceLabelDrafts.size) {
    conflict(instanceLabelsConflict,'实例分类与标签',$('refreshButton')); if (!instanceLabelsDocument) throw Error('实例配置尚未读取。');
    requireDirtyForm('instanceLabelsForm',() => true,'实例分类与标签');
    for (const [id,values] of instanceLabelDrafts) { if (!instanceLabelsDocument.items.some(item => item.id === id)) throw settingsValidationError('分类草稿对应实例已被移除；请取消修改后重新读取。',$('instanceLabelsForm')); if (values.category.length > 100 || values.tag.length > 500 || /[\x00-\x1f]/.test(values.category+values.tag)) throw settingsValidationError('分类/标签长度超出范围或含控制字符。',$('instanceLabelsRows').querySelector('input')); add('labels:'+id,'分类与标签 · '+instanceLabelsDocument.items.find(item => item.id === id).name,'instanceLabelsForm',{id,...values},'instanceLabelsFeedback',instanceLabelsDocument); }
  }
  if (cleanupStorageDirty) {
    conflict(cleanupStorageConflict,'目录检查映射',$('cleanupStorageRefresh')); if (!cleanupStorageDocument) throw Error('目录映射尚未读取。');
    const values = requireDirtyForm('cleanupStorageForm',() => {
      readCleanupStorageEditor(); const absolute = path => typeof path === 'string' && path !== '/' && path.startsWith('/') && !/[\x00-\x1f\x7f]/.test(path) && !path.includes('\\') && !path.includes('//') && !path.split('/').some(part => part === '..' || part === '.');
      if (new Set(cleanupStorageDrafts.map(mapping => mapping.instance_id)).size !== cleanupStorageDrafts.length || cleanupStorageDrafts.some(mapping => !cleanupInstancesList().some(instance => instance.id === mapping.instance_id) || !absolute(mapping.download_root) || !absolute(mapping.inspect_root) || !mapping.inspect_root.startsWith('/inspect/') || mapping.inspect_root.length <= 9 || !(cleanupStorageDocument.allowed_roots || []).some(root => mapping.inspect_root === root || mapping.inspect_root.startsWith(root.replace(/\/$/,'')+'/')) || mapping.protected_paths.some(path => !absolute(path)))) throw Error('每实例需唯一映射，Linux 绝对目录及允许挂载根内的 /inspect 子目录；禁止 . / ..。');
      return {mappings:cleanupClone(cleanupStorageDrafts)};
    },'目录映射'); add('storage','目录检查映射','cleanupStorageForm',values,'cleanupStorageFeedback',cleanupStorageDocument);
  }
  if (cleanupDirty) {
    if (!cleanupPolicy) throw Error('旧清理策略尚未读取。'); const values = requireDirtyForm('cleanupForm',() => { const result = formValues($('cleanupForm'),Object.keys(policyDefaults).filter(key => key !== 'unregistered_instances')); result.unregistered_instances = [...$('cleanupForm').querySelectorAll('[name=unregistered_instances]:checked')].map(field => field.value); return result; },'任务清理策略'); add('policy','任务清理策略','cleanupForm',values,'cleanupFeedback');
  }
  if (transferRuleDirty) {
    conflict(transferRuleConflict,'转种规则',$('transferRulesRefresh')); if (!transferRuleDocument) throw Error('转种规则尚未读取。');
    const values = requireDirtyForm('transferRulesForm',collectTransferRuleValues,'转种规则');
    if (!transferRuleInstances('qb').some(item => item.id === values.source_instance_id) || !transferRuleInstances('tr').some(item => item.id === values.target_instance_id)) throw settingsValidationError('请选择启用的 qB 来源和 TR 目的。',$('transferRulesForm').elements.source_instance_id);
    if (values.enabled && !values.path_mappings.length) throw settingsValidationError('启用自动转种需填写已确认内容路径映射。',$('transferRulesForm').elements.path_mappings);
    if (new Set(values.path_mappings.map(mapping => mapping.qb)).size !== values.path_mappings.length) throw settingsValidationError('规则路径映射 qB 根目录重复。',$('transferRulesForm').elements.path_mappings);
    add('transfer','转种规则','transferRulesForm',values,'transferRulesFeedback',transferRuleDocument);
  }
  if (categoryCleanupDirty) { conflict(categoryCleanupConflict,'分类清理规则',$('categoryCleanupRefresh')); if (!categoryCleanupDocument) throw Error('分类清理规则尚未读取。'); add('category','分类清理规则','categoryCleanupForm',requireDirtyForm('categoryCleanupForm',validateCleanupRules,'分类清理规则'),'categoryCleanupFeedback',categoryCleanupDocument); }
  if (logPolicyDirty) { if (!logPolicy) throw Error('日志策略尚未读取。'); add('logs','日志保留策略','logPolicyForm',requireDirtyForm('logPolicyForm',() => formValues($('logPolicyForm'),['auto_cleanup_enabled','retention_days','cleanup_interval_hours']),'日志策略'),'logPolicyFeedback'); }
  return entries;
}
function invalidateSettingsRequests() {
  settingsRevision++; configurationGeneration++; cleanupVersion++; logPolicyVersion++; instancesVersion++; instanceLabelsVersion++;
  transferRuleRequestVersion++; categoryCleanupRequest++; cleanupStorageRequest++;
  configurationLoading = null; policyLoading = null; logPolicyLoading = null; transferRuleLoading = null; categoryCleanupLoading = null; cleanupStorageLoading = null;
}
function comparableSettings(value,paused = false) {
  const copy = cleanupClone(value); if (paused) { if ('enabled' in copy) copy.enabled = false; for (const rule of copy.rules || []) if (rule.enabled) { rule.enabled = false; rule.observe_only = true; } }
  const canonical = input => Array.isArray(input) ? input.map(canonical) : input && typeof input === 'object' ? Object.fromEntries(Object.keys(input).sort().map(key => [key,canonical(input[key])])) : input;
  return JSON.stringify(canonical(copy));
}
function settingsConflictError(message) { const error = Error(message); error.status = 409; return error; }
function prepareFailedSettingsRetry() {
  if (settingsPendingDocument?.status !== 'failed' || !hasPendingSettings()) return;
  for (const entry of settingsPendingDocument.entries) {
    if (entry.key === 'runtime') settingsDirty = true;
    else if (entry.key === 'configuration') configurationDirty = true;
    else if (entry.key === 'storage') cleanupStorageDirty = true;
    else if (entry.key === 'policy') cleanupDirty = true;
    else if (entry.key === 'transfer') transferRuleDirty = true;
    else if (entry.key === 'category') categoryCleanupDirty = true;
    else if (entry.key === 'logs') logPolicyDirty = true;
    else if (entry.key.startsWith('labels:') && !instanceLabelDrafts.has(entry.values.id)) instanceLabelDrafts.set(entry.values.id,{category:entry.values.category,tag:entry.values.tag});
  }
}
async function saveAllSettings() {
  if (!status || settingsBlocked() || settingsReadBusy || configurationBusy || transferRulePending || categoryCleanupPending) { feedback('settingsFeedback','设置正在读取或提交；草稿保留，请稍后重试。','error'); return false; }
  let entries; try { prepareFailedSettingsRetry(); entries = collectSettingsDraft(); } catch (error) { focusSettingsError(error); return false; }
  if (!entries.length) { feedback('settingsFeedback','没有未保存修改。'); return true; }
  const transfer = entries.find(entry => entry.key === 'transfer'), policy = entries.find(entry => entry.key === 'policy');
  if ((transfer?.values.enabled || policy?.values.cleanup_enabled || policy?.values.unregistered_enabled) && !window.confirm('保存包含已勾选自动转种或任务清理的草稿？这些开关会按保存规则调度；分类数据删除仍需另行明确启用。配置变更导致的安全暂停将保留。')) { feedback('settingsFeedback','保存已取消；全部草稿保留。'); return false; }
  const generation = queryGeneration, owner = ++busyEpoch, completed = []; let current = null, paused = false, dependenciesChanged = false;
  let registryRevision = instanceLabelsDocument?.revision || transferRuleDocument?.instances_revision || categoryCleanupDocument?.instances_revision, storageRevision = cleanupStorageDocument?.revision;
  const lockedButtons = new Map(); busy = true; unifiedSaving = true; invalidateSettingsRequests(); syncControls();
  tokenGeneration++; if (tokenFetched) clearConfigurationSecrets(); else { $('configurationForm').elements.token.type = 'password'; text('tokenVisibility','显示'); $('tokenVisibility').setAttribute('aria-pressed','false'); }
  for (const button of $('settings').querySelectorAll('button')) { lockedButtons.set(button,button.disabled); button.disabled = true; }
  $('settings').setAttribute('aria-busy','true'); const guard = () => { if (!status || generation !== queryGeneration) { const error = Error('会话已变更'); error.stale = true; throw error; } };
  try {
    const staged = await api('settings/defer',{entries:entries.map(entry => ({key:entry.key,values:entry.values,...(entry.document ? {document:{revision:entry.document.revision,instances_revision:entry.document.instances_revision}} : {}),...(entry.key === 'transfer' && entry.values.enabled ? {confirm:'ENABLE_AUTOMATIC_QB_TO_TR_KEEP_DATA'} : {})}))}); guard();
    if (staged.deferred === true) {
      if (!staged.pending || !Array.isArray(staged.pending.entries)) throw Error('待生效保存回执不完整；草稿保留，请刷新核对。');
      for (const entry of entries) { if (entry.key === 'runtime') settingsDirty = false; else if (entry.key === 'configuration') { configurationDirty = false; clearConfigurationSecrets(); } else if (entry.key.startsWith('labels:')) instanceLabelDrafts.delete(entry.values.id); else if (entry.key === 'storage') cleanupStorageDirty = false; else if (entry.key === 'category') categoryCleanupDirty = false; else if (entry.key === 'transfer') transferRuleDirty = false; else if (entry.key === 'policy') cleanupDirty = false; else if (entry.key === 'logs') logPolicyDirty = false; completed.push(entry.label); feedback(entry.feedbackId,'已保存，等待当前作业结束后生效。','success'); }
      applyPendingSettings(staged.pending); const message = `已保存：${completed.join('、')}。等待当前作业结束后自动生效，本轮继续使用原参数。`; feedback('settingsFeedback',message,'success'); notice(message); return true;
    }
    if (staged.deferred !== false) throw Error('设置保存回执缺少生效状态；草稿保留。');
    for (const entry of entries) {
      current = entry; guard(); feedback('settingsFeedback',`正在保存 ${entry.label}；已保存：${completed.join('、') || '无'}。`); let result;
      if (entry.key === 'configuration') {
        result = await api('configuration',entry.values); guard(); clearConfigurationSecrets(); configurationDirty = false; configurationConflict = false; applyConfiguration(result.configuration || result); paused ||= result.automation_paused === true; dependenciesChanged = true;
      } else if (entry.key === 'runtime') {
        result = await api('settings',entry.values); guard(); status.settings = {...status.settings,...entry.values,max_bytes:entry.values.max_size_mib*1048576}; settingsDirty = false; fillRuntimeSettings(status.settings); dependenciesChanged = true;
      } else if (entry.key.startsWith('labels:')) {
        const row = instanceLabelsDocument.items.find(item => item.id === entry.values.id); if (!row) throw settingsConflictError('分类对应实例已移除。');
        const instance = {}; for (const key of ['id','type','name','url','username','enabled','default','download_path','category','tag','keep_torrent','use_proxy','proxy_url']) instance[key] = row[key]; instance.category = entry.values.category; instance.tag = entry.values.tag;
        result = await api('instances/save',{revision:registryRevision,instance}); guard();
        if (!Array.isArray(result.items) || !result.items.some(item => item.id === instance.id) || !result.revision) throw Error('实例保存回执缺少稳定 ID 或版本；草稿保留，请刷新核对。');
        registryRevision = result.revision; instanceLabelsDocument = result; instanceLabelDrafts.delete(instance.id); instanceLabelsConflict = false; instanceLabelsVersion++; applyInstances(result); renderInstanceLabels(); paused ||= result.automation_paused === true; dependenciesChanged = true;
      } else if (entry.key === 'storage') {
        result = await api('fleet/cleanup/storage',{revision:entry.document.revision,values:entry.values}); guard();
        if (!Array.isArray(result?.values?.mappings) || !result.revision) throw Error('映射保存回执不完整，请刷新核对。');
        cleanupStorageDocument = result; storageRevision = result.revision; cleanupStorageDirty = false; cleanupStorageConflict = false; cleanupStorageDrafts = cleanupClone(result.values.mappings); renderCleanupStorageEditor(); dependenciesChanged = true;
      } else if (entry.key === 'policy') {
        const values = paused ? {...entry.values,cleanup_enabled:false,unregistered_enabled:false} : entry.values;
        result = await api('fleet/policy',values); guard(); cleanupPolicy = result.policy || values; cleanupDirty = false; renderCleanup();
      } else if (entry.key === 'transfer' || entry.key === 'category') {
        const isCleanup = entry.key === 'category', path = isCleanup ? 'fleet/cleanup/settings' : 'fleet/transfer/settings'; let revision = entry.document.revision, values = cleanupClone(entry.values);
        if (dependenciesChanged) {
          const fresh = await api(path); guard();
          const same = comparableSettings(fresh.values) === comparableSettings(entry.document.values) || comparableSettings(fresh.values) === comparableSettings(entry.document.values,true);
          if (!same || !fresh.revision || fresh.instances_revision !== registryRevision) throw settingsConflictError('规则或实例有无法归因于本次保存的变化；未接受新版本，请刷新核对草稿。');
          if (isCleanup && storageRevision) { const storage = await api('fleet/cleanup/storage'); guard(); if (storage.revision !== storageRevision) throw settingsConflictError('目录映射在保存期间被其他页面修改。'); }
          revision = fresh.revision;
          if (isCleanup) { for (const rule of values.rules) { const saved = fresh.values.rules.find(item => item.id === rule.id); if (!saved?.enabled) { rule.enabled = false; if (saved?.observe_only) rule.observe_only = true; } } categoryCleanupDocument = {...categoryCleanupDocument,revision:fresh.revision,instances_revision:fresh.instances_revision,values:fresh.values}; }
          else { if (paused || (entry.document.values.enabled && !fresh.values.enabled)) values.enabled = false; transferRuleDocument = {...transferRuleDocument,revision:fresh.revision,instances_revision:fresh.instances_revision,values:fresh.values}; }
        }
        result = await api(path,{revision,values,...(!isCleanup && values.enabled ? {confirm:'ENABLE_AUTOMATIC_QB_TO_TR_KEEP_DATA'} : {})}); guard();
        if (!result.values || !result.revision || !result.instances_revision) throw Error('规则保存回执不完整；请刷新核对，草稿保留。');
        if (isCleanup) { categoryCleanupDirty = false; categoryCleanupConflict = false; categoryCleanupEdit++; applyCategoryCleanupDocument(result,true); clearCategoryCleanupPreview('规则已保存，请重新只读预览。'); }
        else { transferRuleDirty = false; transferRuleConflict = false; transferRuleEditVersion++; applyTransferRuleDocument(result); clearTransferRulePreview('规则已保存，请重新预览。'); }
      } else if (entry.key === 'logs') { result = await api('logs/settings',entry.values); guard(); logPolicy = result.policy || entry.values; logPolicyDirty = false; renderLogPolicy(); }
      completed.push(entry.label); feedback(entry.feedbackId,`${entry.label}已保存。`,'success');
    }
    guard(); const message = `已保存：${completed.join('、')}。${paused ? '配置变化引起的自动化暂停已保留。' : ''}没有提交手动管理作业。`; feedback('settingsFeedback',message,'success'); notice(message); return true;
  } catch (error) {
    if (!error.stale && generation === queryGeneration && status) {
      if (error.status === 409) for (const failed of current ? [current] : entries) { if (failed.key === 'configuration') configurationConflict = true; if (failed.key === 'storage') cleanupStorageConflict = true; if (failed.key === 'transfer') transferRuleConflict = true; if (failed.key === 'category') categoryCleanupConflict = true; if (failed.key.startsWith('labels:')) instanceLabelsConflict = true; }
      const pending = entries.slice(completed.length).map(entry => entry.label), message = `已保存：${completed.join('、') || '无'}。未保存：${pending.join('、')}；失败于${current?.label || '校验'}：${safeMessage(error.message)}。${error.status === 409 ? '请点击顶部刷新，确认接受最新版本并核对后重试。' : '失败及尚未保存草稿保留，可修正后重试。'}`;
      if (current) feedback(current.feedbackId,message,'error'); feedback('settingsFeedback',message,'error'); notice(message,true);
    } return false;
  } finally {
    if (owner === busyEpoch) {
      const finalText = $('settingsFeedback').textContent, finalClass = $('settingsFeedback').className;
      busy = false; unifiedSaving = false; settingsRevision++; for (const [button,disabled] of lockedButtons) if (button.isConnected) button.disabled = disabled; $('settings').setAttribute('aria-busy','false');
      if (generation === queryGeneration && status) {
        syncControls(); updateSettingsAvailability(); renderInstanceLabels();
        if (completed.length) {
          const synchronized = await refreshAllSettings(false);
          if (generation === queryGeneration && status && owner === busyEpoch) {
            $('settingsFeedback').textContent = finalText + (synchronized ? '' : ' 部分版本读取失败，请顶部刷新后核对；未保存草稿保留。');
            $('settingsFeedback').className = finalClass;
          }
        }
      }
    }
  }
}
const settingsReadPaths = ['settings/pending','status','configuration','fleet/policy','fleet/cleanup/settings','fleet/cleanup/storage','fleet/transfer/settings','logs/settings','instances','pts'];
function validateSettingsRead(path,result) {
  if (result?.ok === false) throw Error('设置读取未完成。');
  if (path === 'status' && !result?.settings) throw Error('运行设置响应不完整。');
  if (['configuration','fleet/cleanup/settings','fleet/cleanup/storage','fleet/transfer/settings','instances'].includes(path) && !/^[a-f\d]{64}$/i.test(result?.revision || '')) throw Error('设置响应缺少有效版本。');
  if (path === 'configuration' && !result.values) throw Error('站点配置响应不完整。');
  if (path === 'fleet/cleanup/settings' && (!Array.isArray(result.values?.rules) || !result.instances_revision)) throw Error('清理规则响应不完整。');
  if (path === 'fleet/cleanup/storage' && !Array.isArray(result.values?.mappings)) throw Error('映射响应不完整。');
  if (path === 'fleet/transfer/settings' && (!result.values || !result.instances_revision)) throw Error('转种规则响应不完整。');
  if (path === 'instances' && !Array.isArray(result.items)) throw Error('实例响应不完整。');
  if (path === 'settings/pending' && (!Array.isArray(result?.entries) || !Number.isInteger(result?.count) || result.count !== result.entries.length || !['none','pending','failed','applied'].includes(result.status))) throw Error('待生效设置响应不完整。'); return result;
}
async function readAllSettings() { return Promise.allSettled(settingsReadPaths.map(async path => ({path,result:validateSettingsRead(path,await api(path))}))); }
function applySettingsRead(path,result,explicit = false) {
  if (path === 'status') { status = result; renderStatus(); syncPolling(); }
  else if (path === 'configuration') { applyConfiguration(result,configurationDirty,explicit); if (explicit) configurationConflict = false; feedback('configurationFeedback',configurationConflict ? '站点版本变化；草稿与旧版本保留，请点击顶部刷新。' : '站点已读取；非秘密草稿保留。',configurationConflict ? 'error' : ''); }
  else if (path === 'fleet/policy') { cleanupPolicy = result.policy || result; renderCleanup(); feedback('cleanupFeedback',cleanupDirty ? '已读取；清理草稿保留。' : '清理策略已读取。'); }
  else if (path === 'fleet/cleanup/settings') applyCategoryCleanupDocument(result,explicit);
  else if (path === 'fleet/cleanup/storage') {
    if (cleanupStorageDocument && cleanupStorageDirty && !explicit) { if (result.revision !== cleanupStorageDocument.revision) cleanupStorageConflict = true; }
    else { cleanupStorageDocument = result; cleanupStorageConflict = false; if (!cleanupStorageDirty) { cleanupStorageDrafts = cleanupClone(pendingSettingsValue('storage')?.mappings || result.values.mappings); renderCleanupStorageEditor(); } }
    feedback('cleanupStorageFeedback',cleanupStorageConflict ? '映射版本变化；草稿与旧版本保留，请点击顶部刷新。' : cleanupStorageDirty ? '映射已读取；草稿保留。' : '映射已读取。',cleanupStorageConflict ? 'error' : '');
  } else if (path === 'fleet/transfer/settings') applyTransferRuleDocument(result,explicit);
  else if (path === 'logs/settings') { logPolicy = result.policy || result; renderLogPolicy(); feedback('logPolicyFeedback',logPolicyDirty ? '日志策略已读取；草稿保留。' : '日志策略已读取。'); }
  else if (path === 'instances') { applyInstanceLabels(result,explicit); applyInstances(result); fillTransferRuleInstanceOptions(); }
  else if (path === 'pts') { ptsData = result; renderPts(); updateRecommendationAvailability(); }
  else if (path === 'settings/pending') applyPendingSettings(result);
}
async function refreshAllSettings(explicit = false) {
  if (!status || busy || settingsReadBusy || configurationBusy || transferRulePending || categoryCleanupPending) return false;
  const dirty = settingsDirty || configurationDirty || cleanupDirty || categoryCleanupDirty || cleanupStorageDirty || transferRuleDirty || logPolicyDirty || instanceLabelDrafts.size;
  if (explicit && dirty && !window.confirm('读取全部最新已保存版本并保留非秘密草稿？Token 将清除；接受新版本后请核对范围、路径与自动化开关，再保存。')) return false;
  const generation = queryGeneration, request = ++settingsReadEpoch; readCleanupEditor(); invalidateSettingsRequests(); if (explicit) clearConfigurationSecrets(); settingsReadBusy = true; updateSettingsAvailability();
  const edits = {configuration:configurationEditVersion,category:categoryCleanupEdit,storage:cleanupStorageEdit,transfer:transferRuleEditVersion,labels:instanceLabelsVersion};
  try {
    const results = await readAllSettings(); if (!status || generation !== queryGeneration || request !== settingsReadEpoch) return false;
    const failures = [];
    for (let index = 0; index < results.length; index++) {
      const result = results[index]; if (result.status === 'rejected') { failures.push(settingsReadPaths[index]+'：'+safeMessage(result.reason.message)); continue; }
      const {path,result:document} = result.value; const key = {'configuration':'configuration','fleet/cleanup/settings':'category','fleet/cleanup/storage':'storage','fleet/transfer/settings':'transfer','instances':'labels'}[path];
      const versions = {configuration:configurationEditVersion,category:categoryCleanupEdit,storage:cleanupStorageEdit,transfer:transferRuleEditVersion,labels:instanceLabelsVersion};
      applySettingsRead(path,document,explicit && (!key || edits[key] === versions[key]));
    }
    feedback('settingsFeedback',failures.length ? '部分来源读取失败，草稿保留：'+failures.join('；') : '全部设置来源已读取；未保存草稿保留，请核对后保存。',failures.length ? 'error' : ''); return !failures.length;
  } catch (error) { if (generation === queryGeneration && status) feedback('settingsFeedback',safeMessage(error.message)+'；草稿保留。','error'); return false; }
  finally { if (generation === queryGeneration && request === settingsReadEpoch) { settingsReadBusy = false; syncControls(); updateSettingsAvailability(); } }
}
async function cancelAllSettings() {
  if (!status || busy || settingsReadBusy || configurationBusy || transferRulePending || categoryCleanupPending) return false;
  const generation = queryGeneration, owner = ++busyEpoch, request = ++settingsReadEpoch; busy = true; settingsReadBusy = true; clearConfigurationSecrets(); clearInstanceSecret(); readCleanupEditor(); invalidateSettingsRequests(); syncControls(); updateSettingsAvailability();
  try {
    const results = await readAllSettings(); if (!status || generation !== queryGeneration || request !== settingsReadEpoch) return false;
    const failed = results.filter(result => result.status === 'rejected'); if (failed.length) throw Error('取消读取未完整成功；非秘密草稿仍保留：'+failed.map(result => safeMessage(result.reason.message)).join('；'));
    settingsDirty = false; configurationDirty = false; cleanupDirty = false; categoryCleanupDirty = false; cleanupStorageDirty = false; transferRuleDirty = false; logPolicyDirty = false; instanceLabelDrafts.clear();
    configurationConflict = false; categoryCleanupConflict = false; cleanupStorageConflict = false; transferRuleConflict = false; instanceLabelsConflict = false;
    for (const result of results) applySettingsRead(result.value.path,result.value.result,true);
    clearConfigurationSecrets(); clearCategoryCleanupPreview('未保存草稿已取消；请重新预览。'); clearTransferRulePreview('未保存草稿已取消；请重新预览。'); $('configurationResults').hidden = true;
    for (const field of $('settings').querySelectorAll('.invalid-field')) { field.classList.remove('invalid-field'); field.removeAttribute('aria-invalid'); }
    feedback('settingsFeedback','全部未保存设置已取消；恢复最新已保存状态，包含等待生效的设置。秘密已清除。','success'); return true;
  } catch (error) { if (status && generation === queryGeneration) feedback('settingsFeedback',safeMessage(error.message),'error'); return false; }
  finally { if (owner === busyEpoch) { busy = false; settingsReadBusy = false; settingsRevision++; if (generation === queryGeneration && status) { syncControls(); updateSettingsAvailability(); } } }
}
function installUnifiedSettings() {
  for (const form of $('settings').querySelectorAll('form')) form.noValidate = true;
  $('saveButton').addEventListener('click',saveAllSettings); $('settingsCancel').addEventListener('click',cancelAllSettings);
  $('instanceLabelsForm').addEventListener('input',event => { const row = event.target.closest('[data-instance-id]'); if (!row) return; const values = Object.fromEntries([...row.querySelectorAll('[data-label-field]')].map(field => [field.dataset.labelField,field.value.trim()])); instanceLabelDrafts.set(row.dataset.instanceId,values); instanceLabelsVersion++; feedback('instanceLabelsFeedback','分类与标签有未保存草稿；顶部统一保存，只影响后续新任务。'); });
  $('instanceLabelsForm').addEventListener('submit',event => { event.preventDefault(); saveAllSettings(); });
  $('settings').addEventListener('keydown',event => { if (event.key === 'Enter' && !event.isComposing && event.target.tagName === 'INPUT' && event.target.type !== 'checkbox' && !event.ctrlKey && !event.altKey && !event.shiftKey) { event.preventDefault(); saveAllSettings(); } });
  $('settings').addEventListener('input',event => { if (event.target.classList.contains('invalid-field')) { event.target.classList.remove('invalid-field'); event.target.removeAttribute('aria-invalid'); } });
  updateSettingsAvailability();
}
renderCategoryCleanupAvailability();
buildGroupedControls();
arrangeSettingsSections();
installFieldHelp();
installTokenVisibility();
installUnifiedSettings();
refresh();
for (const id of ['logSearch','logLevel','logTime']) $(id).addEventListener(id === 'logSearch' ? 'input' : 'change',() => renderLogs());
$('taskRulesRun').addEventListener('click',() => runTransferRules());
