<template>
  <div class="analysis-view">
    <div class="metric-grid">
      <div class="metric-item">
        <span>传感器</span>
        <strong>{{ overview.totalSensors }}</strong>
      </div>
      <div class="metric-item">
        <span>监测通道</span>
        <strong>{{ overview.totalChannels }}</strong>
      </div>
      <div class="metric-item danger">
        <span>未处理预警</span>
        <strong>{{ overview.unackedAlertsCount }}</strong>
      </div>
      <div class="metric-item">
        <span>统计记录</span>
        <strong>{{ overview.statRecordCount }}</strong>
      </div>
    </div>

    <section class="panel">
      <div class="panel-header">
        <div>
          <h2>监测数据趋势</h2>
          <p>查看传感器最近一段时间的最大值、最小值和平均值。</p>
        </div>
        <div class="toolbar">
          <el-select v-model="selectedSensorKey" style="width: 280px" @change="loadSeries">
            <el-option
              v-for="item in sensors"
              :key="item.key"
              :label="`${item.sensorName} (${item.sensorNo}${item.channel ? '/' + item.channel : ''})`"
              :value="item.key"
            />
          </el-select>
          <el-date-picker
            v-model="startTime"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm:ss"
            placeholder="起始时间"
            style="width: 185px"
          />
          <span class="range-separator">至</span>
          <el-date-picker
            v-model="endTime"
            type="datetime"
            value-format="YYYY-MM-DD HH:mm:ss"
            placeholder="结束时间"
            style="width: 185px"
          />
          <el-button :icon="Refresh" :loading="loading" @click="loadSeries">刷新</el-button>
        </div>
      </div>
      <div class="range-summary">
        <div>
          <span>区间最大值</span>
          <strong class="max-value">{{ rangeSummary.windowMax ?? '-' }} {{ activeMeta.unit }}</strong>
        </div>
        <div>
          <span>区间最小值</span>
          <strong class="min-value">{{ rangeSummary.windowMin ?? '-' }} {{ activeMeta.unit }}</strong>
        </div>
        <div>
          <span>区间平均值</span>
          <strong class="avg-value">{{ rangeSummary.windowAvg ?? '-' }} {{ activeMeta.unit }}</strong>
        </div>
        <div>
          <span>样本数 / 1分钟记录</span>
          <strong>{{ rangeSummary.totalSamples }} / {{ rangeSummary.bucketCount }}</strong>
        </div>
      </div>
      <div ref="chartRef" v-loading="loading" class="trend-chart"></div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div>
          <h2>区间内 1 分钟统计记录</h2>
          <p>{{ activeMeta.sensorName }} · {{ activeMeta.physicalQuantity }} / {{ activeMeta.unit }}</p>
        </div>
      </div>
      <el-table :data="series.slice().reverse()" size="small" border>
        <el-table-column prop="statTime" label="统计时间" min-width="170" />
        <el-table-column prop="valMin" label="最小值" width="110" align="right" />
        <el-table-column prop="valMax" label="最大值" width="110" align="right" />
        <el-table-column prop="valAvg" label="平均值" width="110" align="right" />
        <el-table-column prop="sampleCount" label="样本数" width="90" align="right" />
      </el-table>
    </section>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import * as echarts from 'echarts'
import {
  getOverviewApi,
  getStatExtremesApi,
  getStatSensorsApi
} from '../api'

const chartRef = ref(null)
const loading = ref(false)
const sensors = ref([])
const selectedSensorKey = ref('')
const startTime = ref('')
const endTime = ref('')
const series = ref([])
const activeMeta = reactive({
  sensorNo: '',
  sensorName: '',
  channel: '',
  physicalQuantity: '',
  unit: ''
})
const overview = reactive({
  totalSensors: 0,
  totalChannels: 0,
  unackedAlertsCount: 0,
  statRecordCount: 0
})
const activeThresholds = reactive({
  level1Upper: null,
  level1Lower: null,
  level2Upper: null,
  level2Lower: null,
  level3Upper: null,
  level3Lower: null
})
const rangeSummary = reactive({
  windowMax: null,
  windowMin: null,
  windowAvg: null,
  totalSamples: 0,
  bucketCount: 0
})

let chart = null
let overviewTimer = null

async function loadOverview() {
  try {
    const data = await getOverviewApi()
    Object.assign(overview, {
      totalSensors: data.totalSensors || 0,
      totalChannels: data.totalChannels || 0,
      unackedAlertsCount: data.unackedAlertsCount || 0,
      statRecordCount: data.statRecordCount || 0
    })
  } catch (_) {}
}

async function loadSensors() {
  try {
    const data = await getStatSensorsApi()
    sensors.value = Array.isArray(data.items) ? data.items : []
    if (!selectedSensorKey.value && sensors.value.length) {
      selectedSensorKey.value = sensors.value[0].key
    }
    initializeTimeRange(sensors.value.find((item) => item.key === selectedSensorKey.value))
  } catch (err) {
    ElMessage.error(err?.message || '加载监测通道失败')
  }
}

function formatDateTime(date) {
  const pad = (value) => String(value).padStart(2, '0')
  return (
    [date.getFullYear(), pad(date.getMonth() + 1), pad(date.getDate())].join('-')
    + ' '
    + [pad(date.getHours()), pad(date.getMinutes()), pad(date.getSeconds())].join(':')
  )
}

