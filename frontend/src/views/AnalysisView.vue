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
          <el-select v-model="selectedSensorKey" style="width: 300px" @change="loadSeries">
            <el-option
              v-for="item in sensors"
              :key="item.key"
              :label="`${item.sensorName} (${item.sensorNo}${item.channel ? '/' + item.channel : ''})`"
              :value="item.key"
            />
          </el-select>
          <el-select v-model="periodType" style="width: 110px" @change="loadSeries">
            <el-option label="1分钟" value="1m" />
            <el-option label="1小时" value="1h" />
            <el-option label="1天" value="1d" />
          </el-select>
          <el-button :icon="Refresh" :loading="loading" @click="loadSeries">刷新</el-button>
        </div>
      </div>
      <div ref="chartRef" v-loading="loading" class="trend-chart"></div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <div>
          <h2>最近统计记录</h2>
          <p>{{ activeMeta.sensorName }} · {{ activeMeta.physicalQuantity }} / {{ activeMeta.unit }}</p>
        </div>
      </div>
      <el-table :data="series.slice().reverse()" size="small" border>
        <el-table-column prop="statTime" label="统计时间" min-width="170" />
        <el-table-column prop="valMin" label="最小值" width="110" align="right" />
        <el-table-column prop="valMax" label="最大值" width="110" align="right" />
        <el-table-column prop="valAvg" label="平均值" width="110" align="right" />
        <el-table-column prop="valStd" label="标准差" width="110" align="right" />
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
const periodType = ref('1m')
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

let chart = null

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
  } catch (err) {
    ElMessage.error(err?.message || '加载监测通道失败')
  }
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
      periodType: periodType.value,
      limit: 60
    })
    series.value = Array.isArray(data.series) ? data.series : []
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
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { top: 0, data: ['最大值', '最小值', '平均值'] },
    grid: { left: 56, right: 24, top: 44, bottom: 44 },
    xAxis: {
      type: 'category',
      data: series.value.map((item) => item.statTime),
      axisLabel: { hideOverlap: true }
    },
    yAxis: { type: 'value', scale: true },
    series: [
      { name: '最大值', type: 'line', smooth: true, data: series.value.map((item) => item.valMax) },
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
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
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
}
</style>
