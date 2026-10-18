<template>
  <div class="shm-app-shell">
    <!-- Top High-Contrast Industrial Navigation Header -->
    <header class="shm-top-navbar">
      <div class="brand-area" @click="$router.push('/alerts')">
        <div class="brand-logo">
          <span class="logo-bridge-icon">🌉</span>
        </div>
        <div class="brand-text">
          <div class="brand-title">
            淮北西 & 玉环大桥 · 结构健康监测(SHM)智能预警中枢
          </div>
          <div class="brand-subtitle">
            TimescaleDB 3.98亿行时序驱动 · 1000ms断流检测 · 三级动态阈值 · 极值包络与16扇区风玫瑰
          </div>
        </div>
      </div>

      <!-- Navigation Tabs -->
      <nav class="shm-nav-tabs">
        <router-link to="/alerts" class="nav-tab-item" active-class="active">
          <el-icon><WarningFilled /></el-icon>
          <span>综合预警监控中心</span>
          <span v-if="activeRedCount > 0" class="tab-badge red">{{ activeRedCount }}</span>
        </router-link>
        <router-link to="/extremes" class="nav-tab-item" active-class="active">
          <el-icon><TrendCharts /></el-icon>
          <span>传感器定时最大最小值看板</span>
        </router-link>
        <router-link to="/wind-rose" class="nav-tab-item" active-class="active">
          <el-icon><Compass /></el-icon>
          <span>桥梁风玫瑰图与风场分析</span>
        </router-link>
        <router-link to="/thresholds" class="nav-tab-item" active-class="active">
          <el-icon><Operation /></el-icon>
          <span>三级阈值调试与回放试算台</span>
        </router-link>
      </nav>

      <!-- Right Status & Quick Action Controls -->
      <div class="shm-header-controls">
        <!-- Engine Watermark & Mode Badge -->
        <div class="engine-status-pill" title="当前时序分析水位线与引擎运行模式">
          <span class="pulse-dot" :class="engineStatus.isRunning ? 'green' : 'orange'"></span>
          <span class="mode-tag">{{ engineStatus.mode || 'REPLAY' }}</span>
          <span class="watermark-text mono-num">{{ engineStatus.currentWatermark || '2026-09-28 13:55:00' }}</span>
        </div>

        <!-- Dual-Mode SSE + Cursor Polling Indicator -->
        <div
          class="comm-status-pill"
          :class="{ connected: commState.sseConnected || commState.pollActive }"
          title="双通道实时通信：EventSource('/api/alerts/stream') + 3秒游标轮询('/api/alerts/poll')"
        >
          <span class="pulse-dot" :class="commState.sseConnected ? 'green' : commState.pollActive ? 'orange' : 'red'"></span>
          <span>{{ commState.sseConnected ? 'SSE毫秒推送+轮询双活' : '游标轮询补偿中' }}</span>
          <span class="seq-counter mono-num">SEQ#{{ commState.lastSeq || 0 }}</span>
        </div>

        <!-- Sound Mute / Unmute Toggle -->
        <el-tooltip :content="isMuted ? '当前已静音，点击开启Web Audio三级声光报警音' : '声光报警已开启 (L1急促警笛/L2双脉冲/L3单音)'" placement="bottom">
          <el-button
            size="small"
            :type="isMuted ? 'info' : 'success'"
            plain
            class="icon-ctrl-btn"
            @click="handleToggleMute"
          >
            <el-icon><BellFilled v-if="!isMuted" /><MuteNotification v-else /></el-icon>
            <span>{{ isMuted ? '已静音' : '声光开启' }}</span>
          </el-button>
        </el-tooltip>

        <!-- Theme Switch (Dark / Light Industrial Theme) -->
        <el-tooltip content="切换深色工业大屏 / 浅色工程报表主题" placement="bottom">
          <el-button size="small" type="info" plain class="icon-ctrl-btn" @click="toggleTheme">
            <el-icon><Moon v-if="isDark" /><Sunny v-else /></el-icon>
          </el-button>
        </el-tooltip>

        <!-- Quick Simulate / Replay Popup Button -->
        <el-button
          type="danger"
          size="small"
          class="quick-sim-btn"
          :loading="simulating"
          @click="handleQuickSimulatePopup"
        >
          <el-icon style="margin-right: 4px"><VideoPlay /></el-icon>
          一键回放/模拟实时预警弹窗
        </el-button>
      </div>
    </header>

    <!-- Main Router View Content -->
    <main class="shm-main-container">
      <router-view
        v-slot="{ Component }"
        :engine-status="engineStatus"
        :last-alert-event="lastAlertEvent"
        @refresh-global="fetchGlobalSummary"
        @trigger-popup="triggerPopupManually"
        @inspect-alert="openInspectDrawer"
      >
        <keep-alive>
          <component
            :is="Component"
            :engine-status="engineStatus"
            :last-alert-event="lastAlertEvent"
            @refresh-global="fetchGlobalSummary"
            @trigger-popup="triggerPopupManually"
            @inspect-alert="openInspectDrawer"
          />
        </keep-alive>
      </router-view>
    </main>

    <!-- Global Real-Time Alert Popup Stack (SSE + 3s Cursor Polling + Cross-tab localStorage Dedup) -->
    <GlobalAlertPopup
      ref="globalPopupRef"
      :muted="isMuted"
      @status-change="onCommStatusChange"
      @alert-arrived="onAlertArrived"
      @inspect-alert="openInspectDrawer"
      @alert-acked="onAlertAcked"
    />

    <!-- Global Incident Snapshot & Timeline Drawer -->
    <el-drawer
      v-model="inspectDrawerVisible"
      title="预警事件全生命周期详情 · 阈值快照 (threshold_snapshot) 与演进时间线 (timeline_log)"
      size="620px"
      destroy-on-close
    >
      <div v-if="inspectedAlert" class="inspect-drawer-body">
        <div class="inspect-top-banner" :class="`banner-l${inspectedAlert.alert_level}`">
          <div class="banner-row">
            <span class="level-badge" :class="`level-badge-${inspectedAlert.alert_level}`">
              {{ formatLevelText(inspectedAlert.alert_level) }}
            </span>
            <el-tag size="small" effect="dark" type="info">{{ formatAlertTypeLabel(inspectedAlert.alert_type) }}</el-tag>
            <el-tag size="small" :type="inspectedAlert.alert_status === 0 ? 'danger' : inspectedAlert.alert_status === 1 ? 'warning' : 'success'">
              {{ formatStatusLabel(inspectedAlert.alert_status) }}
            </el-tag>
            <span class="mono-num seq-tag">SEQ #{{ inspectedAlert.seq }}</span>
          </div>
          <div class="banner-title">
            {{ inspectedAlert.station_name || '玉环站' }} · {{ inspectedAlert.sensor_name || inspectedAlert.sensor_no }}
            <span class="mono-num">({{ inspectedAlert.sensor_no }} / {{ inspectedAlert.channel || '默认通道' }})</span>
          </div>
          <div class="banner-remark">{{ inspectedAlert.remark }}</div>
        </div>

        <el-descriptions :column="2" border size="small" class="inspect-desc">
          <el-descriptions-item label="事件ID">{{ inspectedAlert.id }}</el-descriptions-item>
          <el-descriptions-item label="去重键 (dedup_key)">
            <span class="mono-num">{{ inspectedAlert.dedup_key || '-' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="首次触发值 (trigger_val)">
            <span class="mono-num">{{ inspectedAlert.trigger_val }} {{ inspectedAlert.unit || '' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="历史极值 (peak_val)">
            <span class="mono-num" style="color: var(--shm-red); font-weight: 700">
              {{ inspectedAlert.peak_val }} {{ inspectedAlert.unit || '' }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="极值发生时刻">
            <span class="mono-num">{{ inspectedAlert.peak_time || inspectedAlert.last_fire_time }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="累计触发/升级次数">
            <el-tag size="small" type="danger">{{ inspectedAlert.trigger_count || 1 }} 次</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="首次触发时间">
            <span class="mono-num">{{ inspectedAlert.create_time }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="最新触发时间">
            <span class="mono-num">{{ inspectedAlert.last_fire_time }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="触发时规则版本">
            <el-tag size="small" type="primary">v{{ inspectedAlert.rule_version || 1 }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="超限方向">
            {{ inspectedAlert.direction || 'NONE' }}
          </el-descriptions-item>
        </el-descriptions>

        <!-- Threshold Snapshot JSONB -->
        <div class="drawer-section">
          <div class="section-title">📸 触发时刻阈值快照 (threshold_snapshot JSONB · 规则版本 v{{ inspectedAlert.rule_version || 1 }})</div>
          <pre class="json-viewer mono-num">{{ formatJsonPretty(inspectedAlert.threshold_snapshot) }}</pre>
        </div>

        <!-- Timeline Log JSONB -->
        <div class="drawer-section">
          <div class="section-title">⏱️ 事件演进时间线 (timeline_log JSONB · 触发 ➔ 升级 ➔ 确认 ➔ 恢复)</div>
          <el-timeline v-if="parsedTimeline.length > 0" class="shm-timeline">
            <el-timeline-item
              v-for="(log, idx) in parsedTimeline"
              :key="idx"
              :timestamp="log.time || log.ts"
              :type="log.level === 1 ? 'danger' : log.level === 2 ? 'warning' : 'primary'"
              placement="top"
            >
              <div class="timeline-card">
                <div class="tl-head">
                  <el-tag size="small" :type="log.event === 'TRIGGER' ? 'danger' : log.event === 'ACK' ? 'success' : 'warning'">
                    {{ log.event || 'EVENT' }}
                  </el-tag>
                  <span v-if="log.level" class="mono-num">等级 L{{ log.level }}</span>
                  <span v-if="log.val !== undefined && log.val !== null" class="mono-num">数值: {{ log.val }}</span>
                </div>
                <div class="tl-msg">{{ log.msg || log.note || log.remark }}</div>
              </div>
            </el-timeline-item>
          </el-timeline>
          <el-empty v-else description="暂无扩展时间线记录" :image-size="60" />
        </div>

        <!-- Direct Acknowledge Form inside Drawer -->
        <div class="drawer-section ack-form-box">
          <div class="section-title">🛡️ 人工确认与处置归档</div>
          <el-form :model="ackForm" label-width="85px" size="small">
            <el-form-item label="处置工程师">
              <el-input v-model="ackForm.ack_user" placeholder="请输入值班工程师姓名" />
            </el-form-item>
            <el-form-item label="处置动作">
              <el-radio-group v-model="ackForm.new_status">
                <el-radio :value="1">确认处理中 (Acked)</el-radio>
                <el-radio :value="2">标记已恢复 (Resolved)</el-radio>
                <el-radio :value="3">人工关闭 (Closed)</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="处置备注">
              <el-input
                v-model="ackForm.ack_note"
                type="textarea"
                :rows="2"
                placeholder="填写现场核查结果或传感器维护记录..."
              />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="ackSubmitting" @click="submitDrawerAck">
                提交处置结果并写入时间线
              </el-button>
            </el-form-item>
          </el-form>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import {
  WarningFilled,
  TrendCharts,
  Compass,
  Operation,
  BellFilled,
  MuteNotification,
  Moon,
  Sunny,
  VideoPlay
} from '@element-plus/icons-vue'
import GlobalAlertPopup from './components/GlobalAlertPopup.vue'
import { alertAudio } from './utils/audio'
import {
  getAlertsSummaryApi,
  simulateRealtimeAlertApi,
  ackAlertApi,
  formatAlertTypeLabel
} from './api'

const globalPopupRef = ref(null)
const isMuted = ref(alertAudio.isMuted())
const isDark = ref(true)
const simulating = ref(false)
const activeRedCount = ref(0)
const lastAlertEvent = ref(null)

const engineStatus = ref({
  mode: 'REPLAY',
  isRunning: false,
  currentWatermark: '2026-09-28 13:55:00',
  latestDataTimestamp: '2026-09-30 17:55:20',
  loadedRulesCount: 208,
  enabledRulesCount: 185,
  uncommissionedCount: 23,
  onlineChannels: 185,
  offlineChannels: 23
})

const commState = ref({
  sseConnected: false,
  pollActive: true,
  lastSeq: Number(localStorage.getItem('last_notified_seq') || 0),
  lastHeartbeat: ''
})

// Drawer state
const inspectDrawerVisible = ref(false)
const inspectedAlert = ref(null)
const ackSubmitting = ref(false)
const ackForm = ref({
  ack_user: '张工 (SHM值班主任)',
  new_status: 1,
  ack_note: '已复核传感器时序波形与现场工况，确认记录。'
})

const parsedTimeline = computed(() => {
  if (!inspectedAlert.value) return []
  const raw = inspectedAlert.value.timeline_log
  if (Array.isArray(raw)) return raw
  if (typeof raw === 'string') {
    try {
      return JSON.parse(raw)
    } catch (_) {
      return []
    }
  }
  return []
})

function onCommStatusChange(state) {
  commState.value = { ...commState.value, ...state }
}

function onAlertArrived(alertObj) {
  lastAlertEvent.value = { ...alertObj, _ts: Date.now() }
  fetchGlobalSummary()
}

function onAlertAcked() {
  lastAlertEvent.value = { _ts: Date.now(), acked: true }
  fetchGlobalSummary()
}

function handleToggleMute() {
  isMuted.value = alertAudio.toggleMute()
  if (!isMuted.value) {
    alertAudio.playAlertBeep(3, true)
    ElMessage.success('Web Audio 声光报警已开启 (已播放测试提示音)')
  } else {
    ElMessage.info('已切换为静音模式')
  }
}

function toggleTheme() {
  isDark.value = !isDark.value
  const htmlEl = document.documentElement
  if (isDark.value) {
    htmlEl.classList.add('dark')
    htmlEl.classList.remove('light-theme')
  } else {
    htmlEl.classList.remove('dark')
    htmlEl.classList.add('light-theme')
  }
}

async function fetchGlobalSummary() {
  try {
    const res = await getAlertsSummaryApi()
    if (res.engine) {
      engineStatus.value = {
        ...engineStatus.value,
        ...res.engine
      }
    }
    if (res.kpi) {
      activeRedCount.value = Number(res.kpi.level1Count ?? res.kpi.activeLevel1 ?? 0)
    }
  } catch (_) {}
}

async function handleQuickSimulatePopup() {
  simulating.value = true
  try {
    const alertRecord = await simulateRealtimeAlertApi({})
    if (alertRecord && globalPopupRef.value) {
      globalPopupRef.value.processIncomingAlert(alertRecord, { forcePopup: true })
    }
    lastAlertEvent.value = { ...alertRecord, _ts: Date.now() }
    await fetchGlobalSummary()
    ElMessage.success(
      `已触发实时预警并推送弹窗: [${alertRecord.station_name || '玉环站'}] ${alertRecord.sensor_name || alertRecord.sensor_no}`
    )
  } catch (err) {
    ElMessage.error(`触发模拟预警失败: ${err?.message || '请检查后端连接'}`)
  } finally {
    simulating.value = false
  }
}

function triggerPopupManually(alertRecord) {
  if (alertRecord && globalPopupRef.value) {
    globalPopupRef.value.processIncomingAlert(alertRecord, { forcePopup: true })
  }
}

function openInspectDrawer(alertRow) {
  inspectedAlert.value = { ...alertRow }
  inspectDrawerVisible.value = true
}

async function submitDrawerAck() {
  if (!inspectedAlert.value?.id) return
  ackSubmitting.value = true
  try {
    const updated = await ackAlertApi(inspectedAlert.value.id, ackForm.value)
    if (updated && updated.id) {
      inspectedAlert.value = { ...inspectedAlert.value, ...updated }
    } else {
      inspectedAlert.value.alert_status = ackForm.value.new_status
    }
    ElMessage.success('预警处置状态及时间线已更新')
    lastAlertEvent.value = { _ts: Date.now(), acked: true }
    fetchGlobalSummary()
  } catch (err) {
    ElMessage.error(`处置提交失败: ${err?.message || '未知错误'}`)
  } finally {
    ackSubmitting.value = false
  }
}

function formatLevelText(level) {
  const l = Number(level)
  if (l === 1) return '一级预警 (红色紧急)'
  if (l === 2) return '二级预警 (橙色重要)'
  if (l === 3) return '三级预警 (黄色一般)'
  return '0级断流标记'
}

function formatStatusLabel(status) {
  const s = Number(status)
  if (s === 0) return '未处理 (Active)'
  if (s === 1) return '已确认 (Acked)'
  if (s === 2) return '已恢复 (Resolved)'
  if (s === 3) return '已关闭 (Closed)'
  if (s === 4) return '采集仪抑制 (Suppressed)'
  return '未知状态'
}

function formatJsonPretty(obj) {
  if (!obj) return '{}'
  if (typeof obj === 'string') {
    try {
      return JSON.stringify(JSON.parse(obj), null, 2)
    } catch (_) {
      return obj
    }
  }
  return JSON.stringify(obj, null, 2)
}

let summaryTimer = null
onMounted(() => {
  fetchGlobalSummary()
  summaryTimer = setInterval(fetchGlobalSummary, 6000)
})

onBeforeUnmount(() => {
  clearInterval(summaryTimer)
})
</script>

<style scoped>
.shm-app-shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--shm-bg-primary);
}

.shm-top-navbar {
  height: 62px;
  background: var(--shm-header-gradient);
  border-bottom: 1px solid var(--shm-border-bright);
  padding: 0 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  position: sticky;
  top: 0;
  z-index: 1000;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.45);
}

.brand-area {
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  flex-shrink: 0;
}

.brand-logo {
  width: 38px;
  height: 38px;
  border-radius: 8px;
  background: linear-gradient(135deg, #0284c7, #1d4ed8);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  box-shadow: 0 0 12px rgba(0, 212, 255, 0.4);
}

.brand-title {
  font-size: 15px;
  font-weight: 800;
  color: #f8fafc;
  letter-spacing: 0.4px;
}

.brand-subtitle {
  font-size: 11px;
  color: #93c5fd;
  opacity: 0.85;
}

.shm-nav-tabs {
  display: flex;
  align-items: center;
  gap: 6px;
}

.nav-tab-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 13px;
  border-radius: 6px;
  color: #cbd5e1;
  text-decoration: none;
  font-size: 13px;
  font-weight: 600;
  transition: all 0.2s ease;
  border: 1px solid transparent;
  white-space: nowrap;
}

.nav-tab-item:hover {
  color: #ffffff;
  background: rgba(255, 255, 255, 0.06);
}

.nav-tab-item.active {
  color: #00d4ff;
  background: rgba(0, 212, 255, 0.12);
  border-color: rgba(0, 212, 255, 0.4);
  box-shadow: inset 0 0 10px rgba(0, 212, 255, 0.15);
}

.tab-badge.red {
  background: #ff3b5c;
  color: #fff;
  font-size: 11px;
  font-weight: 800;
  padding: 0 6px;
  border-radius: 10px;
  line-height: 18px;
}

.shm-header-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.engine-status-pill,
.comm-status-pill {
  display: flex;
  align-items: center;
  gap: 6px;
  background: rgba(15, 23, 42, 0.75);
  border: 1px solid rgba(59, 130, 246, 0.35);
  padding: 4px 9px;
  border-radius: 6px;
  font-size: 12px;
  color: #dbeafe;
}

.mode-tag {
  background: rgba(0, 212, 255, 0.18);
  color: #00d4ff;
  font-weight: 700;
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 11px;
}

.watermark-text {
  color: #93c5fd;
  font-size: 11px;
}

.seq-counter {
  background: rgba(255, 255, 255, 0.08);
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 11px;
  color: #38bdf8;
}

.quick-sim-btn {
  font-weight: 700;
  box-shadow: 0 0 14px rgba(255, 59, 92, 0.45);
}

.shm-main-container {
  flex: 1;
  padding: 16px 20px 28px;
  max-width: 1920px;
  width: 100%;
  margin: 0 auto;
}

/* Drawer Styles */
.inspect-drawer-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.inspect-top-banner {
  padding: 12px 14px;
  border-radius: 8px;
  border: 1px solid var(--shm-border-bright);
  background: rgba(15, 23, 42, 0.6);
}

.banner-l1 {
  border-color: rgba(255, 59, 92, 0.6);
  background: rgba(255, 59, 92, 0.08);
}

.banner-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.seq-tag {
  margin-left: auto;
  font-size: 12px;
  color: var(--shm-text-secondary);
}

.banner-title {
  font-size: 15px;
  font-weight: 700;
  margin-bottom: 4px;
}

.banner-remark {
  font-size: 12px;
  color: var(--shm-text-secondary);
}

.drawer-section {
  border: 1px solid var(--shm-border);
  border-radius: 6px;
  padding: 12px;
  background: rgba(13, 22, 41, 0.45);
}

.section-title {
  font-size: 13px;
  font-weight: 700;
  margin-bottom: 10px;
  color: var(--shm-accent);
}

.json-viewer {
  background: #070d19;
  color: #38bdf8;
  padding: 10px;
  border-radius: 6px;
  font-size: 12px;
  overflow-x: auto;
  max-height: 180px;
}

.timeline-card {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--shm-border);
  padding: 8px 10px;
  border-radius: 6px;
}

.tl-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  font-size: 12px;
}

.tl-msg {
  font-size: 12px;
  color: var(--shm-text-primary);
}

@media (max-width: 1480px) {
  .brand-subtitle,
  .watermark-text {
    display: none;
  }
}
</style>
