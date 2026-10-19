<template>
  <div class="wind-rose-view">
    <!-- 1. Top Control Bar -->
    <div class="shm-card wind-ctrl-bar">
      <div class="ctrl-left">
        <div class="ctrl-item">
          <span class="ctrl-label">测风传感器选择</span>
          <el-radio-group v-model="selectedSensor" size="small" @change="fetchWindRoseData">
            <el-radio-button value="wea-01">wea-01 (玉环站小型气象站)</el-radio-button>
            <el-radio-button value="fhj-01">fhj-01 (淮北西站超声风速仪)</el-radio-button>
            <el-radio-button value="all">双站联合风场 (all)</el-radio-button>
          </el-radio-group>
        </div>

        <div class="ctrl-item">
          <span class="ctrl-label">风况数据源模式</span>
          <el-radio-group v-model="includeDemoTyphoon" size="small" @change="fetchWindRoseData">
            <el-radio-button :value="false">【实测原始数据 (窄幅测试风况)】</el-radio-button>
            <el-radio-button :value="true">【标准台风季多扇区演示风况 (对照验证)】</el-radio-button>
          </el-radio-group>
        </div>

        <div class="ctrl-item">
          <span class="ctrl-label">统计时段</span>
          <el-select v-model="timePreset" size="small" style="width: 220px" @change="fetchWindRoseData">
            <el-option label="全量观测时段 (09-24 ~ 09-30)" value="ALL" />
            <el-option label="强风核心时段 (09-28 ~ 09-30)" value="PEAK" />
            <el-option label="9月28日单日风场 (09-28)" value="DAY28" />
          </el-select>
        </div>
      </div>

      <div class="ctrl-right">
        <el-button type="primary" size="small" :loading="loading" @click="fetchWindRoseData">
          <el-icon style="margin-right: 4px"><Refresh /></el-icon>重新合成风玫瑰图
        </el-button>
      </div>
    </div>

    <!-- Engineering Explanation Banner for Raw Test Data vs Typhoon Multi-Sector Mode -->
    <div class="engineering-note-banner">
      <div class="note-title">
        🔬 桥梁抗风工程说明 (GB/T 35227-2017 10分钟矢量合成法 · 正交分解 u = -v·sinθ, v = -v·cosθ)：
      </div>
      <div class="note-desc">
        在 <strong>【实测原始数据 (窄幅测试风况)】</strong> 模式下，数据库 `t_monitor` 实测风向主要集中在
        <code>fhj-01: 10°~20° (NNE 东北偏北扇区)</code> 与 <code>wea-01: 10°~97° (NNE~E 偏东扇区)</code>；
        切换至 <strong>【标准台风季多扇区演示风况 (对照验证)】</strong> 可检验极坐标 16 方位（`startAngle: 101.25°` 使正北 `N` 严格居顶居中）与蒲福 7 级风速色阶的完整渲染效果。
      </div>
    </div>

    <!-- 2. Five Key Wind Field Metrics Cards -->
    <div class="wind-kpi-grid">
      <div class="shm-card wind-kpi-card">
        <div class="wk-label">🌪️ 10min 最大阵风极值 (Max Wind)</div>
        <div class="wk-val mono-num" style="color: #ff3b5c">
          {{ summary.maxWindSpeed ?? 0 }} <small>m/s</small>
        </div>
        <div class="wk-sub mono-num">发生时刻: {{ summary.maxWindTime || '-' }}</div>
      </div>

      <div class="shm-card wind-kpi-card">
        <div class="wk-label">🧭 主导风向扇区与频率 (Dominant)</div>
        <div class="wk-val mono-num" style="color: #00d4ff">
          {{ summary.dominantSectorCn || '东北 (NE)' }}
        </div>
        <div class="wk-sub mono-num">
          扇区占比: <strong>{{ summary.dominantSectorPct ?? 0 }}%</strong> (中心角 {{ summary.dominantDirectionDeg ?? 45 }}°)
        </div>
      </div>

      <div class="shm-card wind-kpi-card">
        <div class="wk-label">🍃 静风频率 (≤0.2 m/s · GB/T 35227-2017)</div>
        <div class="wk-val mono-num" style="color: #00e699">
          {{ summary.calmRatePct ?? 0 }} <small>%</small>
        </div>
        <div class="wk-sub mono-num">
          静风时窗数: {{ summary.calmCount ?? 0 }} / {{ summary.totalBuckets ?? 0 }} 个10min桶
        </div>
      </div>

      <div class="shm-card wind-kpi-card">
        <div class="wk-label">🌬️ 小风频率 (≤0.5 m/s · HJ 2.2-2018)</div>
        <div class="wk-val mono-num" style="color: #fadb14">
          {{ summary.smallWindRatePct ?? 0 }} <small>%</small>
        </div>
        <div class="wk-sub mono-num">
          实测原始桶数: {{ summary.rawDbBuckets ?? 0 }} · 总样本 {{ summary.totalSamples ?? 0 }}
        </div>
      </div>

      <div class="shm-card wind-kpi-card">
        <div class="wk-label">📐 矢量均风速 / 标量均风速 / 湍流度</div>
        <div class="wk-val mono-num" style="color: #c084fc">
          {{ summary.avgVectorSpeed ?? 0 }} / {{ summary.avgScalarSpeed ?? 0 }} <small>m/s</small>
        </div>
        <div class="wk-sub mono-num">
          平均湍流强度 Iu (σ/V): {{ summary.turbulenceIntensityAvg ?? 0 }}
        </div>
      </div>
    </div>

    <!-- 3. Main Charts Split: Left 16-Direction Polar Wind Rose + Right 10-Min Vector Wind Speed/Direction Time-Series -->
    <div class="wind-charts-grid">
      <!-- Left: 16-Direction Polar Stacked Wind Rose -->
      <div class="shm-card polar-card">
        <div class="shm-card-header">
          <div class="shm-card-title">
            <span>16方位 7级蒲福风速极坐标堆叠风玫瑰图 (startAngle: 101.25° · 正北N严格居顶0°)</span>
          </div>
          <el-tag size="small" :type="includeDemoTyphoon ? 'warning' : 'primary'" effect="dark">
            {{ includeDemoTyphoon ? '台风季多扇区对照模式' : '数据库实测原始风场' }}
          </el-tag>
        </div>
        <div ref="polarChartRef" v-loading="loading" class="polar-chart-box"></div>
      </div>

      <!-- Right: 10-Minute Vector Wind Speed & Direction Time-Series + Sector Distribution Table -->
      <div class="right-column-stack">
        <div class="shm-card timeseries-card">
          <div class="shm-card-header">
            <div class="shm-card-title">
              <span>10分钟矢量合成风速 (Vector) vs 阵风极值 (Gust) 与风向角时程图</span>
            </div>
          </div>
          <div ref="windTimeChartRef" v-loading="loading" class="wind-time-chart-box"></div>
        </div>

        <div class="shm-card sector-table-card">
          <div class="shm-card-header">
            <div class="shm-card-title">
              <span>16方位扇区风频统计明细表 (GB/T 35227-2017)</span>
            </div>
          </div>
          <el-table :data="sectorRows" border stripe size="small" max-height="220">
            <el-table-column prop="code" label="方位代号" width="85" align="center">
              <template #default="{ row }">
                <strong class="mono-num" style="color: var(--shm-accent)">{{ row.code }}</strong>
              </template>
            </el-table-column>
            <el-table-column prop="label" label="中文方位" width="135" />
            <el-table-column prop="angleDeg" label="中心方位角" width="100" align="center">
              <template #default="{ row }">
                <span class="mono-num">{{ row.angleDeg }}°</span>
              </template>
            </el-table-column>
            <el-table-column prop="totalCount" label="10min样本桶数" width="120" align="right">
              <template #default="{ row }">
                <span class="mono-num">{{ row.totalCount }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="totalPct" label="扇区总频率 (%)" min-width="150">
              <template #default="{ row }">
                <div class="pct-bar-cell">
                  <el-progress
                    :percentage="Math.min(100, Number(row.totalPct || 0))"
                    :stroke-width="10"
                    :color="row.totalPct > 15 ? '#ff3b5c' : '#00d4ff'"
                  />
                </div>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onBeforeUnmount, nextTick } from 'vue'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { getWindRoseApi } from '../api'

