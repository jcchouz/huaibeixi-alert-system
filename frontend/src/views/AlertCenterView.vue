<template>
  <div class="alert-center-view">
    <!-- 1. Top 6 Industrial KPI Cards -->
    <div class="kpi-grid-6">
      <div class="shm-card kpi-card kpi-blue">
        <div class="kpi-top">
          <span class="kpi-label">监测通道总数 (投运/未投运)</span>
          <el-tag size="small" type="primary" effect="dark">{{ kpi.totalSensors || 146 }} 台传感器</el-tag>
        </div>
        <div class="kpi-main mono-num">
          <span class="big-num">{{ kpi.enabledChannels || 185 }}</span>
          <span class="sub-unit">/ {{ kpi.totalChannels || 208 }} 通道</span>
        </div>
        <div class="kpi-foot">
          <span class="badge-dot green">已投运 {{ kpi.enabledChannels || 185 }}</span>
          <span class="badge-dot muted">未投运工程通道 {{ kpi.uncommissionedChannels || 23 }}</span>
        </div>
      </div>

      <div class="shm-card kpi-card kpi-red" @click="quickFilterLevel(1)">
        <div class="kpi-top">
          <span class="kpi-label">一级预警 (红色紧急 · L1)</span>
          <span class="pulse-dot red"></span>
        </div>
        <div class="kpi-main mono-num">
          <span class="big-num red-text">{{ kpi.level1Count }}</span>
          <span class="sub-unit">当前活跃 / 累计 {{ kpi.totalLevel1Count }}</span>
        </div>
        <div class="kpi-foot">
          <span>结构极限超限 · 点击筛选</span>
        </div>
      </div>

      <div class="shm-card kpi-card kpi-orange" @click="quickFilterLevel(2)">
        <div class="kpi-top">
          <span class="kpi-label">二级预警 (橙色重要 · L2)</span>
          <span class="pulse-dot orange"></span>
        </div>
        <div class="kpi-main mono-num">
          <span class="big-num orange-text">{{ kpi.level2Count }}</span>
          <span class="sub-unit">当前活跃 / 累计 {{ kpi.totalLevel2Count }}</span>
        </div>
        <div class="kpi-foot">
          <span>重要阈值越限 / 超物理量程 · 点击筛选</span>
        </div>
      </div>

      <div class="shm-card kpi-card kpi-yellow" @click="quickFilterLevel(3)">
        <div class="kpi-top">
          <span class="kpi-label">三级预警 (黄色一般 · L3)</span>
          <el-tag size="small" type="warning" effect="plain">早期预警</el-tag>
        </div>
        <div class="kpi-main mono-num">
          <span class="big-num yellow-text">{{ kpi.level3Count }}</span>
          <span class="sub-unit">当前活跃 / 累计 {{ kpi.totalLevel3Count }}</span>
        </div>
        <div class="kpi-foot">
          <span>注意级波动 · 点击筛选</span>
        </div>
      </div>

      <div class="shm-card kpi-card kpi-purple" @click="quickFilterOffline">
        <div class="kpi-top">
          <span class="kpi-label">传感器离线监测</span>
          <el-tag size="small" type="info">人工处理模式</el-tag>
        </div>
        <div class="kpi-main mono-num">
          <span class="big-num purple-text">{{ kpi.offlineChannels }}</span>
          <span class="sub-unit">离线 / {{ kpi.ackedAlertsCount }} 条已处理</span>
        </div>
        <div class="kpi-foot">
          <span>在线正常 {{ kpi.onlineChannels }} 通道 · 离线状态实时监测</span>
        </div>
      </div>

      <div class="shm-card kpi-card kpi-cyan">
        <div class="kpi-top">
          <span class="kpi-label">t_monitor 水位线与引擎模式</span>
          <el-tag size="small" :type="engineInfo.isRunning ? 'success' : 'warning'" effect="dark">
            {{ engineInfo.isRunning ? '自动回放运行中' : engineInfo.mode || 'REPLAY' }}
          </el-tag>
        </div>
        <div class="kpi-main mono-num watermark-kpi">
          {{ engineInfo.currentWatermark || '2026-09-28 13:55:00' }}
        </div>
        <div class="kpi-foot mono-num">
          <span>母表 {{ formatMillion(kpi.totalMonitorRows) }} 行 · 极值归档 {{ kpi.statRecordCount }} 条</span>
        </div>
      </div>
    </div>

    <!-- 2. Replay & Live Test Control Bar -->
    <div class="shm-card replay-control-bar">
      <div class="replay-left">
        <div class="bar-title">
          <el-icon><Cpu /></el-icon>
          <span>时序扫描水位线与实时弹窗测试台</span>
        </div>

        <el-select
          v-model="selectedPresetWatermark"
          size="small"
          style="width: 325px"
          placeholder="选择典型历史工况水位线"
          @change="onPresetWatermarkChange"
        >
          <el-option
            label="🔥 2026-09-28 13:55:00 (吞吐峰值 2,573行/秒 · 应力/振动超限)"
            value="2026-09-28 13:55:00"
          />
          <el-option
            label="⚡ 2026-09-30 17:35:00 (采集仪批量掉线)"
            value="2026-09-30 17:35:00"
          />
          <el-option
            label="🌪️ 2026-09-29 14:20:00 (强风荷载与温湿度动态升级窗口)"
            value="2026-09-29 14:20:00"
          />
          <el-option
            label="📊 2026-09-26 09:30:00 (日常平稳运行基线窗口)"
            value="2026-09-26 09:30:00"
          />
        </el-select>

        <el-select v-model="replaySpeed" size="small" style="width: 105px">
          <el-option label="倍速 1x" :value="1" />
          <el-option label="倍速 2x" :value="2" />
          <el-option label="倍速 5x" :value="5" />
          <el-option label="倍速 10x" :value="10" />
        </el-select>
      </div>

      <div class="replay-right">
        <el-button
          type="primary"
          size="small"
          :loading="stepping"
          @click="handleStepScan"
        >
          <el-icon style="margin-right: 4px"><CaretRight /></el-icon>
          【步进扫描真实数据】
        </el-button>

        <el-button
          :type="engineInfo.isRunning ? 'warning' : 'success'"
          size="small"
          :loading="togglingReplay"
          @click="handleToggleAutoReplay"
        >
          <el-icon style="margin-right: 4px">
            <VideoPause v-if="engineInfo.isRunning" />
            <VideoPlay v-else />
          </el-icon>
          {{ engineInfo.isRunning ? '【暂停自动回放】' : '【开启自动回放】' }}
        </el-button>

        <el-dropdown split-button type="danger" size="small" @click="handleSimulateAlert()" @command="handleSimulateCommand">
          <span>🔔 【模拟触发新预警/升级弹窗】</span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="L1_THRESHOLD">🚨 模拟一级红色预警 (主桁架应力超限 stn-12-b)</el-dropdown-item>
              <el-dropdown-item command="L2_ACCL">🟠 模拟二级橙色预警 (去直流振动幅值超限 accl-02)</el-dropdown-item>
              <el-dropdown-item command="L1_UPGRADE">⚡ 模拟三级黄 ➔ 一级红 动态升级弹窗 (wea-01)</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <!--Last Step Result Banner -->
    <div v-if="lastStepFeedback" class="step-feedback-banner">
      <span class="pulse-dot green"></span>
      <span>{{ lastStepFeedback }}</span>
    </div>

    <!-- 3. Alert Type Distribution Strip (Click to filter) -->
    <div class="type-strip">
      <div
        class="type-chip"
        :class="{ active: filters.alertType === '' }"
        @click="selectAlertType('')"
      >
        <span>全部类型</span>
        <span class="chip-cnt mono-num">{{ kpi.totalAlertsCount }}</span>
      </div>
      <div
        v-for="t in allAlertTypes"
        :key="t.code"
        class="type-chip"
        :class="{ active: filters.alertType === t.code }"
        @click="selectAlertType(t.code)"
      >
        <span>{{ t.label }}</span>
        <span class="chip-cnt mono-num">{{ getTypeCount(t.code) }}</span>
      </div>
    </div>

    <!-- 4. Filter Bar & Alert Table (`t_alert_record`) -->
    <div class="shm-card table-card">
      <div class="filter-toolbar">
        <div class="filter-inputs">
          <el-select
            v-model="filters.alertLevel"
            placeholder="预警等级 (全部)"
            clearable
            size="small"
            style="width: 170px"
            @change="handleSearch"
          >
            <el-option label="🔴 一级预警 (红色紧急)" :value="1" />
            <el-option label="🟠 二级预警 (橙色重要)" :value="2" />
            <el-option label="🟡 三级预警 (黄色一般)" :value="3" />
          </el-select>

          <el-select
            v-model="filters.alertType"
            placeholder="预警类型 (全部)"
            clearable
            size="small"
            style="width: 190px"
            @change="handleSearch"
          >
            <el-option
              v-for="t in allAlertTypes"
              :key="t.code"
              :label="`${t.label} (${t.code})`"
              :value="t.code"
            />
          </el-select>

          <el-select
            v-model="filters.alertStatus"
            placeholder="处置状态 (全部)"
            clearable
            size="small"
            style="width: 175px"
            @change="handleSearch"
          >
            <el-option label="未处理" :value="0" />
            <el-option label="已处理" :value="1" />
          </el-select>

          <el-input
            v-model="filters.sensorNo"
            placeholder="传感器编号 (如 stn-12-b / accl-01)"
            clearable
            size="small"
            style="width: 210px"
            @keyup.enter="handleSearch"
            @clear="handleSearch"
          />

          <el-input
            v-model="filters.keyword"
            placeholder="搜索传感器/物理量/通道关键词..."
            clearable
            size="small"
            style="width: 210px"
            @keyup.enter="handleSearch"
            @clear="handleSearch"
          />

          <el-button type="primary" size="small" @click="handleSearch">
            <el-icon style="margin-right: 4px"><Search /></el-icon>查询
          </el-button>
          <el-button size="small" @click="resetFilters">重置</el-button>
        </div>

        <div class="toolbar-actions">
          <el-button
            type="warning"
            plain
            size="small"
            :loading="batchAcking"
            @click="handleBatchAck"
          >
            <el-icon style="margin-right: 4px"><Check /></el-icon>
            一键批量处理未处理告警 ({{ kpi.unackedAlertsCount }})
          </el-button>
        </div>
      </div>

      <el-table
        v-loading="loading"
        :data="alertList"
        border
        stripe
        size="small"
        style="width: 100%"
        @row-click="onRowClick"
      >
        <el-table-column prop="seq" label="SEQ#" width="78" align="center">
          <template #default="{ row }">
            <span class="mono-num seq-cell">#{{ row.seq }}</span>
          </template>
        </el-table-column>

        <el-table-column prop="alert_level" label="预警等级" width="142" align="center">
          <template #default="{ row }">
            <span class="level-badge" :class="`level-badge-${row.alert_level}`">
              {{ formatLevelLabel(row.alert_level) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column prop="alert_type" label="告警类型" width="145" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="getAlertTypeTagColor(row.alert_type)" effect="dark">
              {{ formatAlertTypeLabel(row.alert_type) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="站点 / 传感器 / 通道" min-width="210">
          <template #default="{ row }">
            <div class="sensor-col">
              <span class="st-badge">{{ row.station_name || '玉环站' }}</span>
              <strong>{{ row.sensor_name || row.sensor_no }}</strong>
              <span class="mono-num muted-code">({{ row.sensor_no }}<span v-if="row.channel"> · {{ row.channel }}</span>)</span>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="physical_quantity" label="监测物理量" width="120" align="center">
          <template #default="{ row }">
            <span>{{ row.physical_quantity || row.channel || '-' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="触发值" width="150" align="right">
          <template #default="{ row }">
            <div class="mono-num val-col">
              <div>{{ formatNum(row.trigger_val) }} {{ row.unit || '' }}</div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="处理人 / 处理时间" width="190" align="center">
          <template #default="{ row }">
            <div v-if="row.alert_status === 1" class="mono-num time-col">
              <div>{{ row.ack_by || '-' }}</div>
              <div class="muted-time">{{ row.ack_time || '-' }}</div>
            </div>
            <span v-else>-</span>
          </template>
        </el-table-column>

        <el-table-column prop="trigger_count" label="触发次数" width="85" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.trigger_count > 1 ? 'danger' : 'info'" effect="plain" class="mono-num">
              {{ row.trigger_count || 1 }}次
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="alert_status" label="处置状态" width="135" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="getStatusTagType(row.alert_status)">
              {{ formatStatusText(row.alert_status) }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="触发时间" width="168" align="center">
          <template #default="{ row }">
            <div class="mono-num time-col">
              <div>{{ row.create_time }}</div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="230" fixed="right" align="center">
          <template #default="{ row }">
            <div class="row-actions" @click.stop>
              <el-button
                v-if="row.alert_status === 0"
                type="danger"
                size="small"
                plain
                @click="handleQuickRowAck(row)"
              >
                处理
              </el-button>
              <el-button
                type="primary"
                size="small"
                link
                @click="emit('inspect-alert', row)"
              >
                详情
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-bar">
        <span class="total-hint mono-num">
          共 {{ pagination.total }} 条预警事件记录
        </span>
        <el-pagination
          v-model:current-page="pagination.page"
          v-model:page-size="pagination.pageSize"
          :page-sizes="[15, 20, 50, 100]"
          layout="sizes, prev, pager, next, jumper"
          :total="pagination.total"
          small
          background
          @size-change="fetchAlerts"
          @current-change="fetchAlerts"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Cpu,
  CaretRight,
  VideoPlay,
  VideoPause,
  Search,
  Check
} from '@element-plus/icons-vue'
import {
  getAlertsSummaryApi,
  listAlertsApi,
  ackAlertApi,
  ackAllAlertsApi,
  controlEngineReplayApi,
  simulateRealtimeAlertApi,
  formatAlertTypeLabel
} from '../api'

const props = defineProps({
  engineStatus: {
    type: Object,
    default: () => ({})
  },
  lastAlertEvent: {
    type: Object,
    default: null
  }
})

const emit = defineEmits(['refresh-global', 'trigger-popup', 'inspect-alert'])

const loading = ref(false)
const stepping = ref(false)
const togglingReplay = ref(false)
const batchAcking = ref(false)
const lastStepFeedback = ref('')

const selectedPresetWatermark = ref('2026-09-28 13:55:00')
const replaySpeed = ref(1)

const kpi = reactive({
  totalSensors: 146,
  totalChannels: 208,
  enabledChannels: 185,
  uncommissionedChannels: 23,
  onlineChannels: 185,
  offlineChannels: 23,
  activeAlertsCount: 0,
  unackedAlertsCount: 0,
  resolvedAlertsCount: 0,
  totalAlertsCount: 0,
  level1Count: 0,
  level2Count: 0,
  level3Count: 0,
  totalLevel1Count: 0,
  totalLevel2Count: 0,
  totalLevel3Count: 0,
  totalMonitorRows: 398059510,
  statRecordCount: 0,
  alertsByTypeMap: {}
})

const engineInfo = reactive({
  mode: 'REPLAY',
  isRunning: false,
  currentWatermark: '2026-09-28 13:55:00'
})

const allAlertTypes = [
  { code: '阈值超限', label: '阈值超限' },
  { code: '传感器离线', label: '传感器离线' },
  { code: '采集仪离线', label: '采集仪离线' },
  { code: '传感器死值', label: '传感器死值' },
  { code: '超物理量程', label: '超物理量程' }
]

const filters = reactive({
  alertLevel: null,
  alertType: '',
  alertStatus: null,
  sensorNo: '',
  keyword: ''
})

const pagination = reactive({
  page: 1,
  pageSize: 20,
  total: 0
})

const alertList = ref([])

watch(
  () => props.lastAlertEvent,
  (val) => {
    if (val) {
      fetchSummaryAndTable()
    }
  }
)

async function fetchSummary() {
  try {
    const res = await getAlertsSummaryApi()
    Object.assign(kpi, res.kpi, {
      alertsByTypeMap: res.raw?.alertsByTypeMap || {}
    })
    Object.assign(engineInfo, res.engine)
  } catch (_) {}
}

async function fetchAlerts() {
  loading.value = true
  try {
    const params = {
      page: pagination.page,
      pageSize: pagination.pageSize
    }
    if (filters.alertLevel !== null && filters.alertLevel !== undefined && filters.alertLevel !== '') {
      params.alertLevel = filters.alertLevel
    }
    if (filters.alertType) {
      params.alertType = filters.alertType
    }
    if (filters.alertStatus !== null && filters.alertStatus !== undefined && filters.alertStatus !== '') {
      params.alertStatus = filters.alertStatus
    }
    if (filters.sensorNo) {
      params.sensorNo = filters.sensorNo
    }
    if (filters.keyword) {
      params.keyword = filters.keyword
    }
    const res = await listAlertsApi(params)
    alertList.value = Array.isArray(res.items) ? res.items : []
    pagination.total = Number(res.total || 0)
  } catch (err) {
    ElMessage.error(`加载预警列表失败: ${err?.message || ''}`)
  } finally {
    loading.value = false
  }
}

async function fetchSummaryAndTable() {
  await Promise.all([fetchSummary(), fetchAlerts()])
}

function handleSearch() {
  pagination.page = 1
  fetchAlerts()
}

function resetFilters() {
  filters.alertLevel = null
  filters.alertType = ''
  filters.alertStatus = null
  filters.sensorNo = ''
  filters.keyword = ''
  pagination.page = 1
  fetchAlerts()
}

function quickFilterLevel(lvl) {
  filters.alertLevel = filters.alertLevel === lvl ? null : lvl
  pagination.page = 1
  fetchAlerts()
}

function quickFilterOffline() {
  filters.alertType = filters.alertType === '传感器离线' ? '' : '传感器离线'
  pagination.page = 1
  fetchAlerts()
}

function selectAlertType(code) {
  filters.alertType = code
  pagination.page = 1
  fetchAlerts()
}

function getTypeCount(code) {
  return kpi.alertsByTypeMap?.[code] ?? 0
}

async function onPresetWatermarkChange(val) {
  try {
    const res = await controlEngineReplayApi({
      action: 'step',
      replayTime: val,
      speed: replaySpeed.value
    })
    if (res.engineStatus) {
      Object.assign(engineInfo, res.engineStatus)
    }
    const sr = res.stepResult || {}
    lastStepFeedback.value = `已切换水位线至 ${val} 并完成窗口扫描：处理原始样本 ${sr.rawPointsProcessed ?? 0} 行，归档极值 ${sr.upsertedStatsCount ?? 0} 条，触发/升级预警 ${sr.triggeredAlertsCount ?? 0} 条 (耗时 ${sr.elapsedMs ?? 0}ms)`
    if (sr.triggeredAlerts?.length > 0) {
      emit('trigger-popup', sr.triggeredAlerts[0])
    }
    await fetchSummaryAndTable()
    emit('refresh-global')
  } catch (err) {
    ElMessage.error(`切换水位线失败: ${err?.message || ''}`)
  }
}

async function handleStepScan() {
  stepping.value = true
  try {
    const res = await controlEngineReplayApi({
      action: 'step',
      speed: replaySpeed.value
    })
    if (res.engineStatus) {
      Object.assign(engineInfo, res.engineStatus)
    }
    const sr = res.stepResult || {}
    lastStepFeedback.value = `单步扫描完成 [${sr.windowStart} -> ${sr.windowEnd}]：扫描 ${sr.rawPointsProcessed ?? 0} 行，更新极值 ${sr.upsertedStatsCount ?? 0} 条，触发预警 ${sr.triggeredAlertsCount ?? 0} 条 (${sr.elapsedMs ?? 0}ms)`
    if (sr.triggeredAlerts?.length > 0) {
      emit('trigger-popup', sr.triggeredAlerts[0])
    }
    await fetchSummaryAndTable()
    emit('refresh-global')
  } catch (err) {
    ElMessage.error(`单步扫描失败: ${err?.message || ''}`)
  } finally {
    stepping.value = false
  }
}

async function handleToggleAutoReplay() {
  togglingReplay.value = true
  try {
    const nextAction = engineInfo.isRunning ? 'stop' : 'start'
    const res = await controlEngineReplayApi({
      action: nextAction,
      speed: replaySpeed.value
    })
    if (res.engineStatus) {
      Object.assign(engineInfo, res.engineStatus)
    }
    ElMessage.success(res.message || (engineInfo.isRunning ? '已启动自动回放' : '已暂停自动回放'))
    await fetchSummaryAndTable()
    emit('refresh-global')
  } catch (err) {
    ElMessage.error(`切换自动回放状态失败: ${err?.message || ''}`)
  } finally {
    togglingReplay.value = false
  }
}

async function handleSimulateAlert(customPayload = {}) {
  try {
    const alertRecord = await simulateRealtimeAlertApi(customPayload)
    if (alertRecord) {
      emit('trigger-popup', alertRecord)
      lastStepFeedback.value = `已触发实时预警弹窗 [SEQ #${alertRecord.seq}]：${alertRecord.station_name} · ${alertRecord.sensor_name}`
    }
    await fetchSummaryAndTable()
    emit('refresh-global')
  } catch (err) {
    ElMessage.error(`模拟触发失败: ${err?.message || ''}`)
  }
}

function handleSimulateCommand(cmd) {
  if (cmd === 'L1_THRESHOLD') {
    handleSimulateAlert({ sensorNo: 'stn-12-b', channel: 'stress', alertLevel: 1, alertType: '阈值超限' })
  } else if (cmd === 'L2_ACCL') {
    handleSimulateAlert({ sensorNo: 'accl-02', channel: 'y', alertLevel: 2, alertType: '阈值超限' })
  } else if (cmd === 'L1_UPGRADE') {
    handleSimulateAlert({ sensorNo: 'wea-01', channel: 'temperature', alertLevel: 1, alertType: '阈值超限' })
  }
}

async function handleQuickRowAck(row) {
  try {
    await ackAlertApi(row.id, {
      ack_user: '值班工程师'
    })
    ElMessage.success(`已处理预警 #${row.seq}`)
    await fetchSummaryAndTable()
    emit('refresh-global')
  } catch (err) {
    ElMessage.error(`处理失败: ${err?.message || ''}`)
  }
}

async function handleBatchAck() {
  try {
    await ElMessageBox.confirm(
      '确定将当前所有未处理预警批量标记为“已处理”吗？',
      '批量处理',
      { type: 'warning', confirmButtonText: '确认批量处理', cancelButtonText: '取消' }
    )
    batchAcking.value = true
    const res = await ackAllAlertsApi({
      ackBy: '值班主任 (批量处理)',
      alertLevel: filters.alertLevel
    })
    ElMessage.success(`批量处理完成，共处理 ${res.ackedCount ?? 0} 条告警`)
    await fetchSummaryAndTable()
    emit('refresh-global')
  } catch (_) {
  } finally {
    batchAcking.value = false
  }
}

function onRowClick(row) {
  emit('inspect-alert', row)
}

function formatLevelLabel(lvl) {
  const n = Number(lvl)
  if (n === 1) return '一级预警 (红色紧急)'
  if (n === 2) return '二级预警 (橙色重要)'
  if (n === 3) return '三级预警 (黄色一般)'
  return '未知等级'
}

function getAlertTypeTagColor(type) {
  if (type === '阈值超限' || type === '采集仪离线') return 'danger'
  if (type === '传感器离线' || type === '超物理量程') return 'warning'
  return 'primary'
}

function getStatusTagType(status) {
  const s = Number(status)
  if (s === 0) return 'danger'
  if (s === 1) return 'warning'
  return 'info'
}

function formatStatusText(status) {
  const s = Number(status)
  if (s === 0) return '未处理'
  if (s === 1) return '已处理'
  return '未知'
}

function formatNum(val) {
  if (val === null || val === undefined || val === '') return '-'
  const num = Number(val)
  if (!Number.isFinite(num)) return String(val)
  return Math.abs(num) >= 100 ? num.toFixed(2) : num.toFixed(4).replace(/\.?0+$/, '')
}

function formatMillion(n) {
  const num = Number(n || 0)
  if (num >= 100000000) return `${(num / 100000000).toFixed(2)}亿`
  if (num >= 10000) return `${(num / 10000).toFixed(1)}万`
  return String(num)
}

onMounted(() => {
  fetchSummaryAndTable()
})
</script>

<style scoped>
.alert-center-view {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.kpi-grid-6 {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 12px;
}

.kpi-card {
  cursor: pointer;
  padding: 13px 15px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  min-height: 112px;
}

.kpi-blue { border-top: 3px solid #3b82f6; }
.kpi-red { border-top: 3px solid #ff3b5c; }
.kpi-orange { border-top: 3px solid #ff8f1f; }
.kpi-yellow { border-top: 3px solid #fadb14; }
.kpi-purple { border-top: 3px solid #a855f7; }
.kpi-cyan { border-top: 3px solid #00d4ff; }

.kpi-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--shm-text-secondary);
  font-weight: 600;
}

.kpi-main {
  margin: 8px 0 6px;
  display: flex;
  align-items: baseline;
  gap: 6px;
}

.big-num {
  font-size: 26px;
  font-weight: 800;
  color: var(--shm-text-primary);
}

.red-text { color: var(--shm-red); }
.orange-text { color: var(--shm-orange); }
.yellow-text { color: var(--shm-yellow); }
.purple-text { color: #c084fc; }

.watermark-kpi {
  font-size: 16px;
  font-weight: 700;
  color: #00d4ff;
}

.sub-unit {
  font-size: 12px;
  color: var(--shm-text-muted);
}

.kpi-foot {
  font-size: 11px;
  color: var(--shm-text-secondary);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.badge-dot.green { color: #00e699; }
.badge-dot.muted { color: #94a9c9; }

/* Replay Control Bar */
.replay-control-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px 16px;
  flex-wrap: wrap;
  gap: 10px;
  background: linear-gradient(90deg, rgba(17, 29, 53, 0.95) 0%, rgba(23, 38, 70, 0.95) 100%);
}

.replay-left,
.replay-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.bar-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 700;
  color: #00d4ff;
  font-size: 13px;
}

.step-feedback-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-radius: 6px;
  background: rgba(0, 230, 153, 0.1);
  border: 1px solid rgba(0, 230, 153, 0.35);
  color: #6ee7b7;
  font-size: 12px;
}

/* Type strip */
.type-strip {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.type-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 11px;
  border-radius: 6px;
  background: var(--shm-bg-card);
  border: 1px solid var(--shm-border);
  font-size: 12px;
  color: var(--shm-text-secondary);
  cursor: pointer;
  transition: all 0.18s ease;
}

.type-chip:hover,
.type-chip.active {
  border-color: var(--shm-accent);
  color: var(--shm-text-primary);
  background: rgba(0, 212, 255, 0.12);
}

.chip-cnt {
  background: rgba(255, 255, 255, 0.08);
  padding: 1px 6px;
  border-radius: 10px;
  font-size: 11px;
  color: #38bdf8;
}

/* Table toolbar */
.filter-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
}

.filter-inputs {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.sensor-col {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.st-badge {
  background: rgba(59, 130, 246, 0.2);
  color: #60a5fa;
  border: 1px solid rgba(59, 130, 246, 0.4);
  font-size: 11px;
  padding: 0 5px;
  border-radius: 3px;
}

.muted-code {
  color: var(--shm-text-secondary);
  font-size: 12px;
}

.val-col {
  font-size: 12px;
  line-height: 1.35;
}

.time-col {
  font-size: 11px;
  line-height: 1.35;
}

.muted-time {
  color: var(--shm-text-secondary);
}

.pagination-bar {
  margin-top: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}

.total-hint {
  font-size: 12px;
  color: var(--shm-text-secondary);
}

@media (max-width: 1440px) {
  .kpi-grid-6 {
    grid-template-columns: repeat(3, 1fr);
  }
}

@media (max-width: 1100px) {
  .kpi-grid-6 {
    grid-template-columns: repeat(2, 1fr);
  }

  .replay-control-bar {
    align-items: flex-start;
    flex-direction: column;
  }

  .replay-left,
  .replay-right {
    width: 100%;
  }
}

@media (max-width: 720px) {
  .alert-center-view {
    gap: 10px;
  }

  .kpi-grid-6 {
    grid-template-columns: 1fr;
    gap: 10px;
  }

  .kpi-card {
    min-height: 96px;
  }

  .kpi-top {
    align-items: flex-start;
    gap: 8px;
  }

  .kpi-foot {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }

  .replay-left,
  .replay-right {
    align-items: stretch;
    flex-direction: column;
  }

  .replay-left :deep(.el-select),
  .replay-right :deep(.el-button),
  .replay-right :deep(.el-dropdown) {
    width: 100% !important;
  }

  .type-strip {
    flex-wrap: nowrap;
    overflow-x: auto;
    padding-bottom: 4px;
  }

  .type-chip {
    flex: none;
  }

  .filter-toolbar,
  .filter-inputs {
    align-items: stretch;
    flex-direction: column;
  }

  .filter-inputs :deep(.el-select),
  .filter-inputs :deep(.el-input) {
    width: 100% !important;
  }
}
</style>
