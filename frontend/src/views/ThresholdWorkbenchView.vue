<template>
  <div class="threshold-workbench-view">
    <!-- 1. Top Summary & Workbench Action Bar -->
    <div class="shm-card workbench-top-bar">
      <div class="wb-stats">
        <div class="wb-stat-item">
          <span class="wb-label">规则总通道数</span>
          <strong class="mono-num">{{ allRulesCount }}</strong>
        </div>
        <div class="wb-stat-item">
          <span class="wb-label">覆盖物理量类型</span>
          <strong class="mono-num green-txt">{{ physicalQuantityOptions.length }}</strong>
        </div>
        <div class="wb-stat-item">
          <span class="wb-label">去直流分量通道 (AC幅值)</span>
          <strong class="mono-num cyan-txt">{{ dcOffsetRulesCount }}</strong>
        </div>
        <div class="wb-stat-item">
          <span class="wb-label">≥1Hz 高频1000ms盯防通道</span>
          <strong class="mono-num orange-txt">{{ highFreqRulesCount }}</strong>
        </div>
      </div>

      <div class="wb-actions">
        <el-button type="warning" size="small" @click="openBatchDialog">
          <el-icon style="margin-right: 4px"><Operation /></el-icon>
          【按物理量批量修改阈值】
        </el-button>
        <el-button type="primary" size="small" @click="openSimulatorDialog()">
          <el-icon style="margin-right: 4px"><DataAnalysis /></el-icon>
          【历史数据回放试算 (Dry-Run Simulator)】
        </el-button>
      </div>
    </div>

    <!-- 2. Filter Toolbar & 208 Rules Table -->
    <div class="shm-card">
      <div class="filter-bar">
        <div class="filter-left">
          <el-select
            v-model="filters.stationName"
            placeholder="所属站点 (全部)"
            clearable
            size="small"
            style="width: 145px"
          >
            <el-option label="玉环站" value="玉环站" />
            <el-option label="淮北西站" value="淮北西站" />
          </el-select>

          <el-select
            v-model="filters.physicalQuantity"
            placeholder="监测物理量 (全部)"
            clearable
            size="small"
            style="width: 175px"
          >
            <el-option
              v-for="pq in physicalQuantityOptions"
              :key="pq"
              :label="`${pq} (${countByPq(pq)}通道)`"
              :value="pq"
            />
          </el-select>

          <el-input
            v-model="filters.keyword"
            placeholder="搜索传感器编号 / 名称 / 通道 (如 stn-12-b, accl-01)..."
            clearable
            size="small"
            style="width: 280px"
          />

          <el-button size="small" @click="resetRuleFilters">重置筛选</el-button>
        </div>

        <div class="filter-right">
          <span class="mono-num" style="font-size: 12px; color: var(--shm-text-secondary)">
            当前匹配 {{ filteredRules.length }} / {{ rules.length }} 条规则 · 修改后内存 ConcurrentDict 零停机热更新
          </span>
        </div>
      </div>

      <el-table
        v-loading="loading"
        :data="pagedRules"
        border
        stripe
        size="small"
        style="width: 100%"
      >
        <el-table-column prop="station_name" label="站点" width="88" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.station_name === '玉环站' ? 'primary' : 'success'" effect="plain">
              {{ row.station_name }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="传感器与通道" min-width="205">
          <template #default="{ row }">
            <div>
              <strong>{{ row.sensor_name }}</strong>
              <div class="mono-num muted-sub">
                {{ row.sensor_no }} · 通道: {{ row.channel || '默认' }}
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column prop="physical_quantity" label="物理量 (单位)" width="130" align="center">
          <template #default="{ row }">
            <span>{{ row.physical_quantity }}</span>
            <span v-if="row.unit" class="mono-num muted-sub"> ({{ row.unit }})</span>
          </template>
        </el-table-column>

        <el-table-column label="🔴 一级阈值 (红色紧急 L1)" width="170" align="center">
          <template #default="{ row }">
            <div class="mono-num limit-box l1-box">
              [{{ formatLim(row.level1_lower) }}, {{ formatLim(row.level1_upper) }}]
            </div>
          </template>
        </el-table-column>

        <el-table-column label="🟠 二级阈值 (橙色重要 L2)" width="170" align="center">
          <template #default="{ row }">
            <div class="mono-num limit-box l2-box">
              [{{ formatLim(row.level2_lower) }}, {{ formatLim(row.level2_upper) }}]
            </div>
          </template>
        </el-table-column>

        <el-table-column label="🟡 三级阈值 (黄色一般 L3)" width="170" align="center">
          <template #default="{ row }">
            <div class="mono-num limit-box l3-box">
              [{{ formatLim(row.level3_lower) }}, {{ formatLim(row.level3_upper) }}]
            </div>
          </template>
        </el-table-column>

        <el-table-column label="采样间隔 / 离线超时" width="145" align="center">
          <template #default="{ row }">
            <div class="mono-num" style="font-size: 11px">
              <div>采样: {{ row.expected_interval_ms }}ms</div>
              <div :style="{ color: row.offline_timeout_ms <= 1000 ? '#ff8f1f' : '#94a9c9' }">
                超时: {{ row.offline_timeout_ms }}ms
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="物理量程 [最小值, 最大值]" width="190" align="center">
          <template #default="{ row }">
            <div class="mono-num" style="font-size: 11px">
              [{{ formatLim(row.range_min) }}, {{ formatLim(row.range_max) }}]
            </div>
          </template>
        </el-table-column>

        <el-table-column label="死值判定时间" width="125" align="center">
          <template #default="{ row }">
            <div class="mono-num" style="font-size: 11px">
              {{ row.stuck_time_ms || '-' }}ms
            </div>
          </template>
        </el-table-column>

        <el-table-column label="连续超限确认点数" width="135" align="center">
          <template #default="{ row }">
            <div class="mono-num" style="font-size: 11px">
              <div>{{ row.confirm_points || 1 }} 点</div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="去直流 (AC)" width="95" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.remove_dc_offset ? 'warning' : 'info'" effect="plain">
              {{ row.remove_dc_offset ? '去直流开启' : '原始量' }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="create_by" label="创建人" width="100" align="center">
          <template #default="{ row }">
            <span class="mono-num">{{ row.create_by || 'system' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作 (调参 / 试算)" width="180" fixed="right" align="center">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="openEditModal(row)">
              编辑阈值
            </el-button>
            <el-button type="success" link size="small" @click="openSimulatorDialog(row)">
              回放试算
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="table-footer">
        <span class="mono-num muted-sub">
          严格遵循层级约束：一级上限(红) ≥ 二级上限(橙) ≥ 三级上限(黄)，一级下限(红) ≤ 二级下限(橙) ≤ 三级下限(黄)
        </span>
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[15, 25, 50, 100]"
          layout="total, sizes, prev, pager, next"
          :total="filteredRules.length"
          small
          background
        />
      </div>
    </div>

    <!-- 3. Single Channel Threshold Edit Modal (with inline "试算效果" Dry-Run button) -->
    <el-dialog
      v-model="editDialogVisible"
      :title="`调整三级预警阈值规则 — ${editForm.sensor_name} (${editForm.sensor_no}/${editForm.channel || '默认'})`"
      width="680px"
      destroy-on-close
    >
      <el-form :model="editForm" label-width="145px" size="small">
        <el-row :gutter="14">
          <el-col :span="12">
            <el-form-item label="🔴 一级上限 (红 L1)">
              <el-input-number v-model="editForm.level1_upper" :step="0.5" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="🔴 一级下限 (红 L1)">
              <el-input-number v-model="editForm.level1_lower" :step="0.5" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>

          <el-col :span="12">
            <el-form-item label="🟠 二级上限 (橙 L2)">
              <el-input-number v-model="editForm.level2_upper" :step="0.5" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="🟠 二级下限 (橙 L2)">
              <el-input-number v-model="editForm.level2_lower" :step="0.5" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>

          <el-col :span="12">
            <el-form-item label="🟡 三级上限 (黄 L3)">
              <el-input-number v-model="editForm.level3_upper" :step="0.5" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="🟡 三级下限 (黄 L3)">
              <el-input-number v-model="editForm.level3_lower" :step="0.5" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>

          <el-col :span="12">
            <el-form-item label="离线超时阈值 (ms)">
              <el-input-number v-model="editForm.offline_timeout_ms" :min="200" :step="500" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="期望采样周期 (ms)">
              <el-input-number v-model="editForm.expected_interval_ms" :min="10" :step="100" style="width: 100%" />
            </el-form-item>
          </el-col>

          <el-col :span="12">
            <el-form-item label="量程最小值">
              <el-input-number v-model="editForm.range_min" :precision="5" :controls="false" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="量程最大值">
              <el-input-number v-model="editForm.range_max" :precision="5" :controls="false" style="width: 100%" />
            </el-form-item>
          </el-col>

          <el-col :span="12">
            <el-form-item label="死值判定时间 (ms)">
              <el-input-number v-model="editForm.stuck_time_ms" :min="1" :step="1000" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="连续防抖点数 (N)">
              <el-input-number v-model="editForm.confirm_points" :min="1" :max="30" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="去直流处理">
              <el-tag :type="editForm.remove_dc_offset ? 'warning' : 'info'" effect="plain">
                {{ editForm.remove_dc_offset ? '按传感器类型自动去直流' : '原始值判定' }}
              </el-tag>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>

      <template #footer>
        <div class="dialog-footer-split">
          <el-button type="success" plain @click="jumpToSimulateFromEdit">
            📊 保存前先【试算效果 (Dry-Run)】
          </el-button>
          <div>
            <el-button @click="editDialogVisible = false">取消</el-button>
            <el-button type="primary" :loading="savingSingle" @click="submitSingleEdit">
              保存并热更新至引擎
            </el-button>
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- 4. Batch Update by Physical Quantity Dialog -->
    <el-dialog
      v-model="batchDialogVisible"
      title="【按监测物理量批量修改三级阈值】 (如一键标定全桥 96 个应力通道或加速度通道)"
      width="640px"
      destroy-on-close
    >
      <el-form :model="batchForm" label-width="145px" size="small">
        <el-form-item label="目标监测物理量">
          <el-select
            v-model="batchForm.physical_quantity"
            style="width: 100%"
            @change="onBatchPqChange"
          >
            <el-option
              v-for="pq in physicalQuantityOptions"
              :key="pq"
              :label="`${pq} (共 ${countByPq(pq)} 个通道)`"
              :value="pq"
            />
          </el-select>
        </el-form-item>

        <el-row :gutter="14">
          <el-col :span="12">
            <el-form-item label="🔴 一级上限 (L1)">
              <el-input-number v-model="batchForm.level1_upper" :step="1" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="🔴 一级下限 (L1)">
              <el-input-number v-model="batchForm.level1_lower" :step="1" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>

          <el-col :span="12">
            <el-form-item label="🟠 二级上限 (L2)">
              <el-input-number v-model="batchForm.level2_upper" :step="1" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="🟠 二级下限 (L2)">
              <el-input-number v-model="batchForm.level2_lower" :step="1" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>

          <el-col :span="12">
            <el-form-item label="🟡 三级上限 (L3)">
              <el-input-number v-model="batchForm.level3_upper" :step="1" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="🟡 三级下限 (L3)">
              <el-input-number v-model="batchForm.level3_lower" :step="1" :precision="4" style="width: 100%" />
            </el-form-item>
          </el-col>

          <el-col :span="12">
            <el-form-item label="离线超时 (ms)">
              <el-input-number v-model="batchForm.offline_timeout_ms" :min="200" :step="500" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="死值判定时间 (ms)">
              <el-input-number v-model="batchForm.stuck_time_ms" :min="1" :step="1000" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="连续防抖点数">
              <el-input-number v-model="batchForm.confirm_points" :min="1" :max="20" style="width: 100%" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>

      <template #footer>
        <el-button @click="batchDialogVisible = false">取消</el-button>
        <el-button type="warning" :loading="savingBatch" @click="submitBatchUpdate">
          确认批量更新 ({{ countByPq(batchForm.physical_quantity) }} 个通道)
        </el-button>
      </template>
    </el-dialog>

    <!-- 5. Historical Data Dry-Run Simulator Dialog -->
    <el-dialog
      v-model="simDialogVisible"
      title="🔬 历史数据回放试算台 (Dry-Run Simulator) — 保存前预演新阈值在峰值历史数据下的触发效果"
      width="960px"
      top="5vh"
    >
      <div class="sim-dialog-body">
        <div class="sim-form-grid">
          <div class="sim-field">
            <label>试算传感器通道</label>
            <el-select
              v-model="simForm.ruleKey"
              filterable
              size="small"
              style="width: 100%"
              @change="onSimRuleSelect"
            >
              <el-option
                v-for="r in rules"
                :key="r.key"
                :label="`[${r.station_name}] ${r.sensor_name} (${r.sensor_no}/${r.channel || '默认'}) · ${r.physical_quantity}`"
                :value="r.key"
              />
            </el-select>
          </div>

          <div class="sim-field">
            <label>历史回放时段选择</label>
            <el-select v-model="simForm.timeWindow" size="small" style="width: 100%" @change="runDryRunSimulation">
              <el-option label="🔥 峰值吞吐时段 (2026-09-28 12:00 ~ 15:00)" value="PEAK_928" />
              <el-option label="⚡ 掉线与波动时段 (2026-09-30 15:00 ~ 18:00)" value="DROP_930" />
              <el-option label="📊 全量历史样本窗口 (2026-09-21 ~ 09-30)" value="ALL" />
            </el-select>
          </div>

        </div>

        <!-- Proposed 3-Level Threshold Inputs -->
        <div class="sim-threshold-inputs">
          <div class="sim-lvl-group l1">
            <span class="lvl-tag">🔴 拟设一级阈值(L1)</span>
            <el-input-number v-model="simForm.level1_lower" size="small" :step="0.5" placeholder="L1下限" />
            <span>~</span>
            <el-input-number v-model="simForm.level1_upper" size="small" :step="0.5" placeholder="L1上限" />
          </div>
          <div class="sim-lvl-group l2">
            <span class="lvl-tag">🟠 拟设二级阈值(L2)</span>
            <el-input-number v-model="simForm.level2_lower" size="small" :step="0.5" placeholder="L2下限" />
            <span>~</span>
            <el-input-number v-model="simForm.level2_upper" size="small" :step="0.5" placeholder="L2上限" />
          </div>
          <div class="sim-lvl-group l3">
            <span class="lvl-tag">🟡 拟设三级阈值(L3)</span>
            <el-input-number v-model="simForm.level3_lower" size="small" :step="0.5" placeholder="L3下限" />
            <span>~</span>
            <el-input-number v-model="simForm.level3_upper" size="small" :step="0.5" placeholder="L3上限" />
          </div>
          <el-button type="primary" size="small" :loading="simLoading" @click="runDryRunSimulation">
            ⚡ 立即执行回放试算
          </el-button>
        </div>

        <!-- Comparison Cards: Current Hits vs Proposed Hits -->
        <div v-if="simResult" class="sim-compare-grid">
          <div class="sim-cmp-card current">
            <div class="cmp-title">当前生效规则触发统计 (共 {{ simResult.totalPoints }} 个历史分钟桶)</div>
            <div class="cmp-badges mono-num">
              <span class="c-pill red">L1红: {{ simResult.currentHits?.level1 ?? 0 }}</span>
              <span class="c-pill orange">L2橙: {{ simResult.currentHits?.level2 ?? 0 }}</span>
              <span class="c-pill yellow">L3黄: {{ simResult.currentHits?.level3 ?? 0 }}</span>
              <span class="c-pill total">总告警率: {{ simResult.currentHits?.alertRatePct ?? 0 }}%</span>
            </div>
          </div>

          <div class="sim-cmp-card proposed">
            <div class="cmp-title">
              ✨ 拟调整新阈值试算结果 (耗时 {{ simResult.elapsedMs }}ms · 极值范围 [{{ simResult.observedExtremes?.minVal }}, {{ simResult.observedExtremes?.maxVal }}])
            </div>
            <div class="cmp-badges mono-num">
              <span class="c-pill red">L1红: {{ simResult.proposedHits?.level1 ?? 0 }}</span>
              <span class="c-pill orange">L2橙: {{ simResult.proposedHits?.level2 ?? 0 }}</span>
              <span class="c-pill yellow">L3黄: {{ simResult.proposedHits?.level3 ?? 0 }}</span>
              <span class="c-pill total">新告警率: {{ simResult.proposedHits?.alertRatePct ?? 0 }}%</span>
            </div>
          </div>
        </div>

        <!-- ECharts Dry-Run Preview Chart -->
        <div ref="simChartRef" v-loading="simLoading" class="sim-chart-box"></div>
      </div>

      <template #footer>
        <el-button @click="simDialogVisible = false">关闭试算台</el-button>
        <el-button type="success" :loading="savingFromSim" @click="applySimulatedThresholds">
          ✅ 试算满意，将此三级阈值正式保存并热更新至引擎
        </el-button>
      </template>
    </el-dialog>

  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import { Operation, DataAnalysis } from '@element-plus/icons-vue'
import {
  listThresholdsApi,
  updateThresholdApi,
  batchUpdateThresholdsApi,
  simulateThresholdApi
} from '../api'

const emit = defineEmits(['refresh-global'])

const loading = ref(false)
const rules = ref([])
const physicalQuantityOptions = ref([])

const filters = reactive({
  stationName: '',
  physicalQuantity: '',
  keyword: ''
})

const page = ref(1)
const pageSize = ref(25)

// Stats
const allRulesCount = computed(() => rules.value.length || 208)
const dcOffsetRulesCount = computed(() => rules.value.filter((r) => r.remove_dc_offset).length)
const highFreqRulesCount = computed(() => rules.value.filter((r) => Number(r.expected_interval_ms || 1000) <= 1000).length)

const filteredRules = computed(() => {
  return rules.value.filter((r) => {
    if (filters.stationName && r.station_name !== filters.stationName) return false
    if (filters.physicalQuantity && r.physical_quantity !== filters.physicalQuantity) return false
    if (filters.keyword.trim()) {
      const kw = filters.keyword.trim().toLowerCase()
      const text = `${r.sensor_no || ''} ${r.sensor_name || ''} ${r.channel || ''} ${r.physical_quantity || ''}`.toLowerCase()
      if (!text.includes(kw)) return false
    }
    return true
  })
})

const pagedRules = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredRules.value.slice(start, start + pageSize.value)
})

function countByPq(pq) {
  return rules.value.filter((r) => r.physical_quantity === pq).length
}

function resetRuleFilters() {
  filters.stationName = ''
  filters.physicalQuantity = ''
  filters.keyword = ''
  page.value = 1
}

async function fetchThresholds() {
  loading.value = true
  try {
    const res = await listThresholdsApi()
    rules.value = Array.isArray(res.items) ? res.items : []
    physicalQuantityOptions.value = Array.isArray(res.physicalQuantities) ? res.physicalQuantities : []
  } catch (err) {
    ElMessage.error(`加载208条阈值规则失败: ${err?.message || ''}`)
  } finally {
    loading.value = false
  }
}

// Single Edit Dialog
const editDialogVisible = ref(false)
const savingSingle = ref(false)
const editForm = reactive({
  id: '',
  key: '',
  sensor_no: '',
  sensor_name: '',
  channel: '',
  level1_upper: 0,
  level1_lower: 0,
  level2_upper: 0,
  level2_lower: 0,
  level3_upper: 0,
  level3_lower: 0,
  offline_timeout_ms: 1000,
  expected_interval_ms: 1000,
  confirm_points: 3,
  range_min: null,
  range_max: null,
  stuck_time_ms: 30000,
  remove_dc_offset: false
})

function openEditModal(row) {
  Object.assign(editForm, {
    id: row.id,
    key: row.key,
    sensor_no: row.sensor_no,
    sensor_name: row.sensor_name,
    channel: row.channel || '',
    level1_upper: Number(row.level1_upper ?? 10),
    level1_lower: Number(row.level1_lower ?? -10),
    level2_upper: Number(row.level2_upper ?? 8),
    level2_lower: Number(row.level2_lower ?? -8),
    level3_upper: Number(row.level3_upper ?? 6),
    level3_lower: Number(row.level3_lower ?? -6),
    offline_timeout_ms: Number(row.offline_timeout_ms ?? 1000),
    expected_interval_ms: Number(row.expected_interval_ms ?? 1000),
    confirm_points: Number(row.confirm_points ?? 3),
    range_min: row.range_min === null || row.range_min === undefined ? null : Number(row.range_min),
    range_max: row.range_max === null || row.range_max === undefined ? null : Number(row.range_max),
    stuck_time_ms: Number(row.stuck_time_ms ?? 30000),
    remove_dc_offset: Boolean(row.remove_dc_offset)
  })
  editDialogVisible.value = true
}

async function submitSingleEdit() {
  if (!(editForm.level1_upper >= editForm.level2_upper && editForm.level2_upper >= editForm.level3_upper)) {
    ElMessage.warning('上限必须满足：一级上限(红) ≥ 二级上限(橙) ≥ 三级上限(黄)')
    return
  }
  if (!(editForm.level1_lower <= editForm.level2_lower && editForm.level2_lower <= editForm.level3_lower)) {
    ElMessage.warning('下限必须满足：一级下限(红) ≤ 二级下限(橙) ≤ 三级下限(黄)')
    return
  }

  savingSingle.value = true
  try {
    await updateThresholdApi(editForm.id, {
      level1_upper: editForm.level1_upper,
      level1_lower: editForm.level1_lower,
      level2_upper: editForm.level2_upper,
      level2_lower: editForm.level2_lower,
      level3_upper: editForm.level3_upper,
      level3_lower: editForm.level3_lower,
      offline_timeout_ms: editForm.offline_timeout_ms,
      expected_interval_ms: editForm.expected_interval_ms,
      confirm_points: editForm.confirm_points,
      range_min: editForm.range_min,
      range_max: editForm.range_max,
      stuck_time_ms: editForm.stuck_time_ms
    })
    ElMessage.success('阈值规则已保存并完成零停机热更新')
    editDialogVisible.value = false
    await fetchThresholds()
    emit('refresh-global')
  } catch (err) {
    ElMessage.error(`保存失败: ${err?.message || ''}`)
  } finally {
    savingSingle.value = false
  }
}

// Batch Update Dialog
const batchDialogVisible = ref(false)
const savingBatch = ref(false)
const batchForm = reactive({
  physical_quantity: '应力',
  level1_upper: 12.0,
  level1_lower: -11.2,
  level2_upper: 9.5,
  level2_lower: -9.0,
  level3_upper: 7.5,
  level3_lower: -7.0,
  offline_timeout_ms: 1000,
  confirm_points: 3,
  stuck_time_ms: 30000
})

function openBatchDialog() {
  if (physicalQuantityOptions.value.length > 0 && !batchForm.physical_quantity) {
    batchForm.physical_quantity = physicalQuantityOptions.value[0]
  }
  onBatchPqChange(batchForm.physical_quantity)
  batchDialogVisible.value = true
}

function onBatchPqChange(pq) {
  const sample = rules.value.find((r) => r.physical_quantity === pq)
  if (sample) {
    batchForm.level1_upper = Number(sample.level1_upper ?? 12)
    batchForm.level1_lower = Number(sample.level1_lower ?? -12)
    batchForm.level2_upper = Number(sample.level2_upper ?? 9)
    batchForm.level2_lower = Number(sample.level2_lower ?? -9)
    batchForm.level3_upper = Number(sample.level3_upper ?? 7)
    batchForm.level3_lower = Number(sample.level3_lower ?? -7)
    batchForm.offline_timeout_ms = Number(sample.offline_timeout_ms ?? 1000)
    batchForm.confirm_points = Number(sample.confirm_points ?? 3)
    batchForm.stuck_time_ms = Number(sample.stuck_time_ms ?? 30000)
  }
}

async function submitBatchUpdate() {
  savingBatch.value = true
  try {
    const res = await batchUpdateThresholdsApi({ ...batchForm })
    ElMessage.success(`批量更新成功！已同步刷新 ${res.updatedCount || 0} 个【${batchForm.physical_quantity}】通道阈值`)
    batchDialogVisible.value = false
    await fetchThresholds()
    emit('refresh-global')
  } catch (err) {
    ElMessage.error(`批量修改失败: ${err?.message || ''}`)
  } finally {
    savingBatch.value = false
  }
}

// Dry-Run Simulator Dialog
const simDialogVisible = ref(false)
const simLoading = ref(false)
const savingFromSim = ref(false)
const simResult = ref(null)
const simChartRef = ref(null)
let simChart = null

const simForm = reactive({
  ruleId: '',
  ruleKey: 'stn-12-b:stress',
  sensor_no: 'stn-12-b',
  channel: 'stress',
  timeWindow: 'PEAK_928',
  level1_upper: 12.0,
  level1_lower: -11.2,
  level2_upper: 9.5,
  level2_lower: -9.0,
  level3_upper: 7.5,
  level3_lower: -7.0
})

function openSimulatorDialog(targetRow = null) {
  const row = targetRow || rules.value.find((r) => r.key === 'stn-12-b:stress') || rules.value[0]
  if (row) {
    simForm.ruleId = row.id
    simForm.ruleKey = row.key
    simForm.sensor_no = row.sensor_no
    simForm.channel = row.channel || ''
    simForm.level1_upper = Number(row.level1_upper ?? 12)
    simForm.level1_lower = Number(row.level1_lower ?? -11.2)
    simForm.level2_upper = Number(row.level2_upper ?? 9.5)
    simForm.level2_lower = Number(row.level2_lower ?? -9.0)
    simForm.level3_upper = Number(row.level3_upper ?? 7.5)
    simForm.level3_lower = Number(row.level3_lower ?? -7.0)
  }
  simDialogVisible.value = true
  nextTick(() => {
    runDryRunSimulation()
  })
}

function jumpToSimulateFromEdit() {
  simForm.ruleId = editForm.id
  simForm.ruleKey = editForm.key
  simForm.sensor_no = editForm.sensor_no
  simForm.channel = editForm.channel || ''
  simForm.level1_upper = editForm.level1_upper
  simForm.level1_lower = editForm.level1_lower
  simForm.level2_upper = editForm.level2_upper
  simForm.level2_lower = editForm.level2_lower
  simForm.level3_upper = editForm.level3_upper
  simForm.level3_lower = editForm.level3_lower
  editDialogVisible.value = false
  simDialogVisible.value = true
  nextTick(() => {
    runDryRunSimulation()
  })
}

function onSimRuleSelect(key) {
  const row = rules.value.find((r) => r.key === key)
  if (row) {
    simForm.ruleId = row.id
    simForm.sensor_no = row.sensor_no
    simForm.channel = row.channel || ''
    simForm.level1_upper = Number(row.level1_upper ?? 12)
    simForm.level1_lower = Number(row.level1_lower ?? -11.2)
    simForm.level2_upper = Number(row.level2_upper ?? 9.5)
    simForm.level2_lower = Number(row.level2_lower ?? -9.0)
    simForm.level3_upper = Number(row.level3_upper ?? 7.5)
    simForm.level3_lower = Number(row.level3_lower ?? -7.0)
    runDryRunSimulation()
  }
}

async function runDryRunSimulation() {
  simLoading.value = true
  try {
    let start_time = '2026-09-28 12:00:00'
    let end_time = '2026-09-28 15:00:00'
    if (simForm.timeWindow === 'DROP_930') {
      start_time = '2026-09-30 15:00:00'
      end_time = '2026-09-30 18:00:00'
    } else if (simForm.timeWindow === 'ALL') {
      start_time = '2026-09-21 00:00:00'
      end_time = '2026-09-30 23:59:59'
    }

    const res = await simulateThresholdApi({
      sensor_no: simForm.sensor_no,
      channel: simForm.channel,
      level1_upper: simForm.level1_upper,
      level1_lower: simForm.level1_lower,
      level2_upper: simForm.level2_upper,
      level2_lower: simForm.level2_lower,
      level3_upper: simForm.level3_upper,
      level3_lower: simForm.level3_lower,
      start_time,
      end_time
    })
    simResult.value = res
    await nextTick()
    renderSimChart(res)
  } catch (err) {
    ElMessage.error(`回放试算失败: ${err?.message || ''}`)
  } finally {
    simLoading.value = false
  }
}

function renderSimChart(res) {
  if (!simChartRef.value) return
  if (!simChart) {
    simChart = echarts.init(simChartRef.value)
  }

  const series = Array.isArray(res.series) ? res.series : []
  const times = series.map((s) => s.time)
  const upperVals = series.map((s) => s.evalUpper)
  const lowerVals = series.map((s) => s.evalLower)

  const pt = res.proposedThresholds || {}

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
      textStyle: { color: '#cbd5e1' },
      data: ['评估上包络 (evalUpper)', '评估下包络 (evalLower)']
    },
    grid: { left: 60, right: 90, top: 38, bottom: 32 },
    xAxis: {
      type: 'category',
      data: times,
      axisLabel: { color: '#94a9c9', fontSize: 10 }
    },
    yAxis: {
      type: 'value',
      scale: true,
      name: `${res.physicalQuantity || ''} (${res.unit || ''})`,
      nameTextStyle: { color: '#94a9c9' },
      axisLabel: { color: '#94a9c9' },
      splitLine: { lineStyle: { color: 'rgba(43, 76, 135, 0.25)', type: 'dashed' } }
    },
    series: [
      {
        name: '评估上包络 (evalUpper)',
        type: 'line',
        data: upperVals,
        showSymbol: false,
        lineStyle: { color: '#00d4ff', width: 2 },
        itemStyle: { color: '#00d4ff' },
        markLine: {
          symbol: ['none', 'none'],
          silent: true,
          data: [
            { yAxis: pt.level1Upper, lineStyle: { color: '#ff3b5c', width: 2 }, label: { formatter: '新L1上限: {c}', color: '#ff3b5c' } },
            { yAxis: pt.level2Upper, lineStyle: { color: '#ff8f1f', width: 1.5, type: 'dashed' }, label: { formatter: '新L2上限: {c}', color: '#ff8f1f' } },
            { yAxis: pt.level3Upper, lineStyle: { color: '#fadb14', width: 1.2, type: 'dotted' }, label: { formatter: '新L3上限: {c}', color: '#fadb14' } },
            { yAxis: pt.level3Lower, lineStyle: { color: '#fadb14', width: 1.2, type: 'dotted' }, label: { formatter: '新L3下限: {c}', color: '#fadb14' } },
            { yAxis: pt.level2Lower, lineStyle: { color: '#ff8f1f', width: 1.5, type: 'dashed' }, label: { formatter: '新L2下限: {c}', color: '#ff8f1f' } },
            { yAxis: pt.level1Lower, lineStyle: { color: '#ff3b5c', width: 2 }, label: { formatter: '新L1下限: {c}', color: '#ff3b5c' } }
          ]
        }
      },
      {
        name: '评估下包络 (evalLower)',
        type: 'line',
        data: lowerVals,
        showSymbol: false,
        lineStyle: { color: '#38bdf8', width: 1.5, type: 'dashed' },
        itemStyle: { color: '#38bdf8' }
      }
    ]
  }

  simChart.setOption(option, true)
}

