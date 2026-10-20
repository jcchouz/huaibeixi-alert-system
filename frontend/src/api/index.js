import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 25000
})

http.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const payload = error?.response?.data
    const detail =
      payload?.error?.message ||
      payload?.detail ||
      error?.message ||
      '网络请求异常'
    return Promise.reject(new Error(detail))
  }
)

export const ALERT_TYPE_MAP = {
  阈值超限: '阈值超限',
  传感器离线: '传感器离线',
  采集仪离线: '采集仪离线',
  传感器死值: '传感器死值',
  超物理量程: '超物理量程'
}

export function formatAlertTypeLabel(type) {
  if (!type) return '未知类型'
  return ALERT_TYPE_MAP[type] || type
}

// 1. Overview & KPI Summary
export async function getOverviewApi() {
  return await http.get('/overview')
}

// 2. Cursor polling for alert popups
export async function pollAlertsApi(afterSeq = 0, limit = 50) {
  return await http.get('/alerts/poll', {
    params: { afterSeq, limit }
  })
}

// 3. Paginated & Filtered Alerts List
export async function listAlertsApi(params = {}) {
  return await http.get('/alerts', { params })
}

export async function createAlertApi(payload = {}) {
  const res = await http.post('/alerts', {
    sensorNo: payload.sensor_no ?? payload.sensorNo,
    channel: payload.channel ?? '',
    alertType: payload.alert_type ?? payload.alertType ?? '阈值超限',
    alertLevel: Number(payload.alert_level ?? payload.alertLevel ?? 1),
    triggerVal: Number(payload.trigger_val ?? payload.triggerVal ?? 0)
  })
  return res.alert || res
}

// 4. Acknowledge Single Alert
export async function ackAlertApi(alertId, payload = {}) {
  const res = await http.post(`/alerts/${alertId}/ack`, {
    alertStatus: 1,
    handleBy: payload.handleBy ?? payload.handle_by ?? 'web',
    handlePhone: payload.handlePhone ?? payload.handle_phone ?? '',
    handleOpinion: payload.handleOpinion ?? ''
  })
  return res.alert || res
}

// 5. Batch Acknowledge Alerts
export async function ackAllAlertsApi(payload = {}) {
  return await http.post('/alerts/ack-all', {
    handleBy: payload.handleBy ?? payload.handle_by ?? 'web',
    handlePhone: payload.handlePhone ?? payload.handle_phone ?? '',
    alertLevel: payload.alertLevel ?? null,
    alertCategory: payload.alertCategory ?? null
  })
}

export async function deleteAlertApi(alertId) {
  return await http.delete(`/alerts/${alertId}`)
}

// 7. Scheduled Max/Min Extremes Dashboard APIs
export async function getStatSensorsApi() {
  return await http.get('/stats/sensors')
}

export async function getStatExtremesApi(params = {}) {
  return await http.get('/stats/extremes', { params })
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
