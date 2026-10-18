import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 25000
})

http.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const detail = error?.response?.data?.detail || error?.message || '网络请求异常'
    return Promise.reject(new Error(detail))
  }
)

export const ALERT_TYPE_MAP = {
  THRESHOLD: '三级阈值超限',
  OFFLINE: '1000ms离线/断流',
  GAP_1000MS: '1000ms采样断流',
  DAQ_OFFLINE: '采集仪批量离线',
  RATE: '变形速率突变',
  STUCK: '传感器死值故障',
  OOR: '超物理量程',
  SPIKE: '毛刺跳变',
  DATA_CONFLICT: '双源时间戳冲突'
}

export function formatAlertTypeLabel(type) {
  if (!type) return '未知类型'
  return ALERT_TYPE_MAP[type] || type
}

// 1. Overview & KPI Summary
export async function getOverviewApi() {
  return await http.get('/overview')
}

export async function getAlertsSummaryApi() {
  const data = await http.get('/overview')
  return {
    raw: data,
    engine: {
      ...(data.engineStatus || {}),
      mode: data.engineMode || data.engineStatus?.mode || 'REPLAY',
      currentWatermark: data.currentWatermark || data.engineStatus?.currentWatermark,
      totalChannels: data.totalChannels || 208,
      enabledChannels: data.enabledChannels || 185,
      uncommissionedChannels: Math.max(0, (data.totalChannels || 208) - (data.enabledChannels || 185)),
      onlineChannels: data.onlineChannels ?? 185,
      offlineChannels: data.offlineChannels ?? 23
    },
    kpi: {
      totalSensors: data.totalSensors ?? 146,
      totalChannels: data.totalChannels ?? 208,
      enabledChannels: data.enabledChannels ?? 185,
      uncommissionedChannels: Math.max(0, (data.totalChannels || 208) - (data.enabledChannels || 185)),
      onlineChannels: data.onlineChannels ?? 185,
      offlineChannels: data.offlineChannels ?? 23,
      activeAlertsCount: data.activeAlertsCount ?? 0,
      unackedAlertsCount: data.unackedAlertsCount ?? 0,
      ackedAlertsCount: data.ackedAlertsCount ?? 0,
      resolvedAlertsCount: data.resolvedAlertsCount ?? 0,
      suppressedAlertsCount: data.suppressedAlertsCount ?? 0,
      totalAlertsCount: data.totalAlertsCount ?? 0,
      level1Count: data.level1Count ?? 0,
      level2Count: data.level2Count ?? 0,
      level3Count: data.level3Count ?? 0,
      totalLevel1Count: data.totalLevel1Count ?? 0,
      totalLevel2Count: data.totalLevel2Count ?? 0,
      totalLevel3Count: data.totalLevel3Count ?? 0,
      totalMonitorRows: data.totalMonitorRows ?? 398059510,
      statRecordCount: data.statRecordCount ?? 0,
      maxSeq: data.maxSeq ?? 0,
      alertsByType: data.alertsByType || [],
      alertsByLevel: data.alertsByLevel || [],
      recentAlerts: data.recentAlerts || []
    }
  }
}

// 2. Cursor Polling for Real-time Alert Popup
export async function pollAlertsApi(afterSeq = 0, limit = 50) {
  return await http.get('/alerts/poll', {
    params: { afterSeq, limit }
  })
}

// 3. Paginated & Filtered Alerts List
export async function listAlertsApi(params = {}) {
  return await http.get('/alerts', { params })
}

// 4. Acknowledge Single Alert
export async function ackAlertApi(alertId, payload = {}) {
  const body = {
    alertStatus: payload.new_status ?? payload.alertStatus ?? payload.alert_status ?? 1,
    ackBy: payload.ack_user ?? payload.ackBy ?? payload.ack_by ?? '值班工程师',
    remark: payload.ack_note ?? payload.remark ?? '已确认处置'
  }
  const res = await http.post(`/alerts/${alertId}/ack`, body)
  return res.alert || res
}

// 5. Batch Acknowledge Alerts
export async function ackAllAlertsApi(payload = {}) {
  return await http.post('/alerts/ack-all', {
    ackBy: payload.ackBy || '值班工程师',
    alertLevel: payload.alertLevel ?? null,
    remark: payload.remark || '值班工程师一键批量确认处置'
  })
}

// 6. Engine Replay & Live Simulation Control
export async function controlEngineReplayApi(payload = {}) {
  return await http.post('/engine/replay', payload)
}

export async function simulateRealtimeAlertApi(options = {}) {
  const res = await http.post('/engine/replay', {
    action: 'trigger_test_alert',
    sensorNo: options.sensorNo || null,
    channel: options.channel ?? null,
    alertLevel: options.alertLevel || null,
    alertType: options.alertType || null
  })
  return res.alert || (res.stepResult?.triggeredAlerts?.[0]) || null
}

// 7. Scheduled Max/Min Extremes Dashboard APIs
export async function getStatSensorsApi() {
  return await http.get('/stats/sensors')
}

export async function getStatExtremesApi(params = {}) {
  return await http.get('/stats/extremes', { params })
}

// 8. Bridge Wind Rose Analysis API
export async function getWindRoseApi(params = {}) {
  return await http.get('/stats/wind-rose', { params })
}

// 9. 3-Level Threshold Rules & Dry-Run Workbench APIs
export async function listThresholdsApi(params = {}) {
  return await http.get('/thresholds', { params })
}

export async function updateThresholdApi(thresholdId, payload = {}) {
  return await http.put(`/thresholds/${thresholdId}`, payload)
}

export async function batchUpdateThresholdsApi(payload = {}) {
  return await http.post('/thresholds/batch-update', payload)
}

export async function simulateThresholdApi(payload = {}) {
  return await http.post('/thresholds/simulate', payload)
}

export default http
