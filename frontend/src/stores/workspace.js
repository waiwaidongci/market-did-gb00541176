import {request, saveFile} from '../api/http.js';
import {session} from './session.js';

const call = (path, body) => request(path, {token:session.token, body});
export const workspace = {
  spec: null,
  datasets: [],
  runs: [],
  async init() { this.spec = await request('/spec'); },
  async refresh() { [this.datasets, this.runs] = await Promise.all([call('/datasets'), call('/runs')]); },
  getDataset: (id, offset = 0) => call(`/datasets/${id}?offset=${offset}`),
  getRun: id => call(`/runs/${id}`),
  upload: data => call('/datasets', data),
  start: data => call('/runs', data),
  review: (id, data) => call(`/runs/${id}/review`, data),
  audit: () => call('/audit'),
  schedules: () => call('/schedules'),
  deliveries: () => call('/deliveries'),
  schedule: data => call('/schedules', data),
  disable: id => call(`/schedules/${id}/disable`, {}),
  marketDays: (marketId, marketDay) => {
    const params = new URLSearchParams();
    if (marketId) params.set('market_id', marketId);
    if (marketDay) params.set('market_day', marketDay);
    const query = params.toString();
    return call(`/reconciliation/market-days${query ? `?${query}` : ''}`);
  },
  truck: id => call(`/reconciliation/trucks/${encodeURIComponent(id)}`),
  anomalies: () => call('/reconciliation/anomalies'),
  correctionBatches: () => call('/reconciliation/batches'),
  importTruckEvents: events => call('/reconciliation/events', {events}),
  submitCorrection: data => call('/reconciliation/corrections', data),
  download: (path, filename) => saveFile(path, session.token, filename),
};