const loading = ref(false)
const selectedSensor = ref('wea-01')
const includeDemoTyphoon = ref(false)
const timePreset = ref('ALL')

const directionLabels = ref([
  'N', 'NNE', 'NE', 'ENE',
  'E', 'ESE', 'SE', 'SSE',
  'S', 'SSW', 'SW', 'WSW',
  'W', 'WNW', 'NW', 'NNW'
])
const speedBins = ref([])
const matrixPct = ref([])
const sectorRows = ref([])
const recentRecords = ref([])

const summary = reactive({
  totalBuckets: 0,
  rawDbBuckets: 0,
  totalSamples: 0,
  calmCount: 0,
  calmRatePct: 0,
  smallWindRatePct: 0,
  maxWindSpeed: 0,
  maxWindTime: '',
  avgVectorSpeed: 0,
  avgScalarSpeed: 0,
  dominantSector: 'NE',
  dominantSectorCn: '东北 (NE)',
  dominantDirectionDeg: 45,
  dominantSectorPct: 0,
  turbulenceIntensityAvg: 0
})

const polarChartRef = ref(null)
const windTimeChartRef = ref(null)
let polarChart = null
let timeChart = null

const BEAUFORT_COLORS = [
  '#38bdf8', // 0.2~1.5 m/s (1级软风)
  '#00e699', // 1.6~3.3 m/s (2级轻风)
  '#a3e635', // 3.4~5.4 m/s (3级微风)
  '#facc15', // 5.5~7.9 m/s (4级和风)
  '#fb923c', // 8.0~10.7 m/s (5级清劲风)
  '#f43f5e', // 10.8~13.8 m/s (6级强风)
  '#a855f7'  // >=13.9 m/s (7级及以上疾风/台风)
]

