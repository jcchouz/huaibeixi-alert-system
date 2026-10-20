<template>
  <div class="global-alert-popup-stack" v-if="popupQueue.length > 0">
    <!-- Stack Header Bar when multiple alerts are active -->
    <div class="popup-stack-bar" v-if="popupQueue.length > 1">
      <span class="stack-count">
        <span class="pulse-dot red"></span>
        当前实时预警弹窗队列 ({{ popupQueue.length }})
      </span>
      <div class="stack-actions">
        <el-button size="small" type="primary" link @click="ackAllPopups">全部一键处理</el-button>
        <el-button size="small" type="info" link @click="clearAllPopups">全部收起</el-button>
      </div>
    </div>

    <transition-group name="alert-slide" tag="div" class="popup-list">
      <div
        v-for="item in popupQueue"
        :key="item.popupKey"
        class="industrial-alarm-toast"
        :class="`alarm-level-${item.alert_level}`"
      >
        <!-- Top Alert Banner -->
        <div class="alarm-toast-header">
          <div class="header-left">
            <span class="severity-pill" :class="`pill-l${item.alert_level}`">
              <el-icon class="siren-icon"><BellFilled /></el-icon>
              {{ formatSeverityBadge(item.alert_level) }}
            </span>
            <span class="type-pill">{{ formatAlertType(item.alert_type) }}</span>
            <span v-if="item.isUpgrade || (item.trigger_count && item.trigger_count > 1)" class="upgrade-pill">
              ⚡ 动态升级/持续刷新 (#{{ item.seq }})
            </span>
          </div>
          <button class="close-btn" @click="dismissPopup(item.popupKey)" title="收起弹窗">×</button>
        </div>

        <!-- Body Details -->
        <div class="alarm-toast-body">
          <div class="sensor-headline">
            <span class="station-tag">{{ item.station_name || '玉环站' }}</span>
            <span class="sensor-title">{{ item.sensor_name || item.sensor_no }}</span>
            <span class="sensor-code mono-num">
              ({{ item.sensor_no }} / {{ item.channel || '默认通道' }})
            </span>
          </div>

          <div class="metrics-grid">
            <div class="metric-cell">
              <span class="m-label">监测物理量</span>
              <span class="m-val">{{ item.physical_quantity || item.channel || '结构响应' }}</span>
            </div>
            <div class="metric-cell">
              <span class="m-label">触发值</span>
              <span class="m-val mono-num highlight-val">
                {{ formatNumber(item.trigger_val) }}
                <small>{{ item.unit || '' }}</small>
              </span>
            </div>
            <div class="metric-cell">
              <span class="m-label">触发次数</span>
              <span class="m-val mono-num">{{ item.trigger_count || 1 }} 次</span>
            </div>
            <div class="metric-cell">
              <span class="m-label">首次触发</span>
              <span class="m-val mono-num">{{ formatDateTimeWithMs(item.create_time) }}</span>
            </div>
            <div class="metric-cell">
              <span class="m-label">最后更新</span>
              <span class="m-val mono-num">
                {{ formatDateTimeWithMs(item.update_time || item.create_time) }}
              </span>
            </div>
          </div>

        </div>

        <!-- Footer Direct Actions -->
        <div class="alarm-toast-footer">
          <span class="seq-badge mono-num">SEQ #{{ item.seq }}</span>
          <div class="footer-btns">
            <el-button
              size="small"
              type="info"
              plain
              @click="handleInspectDetail(item)"
            >
              快照与时间线
            </el-button>
            <el-button
              size="small"
              :type="item.alert_level === 1 ? 'danger' : item.alert_level === 2 ? 'warning' : 'primary'"
              :loading="item.acking"
              @click="handleQuickAck(item)"
            >
              <el-icon style="margin-right: 4px"><Select /></el-icon>
              标记已处理
            </el-button>
          </div>
        </div>
      </div>
    </transition-group>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { BellFilled, Select } from '@element-plus/icons-vue'
import { alertAudio } from '../utils/audio'
import { formatDateTimeWithMs } from '../utils/time'
import {
  pollAlertsApi,
  ackAlertApi,
  formatAlertTypeLabel
} from '../api'

const props = defineProps({
  muted: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits([
  'status-change',
  'alert-arrived',
  'inspect-alert',
  'alert-acked'
])

const LAST_SEQ_STORAGE_KEY = 'last_notified_seq'
const BASELINE_STORAGE_KEY = 'alert_baseline_initialized'
const POLL_INTERVAL_MS = 5000
const popupQueue = ref([])
const pollActive = ref(false)
const lastNotifiedSeq = ref(Number(localStorage.getItem(LAST_SEQ_STORAGE_KEY) || 0))
const baselineInitialized = ref(localStorage.getItem(BASELINE_STORAGE_KEY) === '1')
const lastPollTime = ref('')

let pollTimer = null
let pollInFlight = false

function getStoredMaxSeq() {
  const val = Number(localStorage.getItem(LAST_SEQ_STORAGE_KEY) || 0)
  return Number.isFinite(val) ? val : 0
}

function setStoredMaxSeq(seq) {
  const current = getStoredMaxSeq()
  const next = Math.max(current, Number(seq) || 0)
  localStorage.setItem(LAST_SEQ_STORAGE_KEY, String(next))
  lastNotifiedSeq.value = next
}

function emitStatus() {
  emit('status-change', {
    pollActive: pollActive.value,
    lastSeq: lastNotifiedSeq.value,
    lastPollTime: lastPollTime.value
  })
}

function formatSeverityBadge(level) {
  const lvl = Number(level)
  if (lvl === 1) return '一级预警 (红色紧急)'
  if (lvl === 2) return '二级预警 (橙色重要)'
  if (lvl === 3) return '三级预警 (黄色一般)'
  return '未知等级'
}

function formatAlertType(type) {
  return formatAlertTypeLabel(type)
}

function formatNumber(val) {
  if (val === null || val === undefined || val === '') return '-'
  const num = Number(val)
  if (!Number.isFinite(num)) return String(val)
  return Math.abs(num) >= 100 ? num.toFixed(2) : num.toFixed(4).replace(/\.?0+$/, '')
}

/**
 * Process an incoming alert record from API polling.
 * Deduplicates across browser tabs via localStorage('last_notified_seq')
 */
function processIncomingAlert(alertObj, options = { forcePopup: false }) {
  if (!alertObj || typeof alertObj !== 'object') return
  const seq = Number(alertObj.seq || 0)
  const level = Number(alertObj.alert_level ?? 3)
  const alertStatus = Number(alertObj.alert_status ?? 0)
  const storedSeq = getStoredMaxSeq()

  if (!options.forcePopup) {
    if (seq <= storedSeq || alertStatus !== 0) {
      if (seq > storedSeq) setStoredMaxSeq(seq)
      return
    }
    if (level < 1) {
      setStoredMaxSeq(seq)
      return
    }
  }

  if (seq > storedSeq) {
    setStoredMaxSeq(seq)
  }

  // Play synthesized Web Audio API alarm beep for Level 1/2/3
  alertAudio.playAlertBeep(level, options.forcePopup && !props.muted)

  // Merge repeated events for the same active alert.
  const existingIdx = popupQueue.value.findIndex(
    (p) => alertObj.id && p.id === alertObj.id
  )

  const enrichedPopup = {
    ...alertObj,
    popupKey: `${alertObj.id || alertObj.sensor_no}-${seq}-${Date.now()}`,
    isUpgrade: existingIdx >= 0 || Number(alertObj.trigger_count || 1) > 1,
    acking: false
  }

  if (existingIdx >= 0) {
    popupQueue.value.splice(existingIdx, 1)
  }
  popupQueue.value.unshift(enrichedPopup)

  // Keep maximum 5 popups visible at once
  if (popupQueue.value.length > 5) {
    popupQueue.value.length = 5
  }

  emit('alert-arrived', enrichedPopup)
  emitStatus()
}

function dismissPopup(popupKey) {
  popupQueue.value = popupQueue.value.filter((p) => p.popupKey !== popupKey)
}

function clearAllPopups() {
  popupQueue.value = []
}

async function handleQuickAck(item) {
  if (!item.id) {
    dismissPopup(item.popupKey)
    return
  }
  item.acking = true
  try {
    await ackAlertApi(item.id)
    ElMessage.success(`已处理预警：${item.sensor_name || item.sensor_no}`)
    dismissPopup(item.popupKey)
    emit('alert-acked', item)
  } catch (err) {
    ElMessage.error(`处理失败: ${err?.message || '未知错误'}`)
  } finally {
    item.acking = false
  }
}

async function ackAllPopups() {
  const items = [...popupQueue.value]
  for (const item of items) {
    await handleQuickAck(item)
  }
}

function handleInspectDetail(item) {
  emit('inspect-alert', item)
}

// Poll unhandled alerts every 5 seconds.
async function runCursorPoll() {
  if (pollInFlight) return
  pollInFlight = true
  try {
    const afterSeq = getStoredMaxSeq()
    const res = await pollAlertsApi(afterSeq)
    pollActive.value = true
    lastPollTime.value = new Date().toLocaleTimeString()

    // On initial page load when localStorage has 0, initialize baseline seq so historical alerts don't flood popups
    const serverMaxSeq = Number(res.latestSeq ?? res.latest_seq ?? res.maxSeq ?? res.max_seq ?? 0)
    const items = Array.isArray(res.alerts) ? res.alerts : Array.isArray(res.items) ? res.items : []

    if (!baselineInitialized.value) {
      // The first poll establishes the baseline only; history must not open popups.
      setStoredMaxSeq(serverMaxSeq)
      baselineInitialized.value = true
      localStorage.setItem(BASELINE_STORAGE_KEY, '1')
    } else if (items.length > 0) {
      // Sort ascending by seq so upgrades play in chronological order
      const sorted = [...items].sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0))
      for (const alertItem of sorted) {
        processIncomingAlert(alertItem)
      }
    }
    emitStatus()
  } catch (_) {
    pollActive.value = false
    emitStatus()
  } finally {
    pollInFlight = false
  }
}

function handleStorageSync(e) {
  if (e.key === LAST_SEQ_STORAGE_KEY && e.newValue) {
    lastNotifiedSeq.value = Number(e.newValue) || 0
    emitStatus()
  }
}

onMounted(async () => {
  window.addEventListener('storage', handleStorageSync)
  await runCursorPoll()
  pollTimer = setInterval(runCursorPoll, POLL_INTERVAL_MS)
})

onBeforeUnmount(() => {
  window.removeEventListener('storage', handleStorageSync)
  clearInterval(pollTimer)
})

defineExpose({
  processIncomingAlert,
  runCursorPoll,
  clearAllPopups
})
</script>

<style scoped>
.global-alert-popup-stack {
  position: fixed;
  top: 70px;
  right: 20px;
  width: 440px;
  max-width: calc(100vw - 32px);
  z-index: 3000;
  display: flex;
  flex-direction: column;
  gap: 10px;
  pointer-events: none;
}

.popup-stack-bar {
  pointer-events: auto;
  background: rgba(13, 22, 41, 0.95);
  border: 1px solid #335594;
  border-radius: 6px;
  padding: 6px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(8px);
}

.stack-count {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  font-weight: 700;
  color: #e8f1ff;
}

.popup-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.industrial-alarm-toast {
  pointer-events: auto;
  background: linear-gradient(145deg, rgba(18, 30, 56, 0.98) 0%, rgba(11, 20, 38, 0.98) 100%);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.65);
  backdrop-filter: blur(12px);
  border: 1px solid #2b4c87;
  transition: all 0.28s cubic-bezier(0.16, 1, 0.3, 1);
}

