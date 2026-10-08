(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const STATES = { pending: '等待上报', retry: '等待重试', inflight: '正在上报', uncertain: '结果待确认', reported: '上报成功', existing: '云端已有', failed: '上报失败', blocked: '已阻止' };
  const ACTIONS = { collect_now: '立即采集', pause_reporting: '暂停上报', resume_reporting: '恢复上报', retry_failed: '重试失败项', import: '导入链接' };
  const MAX_BYTES = 4 * 1024 * 1024;
  const state = { authenticated: false, csrf: '', epoch: 0, status: null, items: [], total: 0, page: 1, pageSize: 25, filter: 'all', query: '', poll: null, refreshing: false, statusBusy: false, itemsController: null, itemVersion: 0, itemsSignature: '', job: null, jobTimer: null, jobFailures: 0, submitting: false, previewBusy: false, importBusy: false, previewText: null, preview: null, detail: null, confirmAction: null };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('icon'); svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name); svg.append(use); return svg;
  }
  function text(id, value) { $(id).textContent = String(value ?? '—'); }
  function number(value) { const n = Number(value); return Number.isFinite(n) && n >= 0 ? n : 0; }
  function count(value) { return number(value).toLocaleString('zh-CN'); }
  function dateValue(value) {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    const d = typeof value === 'number' || /^\d+(\.\d+)?$/.test(String(value)) ? new Date(n < 1e12 ? n * 1000 : n) : new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  function time(value, full = false) {
    const d = dateValue(value);
    return d ? new Intl.DateTimeFormat('zh-CN', { ...(full ? { year: 'numeric' } : {}), month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(d) : '暂无记录';
  }
  function size(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n < 0) return '—';
    if (n < 1024) return n.toLocaleString('zh-CN') + ' B';
    const units = ['KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)) - 1, units.length - 1);
    return (n / Math.pow(1024, i + 1)).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + ' ' + units[i];
  }
  function errorMessage(error, context) {
    if (context === 'login' && error.code === 'login_failed') return '用户名或密码不正确，请重新输入。';
    if (error.status === 401) return '登录已失效，请重新登录。';
    if (error.code === 'login_throttled' || error.status === 429) return '操作较频繁，请稍后再试。';
    if (context === 'login' && (error.status === 400 || error.status === 403)) return '用户名或密码不正确，请重新输入。';
    if (context === 'password' && ['invalid_password', 'password_incorrect', 'current_password_invalid', 'wrong_password', 'password_failed', 'current_password_incorrect'].includes(error.code)) return '当前密码不正确，请重新输入。';
    if (error.status === 413) return '内容超过大小限制，请减少文本或选择不超过 4 MiB 的文件。';
    if (error.status === 503) return '服务暂时繁忙，请稍后再试。';
    if (error.status === 403) return context === 'password' ? '当前密码不正确，或登录已失效。请核对后重新登录。' : '本次操作未获允许，请刷新页面或重新登录后再试。';
    if (error.status === 400) return context === 'password' ? '请核对当前密码，新密码须为 12–128 个字符。' : '提交内容无法识别，请检查内容后再试。';
    if (error.status === 404) return '操作记录已过期或服务已重启。请查看最新状态，本次操作不会自动重发。';
    if (error.code === 'timeout') return '连接超时，请检查服务是否正常后重试。';
    if (error.code === 'invalid_response') return '服务返回的数据暂时无法读取，请稍后重试。';
    return '连接暂时中断，请检查网络或服务状态后重试。';
  }
  function showError(id, message) { text(id, message); $(id).hidden = !message; }
  function toast(message, isError = false) {
    const node = el('div', 'toast' + (isError ? ' error' : ''));
    node.setAttribute('role', isError ? 'alert' : 'status');
    const close = el('button', 'icon-button'); close.setAttribute('aria-label', '关闭通知'); close.append(icon('close'));
    close.addEventListener('click', () => node.remove());
    node.append(icon(isError ? 'alert' : 'check'), el('span', '', message), close);
    $('toast-region').append(node);
    while ($('toast-region').children.length > 4) $('toast-region').firstElementChild.remove();
    setTimeout(() => node.remove(), isError ? 14000 : 7000);
  }
  function busy(button, active, label) {
    if (active && !button.dataset.normalLabel) button.dataset.normalLabel = button.textContent.trim();
    button.disabled = active; button.setAttribute('aria-busy', String(active));
    if (label) button.textContent = label;
    else if (!active && button.dataset.normalLabel) { button.textContent = button.dataset.normalLabel; delete button.dataset.normalLabel; }
  }

  const requestControllers = new Set();
  async function api(path, options = {}) {
    const controller = new AbortController(), requestEpoch = state.epoch;
    requestControllers.add(controller);
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 18000);
    const external = options.signal;
    const abort = () => controller.abort();
    if (external?.aborted) controller.abort();
    else external?.addEventListener('abort', abort, { once: true });
    const headers = { Accept: 'application/json' };
    if (options.body !== undefined) { headers['Content-Type'] = 'application/json'; if (path !== '/api/login') headers['X-CSRF-Token'] = state.csrf; }
    try {
      const response = await fetch(path, { method: options.body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: controller.signal });
      if (response.status === 401 && path !== '/api/login') { if (requestEpoch === state.epoch) showLogin('登录已失效，请重新登录。'); throw { status: 401 }; }
      if (options.blob && response.ok) return await response.blob();
      let data;
      try { data = await response.json(); } catch { throw { status: response.status, code: 'invalid_response' }; }
      if (!response.ok) throw { status: response.status, code: typeof data.error === 'string' ? data.error : '' };
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw { code: timedOut ? 'timeout' : 'aborted' };
      throw error;
    } finally { clearTimeout(timeout); requestControllers.delete(controller); external?.removeEventListener('abort', abort); }
  }

  function clearPolling() { clearTimeout(state.poll); state.poll = null; clearTimeout(state.jobTimer); state.jobTimer = null; }
  function showLogin(message = '') {
    state.authenticated = false; state.csrf = ''; state.epoch += 1; state.status = null; state.job = null; state.submitting = false; state.itemsSignature = ''; state.items = []; state.detail = null;
    clearPolling(); requestControllers.forEach((controller) => controller.abort()); requestControllers.clear(); state.itemsController?.abort();
    document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close());
    $('password-form').reset(); $('password').value = ''; $('import-text').value = ''; resetPreview();
    $('app-view').hidden = true; $('boot').hidden = true; $('login-view').hidden = false;
    showError('login-error', message); updateBusy();
    setTimeout(() => { if (!state.authenticated) (message ? $('password') : $('username')).focus(); }, 0);
  }
  function enterApp(session) {
    state.authenticated = true; state.csrf = session.csrf_token || ''; state.epoch += 1;
    $('password').value = ''; $('login-view').hidden = true; $('boot').hidden = true; $('app-view').hidden = false;
    text('account-name', session.username || '管理员');
    showError('login-error', ''); $('global-error').hidden = true; $('job-notice').hidden = true;
    updateBusy(); refresh();
  }
  function scheduleRefresh() {
    clearTimeout(state.poll);
    if (state.authenticated && !document.hidden) state.poll = setTimeout(refresh, 5000);
  }
  function globalError(error) {
    $('global-error').hidden = false; text('global-error-text', errorMessage(error));
    text('service-text', '连接暂时中断'); $('service-dot').classList.add('offline');
  }
  async function refresh() {
    if (!state.authenticated || state.refreshing) return;
    state.refreshing = true;
    const epoch = state.epoch;
    $('refresh-button').disabled = true;
    await Promise.allSettled([loadStatus(epoch), loadItems(false)]);
    state.refreshing = false; $('refresh-button').disabled = false;
    if (epoch === state.epoch) scheduleRefresh();
    else if (state.authenticated) refresh();
  }
  async function loadStatus(epoch = state.epoch) {
    if (state.statusBusy || !state.authenticated) return;
    state.statusBusy = true;
    try {
      const data = await api('/api/status');
      if (!state.authenticated || epoch !== state.epoch) return;
      if (!data.status || !data.settings || !data.runtime) throw { code: 'invalid_response' };
      state.status = data; renderStatus(data); $('global-error').hidden = true;
      text('service-text', '服务已连接'); $('service-dot').classList.remove('offline');
    } catch (error) { if (state.authenticated && epoch === state.epoch && error.code !== 'aborted') globalError(error); }
    finally { state.statusBusy = false; updateBusy(); }
  }
  function renderStatus(data) {
    const s = data.status, settings = data.settings, runtime = data.runtime, c = s.counts || {};
    text('stat-reported', count(c.reported)); text('stat-existing', count(c.existing));
    text('stat-pending', count(number(c.pending) + number(c.retry) + number(c.inflight) + number(c.uncertain)));
    text('stat-attention', count(number(c.failed) + number(c.blocked)));
    text('attention-detail', '失败 ' + count(c.failed) + ' · 已阻止 ' + count(c.blocked));
    text('nav-record-count', count(s.total_unique)); text('source-messages', count(s.source_messages)); text('repaired-links', count(s.repaired_links)); text('parse-errors', count(s.parse_errors));
    text('last-sync', '最后同步：' + time(s.last_cycle?.at || s.heartbeat));
    text('cycle-status', runtime.cycle_running ? '正在同步' : '等待下一次采集');
    text('next-cycle', runtime.cycle_running ? '采集与上报正在处理中' : runtime.next_cycle_at ? '下一周期 ' + time(runtime.next_cycle_at) : '下次采集时间待定');
    const paused = Boolean(s.report_pause) || settings.report_enabled === false;
    $('pause-notice').hidden = !(paused || runtime.pause_requested);
    text('pause-title', runtime.pause_requested ? '正在暂停上报' : '上报已暂停');
    text('pause-description', runtime.pause_requested ? '当前请求结束后暂停；频道采集仍会继续。' : pauseDescription(s.report_pause, settings));
    $('cycle-dot').classList.toggle('paused', paused || runtime.pause_requested);
    const button = $('pause-button'); button.replaceChildren(icon(paused ? 'play' : 'pause'), el('span', '', runtime.pause_requested ? '正在暂停…' : paused ? '恢复上报' : '暂停上报'));
    button.dataset.action = paused ? 'resume_reporting' : 'pause_reporting';
    text('bootstrap-note', '首次采集最近 ' + number(settings.initial_days) + ' 天的消息，之后从上次进度继续。');
    renderChannels(Array.isArray(s.channels) ? s.channels : [], settings.channels);
    renderEvents(Array.isArray(data.recent_events) ? data.recent_events : [], s.last_cycle);
  }
  function pauseDescription(pause, settings) {
    if (settings.report_enabled === false) return '服务配置已关闭自动上报；频道采集仍会继续。';
    const reasons = { manual: '已手动暂停。点击“恢复上报”继续。', user: '已手动暂停。点击“恢复上报”继续。', user_requested: '已手动暂停。点击“恢复上报”继续。', rate_limit: '云端要求暂缓处理，请稍后恢复上报。', rate_limited: '云端要求暂缓处理，请稍后恢复上报。', authentication: '云端验证暂未通过，请检查服务设置后恢复。', auth_failed: '云端验证暂未通过，请检查服务设置后恢复。', quota: '云端处理额度暂不可用，请稍后恢复。' };
    return (reasons[pause?.reason] || '上报暂时停止。请确认服务状态后恢复；频道采集仍会继续。') + (pause?.at ? ' 暂停于 ' + time(pause.at) + '。' : '');
  }
  function channelName(value) { return typeof value === 'string' ? value.replace(/^@/, '') : ''; }
  function telegramURL(channel, message) {
    const name = channelName(channel);
    if (!/^[a-zA-Z][a-zA-Z0-9_]{4,31}$/.test(name) || ['web', 'inbox'].includes(name.toLowerCase())) return null;
    if (message !== null && message !== undefined && !/^[1-9]\d*$/.test(String(message))) return null;
    return 'https://t.me/' + name + (message === null || message === undefined ? '' : '/' + String(message));
  }
  function sourceNode(channel, message) {
    const label = channel === 'web' ? '网页导入' : channel === 'inbox' ? 'TXT 导入' : channel ? '@' + channelName(channel) : '来源未记录';
    const url = telegramURL(channel, message);
    const node = el(url ? 'a' : 'span', 'source-link', label);
    if (url) { node.href = url; node.target = '_blank'; node.rel = 'noopener noreferrer'; }
    return node;
  }
  function renderChannels(channels, configured) {
    const list = $('channel-list'); list.replaceChildren();
    const fallback = Array.isArray(configured) ? configured.filter((v) => typeof v === 'string').map((name) => ({ name })) : [];
    const entries = channels.length ? channels : fallback;
    text('source-subtitle', entries.length ? count(entries.length) + ' 个频道 · 持续增量采集' : '尚未配置频道');
    if (!entries.length) list.append(el('p', 'muted', '暂无采集来源，可先导入链接。'));
    entries.forEach((channel) => {
      const row = el('div', 'channel-row'), avatar = el('span', 'channel-avatar'), info = el('div', 'channel-info'), title = el('div', 'channel-title');
      avatar.append(icon('source'));
      title.append(sourceNode(channel.name, null), el('span', 'channel-state' + (channel.bootstrap_done ? '' : ' initial'), channel.bootstrap_done ? '增量采集' : '首次采集'));
      info.append(title, el('p', 'channel-meta', '最近成功 ' + time(channel.last_success)), el('p', 'channel-meta', channel.cursor !== null && channel.cursor !== undefined ? '已采集至消息 ' + String(channel.cursor) : '等待首次采集'));
      row.append(avatar, info); list.append(row);
    });
  }
  function eventSummary(event) {
    const summary = event.summary || {};
    if (event.type === 'action') return { title: ACTIONS[summary.action] || '管理操作', detail: summary.requeued !== undefined ? '重新排队 ' + count(summary.requeued) + ' 条记录' : '操作已记录' };
    if (event.type === 'import') return { title: '链接导入', detail: '新增 ' + count(summary.new) + ' · 修复 ' + count(summary.repaired) + ' · 非法 ' + count(summary.invalid) };
    const report = summary.report || summary;
    const collected = Array.isArray(summary.collect) ? summary.collect.reduce((n, item) => n + number(item.new), 0) : number(summary.new);
    return { title: '完成一轮同步', detail: '新增 ' + count(collected) + ' · 上报 ' + count(report.reported) + ' · 已有 ' + count(report.existing) };
  }
  function renderEvents(events, cycle) {
    const list = $('activity-list'); list.replaceChildren();
    const entries = events.length ? events.slice(0, 6) : cycle?.at ? [{ at: cycle.at, type: 'cycle', summary: cycle }] : [];
    if (!entries.length) { list.append(el('li', 'muted', '暂无近期活动，首次同步后会在此显示。')); return; }
    entries.forEach((event) => {
      const item = el('li'), marker = el('span', 'event-marker'), content = el('div', 'event-content');
      const summary = eventSummary(event); marker.append(icon(event.type === 'import' ? 'upload' : event.type === 'action' ? 'check' : 'refresh'));
      content.append(el('strong', '', summary.title), el('p', '', summary.detail), el('time', '', time(event.at)));
      item.append(marker, content); list.append(item);
    });
  }

  function saveFilters() {
    // Session storage contains only non-sensitive view preferences, never search text or credentials.
    try { sessionStorage.setItem('ed2k-view', JSON.stringify({ filter: state.filter })); } catch { /* Storage can be disabled. */ }
  }
  function restoreFilters() {
    try { const saved = JSON.parse(sessionStorage.getItem('ed2k-view') || '{}'); if (saved.filter === 'all' || Object.hasOwn(STATES, saved.filter)) state.filter = saved.filter; } catch { /* Start with default view. */ }
    $('state-filter').value = state.filter;
  }
  function setFilter(value) {
    state.filter = value; state.page = 1; $('state-filter').value = value; saveFilters(); loadItems(true);
    document.querySelectorAll('.stat-card').forEach((card) => card.classList.toggle('selected', card.dataset.filter === value));
  }
  async function loadItems(showLoading) {
    if (!state.authenticated) return;
    state.itemsController?.abort();
    const controller = new AbortController(); state.itemsController = controller;
    const version = ++state.itemVersion, epoch = state.epoch;
    const params = new URLSearchParams({ state: state.filter, q: state.query, page: String(state.page), page_size: String(state.pageSize) });
    if (showLoading) $('records-loading').hidden = false;
    $('records-section').setAttribute('aria-busy', 'true'); $('previous-page').disabled = true; $('next-page').disabled = true;
    try {
      const data = await api('/api/items?' + params, { signal: controller.signal });
      if (version !== state.itemVersion || epoch !== state.epoch || !state.authenticated) return;
      if (!Array.isArray(data.items)) throw { code: 'invalid_response' };
      state.items = data.items; state.total = number(data.total); state.page = Math.max(1, number(data.page) || state.page); state.pageSize = Math.max(1, number(data.page_size) || 25);
      const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
      if (state.page > pages) { state.page = pages; await loadItems(showLoading); return; }
      const signature = JSON.stringify([data.items, state.total, state.page, state.filter, state.query]);
      if (signature !== state.itemsSignature) { renderItems(); state.itemsSignature = signature; }
      renderPagination();
    } catch (error) {
      if (version === state.itemVersion && epoch === state.epoch && state.authenticated && error.code !== 'aborted') {
        state.itemsSignature = ''; tableMessage('记录暂时无法加载', errorMessage(error), true);
        text('pagination-info', '连接恢复后可重新加载'); $('previous-page').disabled = true; $('next-page').disabled = true;
      }
    } finally {
      if (version === state.itemVersion) { $('records-loading').hidden = true; $('records-section').setAttribute('aria-busy', 'false'); }
    }
  }
  function tableMessage(title, description, failed = false) {
    const row = el('tr'), cell = el('td'), empty = el('div', 'empty-state'); cell.colSpan = 6;
    empty.append(icon(failed ? 'alert' : 'file'), el('p', '', title), el('small', '', description));
    const button = el('button', 'button small secondary', failed ? '重新加载' : '导入链接');
    button.addEventListener('click', failed ? () => loadItems(true) : openImport); empty.append(button); cell.append(empty); row.append(cell); $('records-body').replaceChildren(row);
  }
  function statusBadge(value) { return el('span', 'status-badge' + (Object.hasOwn(STATES, value) ? ' status-' + value : ''), STATES[value] || '状态待确认'); }
  function renderItems() {
    text('records-total', count(state.total));
    const body = $('records-body'); body.replaceChildren();
    if (!state.items.length) { tableMessage(state.query || state.filter !== 'all' ? '没有匹配的记录' : '还没有链接记录', state.query || state.filter !== 'all' ? '试试其他关键词或切换状态筛选。' : '等待频道采集，或先导入一批链接。'); return; }
    state.items.forEach((item) => {
      const row = el('tr'); row.dataset.item = String(item.id);
      const nameCell = el('td'), name = el('button', 'record-name', item.name || '未命名文件'); name.type = 'button'; name.title = String(item.name || '');
      name.setAttribute('aria-label', '查看详情：' + String(item.name || '未命名文件')); name.addEventListener('click', () => openDetail(item));
      nameCell.append(name, el('span', 'record-hash', item.md4 || ''));
      const sizeCell = el('td', '', size(item.size)), statusCell = el('td'), sourceCell = el('td'), timeCell = el('td', 'cell-time', time(item.updated_at)), actionCell = el('td');
      statusCell.append(statusBadge(item.state)); sourceCell.append(sourceNode(item.source_channel, item.source_message_id)); timeCell.title = time(item.updated_at, true);
      if (['failed', 'blocked'].includes(item.state)) {
        const retry = el('button', 'row-retry', '重试'); retry.setAttribute('aria-label', '重试：' + String(item.name || '此记录')); retry.disabled = Boolean(state.job || state.submitting);
        retry.addEventListener('click', () => confirmRetry(item)); actionCell.append(retry);
      } else { const detail = el('button', 'row-action'); detail.setAttribute('aria-label', '查看记录详情'); detail.append(icon('chevron')); detail.addEventListener('click', () => openDetail(item)); actionCell.append(detail); }
      row.append(nameCell, sizeCell, statusCell, sourceCell, timeCell, actionCell);
      row.addEventListener('click', (event) => { if (!event.target.closest('button, a')) openDetail(item); });
      body.append(row);
    });
  }
  function renderPagination() {
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    text('page-number', state.page + ' / ' + pages);
    text('pagination-info', state.total ? '第 ' + count((state.page - 1) * state.pageSize + 1) + '–' + count(Math.min(state.page * state.pageSize, state.total)) + ' 条，共 ' + count(state.total) + ' 条' : '共 0 条记录');
    $('previous-page').disabled = state.page <= 1; $('next-page').disabled = state.page >= pages;
  }
  function openDialog(id) { if (!$(id).open) $(id).showModal(); }
  function openDetail(item) {
    state.detail = item; showError('detail-error', '');
    const content = $('detail-content'); content.replaceChildren(el('div', 'detail-file-name', item.name || '未命名文件'));
    const grid = el('dl', 'detail-grid');
    function field(label, value, className = '') { const dd = el('dd', className); if (value instanceof Node) dd.append(value); else dd.textContent = String(value ?? '—'); grid.append(el('dt', '', label), dd); }
    field('状态', statusBadge(item.state)); field('文件大小', size(item.size)); field('MD4', item.md4 || '—', 'mono'); field('规范链接', item.normalized || '未记录', 'mono'); field('来源', sourceNode(item.source_channel, item.source_message_id));
    field('尝试次数', count(item.attempts)); field('首次收录', time(item.created_at, true)); field('最近更新', time(item.updated_at, true)); field('下次重试', item.next_retry ? time(item.next_retry, true) : '未安排');
    if (item.receipt) {
      const via = { api: '云端确认', report: '上报确认', recheck: '再次确认', lookup: '云端查询', cache: '已有记录' };
      field('确认方式', via[item.receipt.via] || '云端回执'); field('确认结果', [item.receipt.status, item.receipt.code].filter((v) => v !== null && v !== undefined && v !== '').map(String).join(' · ') || '已收到回执');
    } else field('云端回执', '暂无回执');
    field('最近错误', item.last_error || '无'); content.append(grid);
    $('copy-md4').disabled = !item.md4; $('copy-ed2k').disabled = !item.normalized; $('retry-item').hidden = !['failed', 'blocked'].includes(item.state); updateBusy(); openDialog('detail-dialog');
  }
  async function copyValue(value, label) {
    if (!value) return;
    try { if (!navigator.clipboard?.writeText) throw new Error('unavailable'); await navigator.clipboard.writeText(String(value)); toast(label + ' 已复制'); return; } catch { /* HTTP or clipboard permission restriction: use an in-dialog field. */ }
    const content = $('detail-content'); content.querySelector('.copy-fallback')?.remove();
    const area = el('div', 'copy-fallback'), field = el('textarea', 'link-copy-field'); field.value = String(value); field.readOnly = true; field.setAttribute('aria-label', '待复制的 ' + label);
    area.append(el('p', '', '如未自动复制，请使用下方已选中的内容手动复制。'), field); content.append(area); field.focus(); field.select(); field.setSelectionRange(0, field.value.length);
    try { if (document.execCommand('copy')) { toast(label + ' 已复制'); area.remove(); $('copy-' + (label === 'MD4' ? 'md4' : 'ed2k')).focus(); } } catch { /* The selected field remains available for manual copy. */ }
  }

  function resetPreview() {
    state.previewText = null; state.preview = null; $('import-preview').hidden = true; $('submit-import').disabled = true;
    text('import-bytes', size(new TextEncoder().encode($('import-text').value).length) + ' / 4 MiB');
  }
  function openImport() { showError('import-error', ''); openDialog('import-dialog'); updateBusy(); }
  function importText() {
    const value = $('import-text').value.replace(/^\uFEFF/, '');
    if (!value.trim()) throw { ui: '请粘贴内容或选择 TXT 文件。' };
    if (new TextEncoder().encode(value).length > MAX_BYTES) throw { status: 413 };
    return value;
  }
  async function previewImport() {
    if (state.previewBusy || state.importBusy || state.job || state.submitting) return;
    showError('import-error', '');
    let value; try { value = importText(); } catch (error) { showError('import-error', error.ui || errorMessage(error)); return; }
    resetPreview(); state.previewBusy = true; busy($('preview-button'), true, '正在预览…'); updateBusy();
    const epoch = state.epoch;
    try {
      const preview = await api('/api/import/preview', { body: { text: value } });
      if (epoch !== state.epoch || !state.authenticated || $('import-text').value.replace(/^\uFEFF/, '') !== value) return;
      state.preview = preview; state.previewText = value;
      ['new', 'duplicates', 'repaired', 'invalid'].forEach((key) => text('preview-' + key, count(preview[key])));
      text('preview-valid', count(preview.valid) + ' 条有效链接');
      const errors = $('preview-errors'); errors.replaceChildren();
      const reasons = { invalid_link: '链接格式无法识别', invalid_md4: 'MD4 格式不正确', invalid_size: '文件大小无效', missing_link: '未找到有效链接', no_link: '未找到有效链接', invalid: '内容无法识别' };
      if (Array.isArray(preview.errors)) {
        preview.errors.slice(0, 100).forEach((error) => errors.append(el('p', '', '第 ' + String(error.index ?? '—') + ' 项：' + (reasons[error.reason] || String(error.reason || '内容无法识别')))));
        if (preview.errors.length > 100) errors.append(el('p', '', '还有 ' + count(preview.errors.length - 100) + ' 项异常未展开。'));
      }
      $('import-preview').hidden = false;
      if (!number(preview.valid)) showError('import-error', '没有识别到有效链接，请检查文本内容。');
    } catch (error) { if (state.authenticated && epoch === state.epoch) showError('import-error', errorMessage(error)); }
    finally { state.previewBusy = false; busy($('preview-button'), false); updateBusy(); }
  }
  async function submitImport() {
    if (state.job || state.submitting || state.importBusy || !state.preview || !number(state.preview.valid)) return;
    let value; try { value = importText(); } catch (error) { showError('import-error', error.ui || errorMessage(error)); return; }
    if (value !== state.previewText) { resetPreview(); showError('import-error', '内容已变化，请重新预览后再导入。'); return; }
    state.importBusy = true; updateBusy(); showError('import-error', '');
    const ok = await startJob('/api/import', { text: value }, 'import', 'import-error');
    state.importBusy = false;
    if (ok) { $('import-dialog').close(); $('import-text').value = ''; $('import-file').value = ''; text('import-file-name', 'UTF-8 / UTF-8 BOM · 最大 4 MiB'); resetPreview(); }
    updateBusy();
  }
  async function chooseFile() {
    const file = $('import-file').files[0]; if (!file) return;
    resetPreview(); showError('import-error', '');
    if (file.size > MAX_BYTES) { showError('import-error', errorMessage({ status: 413 })); $('import-file').value = ''; return; }
    state.importBusy = true; updateBusy();
    const epoch = state.epoch;
    try {
      const value = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()).replace(/^\uFEFF/, '');
      if (epoch !== state.epoch || !state.authenticated) return;
      $('import-text').value = value; text('import-file-name', file.name); resetPreview();
    } catch { showError('import-error', '文件无法按 UTF-8 读取，请转换为 UTF-8 编码后重试。'); }
    finally { state.importBusy = false; updateBusy(); }
  }

  function updateBusy() {
    const occupied = Boolean(state.job || state.submitting), ready = state.authenticated && Boolean(state.status);
    $('collect-button').disabled = occupied || !ready || Boolean(state.status?.runtime?.cycle_running);
    $('pause-button').disabled = occupied || !ready || Boolean(state.status?.runtime?.pause_requested) || state.status?.settings?.report_enabled === false;
    $('retry-all-button').disabled = occupied || !ready || !(number(state.status?.status?.counts?.failed) + number(state.status?.status?.counts?.blocked));
    $('retry-item').disabled = occupied; document.querySelectorAll('.row-retry').forEach((button) => { button.disabled = occupied; });
    const importLocked = occupied || state.previewBusy || state.importBusy;
    $('preview-button').disabled = importLocked; $('submit-import').disabled = importLocked || !state.preview || !number(state.preview.valid);
    $('import-text').disabled = state.previewBusy || state.importBusy; $('import-file').disabled = importLocked;
    for (const id of ['collect-button', 'pause-button', 'retry-all-button', 'retry-item']) $(id).setAttribute('aria-busy', String(occupied));
    $('submit-import').setAttribute('aria-busy', String(state.importBusy));
  }
  function confirmRetry(item = null) {
    if (state.job || state.submitting) return;
    state.confirmAction = item ? { action: 'retry_failed', item_id: String(item.id) } : { action: 'retry_failed' };
    text('confirm-title', item ? '重试这条记录？' : '重试全部失败项？');
    text('confirm-description', item ? '将“' + String(item.name || '此记录') + '”重新加入处理队列。手动暂停状态会保留。' : '仅将上报失败与已阻止的记录重新排队。已完成、云端已有和结果待确认的记录保持原状；手动暂停状态会保留。');
    openDialog('confirm-dialog'); $('confirm-cancel').focus();
  }
  async function startJob(path, body, action, errorTarget = null) {
    if (state.job || state.submitting || !state.authenticated) return false;
    state.submitting = true; updateBusy();
    const epoch = state.epoch;
    try {
      const data = await api(path, { body });
      if (epoch !== state.epoch || !state.authenticated) return false;
      if (!data.job || !data.job.id || !['queued', 'running'].includes(data.job.state)) throw { code: 'invalid_response' };
      state.job = { id: String(data.job.id), action, epoch }; state.jobFailures = 0;
      showJob('操作已排队', ACTIONS[action] + '已提交，正在等待处理。');
      scheduleJob(300); return true;
    } catch (error) {
      if (state.authenticated && epoch === state.epoch) {
        const message = errorMessage(error) + (['timeout', 'invalid_response'].includes(error.code) || !error.status ? ' 操作可能已送达，请查看最新状态，确认后再操作。' : '');
        if (errorTarget) showError(errorTarget, message); else toast(message, true);
      }
      return false;
    } finally { state.submitting = false; updateBusy(); }
  }
  function showJob(title, description) { $('job-notice').hidden = false; text('job-title', title); text('job-description', description); }
  function scheduleJob(delay = 1200) { clearTimeout(state.jobTimer); if (state.job && !document.hidden) state.jobTimer = setTimeout(pollJob, delay); }
  async function pollJob() {
    const job = state.job; if (!job || !state.authenticated || document.hidden) return;
    try {
      const data = await api('/api/jobs/' + encodeURIComponent(job.id));
      if (state.job !== job || job.epoch !== state.epoch) return;
      const result = data.job; if (!result || !['queued', 'running', 'succeeded', 'failed'].includes(result.state)) throw { code: 'invalid_response' };
      state.jobFailures = 0;
      if (result.state === 'queued' || result.state === 'running') { showJob(result.state === 'queued' ? '操作已排队' : '正在处理', ACTIONS[job.action] + (result.state === 'queued' ? '正在等待执行，请勿重复提交。' : '正在执行，完成后会自动更新。')); scheduleJob(); return; }
      state.job = null; $('job-notice').hidden = true; updateBusy();
      if (result.state === 'succeeded') {
        const r = result.result || {}; const detail = job.action === 'import' ? ' · 新增 ' + count(r.new) + ' 条' : job.action === 'retry_failed' ? ' · 已重新排队 ' + count(r.requeued) + ' 条' : '';
        if (job.action === 'resume_reporting' && r.paused) toast('云端验证仍未通过，上报保持暂停。请核对认证后重试失败项。', true);
        else toast(ACTIONS[job.action] + '已完成' + detail);
        if (job.action === 'retry_failed' && $('detail-dialog').open) $('detail-dialog').close();
      } else toast(ACTIONS[job.action] + '未完成，请查看最新状态后重试。', true);
      await refresh();
    } catch (error) {
      if (state.job !== job || !state.authenticated || job.epoch !== state.epoch) return;
      if (error.status === 404) { state.job = null; $('job-notice').hidden = true; updateBusy(); toast(errorMessage(error), true); refresh(); return; }
      state.jobFailures += 1;
      showJob('暂时无法确认操作结果', errorMessage(error) + ' 将继续查询，本次操作不会重复提交。');
      scheduleJob(Math.min(15000, 2000 * state.jobFailures));
    }
  }

  async function login(event) {
    event.preventDefault(); if ($('login-button').disabled) return;
    showError('login-error', ''); busy($('login-button'), true, '正在登录…');
    try {
      const result = await api('/api/login', { body: { username: $('username').value.trim(), password: $('password').value } });
      if (!result.authenticated || !result.csrf_token) throw { code: 'invalid_response' };
      enterApp(result);
    } catch (error) { $('password').value = ''; showError('login-error', errorMessage(error, 'login')); $('password').focus(); }
    finally { busy($('login-button'), false); }
  }
  async function logout() {
    if ($('logout-button').disabled) return;
    $('logout-button').disabled = true;
    try { await api('/api/logout', { body: {} }); showLogin('你已退出登录。'); }
    catch (error) { if (state.authenticated) toast(errorMessage(error), true); }
    finally { $('logout-button').disabled = false; }
  }
  async function password(event) {
    event.preventDefault(); if ($('password-submit').disabled) return;
    showError('password-error', '');
    const value = $('new-password').value;
    if ([...value].length < 12 || [...value].length > 128) { showError('password-error', '新密码需要 12–128 个字符。'); return; }
    if (value !== $('confirm-password').value) { showError('password-error', '两次输入的新密码不一致。'); $('confirm-password').focus(); return; }
    busy($('password-submit'), true, '正在保存…');
    try { await api('/api/password', { body: { current_password: $('current-password').value, new_password: value } }); showLogin('密码已更新，请使用新密码重新登录。'); }
    catch (error) { if (state.authenticated) { $('current-password').value = ''; showError('password-error', errorMessage(error, 'password')); $('current-password').focus(); } }
    finally { busy($('password-submit'), false); }
  }
  async function exportItems() {
    if ($('export-button').disabled) return;
    $('export-button').disabled = true; $('export-button').setAttribute('aria-busy', 'true');
    try {
      const blob = await api('/api/export', { blob: true });
      if (!state.authenticated) return;
      const url = URL.createObjectURL(blob), anchor = el('a'); anchor.href = url; anchor.download = 'ed2k-links-' + new Date().toISOString().slice(0, 10) + '.txt'; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000); toast('导出文件已准备，正在下载。');
    } catch (error) { if (state.authenticated) toast(errorMessage(error), true); }
    finally { $('export-button').disabled = false; $('export-button').setAttribute('aria-busy', 'false'); }
  }
  function selectSection(name) {
    const names = { overview: '总览', records: '链接记录', sources: '采集来源', activity: '近期活动' };
    document.querySelectorAll('[data-section]').forEach((button) => { const active = button.dataset.section === name; button.classList.toggle('active', active); if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
    text('section-name', names[name] || '总览');
    const target = name === 'overview' ? document.querySelector('.page-heading') : $(name + '-section');
    target?.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
  }

  $('login-form').addEventListener('submit', login);
  $('logout-button').addEventListener('click', logout);
  $('password-form').addEventListener('submit', password);
  $('open-password').addEventListener('click', () => { $('password-form').reset(); showError('password-error', ''); openDialog('password-dialog'); });
  $('password-dialog').addEventListener('close', () => $('password-form').reset());
  document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => $(button.dataset.close).close()));
  document.querySelectorAll('[data-section]').forEach((button) => button.addEventListener('click', () => selectSection(button.dataset.section)));
  document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => { setFilter(button.dataset.filter); selectSection('records'); }));
  $('refresh-button').addEventListener('click', refresh); $('retry-connection').addEventListener('click', refresh);
  $('collect-button').addEventListener('click', () => startJob('/api/actions', { action: 'collect_now' }, 'collect_now'));
  $('pause-button').addEventListener('click', () => { const action = $('pause-button').dataset.action || 'pause_reporting'; startJob('/api/actions', { action }, action); });
  $('retry-all-button').addEventListener('click', () => confirmRetry()); $('retry-item').addEventListener('click', () => { if (state.detail) confirmRetry(state.detail); });
  $('confirm-cancel').addEventListener('click', () => $('confirm-dialog').close());
  $('confirm-accept').addEventListener('click', () => { const action = state.confirmAction; $('confirm-dialog').close(); state.confirmAction = null; if (action) startJob('/api/actions', action, 'retry_failed'); });
  $('confirm-dialog').addEventListener('close', () => { state.confirmAction = null; });
  $('copy-md4').addEventListener('click', () => copyValue(state.detail?.md4, 'MD4')); $('copy-ed2k').addEventListener('click', () => copyValue(state.detail?.normalized, 'ED2K'));
  $('state-filter').addEventListener('change', () => setFilter($('state-filter').value));
  $('search-form').addEventListener('submit', (event) => { event.preventDefault(); state.query = $('search-input').value.trim(); state.page = 1; loadItems(true); });
  $('search-input').addEventListener('search', () => { if (!$('search-input').value) { state.query = ''; state.page = 1; loadItems(true); } });
  $('previous-page').addEventListener('click', () => { if (state.page > 1) { state.page -= 1; loadItems(true); } });
  $('next-page').addEventListener('click', () => { state.page += 1; loadItems(true); });
  $('open-import').addEventListener('click', openImport); $('export-button').addEventListener('click', exportItems);
  $('import-text').addEventListener('input', () => { resetPreview(); showError('import-error', ''); });
  $('import-file').addEventListener('change', chooseFile); $('preview-button').addEventListener('click', previewImport); $('submit-import').addEventListener('click', submitImport);
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearPolling(); else if (state.authenticated) { refresh(); if (state.job) scheduleJob(0); } });
  window.addEventListener('online', () => { if (state.authenticated) { refresh(); if (state.job) scheduleJob(0); } });
  window.addEventListener('pagehide', clearPolling);
  window.addEventListener('pageshow', (event) => { if (event.persisted) initialize(); });
  restoreFilters();
  async function initialize() {
    try { const session = await api('/api/session'); if (session.authenticated && session.csrf_token) enterApp(session); else showLogin(); }
    catch (error) { showLogin(errorMessage(error)); }
  }
  initialize();
})();