function resolveTimeParams() {
  if (timePreset.value === 'PEAK') {
    return { startTime: '2026-09-28 00:00:00', endTime: '2026-09-30 23:59:59' }
  }
  if (timePreset.value === 'DAY28') {
    return { startTime: '2026-09-28 00:00:00', endTime: '2026-09-28 23:59:59' }
  }
  return {}
}

async function fetchWindRoseData() {
  loading.value = true
  try {
    const res = await getWindRoseApi({
      sensorNo: selectedSensor.value,
      includeDemoTyphoon: includeDemoTyphoon.value,
      ...resolveTimeParams()
    })

    if (Array.isArray(res.directionLabels) && res.directionLabels.length === 16) {
      directionLabels.value = res.directionLabels
    }
    speedBins.value = Array.isArray(res.speedBins) ? res.speedBins : []
    matrixPct.value = Array.isArray(res.matrixPct) ? res.matrixPct : []
    sectorRows.value = Array.isArray(res.sectors) ? res.sectors : []
    recentRecords.value = Array.isArray(res.recentRecords) ? [...res.recentRecords].reverse() : []

    Object.assign(summary, res.summary || {})

    await nextTick()
    renderPolarWindRose()
    renderWindTimeSeries()
  } catch (err) {
    ElMessage.error(`加载桥梁风玫瑰数据失败: ${err?.message || ''}`)
  } finally {
    loading.value = false
  }
}