.industrial-alarm-toast.alarm-level-1 {
  border: 1.5px solid #ff3b5c;
  box-shadow: 0 10px 30px rgba(255, 59, 92, 0.32), inset 0 0 18px rgba(255, 59, 92, 0.12);
}

.industrial-alarm-toast.alarm-level-2 {
  border: 1.5px solid #ff8f1f;
  box-shadow: 0 10px 30px rgba(255, 143, 31, 0.26), inset 0 0 14px rgba(255, 143, 31, 0.1);
}

.industrial-alarm-toast.alarm-level-3 {
  border: 1.5px solid #fadb14;
  box-shadow: 0 10px 26px rgba(250, 219, 20, 0.2);
}

.alarm-toast-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 12px;
  background: rgba(255, 255, 255, 0.04);
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.severity-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 800;
  color: #fff;
}

.pill-l1 {
  background: linear-gradient(90deg, #e11d48, #ff3b5c);
  box-shadow: 0 0 10px rgba(255, 59, 92, 0.5);
}

.pill-l2 {
  background: linear-gradient(90deg, #d97706, #ff8f1f);
}

.pill-l3 {
  background: linear-gradient(90deg, #ca8a04, #eab308);
  color: #0f172a;
}

.siren-icon {
  animation: bellShake 0.9s infinite alternate;
}

@keyframes bellShake {
  0% { transform: rotate(-12deg); }
  100% { transform: rotate(12deg); }
}

.type-pill {
  background: rgba(0, 212, 255, 0.14);
  color: #00d4ff;
  border: 1px solid rgba(0, 212, 255, 0.35);
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 4px;
}

.upgrade-pill {
  background: rgba(168, 85, 247, 0.2);
  color: #d8b4fe;
  border: 1px solid rgba(168, 85, 247, 0.45);
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
}

.close-btn {
  background: transparent;
  border: none;
  color: #94a9c9;
  font-size: 18px;
  cursor: pointer;
  line-height: 1;
  padding: 0 4px;
}

.close-btn:hover {
  color: #fff;
}

.alarm-toast-body {
  padding: 11px 12px;
}

.sensor-headline {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.station-tag {
  background: #1e3a8a;
  color: #93c5fd;
  font-size: 11px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 3px;
}

.sensor-title {
  font-weight: 700;
  font-size: 14px;
  color: #f8fafc;
}

.sensor-code {
  font-size: 12px;
  color: #94a9c9;
}

.metrics-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 10px;
  background: rgba(7, 13, 25, 0.65);
  border: 1px solid rgba(43, 76, 135, 0.45);
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 8px;
}

.metric-cell {
  display: flex;
  flex-direction: column;
}

.m-label {
  font-size: 11px;
  color: #7c93b8;
}

.m-val {
  font-size: 12px;
  color: #e2e8f0;
  font-weight: 600;
}

.highlight-val {
  color: #ff6b81;
}

.alarm-toast-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: rgba(7, 13, 25, 0.75);
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}

.seq-badge {
  font-size: 11px;
  color: #64748b;
}

.footer-btns {
  display: flex;
  align-items: center;
  gap: 8px;
}

.alert-slide-enter-active,
.alert-slide-leave-active {
  transition: all 0.3s ease;
}
.alert-slide-enter-from {
  opacity: 0;
  transform: translateX(60px) scale(0.95);
}
.alert-slide-leave-to {
  opacity: 0;
  transform: translateX(60px) scale(0.92);
}
</style>
