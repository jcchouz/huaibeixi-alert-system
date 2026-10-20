<template>
  <div class="alert-management-view">
    <div class="metric-grid">
      <div class="metric-item">
        <span>全部预警</span>
        <strong>{{ summary.total }}</strong>
      </div>
      <div class="metric-item danger">
        <span>未处理</span>
        <strong>{{ summary.unhandled }}</strong>
      </div>
      <div class="metric-item warning">
        <span>已处理</span>
        <strong>{{ summary.handled }}</strong>
      </div>
    </div>

    <section class="panel">
      <div class="toolbar">
        <el-button-group class="category-buttons" aria-label="预警分类">
          <el-button
            :type="filters.alertCategory === 0 ? 'primary' : ''"
            :icon="DataAnalysis"
            @click="selectAlertCategory(0)"
          >
            数据预警
          </el-button>
          <el-button
            :type="filters.alertCategory === 1 ? 'warning' : ''"
            :icon="Monitor"
            @click="selectAlertCategory(1)"
          >
            设备预警
          </el-button>
        </el-button-group>
        <el-input
          v-model="filters.sensorNo"
          class="sensor-filter"
          clearable
          placeholder="传感器编号"
          style="width: 180px"
          @keyup.enter="search"
          @clear="search"
        />
        <el-select
          v-model="filters.alertType"
          class="filter-select"
          clearable
          placeholder="预警类型"
          style="width: 160px"
        >
          <el-option v-for="item in alertTypes" :key="item" :label="item" :value="item" />
        </el-select>
        <el-select
          v-model="filters.alertStatus"
          class="filter-select"
          clearable
          placeholder="处理状态"
          style="width: 140px"
        >
          <el-option label="未处理" :value="0" />
          <el-option label="已处理" :value="1" />
        </el-select>
        <el-button class="query-button" :icon="Search" @click="search">查询</el-button>
        <el-button class="reset-button" :icon="Refresh" @click="reset">重置</el-button>
        <span class="toolbar-spacer"></span>
        <el-button
          class="ack-all-button"
          type="success"
          plain
          :icon="Finished"
          :loading="ackAllLoading"
          @click="handleAllAlerts"
        >
          一键处理{{ currentCategoryLabel }}
        </el-button>
        <el-button
          class="create-alert-button"
          type="primary"
          :icon="Plus"
          @click="createVisible = true"
        >
          新增预警
        </el-button>
      </div>

      <el-table v-loading="loading" :data="rows" border size="small">
        <el-table-column prop="seq" label="序号" width="70" align="center" />
        <el-table-column label="等级" width="80" align="center">
          <template #default="{ row }">
            <el-tag :type="row.alert_level === 1 ? 'danger' : row.alert_level === 2 ? 'warning' : 'info'">
              L{{ row.alert_level }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="alert_type" label="预警类型" width="120" />
        <el-table-column label="传感器" min-width="160">
          <template #default="{ row }">
            <div>{{ row.sensor_name || row.sensor_no }}</div>
            <span class="muted">{{ row.sensor_no }}</span>
          </template>
        </el-table-column>
        <el-table-column label="通道" width="90" align="center">
          <template #default="{ row }">
            <span>{{ row.channel || '默认通道' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="trigger_val" label="触发值" width="90" align="right" />
        <el-table-column label="状态" width="80" align="center">
          <template #default="{ row }">
            <el-tag :type="statusType(row.alert_status)">{{ statusText(row.alert_status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="首次触发" width="150" align="center">
          <template #default="{ row }">
            <div class="time-stack mono-num">
              <span>{{ datePart(row.create_time) }}</span>
              <span class="time-sub">{{ timePart(row.create_time) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="最后更新" width="150" align="center">
          <template #default="{ row }">
            <div class="time-stack mono-num">
              <span>{{ datePart(row.update_time) }}</span>
              <span class="time-sub">{{ timePart(row.update_time) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="处理人" width="100" align="center">
          <template #default="{ row }">
            {{ row.handle_by || '-' }}
          </template>
        </el-table-column>
        <el-table-column label="处理人电话" width="130" align="center">
          <template #default="{ row }">
            {{ row.handle_phone || '-' }}
          </template>
        </el-table-column>
        <el-table-column label="处理时间" width="150" align="center">
          <template #default="{ row }">
            <div v-if="row.handle_time" class="time-stack mono-num">
              <span>{{ datePart(row.handle_time) }}</span>
              <span class="time-sub">{{ timePart(row.handle_time) }}</span>
            </div>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column
          prop="handle_opinion"
          label="处理意见"
          min-width="160"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            {{ row.handle_opinion || '-' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" align="center">
          <template #default="{ row }">
            <el-button
              v-if="row.alert_status === 0"
              size="small"
              type="warning"
              plain
              :icon="Check"
              @click="handleAlert(row)"
            >
              处理
            </el-button>
            <el-button size="small" type="danger" plain :icon="Delete" @click="removeAlert(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          layout="total, sizes, prev, pager, next"
          :page-sizes="[10, 20, 50]"
          :total="total"
          @current-change="loadAlerts"
          @size-change="search"
        />
      </div>
    </section>

    <el-dialog v-model="createVisible" class="responsive-dialog" title="新增预警" width="480px">
      <el-form :model="createForm" label-width="90px">
        <el-form-item label="传感器编号">
          <el-input v-model="createForm.sensor_no" placeholder="例如 stn-12-b" />
        </el-form-item>
        <el-form-item label="通道">
          <el-input v-model="createForm.channel" placeholder="例如 stress，可留空" />
        </el-form-item>
        <el-form-item label="预警类型">
          <el-select v-model="createForm.alert_type" style="width: 100%">
            <el-option v-for="item in alertTypes" :key="item" :label="item" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item label="预警等级">
          <el-radio-group v-model="createForm.alert_level">
            <el-radio-button :value="1">一级</el-radio-button>
            <el-radio-button :value="2">二级</el-radio-button>
            <el-radio-button :value="3">三级</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="触发值">
          <el-input-number v-model="createForm.trigger_val" :precision="5" :controls="false" style="width: 100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="handleDialogVisible"
      class="responsive-dialog"
      title="处理预警"
      width="520px"
    >
      <el-form :model="handleForm" label-width="100px">
        <el-form-item label="处理人">
          <el-input v-model="handleForm.handle_by" placeholder="请输入处理人" />
        </el-form-item>
        <el-form-item label="处理人电话">
          <el-input v-model="handleForm.handle_phone" placeholder="请输入联系电话" />
        </el-form-item>
        <el-form-item label="处理意见">
          <el-input
            v-model="handleForm.handle_opinion"
            type="textarea"
            :rows="4"
            placeholder="可留空"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="handleDialogVisible = false">取消</el-button>
        <el-button type="warning" :loading="handleSubmitting" @click="submitHandle">
          确认处理
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Check,
  DataAnalysis,
  Delete,
  Finished,
  Monitor,
  Plus,
  Refresh,
  Search
} from '@element-plus/icons-vue'
import {
  ackAlertApi,
  ackAllAlertsApi,
  createAlertApi,
  deleteAlertApi,
  listAlertsApi
} from '../api'
import { formatDateTimeWithMs } from '../utils/time'

const props = defineProps({
  lastAlertEvent: {
    type: Object,
    default: null
  }
})

const emit = defineEmits(['alert-created'])

const POLL_INTERVAL_MS = 5000
const alertTypes = ['阈值超限', '传感器离线', '采集仪离线', '传感器死值', '超物理量程']
const loading = ref(false)
const ackAllLoading = ref(false)
const submitting = ref(false)
const handleSubmitting = ref(false)
const createVisible = ref(false)
const handleDialogVisible = ref(false)
const handleTarget = ref(null)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = reactive({
  sensorNo: '',
  alertType: '',
  alertStatus: null,
  alertCategory: 0
})
const summary = reactive({
  total: 0,
  unhandled: 0,
  handled: 0
})
const createForm = reactive({
  sensor_no: 'stn-12-b',
  channel: 'stress',
  alert_type: '阈值超限',
  alert_level: 1,
  trigger_val: -11.2
})
const handleForm = reactive({
  handle_by: 'web',
  handle_phone: '',
  handle_opinion: ''
})
const currentCategoryLabel = computed(() =>
  filters.alertCategory === 0 ? '数据预警' : '设备预警'
)
let pollTimer = null
let requestInFlight = false

async function loadAlerts(silent = false) {
  if (requestInFlight) return
  requestInFlight = true
  loading.value = true
  try {
    const params = {
      page: page.value,
      pageSize: pageSize.value,
      alertCategory: filters.alertCategory
    }
    if (filters.sensorNo) params.sensorNo = filters.sensorNo
    if (filters.alertType) params.alertType = filters.alertType
    if (filters.alertStatus !== null && filters.alertStatus !== '') {
      params.alertStatus = filters.alertStatus
    }
    const data = await listAlertsApi(params)
    rows.value = Array.isArray(data.items) ? data.items : []
    total.value = Number(data.total || 0)
    const counts = data.statusCounts || {}
    summary.total = Number(counts.total || 0)
    summary.unhandled = Number(counts.unhandled || 0)
    summary.handled = Number(counts.handled || 0)
  } catch (err) {
    if (silent !== true) {
      ElMessage.error(err?.message || '加载预警失败')
    }
  } finally {
    loading.value = false
    requestInFlight = false
  }
}

function search() {
  page.value = 1
  loadAlerts()
}

function selectAlertCategory(category) {
  if (filters.alertCategory === category) return
  filters.alertCategory = category
  search()
}

function reset() {
  filters.sensorNo = ''
  filters.alertType = ''
  filters.alertStatus = null
  search()
}

async function submitCreate() {
  if (!createForm.sensor_no.trim()) {
    ElMessage.warning('请输入传感器编号')
    return
  }
  submitting.value = true
  try {
    const alert = await createAlertApi(createForm)
    ElMessage.success('预警创建成功')
    createVisible.value = false
    await loadAlerts()
    emit('alert-created', alert)
  } catch (err) {
    ElMessage.error(err?.message || '创建失败')
  } finally {
    submitting.value = false
  }
}

function handleAlert(row) {
  handleTarget.value = row
  handleForm.handle_by = 'web'
  handleForm.handle_phone = ''
  handleForm.handle_opinion = ''
  handleDialogVisible.value = true
}

async function submitHandle() {
  if (!handleTarget.value?.id) return
  if (!handleForm.handle_by.trim()) {
    ElMessage.warning('请输入处理人')
    return
  }
  handleSubmitting.value = true
  try {
    await ackAlertApi(handleTarget.value.id, {
      handleBy: handleForm.handle_by.trim(),
      handlePhone: handleForm.handle_phone.trim(),
      handleOpinion: handleForm.handle_opinion.trim()
    })
    ElMessage.success('已处理')
    handleDialogVisible.value = false
    handleTarget.value = null
    await loadAlerts()
  } catch (err) {
    ElMessage.error(err?.message || '更新失败')
  } finally {
    handleSubmitting.value = false
  }
}

async function handleAllAlerts() {
  ackAllLoading.value = true
  let unhandledCount = 0
  try {
    const overview = await listAlertsApi({
      page: 1,
      pageSize: 1,
      alertCategory: filters.alertCategory
    })
    unhandledCount = Number(overview.statusCounts?.unhandled || 0)
  } catch (err) {
    ackAllLoading.value = false
    ElMessage.error(err?.message || '获取未处理预警数量失败')
    return
  }

  if (unhandledCount === 0) {
    ackAllLoading.value = false
    ElMessage.info('当前没有未处理预警')
    return
  }

  try {
    await ElMessageBox.confirm(
      `确定一键处理全部 ${unhandledCount} 条${currentCategoryLabel.value}吗？`,
      `一键处理${currentCategoryLabel.value}`,
      {
        type: 'warning',
        confirmButtonText: '全部处理',
        cancelButtonText: '取消'
      }
    )
  } catch (err) {
    ackAllLoading.value = false
    if (err === 'cancel' || err === 'close') return
    ElMessage.error(err?.message || '处理失败')
    return
  }

  try {
    const result = await ackAllAlertsApi({
      alertCategory: filters.alertCategory
    })
    const count = Number(result.ackedCount || 0)
    ElMessage.success(`已一键处理 ${count} 条${currentCategoryLabel.value}`)
    await loadAlerts()
  } catch (err) {
    ElMessage.error(err?.message || '一键处理失败')
  } finally {
    ackAllLoading.value = false
  }
}

async function removeAlert(row) {
  try {
    await ElMessageBox.confirm(`确定删除预警 #${row.seq}？`, '删除预警', { type: 'warning' })
    await deleteAlertApi(row.id)
    ElMessage.success('已删除')
    await loadAlerts()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(err?.message || '删除失败')
  }
}

function statusText(status) {
  if (Number(status) === 0) return '未处理'
  if (Number(status) === 1) return '已处理'
  return '未知'
}

function statusType(status) {
  if (Number(status) === 0) return 'danger'
  if (Number(status) === 1) return 'warning'
  return 'info'
}

function datePart(value) {
  const formatted = formatDateTimeWithMs(value)
  return formatted === '-' ? '-' : formatted.split(' ')[0]
}

function timePart(value) {
  const formatted = formatDateTimeWithMs(value)
  const parts = formatted === '-' ? [] : formatted.split(' ')
  return parts.length > 1 ? parts.slice(1).join(' ') : ''
}

watch(
  () => props.lastAlertEvent,
  () => loadAlerts()
)

onMounted(() => {
  loadAlerts()
  pollTimer = setInterval(() => loadAlerts(true), POLL_INTERVAL_MS)
})

onBeforeUnmount(() => {
  clearInterval(pollTimer)
})
</script>

<style scoped>
.alert-management-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
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

.metric-item.warning strong {
  color: #f59e0b;
}

.metric-item.success strong {
  color: #10b981;
}

.panel {
  min-width: 0;
  padding: 16px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.toolbar-spacer {
  flex: 1;
}

.category-buttons {
  margin-right: 4px;
}

.category-buttons :deep(.el-button) {
  min-width: 92px;
}

.muted {
  color: var(--shm-text-secondary);
  font-size: 12px;
}

.time-stack {
  display: flex;
  flex-direction: column;
  align-items: center;
  line-height: 1.45;
  white-space: nowrap;
}

.time-sub {
  color: var(--shm-text-secondary);
  font-size: 11px;
}

.pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}

@media (max-width: 900px) {
  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .alert-management-view {
    gap: 12px;
  }

  .metric-grid {
    grid-template-columns: 1fr;
    gap: 8px;
  }

  .metric-item {
    padding: 12px 14px;
  }

  .panel {
    padding: 12px;
  }

  .toolbar {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    align-items: stretch;
    gap: 8px;
  }

  .toolbar > * {
    min-width: 0;
  }

  .toolbar-spacer {
    display: none;
  }

  .category-buttons {
    grid-column: 1 / -1;
    display: flex;
    width: 100%;
    margin-right: 0;
  }

  .category-buttons :deep(.el-button) {
    flex: 1;
    min-width: 0;
    margin-left: 0;
  }

  .sensor-filter {
    grid-column: 1 / -1;
  }

  .sensor-filter,
  .filter-select,
  .query-button,
  .reset-button {
    width: 100% !important;
  }

  .query-button,
  .reset-button,
  .ack-all-button,
  .create-alert-button {
    margin-left: 0 !important;
  }

  .ack-all-button,
  .create-alert-button {
    grid-column: 1 / -1;
    width: 100%;
  }

  .pagination {
    justify-content: center;
    overflow-x: auto;
    padding-bottom: 4px;
  }

  .pagination :deep(.el-pagination) {
    flex-wrap: wrap;
    justify-content: center;
    row-gap: 8px;
  }
}
</style>
