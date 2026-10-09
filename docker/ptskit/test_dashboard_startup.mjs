import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createContext, Script} from 'node:vm';

const source = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
function section(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert(first >= 0 && last > first, `Missing application seam: ${start}`);
  return source.slice(first, last);
}
const cacheCode = section('function mergePtsStatistics(', 'async function refreshPts(');
const refreshCode = section('async function refresh(forcePts = false)', '\nfunction navigate(');
const candidatesCode = section('async function refreshPts(', '\nasync function refreshLocalSeedkeep(');
const progressCode = section('function renderSiteProgress()', '\nfunction renderDashboardDownloaders(');
const statisticsFields = source.match(/const ptsStatisticsFields = (\[[^\n]+\]);/);
assert(statisticsFields, 'Missing statistics whitelist');
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const turn = () => new Promise(resolve => setImmediate(resolve));
function fixture() {
  const inventory = deferred(), cache = deferred(), calls = [], nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {hidden:true, textContent:'', title:'', style:{}, parentNode:{}, classList:{toggle() {}}, setAttribute() {}});
    return nodes.get(id);
  };
  const noop = () => {};
  const context = createContext({
    unifiedSaving:false, settingsReadBusy:false, statusForceRefreshing:false, queuedRefresh:null,
    settingsRevision:0, queryGeneration:0, categoryCleanupRequest:0, statusRefreshEpoch:0,
    refreshing:false, status:null, ptsData:null, ptsStatisticsVersion:0, ptsCacheRequest:0,
    ptsStatisticsFields:JSON.parse(statisticsFields[1].replaceAll("'", '"')),
    categoryCleanupDocument:null, page:'dashboard',
    api:async name => { calls.push(name); if (name === 'status') return inventory.promise; if (name === 'pts/statistics') return cache.promise; throw Error(`Unexpected API ${name}`); },
    $:node, text:(id, value) => { node(id).textContent = String(value ?? '—'); },
    renderStatus:noop, renderPts:noop, syncPolling:noop, syncControls:noop,
    refreshTransferRules:noop, refreshCategoryCleanup:noop, refreshPendingSettings:noop,
    refreshDownloads:noop, refreshCleanupStorage:noop, refreshConfiguration:noop, refreshPolicy:noop,
    refreshLogPolicy:noop, refreshLogs:noop, refreshInstances:noop, notice:noop, feedback:noop,
    safeMessage:String, refreshPts:async mode => { calls.push(`candidates:${mode}`); },
    displayCount:value => Number.isInteger(value) && value >= 0 ? value : null,
    siteEffective:settings => settings?.refill_count_basis === 'site_effective', date:String,
  });
  new Script(cacheCode + '\n' + refreshCode + '\n' + progressCode).runInContext(context);
  return {context, inventory, cache, calls, node};
}
let checks = 0;
{
  const f = fixture(), running = f.context.refresh();
  await turn();
  assert.deepEqual(f.calls, ['pts/statistics', 'status']); checks++;
  f.cache.resolve({current:439, target:1000, local_target:1250, available:true, stale:false, fetched_at:100});
  await turn();
  assert.equal(f.context.status, null); checks++;
  assert.equal(f.context.ptsData.current, 439); checks++;
  assert.equal(f.node('appView').hidden, false); checks++;
  assert.equal(f.node('loginView').hidden, true); checks++;
  assert(!f.calls.some(name => name.startsWith('candidates:'))); checks++;
  f.inventory.resolve({settings:{}});
  await running;
  assert(f.calls.includes('candidates:normal')); checks++;
}
{
  const f = fixture(), running = f.context.refresh();
  f.context.mergePtsStatistics({current:900, available:true, stale:false, fetched_at:200});
  f.cache.resolve({current:439, available:true, stale:false, fetched_at:100});
  await turn();
  assert.equal(f.context.ptsData.current, 900); checks++;
  f.inventory.resolve({settings:{}});
  await running;
}
{
  const f = fixture(), running = f.context.refresh();
  f.context.settingsRevision++;
  f.cache.resolve({current:439, available:true, stale:false});
  await turn();
  assert.equal(f.context.ptsData, null); checks++;
  f.context.queryGeneration++;
  f.inventory.resolve({settings:{}});
  await running;
}
{
  const f = fixture(), running = f.context.refresh();
  f.context.queryGeneration++;
  f.cache.resolve({current:439, available:true, stale:false});
  f.inventory.resolve({settings:{}});
  await running;
  await turn();
  assert.equal(f.context.ptsData, null); checks++;
  assert.equal(f.node('appView').hidden, true); checks++;
}
{
  const f = fixture();
  f.context.status = {settings:{target:1250, refill_count_basis:'site_effective', refill_trigger:1100, refill_floor:1000}};
  f.context.ptsData = {current:439, target:1000, stale:true, cache_expired:true, fetched_at:100};
  f.context.renderSiteProgress();
  assert.equal(f.node('progressCurrentValue').textContent, '439'); checks++;
  assert.match(f.node('progressCaption').textContent, /旧记录/); checks++;
  assert.equal(f.node('dashboardTargetGap').textContent, '—'); checks++;
  f.context.ptsData = {current:0, target:1000, available:true, stale:false, fetched_at:200};
  f.context.renderSiteProgress();
  assert.equal(f.node('progressCurrentValue').textContent, '0'); checks++;
  f.context.ptsData = {available:false, stale:false};
  f.context.renderSiteProgress();
  assert.equal(f.node('progressCurrentValue').textContent, '—'); checks++;
}
{
  const f = fixture();
  f.context.status = {settings:{}};
  f.context.ptsRefreshing = false;
  f.context.queuedPtsRefresh = null;
  let result = {current:null, target:null, fetched_at:null, available:false, stale:false, error:'查询失败', items:[]};
  f.context.api = async () => result;
  new Script(candidatesCode).runInContext(f.context);
  f.context.ptsData = {current:439, target:1000, fetched_at:100, available:true, stale:false};
  await f.context.refreshPts('normal');
  assert.equal(f.context.ptsData.current, 439); checks++;
  assert.equal(f.context.ptsData.fetched_at, 100); checks++;
  assert.equal(f.context.ptsData.available, false); checks++;
  assert.equal(f.context.ptsData.stale, true); checks++;
  assert.equal(f.context.ptsData.error, '查询失败'); checks++;
  result = {current:0, target:1000, fetched_at:200, available:true, stale:false, items:[]};
  await f.context.refreshPts('normal');
  assert.equal(f.context.ptsData.current, 0); checks++;
  assert.equal(f.context.ptsData.stale, false); checks++;
  result = {current:null, has_record:false, fetched_at:300, available:true, stale:false, items:[]};
  await f.context.refreshPts('normal');
  assert.equal(f.context.ptsData.current, null); checks++;
  assert.equal(f.context.ptsData.fetched_at, 300); checks++;
  assert.equal(f.context.ptsData.stale, false); checks++;
}
console.log(JSON.stringify({passed:true, checks, real_application_functions:true, cases:['cache_before_inventory','late_cache_isolation','settings_revision_isolation','session_isolation','expired_zero_unknown_display','candidate_failure_keeps_cache','confirmed_empty_replaces_cache']}));