function initializeTimeRange(meta) {
  if (startTime.value && endTime.value) return
  const parsedEnd = meta?.latestStatTime
    ? new Date(String(meta.latestStatTime).replace(' ', 'T'))
    : new Date()
  const end = Number.isNaN(parsedEnd.getTime()) ? new Date() : parsedEnd
  const start = new Date(end.getTime() - 60 * 60 * 1000)
  startTime.value = formatDateTime(start)
  endTime.value = formatDateTime(end)
}

async function loadSeries() {
  if (!selectedSensorKey.value) return
  loading.value = true
  try {
    const [sensorNo, channel = ''] = selectedSensorKey.value.split(':')
    const meta = sensors.value.find((item) => item.key === selectedSensorKey.value) || {}
    Object.assign(activeMeta, {
      sensorNo,
      sensorName: meta.sensorName || sensorNo,
      channel,
      physicalQuantity: meta.physicalQuantity || channel,
      unit: meta.unit || ''
    })
    const data = await getStatExtremesApi({
      sensorNo,
      channel,
      startTime: startTime.value,
      endTime: endTime.value,
      limit: 2000
    })
    series.value = Array.isArray(data.series) ? data.series : []
    Object.assign(rangeSummary, data.summary || {})
    Object.assign(activeThresholds, data.thresholds || {})
    await nextTick()
    renderChart()
  } catch (err) {
    ElMessage.error(err?.message || '加载分析数据失败')
  } finally {
    loading.value = false
  }
}

function renderChart() {
  if (!chartRef.value) return
  if (!chart) chart = echarts.init(chartRef.value)
  const thresholdLines = [
    ['一级上限', activeThresholds.level1Upper, '#ef4444'],
    ['一级下限', activeThresholds.level1Lower, '#ef4444'],
    ['二级上限', activeThresholds.level2Upper, '#f97316'],
    ['二级下限', activeThresholds.level2Lower, '#f97316'],
    ['三级上限', activeThresholds.level3Upper, '#eab308'],
    ['三级下限', activeThresholds.level3Lower, '#eab308']
  ]
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([name, value, color]) => ({
      name,
      yAxis: Number(value),
      lineStyle: { color, type: 'dashed', width: 1.5 },
      label: {
        show: true,
        position: 'insideEndTop',
        color,
        backgroundColor: 'rgba(8, 26, 48, 0.88)',
        padding: [2, 5],
        borderRadius: 3,
        formatter: `${name} ${value}`
      }
    }))
  const axisValues = [
    ...series.value.flatMap((item) => [item.valMax, item.valMin, item.valAvg]),
    activeThresholds.level1Upper,
    activeThresholds.level1Lower,
    activeThresholds.level2Upper,
    activeThresholds.level2Lower,
    activeThresholds.level3Upper,
    activeThresholds.level3Lower
  ]
    .map(Number)
    .filter(Number.isFinite)
  let yAxisMin
  let yAxisMax
  if (axisValues.length) {
    const valueMin = Math.min(...axisValues)
    const valueMax = Math.max(...axisValues)
    const span = valueMax - valueMin
    const padding = span > 0 ? span * 0.08 : Math.max(Math.abs(valueMax) * 0.1, 1)
    yAxisMin = valueMin - padding
    yAxisMax = valueMax + padding
  }
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { top: 0, data: ['最大值', '最小值', '平均值'] },
    grid: { left: 56, right: 24, top: 44, bottom: 44 },
    xAxis: {
      type: 'category',
      data: series.value.map((item) => item.statTime),
      axisLabel: { hideOverlap: true }
    },
    yAxis: {
      type: 'value',
      scale: true,
      min: yAxisMin,
      max: yAxisMax,
      splitNumber: 6
    },
    series: [
      {
        name: '最大值',
        type: 'line',
        smooth: true,
        data: series.value.map((item) => item.valMax),
        markLine: {
          symbol: 'none',
          silent: true,
          data: thresholdLines
        }
      },
      { name: '最小值', type: 'line', smooth: true, data: series.value.map((item) => item.valMin) },
      { name: '平均值', type: 'line', smooth: true, data: series.value.map((item) => item.valAvg) }
    ]
  }, true)
}

function handleResize() {
  chart?.resize()
}

onMounted(async () => {
  await Promise.all([loadOverview(), loadSensors()])
  await loadSeries()
  overviewTimer = setInterval(loadOverview, 5000)
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  clearInterval(overviewTimer)
  chart?.dispose()
})
</script>

<style scoped>
.analysis-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.metric-item,
.panel {
  background: var(--shm-bg-card);
  border: 1px solid var(--shm-border);
  border-radius: 8px;
}

.metric-item {
  padding: 16px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.metric-item span {
  color: var(--shm-text-secondary);
  font-size: 13px;
}

.metric-item strong {
  font-size: 24px;
}

.metric-item.danger strong {
  color: #ef4444;
}

.panel {
  padding: 16px;
}

.panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.panel-header h2 {
  margin: 0;
  font-size: 16px;
}

.panel-header p {
  margin: 5px 0 0;
  color: var(--shm-text-secondary);
  font-size: 12px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.range-separator {
  color: var(--shm-text-secondary);
  font-size: 12px;
}

.range-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}

.range-summary > div {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--shm-border);
  border-radius: 6px;
  background: var(--shm-bg-elevated);
}

.range-summary span {
  color: var(--shm-text-secondary);
  font-size: 12px;
}

.range-summary strong {
  font-size: 16px;
}

.max-value {
  color: #ef4444;
}

.min-value {
  color: #38bdf8;
}

.avg-value {
  color: #10b981;
}

.trend-chart {
  height: 360px;
}

@media (max-width: 900px) {
  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .panel-header,
  .toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .range-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