function renderPolarWindRose() {
  if (!polarChartRef.value) return
  if (!polarChart) {
    polarChart = echarts.init(polarChartRef.value)
  }

  const dirs = directionLabels.value
  const bins = speedBins.value.length
    ? speedBins.value
    : [
        { index: 0, label: '0.2~1.5 m/s (1级软风)' },
        { index: 1, label: '1.6~3.3 m/s (2级轻风)' },
        { index: 2, label: '3.4~5.4 m/s (3级微风)' },
        { index: 3, label: '5.5~7.9 m/s (4级和风)' },
        { index: 4, label: '8.0~10.7 m/s (5级清劲风)' },
        { index: 5, label: '10.8~13.8 m/s (6级强风)' },
        { index: 6, label: '≥13.9 m/s (7级+疾风)' }
      ]

  const series = bins.map((bin, bIdx) => {
    const dataPerSector = dirs.map((_, sIdx) => {
      const row = matrixPct.value[sIdx]
      return row && row[bIdx] !== undefined ? Number(row[bIdx]) : 0
    })
    return {
      type: 'bar',
      name: bin.label,
      data: dataPerSector,
      coordinateSystem: 'polar',
      stack: 'wind-rose-stack',
      itemStyle: {
        color: BEAUFORT_COLORS[bIdx % BEAUFORT_COLORS.length],
        borderColor: 'rgba(7, 13, 25, 0.6)',
        borderWidth: 0.8
      },
      emphasis: {
        focus: 'series'
      }
    }
  })

  const option = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(11, 20, 38, 0.95)',
      borderColor: '#2b4c87',
      textStyle: { color: '#e8f1ff', fontSize: 12 },
      formatter: (params) => {
        if (!params || !params.length) return ''
        const sIdx = params[0].dataIndex
        const sec = sectorRows.value[sIdx] || {}
        let html = `<div style="font-weight:700;color:#00d4ff;margin-bottom:6px;">
          🧭 方位扇区: ${sec.label || dirs[sIdx]} (中心角 ${sec.angleDeg ?? sIdx * 22.5}°)
        </div>`
        let total = 0
        params.forEach((p) => {
          const val = Number(p.value || 0)
          total += val
          if (val > 0) {
            html += `<div style="display:flex;justify-content:space-between;gap:14px;">
              <span>${p.marker} ${p.seriesName}</span>
              <strong>${val.toFixed(2)}%</strong>
            </div>`
          }
        })
        html += `<div style="border-top:1px solid #264378;margin-top:5px;padding-top:4px;font-weight:700;color:#facc15;">
          扇区总频率: ${total.toFixed(2)}% (样本桶: ${sec.totalCount ?? 0})
        </div>`
        return html
      }
    },
    legend: {
      bottom: 4,
      type: 'scroll',
      textStyle: { color: '#cbd5e1', fontSize: 11 },
      itemWidth: 12,
      itemHeight: 10
    },
    polar: {
      radius: ['10%', '72%'],
      center: ['50%', '46%']
    },
    angleAxis: {
      type: 'category',
      data: dirs,
      // startAngle: 101.25 (90 + 22.5/2) with clockwise: true strictly centers the first category 'N' at 0 deg top!
      startAngle: 101.25,
      clockwise: true,
      boundaryGap: true,
      axisLine: { lineStyle: { color: '#2b4c87' } },
      axisTick: { show: true, alignWithLabel: true, lineStyle: { color: '#3b82f6' } },
      axisLabel: {
        color: '#e2e8f0',
        fontWeight: 700,
        fontSize: 12
      },
      splitLine: {
        show: true,
        lineStyle: { color: 'rgba(59, 130, 246, 0.22)', type: 'dashed' }
      }
    },
    radiusAxis: {
      type: 'value',
      name: '频率 (%)',
      nameTextStyle: { color: '#94a9c9', fontSize: 11 },
      axisLine: { show: false },
      axisLabel: {
        color: '#93c5fd',
        fontSize: 10,
        formatter: '{value}%'
      },
      splitLine: {
        lineStyle: { color: 'rgba(59, 130, 246, 0.28)', type: 'dashed' }
      }
    },
    series
  }

  polarChart.setOption(option, true)
}

function renderWindTimeSeries() {
  if (!windTimeChartRef.value) return
  if (!timeChart) {
    timeChart = echarts.init(windTimeChartRef.value)
  }

  const rows = recentRecords.value
  const times = rows.map((r) => r.statTime)
  const vecSpeeds = rows.map((r) => r.vectorSpeed)
  const gustSpeeds = rows.map((r) => r.gustMax)
  const dirs = rows.map((r) => r.vectorDirection)

  const option = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(11, 20, 38, 0.95)',
      borderColor: '#2b4c87',
      textStyle: { color: '#e8f1ff', fontSize: 12 }
    },
    legend: {
      top: 4,
      textStyle: { color: '#cbd5e1', fontSize: 11 },
      data: ['10min矢量风速 (m/s)', '10min最大阵风 (m/s)', '矢量风向角 (°)']
    },
    grid: {
      left: 52,
      right: 56,
      top: 38,
      bottom: 30
    },
    xAxis: {
      type: 'category',
      data: times,
      axisLine: { lineStyle: { color: '#2b4c87' } },
      axisLabel: { color: '#94a9c9', fontSize: 10 }
    },
    yAxis: [
      {
        type: 'value',
        name: '风速 (m/s)',
        nameTextStyle: { color: '#94a9c9', fontSize: 11 },
        axisLabel: { color: '#94a9c9' },
        splitLine: { lineStyle: { color: 'rgba(43, 76, 135, 0.25)', type: 'dashed' } }
      },
      {
        type: 'value',
        name: '风向 (°)',
        min: 0,
        max: 360,
        nameTextStyle: { color: '#facc15', fontSize: 11 },
        axisLabel: { color: '#facc15', formatter: '{value}°' },
        splitLine: { show: false }
      }
    ],
    series: [
      {
        name: '10min矢量风速 (m/s)',
        type: 'line',
        data: vecSpeeds,
        smooth: true,
        lineStyle: { color: '#00d4ff', width: 2 },
        itemStyle: { color: '#00d4ff' },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(0, 212, 255, 0.25)' },
            { offset: 1, color: 'rgba(0, 212, 255, 0.02)' }
          ])
        }
      },
      {
        name: '10min最大阵风 (m/s)',
        type: 'line',
        data: gustSpeeds,
        smooth: true,
        lineStyle: { color: '#ff3b5c', width: 1.8, type: 'dashed' },
        itemStyle: { color: '#ff3b5c' }
      },
      {
        name: '矢量风向角 (°)',
        type: 'scatter',
        yAxisIndex: 1,
        data: dirs,
        symbolSize: 6,
        itemStyle: { color: '#facc15' }
      }
    ]
  }

  timeChart.setOption(option, true)
}