async function applySimulatedThresholds() {
  const row = rules.value.find((r) => r.key === simForm.ruleKey || r.id === simForm.ruleId)
  if (!row) return
  savingFromSim.value = true
  try {
    await updateThresholdApi(row.id, {
      level1_upper: simForm.level1_upper,
      level1_lower: simForm.level1_lower,
      level2_upper: simForm.level2_upper,
      level2_lower: simForm.level2_lower,
      level3_upper: simForm.level3_upper,
      level3_lower: simForm.level3_lower,
      expected_interval_ms: row.expected_interval_ms,
      confirm_points: row.confirm_points
    })
    ElMessage.success('已将试算阈值正式保存并热更新至实时检测引擎！')
    simDialogVisible.value = false
    await fetchThresholds()
    emit('refresh-global')
  } catch (err) {
    ElMessage.error(`保存试算阈值失败: ${err?.message || ''}`)
  } finally {
    savingFromSim.value = false
  }
}

function formatLim(val) {
  if (val === null || val === undefined) return '-'
  const n = Number(val)
  return Number.isFinite(n) ? Number(n.toFixed(4)) : val
}

function handleResize() {
  if (simChart) simChart.resize()
}

onMounted(() => {
  window.addEventListener('resize', handleResize)
  fetchThresholds()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  if (simChart) {
    simChart.dispose()
    simChart = null
  }
})
</script>

