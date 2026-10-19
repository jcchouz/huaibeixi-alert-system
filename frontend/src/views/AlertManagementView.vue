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
        <el-input
          v-model="filters.sensorNo"
          clearable
          placeholder="传感器编号"
          style="width: 180px"
          @keyup.enter="search"
          @clear="search"
        />
        <el-select v-model="filters.alertType" clearable placeholder="预警类型" style="width: 160px">
          <el-option v-for="item in alertTypes" :key="item" :label="item" :value="item" />
        </el-select>
        <el-select v-model="filters.alertStatus" clearable placeholder="处理状态" style="width: 140px">
          <el-option label="未处理" :value="0" />
          <el-option label="已处理" :value="1" />
        </el-select>
        <el-button :icon="Search" @click="search">查询</el-button>
        <el-button :icon="Refresh" @click="reset">重置</el-button>
        <span class="toolbar-spacer"></span>
        <el-button type="primary" :icon="Plus" @click="createVisible = true">新增预警</el-button>
      </div>

      <el-table v-loading="loading" :data="rows" border size="small">
        <el-table-column prop="seq" label="序号" width="80" align="center" />
        <el-table-column label="等级" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="row.alert_level === 1 ? 'danger' : row.alert_level === 2 ? 'warning' : 'info'">
              L{{ row.alert_level }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="alert_type" label="预警类型" width="130" />
        <el-table-column label="传感器" min-width="170">
          <template #default="{ row }">
            <div>{{ row.sensor_name || row.sensor_no }}</div>
            <span class="muted">{{ row.sensor_no }}</span>
          </template>
        </el-table-column>
        <el-table-column label="通道" width="110" align="center">
          <template #default="{ row }">
            <span>{{ row.channel || '默认通道' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="direction" label="越限方向" width="110" align="center" />
        <el-table-column prop="trigger_val" label="触发值" width="110" align="right" />
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="statusType(row.alert_status)">{{ statusText(row.alert_status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="create_time" label="触发时间" min-width="170" />
        <el-table-column label="操作" width="250" fixed="right" align="center">
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

    <el-dialog v-model="createVisible" title="新增预警" width="480px">
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
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Delete, Plus, Refresh, Search } from '@element-plus/icons-vue'
import {
  ackAlertApi,
  createAlertApi,
  deleteAlertApi,
  listAlertsApi
} from '../api'

const props = defineProps({
  lastAlertEvent: {
    type: Object,
    default: null
  }
})

const emit = defineEmits(['alert-created'])

const alertTypes = ['阈值超限', '传感器离线', '采集仪离线', '传感器死值', '超物理量程']
const loading = ref(false)
const submitting = ref(false)
const createVisible = ref(false)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = reactive({
  sensorNo: '',
  alertType: '',
  alertStatus: null
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

async function loadAlerts() {
  loading.value = true
  try {
    const params = { page: page.value, pageSize: pageSize.value }
    if (filters.sensorNo) params.sensorNo = filters.sensorNo
    if (filters.alertType) params.alertType = filters.alertType
    if (filters.alertStatus !== null && filters.alertStatus !== '') {
      params.alertStatus = filters.alertStatus
    }
    const data = await listAlertsApi(params)
    rows.value = Array.isArray(data.items) ? data.items : []
    total.value = Number(data.total || 0)
    updateSummary()
  } catch (err) {
    ElMessage.error(err?.message || '加载预警失败')
  } finally {
    loading.value = false
  }
}

function updateSummary() {
  summary.total = total.value
  summary.unhandled = rows.value.filter((item) => item.alert_status === 0).length
  summary.handled = rows.value.filter((item) => item.alert_status === 1).length
}

function search() {
  page.value = 1
  loadAlerts()
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

async function handleAlert(row) {
  try {
    await ackAlertApi(row.id, { ack_user: 'local-admin' })
    ElMessage.success('已处理')
    await loadAlerts()
  } catch (err) {
    ElMessage.error(err?.message || '更新失败')
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

watch(
  () => props.lastAlertEvent,
  () => loadAlerts()
)

onMounted(loadAlerts)
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

.muted {
  color: var(--shm-text-secondary);
  font-size: 12px;
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
</style>
