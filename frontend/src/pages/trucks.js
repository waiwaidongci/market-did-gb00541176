import {feedback, field} from '../components/common/fields.js';
import {empty, panel, metrics, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';

const EVENT_LABEL = {gate_queue: '门岗入队', handover: '调度交接', unload: '门岗开卸'};
const STATE_TONE = {'正常': 'complete', '断链': 'failed', '交接分叉': 'failed', '成环': 'failed'};

function pillFor(state) {
  return pill(state, STATE_TONE[state] || 'queued');
}

function marketDayPanel(data) {
  const columns = [
    {key: 'market_id', label: '市场'},
    {key: 'market_day', label: '市场日（首次入队）'},
    {key: 'truck_count', label: '计入车次'},
    {key: 'transfer_truck_count', label: '含转场车次'},
    {key: 'total_duration_minutes', label: '整段用时合计（分钟）'},
    {key: 'avg_duration_minutes', label: '车均整段用时（分钟）'},
    {label: '车辆回查', render: row => el('div', {class: 'activity'},
      row.truck_ids.map(id => link(id, `#trucks/${encodeURIComponent(id)}`, {class: 'quiet'})))},
  ];
  return panel('市场日汇总（整段用时只归首次入队市场日）',
    p(data.rule, 'subtle'),
    table(columns, data.rows, {caption: '市场日汇总', pageSize: 10}),
    p(`暂不计入车次 ${data.excluded_truck_count} 辆：${data.excluded_truck_ids.join('、') || '无'}`, 'filename'),
    data.excluded_truck_count > 0 && link('查看异常车次与原因', '#trucks', {class: 'quiet'}));
}

function anomalyPanel(anomalies) {
  const columns = [
    {key: 'truck_id', label: '车次', render: row => link(row.truck_id, `#trucks/${encodeURIComponent(row.truck_id)}`)},
    {key: 'state', label: '链状态', render: row => pillFor(row.state)},
    {key: 'reasons', label: '原因（原始记录保留）'},
    {key: 'first_market', label: '首条记录市场'},
  ];
  return panel('异常车次（分叉 / 断链 / 成环，暂不计入）',
    table(columns, anomalies, {caption: '异常车次', empty: '没有异常车次，全部转场链贯通。', pageSize: 8}));
}

function batchPanel(batches) {
  const columns = [
    {key: 'batch_seq', label: '批次号'},
    {key: 'status', label: '状态', render: row => pill(row.status === 'applied' ? '已生效' : '已拒绝',
      row.status === 'applied' ? 'complete' : 'failed')},
    {key: 'touched_trucks', label: '涉事车次'},
    {key: 'note', label: '说明'},
    {label: '原归属 → 新归属', render: row => el('ul', {class: 'switch-list'},
      (row.switches || []).map(item => el('li', {},
        `${item.truck_id}：${formatAttr(item.old_attribution)} → ${formatAttr(item.new_attribution)}${item.changed ? '' : '（无变化）'}`)))},
    {key: 'reject_reasons', label: '拒绝原因'},
  ];
  return panel('补报批次（按批次号顺序生效，旧批不得覆盖新归属）',
    table(columns, batches, {caption: '补报批次', empty: '暂无补报批次。', pageSize: 6}));
}

function formatAttr(attr) {
  if (!attr) return '不计入';
  return `${attr.market_id} ${attr.market_day}（${attr.duration_minutes ?? '—'} 分钟）`;
}

function correctionForm(error, refresh) {
  const seqConfig = {key: 'batch_seq', label: '批次号（正整数，按序生效）', type: 'int', min: 1};
  const noteConfig = {key: 'note', label: '批次说明', type: 'string', required: false};
  const submit = button('校验并一次性切换', {type: 'submit', class: 'primary'});
  return el('form', {class: 'fields'},
    handler(submit, error, async event => {
      const form = new FormData(event.target);
      let events;
      try {
        events = JSON.parse(form.get('event_json'));
      } catch {
        throw new Error('补报事件须为 JSON 数组');
      }
      if (!Array.isArray(events) || !events.length) throw new Error('至少补报一条事件');
      const result = await workspace.submitCorrection({
        batch_seq: Number(form.get('batch_seq')),
        note: form.get('note') || '',
        corrections: events,
      });
      if (!result.applied) {
        error.textContent = `批次已拒绝：${(result.reject_reasons || []).join('；')}`;
      } else {
        event.target.reset();
        await refresh();
      }
    }),
    field(seqConfig), field(noteConfig),
    el('label', {class: 'field', for: 'field-event_json'},
      el('span', {}, '补报事件 JSON 数组'),
      el('textarea', {id: 'field-event_json', name: 'event_json', rows: 9, required: true,
        placeholder: '[\n  {"truck_id":"皖A-1004","seq":20,"event_type":"unload","system":"门岗","market_id":"西苑","event_time":"2026-11-10T22:30"}\n]'})),
    p('同 (车次, 事件类型, 流水号) 的补报事件覆盖原值；批次先算齐原归属与新归属再整体生效。', 'subtle'),
    submit, error);
}

function importForm(error, refresh) {
  const submit = button('导入门岗/调度事件', {type: 'submit', class: 'quiet'});
  return el('form', {class: 'fields'},
    handler(submit, error, async event => {
      const form = new FormData(event.target);
      const file = form.get('source');
      if (!file?.size) throw new Error('请选择一个 JSON 文件');
      const parsed = JSON.parse(await file.text());
      if (!Array.isArray(parsed)) throw new Error('文件须为事件 JSON 数组');
      const result = await workspace.importTruckEvents(parsed);
      error.textContent = `导入完成：新增 ${result.inserted} 条，重复跳过 ${result.skipped_duplicates} 条。`;
      await refresh();
    }),
    el('label', {class: 'field', for: 'truck-source'}, el('span', {}, '事件 JSON 文件'),
      el('input', {id: 'truck-source', type: 'file', name: 'source', accept: '.json,application/json', required: true})),
    submit, error);
}

async function truckDetail(truckId) {
  const detail = await workspace.truck(truckId);
  const eventColumns = [
    {key: 'event_type', label: '事件', render: row => EVENT_LABEL[row.event_type] || row.event_type},
    {key: 'seq', label: '流水号'},
    {key: 'system', label: '来源系统'},
    {key: 'market_id', label: '市场'},
    {key: 'event_time', label: '事件时间'},
  ];
  const rawColumns = [
    {key: 'event_type', label: '事件', render: row => EVENT_LABEL[row.event_type] || row.event_type},
    {key: 'seq', label: '流水号'}, {key: 'market_id', label: '市场'},
    {key: 'event_time', label: '入库时间'},
    {key: 'source', label: '记录性质', render: row => row.source === 'source' ? '原始记录' : `补报（${row.origin}）`},
    {key: 'author', label: '操作者'},
  ];
  return el('div', {class: 'wide-panel'},
    panel(`车次 ${truckId} 的转场依据`,
      el('div', {class: 'activity'}, pillFor(detail.state),
        p(`首次入队市场：${detail.first_market || '—'}　市场日：${detail.market_day || '不计入'}`, 'filename'),
        p(`整段用时：${detail.duration_minutes ?? '—'} 分钟　交接次数：${detail.transfer_count}`, 'filename'),
        link('返回市场日汇总', '#trucks', {class: 'quiet'})),
      detail.reasons?.length && el('ul', {class: 'method-list'},
        detail.reasons.map(reason => el('li', {}, reason))),
      h(3, '转场链（对账口径）'),
      table(eventColumns, detail.events, {caption: '转场链', pageSize: 20}),
      h(3, '原始入库记录与补报痕迹（不可变）'),
      table(rawColumns, detail.raw_records, {caption: '原始记录', pageSize: 20})));
}

export async function trucksPage(route, refresh) {
  const error = feedback();
  if (route.slug) {
    return truckDetail(route.slug);
  }
  const [market, anomalies, batches] = await Promise.all([
    workspace.marketDays(), workspace.anomalies(), workspace.correctionBatches()]);
  const headline = metrics([
    {label: '市场日记录', value: market.rows.length},
    {label: '计入车次', value: market.rows.reduce((sum, row) => sum + row.truck_count, 0)},
    {label: '暂不计入车次', value: market.excluded_truck_count},
    {label: '补报批次', value: batches.length},
  ]);
  return el('div', {class: 'two-col'},
    el('div', {class: 'wide-panel'},
      headline,
      marketDayPanel(market),
      anomalyPanel(anomalies.rows),
      batchPanel(batches)),
    el('aside', {class: 'sidebar-panel'},
      panel('提交补报批次', p('原归属与新归属算齐后一次性切换；被拒绝批次不改动任何记录。', 'subtle'),
        session.canWrite ? correctionForm(error, refresh) : empty('当前角色只读，不能提交补报。')),
      panel('导入原始事件', p('门岗入队/开卸与调度交接单按车次对账，原始行不可变。', 'subtle'),
        session.canWrite ? importForm(feedback(), refresh) : empty('当前角色只读。'))));
}
