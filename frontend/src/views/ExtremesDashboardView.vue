<template>
  <div class="extremes-dashboard-view">
    <!-- 1. Top Control & Filter Bar -->
    <div class="shm-card control-bar">
      <div class="ctrl-left">
        <div class="ctrl-group">
          <span class="ctrl-label">所属桥梁站点</span>
          <el-radio-group v-model="stationFilter" size="small" @change="onStationChange">
            <el-radio-button value="ALL">全部站点</el-radio-button>
            <el-radio-button value="玉环站">玉环站</el-radio-button>
            <el-radio-button value="淮北西站">淮北西站</el-radio-button>
          </el-radio-group>
        </div>

        <div class="ctrl-group">
          <span class="ctrl-label">传感器与监测通道</span>
          <el-select
            v-model="selectedSensorKey"
            filterable
            size="small"
            style="width: 310px"
            placeholder="选择传感器及通道"
            @change="onSensorSelectChange"
          >
            <el-option
              v-for="s in filteredSensorOptions"
              :key="s.key"
              :label="`[${s.stationName}] ${s.sensorName} (${s.sensorNo}${s.channel ? '/' + s.channel : ''}) · ${s.physicalQuantity}`"
              :value="s.key"
            />
          </el-select>
        </div>

        <div class="ctrl-group">
          <span class="ctrl-label">统计周期粒度</span>
          <el-radio-group v-model="periodType" size="small" @change="fetchExtremesSeries">
            <el-radio-button value="1m">1m (每分钟极值)</el-radio-button>
            <el-radio-button value="1h">1h (每小时极值)</el-radio-button>
            <el-radio-button value="1d">1d (每日极值)</el-radio-button>
          </el-radio-group>
        </div>

        <div class="ctrl-group">
          <span class="ctrl-label">时间范围预设</span>
          <el-select
            v-model="selectedTimePreset"
            size="small"
            style="width: 245px"
            @change="onTimePresetChange"
          >
            <el-option label="全量归档区间 (2026-09-21 ~ 09-30)" value="ALL" />
            <el-option label="吞吐峰值日 (2026-09-28 00:00 ~ 23:59)" value="2026-09-28" />
            <el-option label="峰值核心段 (2026-09-28 12:00 ~ 16:00)" value="2026-09-28-PEAK" />
          </el-select>
        </div>
      </div>

      <div class="ctrl-right">
        <el-checkbox v-model="showThresholdLines" size="small" @change="renderEnvelopeChart">
          叠加 L1/L2/L3 三级阈值参考线
        </el-checkbox>
        <el-button type="primary" size="small" :loading="loading" @click="fetchExtremesSeries">
          <el-icon style="margin-right: 4px"><Refresh /></el-icon>刷新极值曲线
        </el-button>
      </div>
    </div>

    <!-- 2. Active Sensor Summary KPI Cards -->
    <div class="extremes-kpi-row">
      <div class="shm-card ext-kpi-card max-card">
        <div class="ext-kpi-head">
          <span>📈 窗口最大极值 (val_max)</span>
          <el-tag size="small" type="danger" effect="plain">波峰包络上界</el-tag>
        </div>
        <div class="ext-kpi-val mono-num red-val">
          {{ formatNum(extremeSummary.windowMax) }}
          <small>{{ activeMeta.unit }}</small>
        </div>
        <div class="ext-kpi-sub mono-num">
          发生时刻 max_time: {{ extremeSummary.windowMaxTime || '-' }}
        </div>
      </div>

      <div class="shm-card ext-kpi-card min-card">
        <div class="ext-kpi-head">
          <span>📉 窗口最小极值 (val_min)</span>
          <el-tag size="small" type="primary" effect="plain">波谷包络下界</el-tag>
        </div>
        <div class="ext-kpi-val mono-num cyan-val">
          {{ formatNum(extremeSummary.windowMin) }}
          <small>{{ activeMeta.unit }}</small>
        </div>
        <div class="ext-kpi-sub mono-num">
          发生时刻 min_time: {{ extremeSummary.windowMinTime || '-' }}
        </div>
      </div>

      <div class="shm-card ext-kpi-card avg-card">
        <div class="ext-kpi-head">
          <span>⚖️ 窗口算术均值 (val_avg)</span>
          <el-tag size="small" type="success" effect="plain">基线趋势</el-tag>
        </div>
        <div class="ext-kpi-val mono-num green-val">
          {{ formatNum(extremeSummary.windowAvg) }}
          <small>{{ activeMeta.unit }}</small>
        </div>
        <div class="ext-kpi-sub mono-num">
          统计桶数: {{ extremeSummary.bucketCount || 0 }} 个 ({{ periodType }}) · 样本 {{ extremeSummary.totalSamples || 0 }}
        </div>
      </div>

      <div class="shm-card ext-kpi-card std-card">
        <div class="ext-kpi-head">
          <span>↕ 窗口极差 (Max-Min)</span>
          <el-tag size="small" type="warning" effect="plain">波动范围</el-tag>
        </div>
        <div class="ext-kpi-val mono-num orange-val">
          {{ formatNum(windowSpan) }}
          <small>{{ activeMeta.unit }}</small>
        </div>
        <div class="ext-kpi-sub mono-num">
          统计桶数: {{ extremeSummary.bucketCount || 0 }} · 样本 {{ extremeSummary.totalSamples || 0 }}
        </div>
      </div>

    </div>

    <!-- 3. Interactive ECharts Max/Min/Avg Envelope Chart + Threshold Overlay -->
    <div class="shm-card chart-card">
      <div class="shm-card-header">
        <div class="shm-card-title">
          <span>
            {{ activeMeta.stationName }} · {{ activeMeta.sensorName }} ({{ activeMeta.sensorNo }}{{ activeMeta.channel ? '/' + activeMeta.channel : '' }})
            — 定时最大值(val_max) / 最小值(val_min) / 均值(val_avg) 包络带与三级阈值线
          </span>
        </div>
        <div class="chart-legend-tags">
          <span class="legend-chip l1">L1红线: {{ activeThresholds.level1Lower ?? '-' }} ~ {{ activeThresholds.level1Upper ?? '-' }}</span>
          <span class="legend-chip l2">L2橙线: {{ activeThresholds.level2Lower ?? '-' }} ~ {{ activeThresholds.level2Upper ?? '-' }}</span>
          <span class="legend-chip l3">L3黄线: {{ activeThresholds.level3Lower ?? '-' }} ~ {{ activeThresholds.level3Upper ?? '-' }}</span>
        </div>
      </div>

      <div ref="envelopeChartRef" v-loading="loading" class="envelope-chart-box"></div>
    </div>

    <!-- 4. All Sensors Latest Scheduled Max/Min Overview Grid & Table -->
    <div class="shm-card">
      <div class="shm-card-header">
        <div class="shm-card-title">
          <span>全桥传感器最新定时极值总览卡片与明细表 (点击任意行切换上方包络曲线)</span>
        </div>
        <div class="header-search">
          <el-input
            v-model="tableKeyword"
            size="small"
            clearable
            placeholder="快速筛选传感器编号/名称/物理量..."
            style="width: 240px"
          />
        </div>
      </div>

      <el-table
        :data="displayedSensorTable"
        border
        stripe
        size="small"
        highlight-current-row
        max-height="360"
        @row-click="handleSelectSensorRow"
      >
        <el-table-column prop="stationName" label="站点" width="95" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.stationName === '玉环站' ? 'primary' : 'success'" effect="plain">
              {{ row.stationName }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="传感器名称与编号" min-width="200">
          <template #default="{ row }">
            <strong>{{ row.sensorName }}</strong>
            <span class="mono-num muted-txt"> ({{ row.sensorNo }}{{ row.channel ? '/' + row.channel : '' }})</span>
          </template>
        </el-table-column>

        <el-table-column prop="physicalQuantity" label="物理量" width="120" align="center" />

        <el-table-column label="最新最大值 (val_max) & 发生时刻" min-width="210" align="right">
          <template #default="{ row }">
            <div class="mono-num">
              <strong style="color: var(--shm-red)">{{ formatNum(row.valMax) }} {{ row.unit }}</strong>
              <div class="muted-txt">{{ row.maxTime || row.latestStatTime }}</div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="最新最小值 (val_min) & 发生时刻" min-width="210" align="right">
          <template #default="{ row }">
            <div class="mono-num">
              <strong style="color: var(--shm-accent)">{{ formatNum(row.valMin) }} {{ row.unit }}</strong>
              <div class="muted-txt">{{ row.minTime || row.latestStatTime }}</div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="均值 (val_avg)" width="130" align="right">
          <template #default="{ row }">
            <span class="mono-num">{{ formatNum(row.valAvg) }} {{ row.unit }}</span>
          </template>
        </el-table-column>

        <el-table-column label="三级上限 (L3/L2/L1)" width="175" align="center">
          <template #default="{ row }">
            <span class="mono-num" style="font-size: 11px">
              {{ row.level3Upper ?? '-' }} / {{ row.level2Upper ?? '-' }} / {{ row.level1Upper ?? '-' }}
            </span>
          </template>
        </el-table-column>

        <el-table-column prop="sampleCount" label="窗口样本数" width="100" align="center">
          <template #default="{ row }">
            <span class="mono-num">{{ row.sampleCount }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="100" align="center" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click.stop="handleSelectSensorRow(row)">
              查看包络图
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { getStatSensorsApi, getStatExtremesApi } from '../api'

const loading = ref(false)
const stationFilter = ref('ALL')
const selectedSensorKey = ref('stn-12-b:stress')
const periodType = ref('1m')
const selectedTimePreset = ref('ALL')
const showThresholdLines = ref(true)
const tableKeyword = ref('')

const sensorOptions = ref([])
const seriesData = ref([])

const activeMeta = reactive({
  sensorNo: 'stn-12-b',
  sensorName: '光纤光栅应变计_25_YH',
  stationName: '玉环站',
  channel: 'stress',
  physicalQuantity: '应力',
  unit: 'MPa'
})

const activeThresholds = reactive({
  level1Upper: null,
  level1Lower: null,
  level2Upper: null,
  level2Lower: null,
  level3Upper: null,
  level3Lower: null
})

const extremeSummary = reactive({
  bucketCount: 0,
  windowMax: null,
  windowMaxTime: '',
  windowMin: null,
  windowMinTime: '',
  windowAvg: null,
  totalSamples: 0
})

const envelopeChartRef = ref(null)
let chartInstance = null

const filteredSensorOptions = computed(() => {
  if (stationFilter.value === 'ALL') return sensorOptions.value
  return sensorOptions.value.filter((s) => s.stationName === stationFilter.value)
})

const displayedSensorTable = computed(() => {
  const list = filteredSensorOptions.value
  if (!tableKeyword.value.trim()) return list
  const kw = tableKeyword.value.trim().toLowerCase()
  return list.filter(
    (r) =>
      (r.sensorNo || '').toLowerCase().includes(kw) ||
      (r.sensorName || '').toLowerCase().includes(kw) ||
      (r.physicalQuantity || '').toLowerCase().includes(kw) ||
      (r.channel || '').toLowerCase().includes(kw)
  )
})

const windowSpan = computed(() => {
  if (extremeSummary.windowMax === null || extremeSummary.windowMin === null) return 0
  return Number(extremeSummary.windowMax) - Number(extremeSummary.windowMin)
})

async function loadSensorsList() {
  try {
    const res = await getStatSensorsApi()
    sensorOptions.value = Array.isArray(res.items) ? res.items : []
    if (sensorOptions.value.length > 0) {
      const found = sensorOptions.value.find((s) => s.key === selectedSensorKey.value)
      if (!found) {
        selectedSensorKey.value = sensorOptions.value[0].key
      }
    }
  } catch (err) {
    ElMessage.error(`加载极值传感器目录失败: ${err?.message || ''}`)
  }
}

function onStationChange() {
  const list = filteredSensorOptions.value
  if (list.length > 0 && !list.some((s) => s.key === selectedSensorKey.value)) {
    selectedSensorKey.value = list[0].key
    fetchExtremesSeries()
  }
}

function onSensorSelectChange() {
  fetchExtremesSeries()
}

function onTimePresetChange() {
  fetchExtremesSeries()
}

function handleSelectSensorRow(row) {
  selectedSensorKey.value = row.key
  fetchExtremesSeries()
}

function resolveTimeRangeParams() {
  if (selectedTimePreset.value === '2026-09-28') {
    return { startTime: '2026-09-28 00:00:00', endTime: '2026-09-28 23:59:59' }
  }
  if (selectedTimePreset.value === '2026-09-28-PEAK') {
    return { startTime: '2026-09-28 12:00:00', endTime: '2026-09-28 16:00:00' }
  }
  return {}
}

async function fetchExtremesSeries() {
  loading.value = true
  try {
    const [sensorNo, channel = ''] = (selectedSensorKey.value || 'stn-12-b:stress').split(':')
    const timeParams = resolveTimeRangeParams()
    const res = await getStatExtremesApi({
      sensorNo,
      channel,
      periodType: periodType.value,
      ...timeParams,
      limit: 600
    })

    activeMeta.sensorNo = res.sensorNo || sensorNo
    activeMeta.sensorName = res.sensorName || sensorNo
    activeMeta.stationName = res.stationName || '玉环站'
    activeMeta.channel = res.channel ?? channel
    activeMeta.physicalQuantity = res.physicalQuantity || '监测值'
    activeMeta.unit = res.unit || ''

    Object.assign(activeThresholds, res.thresholds || {})
    Object.assign(extremeSummary, res.summary || {})
    seriesData.value = Array.isArray(res.series) ? res.series : []

    await nextTick()
    renderEnvelopeChart()
  } catch (err) {
    ElMessage.error(`加载定时最大最小值曲线失败: ${err?.message || ''}`)
  } finally {
    loading.value = false
  }
}

function renderEnvelopeChart() {
  if (!envelopeChartRef.value) return
  if (!chartInstance) {
    chartInstance = echarts.init(envelopeChartRef.value)
  }

  const rows = seriesData.value
  const xTimes = rows.map((r) => r.statTime)
  const maxArr = rows.map((r) => r.valMax)
  const minArr = rows.map((r) => r.valMin)
  const avgArr = rows.map((r) => r.valAvg)
  const bandDiffArr = rows.map((r) => Number((r.valMax - r.valMin).toFixed(5)))

  const markLineData = []
  if (showThresholdLines.value) {
    if (activeThresholds.level1Upper !== null && activeThresholds.level1Upper !== undefined) {
      markLineData.push({
        yAxis: Number(activeThresholds.level1Upper),
        name: '一级上限(红)',
        lineStyle: { color: '#ff3b5c', width: 2, type: 'solid' },
        label: { formatter: 'L1上限: {c}', color: '#ff3b5c' }
      })
    }
    if (activeThresholds.level1Lower !== null && activeThresholds.level1Lower !== undefined) {
      markLineData.push({
        yAxis: Number(activeThresholds.level1Lower),
        name: '一级下限(红)',
        lineStyle: { color: '#ff3b5c', width: 2, type: 'solid' },
        label: { formatter: 'L1下限: {c}', color: '#ff3b5c' }
      })
    }
    if (activeThresholds.level2Upper !== null && activeThresholds.level2Upper !== undefined) {
      markLineData.push({
        yAxis: Number(activeThresholds.level2Upper),
        name: '二级上限(橙)',
        lineStyle: { color: '#ff8f1f', width: 1.5, type: 'dashed' },
        label: { formatter: 'L2上限: {c}', color: '#ff8f1f' }
      })
    }
    if (activeThresholds.level2Lower !== null && activeThresholds.level2Lower !== undefined) {
      markLineData.push({
        yAxis: Number(activeThresholds.level2Lower),
        name: '二级下限(橙)',
        lineStyle: { color: '#ff8f1f', width: 1.5, type: 'dashed' },
        label: { formatter: 'L2下限: {c}', color: '#ff8f1f' }
      })
    }
    if (activeThresholds.level3Upper !== null && activeThresholds.level3Upper !== undefined) {
      markLineData.push({
        yAxis: Number(activeThresholds.level3Upper),
        name: '三级上限(黄)',
        lineStyle: { color: '#fadb14', width: 1.2, type: 'dotted' },
        label: { formatter: 'L3上限: {c}', color: '#fadb14' }
      })
    }
    if (activeThresholds.level3Lower !== null && activeThresholds.level3Lower !== undefined) {
      markLineData.push({
        yAxis: Number(activeThresholds.level3Lower),
        name: '三级下限(黄)',
        lineStyle: { color: '#fadb14', width: 1.2, type: 'dotted' },
        label: { formatter: 'L3下限: {c}', color: '#fadb14' }
      })
    }
  }

  const unit = activeMeta.unit || ''

  const option = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(11, 20, 38, 0.95)',
      borderColor: '#2b4c87',
      textStyle: { color: '#e8f1ff', fontSize: 12 },
      formatter: (params) => {
        if (!params || !params.length) return ''
        const idx = params[0].dataIndex
        const item = rows[idx]
        if (!item) return ''
        return `
          <div style="font-weight:700;color:#00d4ff;margin-bottom:6px;">
            ⏱️ 统计窗口: ${item.statTime} (${periodType.value})
          </div>
          <div style="display:flex;justify-content:space-between;gap:16px;">
            <span style="color:#ff3b5c;">▲ 最大值 (val_max):</span>
            <strong>${item.valMax} ${unit}</strong>
          </div>
          <div style="font-size:11px;color:#94a9c9;margin-bottom:4px;">
            ↳ 极值时刻 max_time: ${item.maxTime}
          </div>
          <div style="display:flex;justify-content:space-between;gap:16px;">
            <span style="color:#00e699;">● 窗口均值 (val_avg):</span>
            <strong>${item.valAvg} ${unit}</strong>
          </div>
          <div style="display:flex;justify-content:space-between;gap:16px;margin-top:4px;">
            <span style="color:#38bdf8;">▼ 最小值 (val_min):</span>
            <strong>${item.valMin} ${unit}</strong>
          </div>
          <div style="font-size:11px;color:#94a9c9;margin-bottom:4px;">
            ↳ 极值时刻 min_time: ${item.minTime}
          </div>
          <div style="border-top:1px solid #264378;margin-top:4px;padding-top:4px;font-size:11px;color:#cbd5e1;">
            样本数: ${item.sampleCount}
          </div>
        `
      }
    },
    legend: {
      top: 6,
      textStyle: { color: '#cbd5e1' },
      data: ['最大值曲线 (val_max)', '均值曲线 (val_avg)', '最小值曲线 (val_min)']
    },
    grid: {
      left: 65,
      right: 95,
      top: 46,
      bottom: 58
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      {
        type: 'slider',
        height: 20,
        bottom: 12,
        borderColor: '#1e335c',
        textStyle: { color: '#94a9c9' }
      }
    ],
    xAxis: {
      type: 'category',
      data: xTimes,
      axisLine: { lineStyle: { color: '#2b4c87' } },
      axisLabel: { color: '#94a9c9', fontSize: 11 }
    },
    yAxis: {
      type: 'value',
      name: `${activeMeta.physicalQuantity} (${unit})`,
      scale: true,
      nameTextStyle: { color: '#94a9c9' },
      axisLine: { lineStyle: { color: '#2b4c87' } },
      axisLabel: { color: '#94a9c9' },
      splitLine: { lineStyle: { color: 'rgba(43, 76, 135, 0.28)', type: 'dashed' } }
    },
    series: [
      // Base invisible stack for shaded Min-Max envelope band
      {
        name: '包络基线',
        type: 'line',
        data: minArr,
        stack: 'envelope-band',
        symbol: 'none',
        lineStyle: { opacity: 0 },
        tooltip: { show: false }
      },
      {
        name: '极值包络带',
        type: 'line',
        data: bandDiffArr,
        stack: 'envelope-band',
        symbol: 'none',
        lineStyle: { opacity: 0 },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(255, 59, 92, 0.22)' },
            { offset: 1, color: 'rgba(56, 189, 248, 0.18)' }
          ])
        },
        tooltip: { show: false }
      },
      {
        name: '最大值曲线 (val_max)',
        type: 'line',
        data: maxArr,
        showSymbol: rows.length < 80,
        symbolSize: 5,
        lineStyle: { color: '#ff3b5c', width: 2 },
        itemStyle: { color: '#ff3b5c' },
        markLine: {
          symbol: ['none', 'none'],
          silent: true,
          data: markLineData
        }
      },
      {
        name: '均值曲线 (val_avg)',
        type: 'line',
        data: avgArr,
        showSymbol: false,
        lineStyle: { color: '#00e699', width: 2, type: 'solid' },
        itemStyle: { color: '#00e699' }
      },
      {
        name: '最小值曲线 (val_min)',
        type: 'line',
        data: minArr,
        showSymbol: rows.length < 80,
        symbolSize: 5,
        lineStyle: { color: '#38bdf8', width: 2 },
        itemStyle: { color: '#38bdf8' }
      }
    ]
  }

  chartInstance.setOption(option, true)
}