<style scoped>
.threshold-workbench-view {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.workbench-top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  padding: 12px 16px;
}

.wb-stats {
  display: flex;
  align-items: center;
  gap: 24px;
  flex-wrap: wrap;
}

.wb-stat-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.wb-label {
  font-size: 11px;
  color: var(--shm-text-secondary);
}

.wb-stat-item strong {
  font-size: 18px;
}

.green-txt { color: #00e699; }
.cyan-txt { color: #00d4ff; }
.orange-txt { color: #ff8f1f; }
.muted-txt { color: #94a9c9; }

.wb-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.filter-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
}

.filter-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.muted-sub {
  font-size: 11px;
  color: var(--shm-text-secondary);
}

.limit-box {
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
}

.l1-box {
  background: rgba(255, 59, 92, 0.12);
  color: #ff6b81;
  border: 1px solid rgba(255, 59, 92, 0.3);
}

.l2-box {
  background: rgba(255, 143, 31, 0.12);
  color: #fb923c;
  border: 1px solid rgba(255, 143, 31, 0.3);
}

.l3-box {
  background: rgba(250, 219, 20, 0.12);
  color: #facc15;
  border: 1px solid rgba(250, 219, 20, 0.3);
}

.table-footer {
  margin-top: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}

.dialog-footer-split {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

/* Simulator Styles */
.sim-dialog-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.sim-form-grid {
  display: grid;
  grid-template-columns: 1.5fr 1.2fr 0.9fr;
  gap: 12px;
}

.sim-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.sim-field label {
  font-size: 12px;
  color: var(--shm-text-secondary);
  font-weight: 600;
}

.sim-threshold-inputs {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  padding: 10px 12px;
  background: rgba(7, 13, 25, 0.65);
  border: 1px solid var(--shm-border);
  border-radius: 6px;
}

.sim-lvl-group {
  display: flex;
  align-items: center;
  gap: 6px;
}

.lvl-tag {
  font-size: 12px;
  font-weight: 700;
}

.sim-compare-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.sim-cmp-card {
  padding: 10px 12px;
  border-radius: 6px;
  border: 1px solid var(--shm-border);
  background: rgba(17, 29, 53, 0.7);
}

.sim-cmp-card.proposed {
  border-color: #00d4ff;
  background: rgba(0, 212, 255, 0.08);
}

.cmp-title {
  font-size: 12px;
  font-weight: 700;
  margin-bottom: 6px;
  color: var(--shm-text-primary);
}

.cmp-badges {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.c-pill {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 700;
}

.c-pill.red { background: rgba(255, 59, 92, 0.2); color: #ff3b5c; }
.c-pill.orange { background: rgba(255, 143, 31, 0.2); color: #ff8f1f; }
.c-pill.yellow { background: rgba(250, 219, 20, 0.2); color: #facc15; }
.c-pill.total { background: rgba(0, 212, 255, 0.2); color: #00d4ff; }

.sim-chart-box {
  width: 100%;
  height: 310px;
}

/* History Drawer */
.curr-rule-banner {
  background: rgba(0, 212, 255, 0.1);
  border: 1px solid rgba(0, 212, 255, 0.35);
  padding: 10px 12px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
}

.hist-item-card {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--shm-border);
  border-radius: 6px;
  padding: 10px 12px;
}

.hist-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.hist-reason {
  font-size: 12px;
  color: var(--shm-text-secondary);
  margin-bottom: 6px;
}

.hist-vals {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 11px;
  color: #93c5fd;
}

@media (max-width: 1100px) {
  .workbench-top-bar,
  .filter-bar {
    align-items: flex-start;
    flex-direction: column;
  }

  .wb-stats,
  .wb-actions,
  .filter-left,
  .filter-right {
    width: 100%;
  }

  .sim-form-grid {
    grid-template-columns: 1fr 1fr;
  }
}

@media (max-width: 720px) {
  .threshold-workbench-view {
    gap: 10px;
  }

  .wb-stats {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }

  .wb-actions,
  .filter-left {
    align-items: stretch;
    flex-direction: column;
  }

  .wb-actions :deep(.el-button),
  .filter-left :deep(.el-select),
  .filter-left :deep(.el-input),
  .filter-left :deep(.el-button) {
    width: 100% !important;
  }

  .table-footer {
    align-items: flex-start;
    flex-direction: column;
  }

  .sim-form-grid,
  .sim-compare-grid {
    grid-template-columns: 1fr;
  }

  .sim-threshold-inputs {
    align-items: flex-start;
    flex-direction: column;
  }

  .dialog-footer-split {
    align-items: stretch;
    flex-direction: column;
    gap: 10px;
  }
}
</style>