function handleResize() {
  if (polarChart) polarChart.resize()
  if (timeChart) timeChart.resize()
}

onMounted(() => {
  window.addEventListener('resize', handleResize)
  fetchWindRoseData()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  if (polarChart) {
    polarChart.dispose()
    polarChart = null
  }
  if (timeChart) {
    timeChart.dispose()
    timeChart = null
  }
})
</script>

<style scoped>
.wind-rose-view {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.wind-ctrl-bar {
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
  gap: 16px;
  flex-wrap: wrap;
}

.ctrl-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ctrl-label {
  font-size: 12px;
  color: var(--shm-text-secondary);
  font-weight: 600;
}

.engineering-note-banner {
  background: rgba(2, 132, 199, 0.12);
  border: 1px solid rgba(56, 189, 248, 0.35);
  border-left: 4px solid #00d4ff;
  border-radius: 6px;
  padding: 10px 14px;
  font-size: 12px;
  line-height: 1.55;
}

.note-title {
  font-weight: 700;
  color: #38bdf8;
  margin-bottom: 2px;
}

.note-desc {
  color: var(--shm-text-primary);
}

.note-desc code {
  background: rgba(15, 23, 42, 0.65);
  color: #facc15;
  padding: 1px 6px;
  border-radius: 4px;
  font-family: 'JetBrains Mono', monospace;
}

.wind-kpi-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
}

.wind-kpi-card {
  padding: 13px 15px;
}

.wk-label {
  font-size: 12px;
  color: var(--shm-text-secondary);
  font-weight: 600;
}

.wk-val {
  font-size: 22px;
  font-weight: 800;
  margin: 8px 0 4px;
}

.wk-val small {
  font-size: 12px;
  color: var(--shm-text-secondary);
}

.wk-sub {
  font-size: 11px;
  color: var(--shm-text-secondary);
}

.wind-charts-grid {
  display: grid;
  grid-template-columns: 1.05fr 1fr;
  gap: 14px;
}

.polar-chart-box {
  width: 100%;
  height: 500px;
}

.right-column-stack {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.wind-time-chart-box {
  width: 100%;
  height: 230px;
}

.pct-bar-cell {
  padding-right: 12px;
}

@media (max-width: 1400px) {
  .wind-kpi-grid {
    grid-template-columns: repeat(3, 1fr);
  }
  .wind-charts-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 1100px) {
  .wind-kpi-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 720px) {
  .wind-rose-view {
    gap: 10px;
  }

  .wind-ctrl-bar,
  .ctrl-left,
  .ctrl-item {
    align-items: flex-start;
    flex-direction: column;
  }

  .ctrl-left,
  .ctrl-item,
  .ctrl-item :deep(.el-radio-group),
  .ctrl-item :deep(.el-select) {
    width: 100% !important;
  }

  .wind-kpi-grid {
    grid-template-columns: 1fr;
    gap: 10px;
  }

  .shm-card-header {
    align-items: flex-start;
    flex-direction: column;
  }

  .polar-chart-box {
    height: 390px;
  }

  .wind-time-chart-box {
    height: 250px;
  }
}
</style>