function formatNum(val) {
  if (val === null || val === undefined || val === '') return '-'
  const n = Number(val)
  if (!Number.isFinite(n)) return String(val)
  return Math.abs(n) >= 100 ? n.toFixed(2) : n.toFixed(4).replace(/\.?0+$/, '')
}

function handleResize() {
  if (chartInstance) chartInstance.resize()
}

onMounted(async () => {
  window.addEventListener('resize', handleResize)
  await loadSensorsList()
  await fetchExtremesSeries()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  if (chartInstance) {
    chartInstance.dispose()
    chartInstance = null
  }
})
</script>

<style scoped>
.extremes-dashboard-view {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.control-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  padding: 12px 16px;
}

.ctrl-left,
.ctrl-right {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.ctrl-group {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ctrl-label {
  font-size: 12px;
  color: var(--shm-text-secondary);
  font-weight: 600;
}

.extremes-kpi-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}

.ext-kpi-card {
  padding: 13px 15px;
}

.max-card { border-top: 3px solid #ff3b5c; }
.min-card { border-top: 3px solid #38bdf8; }
.avg-card { border-top: 3px solid #00e699; }
.std-card { border-top: 3px solid #ff8f1f; }
.ext-kpi-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--shm-text-secondary);
  font-weight: 600;
}

.ext-kpi-val {
  font-size: 24px;
  font-weight: 800;
  margin: 8px 0 4px;
}

.ext-kpi-val small {
  font-size: 13px;
  font-weight: 500;
  margin-left: 4px;
  color: var(--shm-text-secondary);
}

.red-val { color: #ff3b5c; }
.cyan-val { color: #38bdf8; }
.green-val { color: #00e699; }
.orange-val { color: #ff8f1f; }

.ext-kpi-sub {
  font-size: 11px;
  color: var(--shm-text-secondary);
}

.chart-legend-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.legend-chip {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  font-family: 'JetBrains Mono', monospace;
}

.legend-chip.l1 {
  background: rgba(255, 59, 92, 0.14);
  color: #ff3b5c;
  border: 1px solid rgba(255, 59, 92, 0.4);
}

.legend-chip.l2 {
  background: rgba(255, 143, 31, 0.14);
  color: #ff8f1f;
  border: 1px solid rgba(255, 143, 31, 0.4);
}

.legend-chip.l3 {
  background: rgba(250, 219, 20, 0.14);
  color: #fadb14;
  border: 1px solid rgba(250, 219, 20, 0.4);
}

.envelope-chart-box {
  width: 100%;
  height: 410px;
}

.muted-txt {
  font-size: 11px;
  color: var(--shm-text-secondary);
}

@media (max-width: 1400px) {
  .extremes-kpi-row {
    grid-template-columns: repeat(3, 1fr);
  }
}

@media (max-width: 1100px) {
  .control-bar,
  .ctrl-left,
  .ctrl-right {
    align-items: flex-start;
  }

  .control-bar,
  .ctrl-left {
    flex-direction: column;
  }

  .ctrl-left,
  .ctrl-right {
    width: 100%;
  }

  .extremes-kpi-row {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 720px) {
  .extremes-dashboard-view {
    gap: 10px;
  }

  .ctrl-group {
    align-items: flex-start;
    flex-direction: column;
    width: 100%;
  }

  .ctrl-group :deep(.el-select),
  .ctrl-group :deep(.el-radio-group) {
    width: 100% !important;
  }

  .extremes-kpi-row {
    grid-template-columns: 1fr;
    gap: 10px;
  }

  .chart-legend-tags {
    margin-top: 8px;
  }

  .shm-card-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .envelope-chart-box {
    height: 330px;
  }
}
</style>
