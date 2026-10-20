<template>
  <div class="app-shell">
    <header class="app-header">
      <div class="brand">
        <span class="brand-mark"><el-icon><DataAnalysis /></el-icon></span>
        <div>
          <strong>淮北西站健康监测平台</strong>
          <small>结构监测数据分析与预警管理</small>
        </div>
      </div>

      <nav class="main-nav">
        <router-link to="/analysis">数据分析</router-link>
        <router-link to="/alerts">预警管理</router-link>
      </nav>

      <div class="header-status">
        <span class="status-dot" :class="{ online: commState.pollActive }"></span>
        <span class="poll-label">{{ commState.pollActive ? '5秒轮询正常' : '等待轮询' }}</span>
        <el-button
          text
          :icon="isMuted ? MuteNotification : BellFilled"
          @click="toggleMute"
        >
          {{ isMuted ? '已静音' : '声音开启' }}
        </el-button>
      </div>
    </header>

    <main class="app-content">
      <router-view
        v-slot="{ Component }"
        :last-alert-event="lastAlertEvent"
        @alert-created="handleAlertCreated"
      >
        <component
          :is="Component"
          :last-alert-event="lastAlertEvent"
          @alert-created="handleAlertCreated"
        />
      </router-view>
    </main>

    <GlobalAlertPopup
      ref="popupRef"
      :muted="isMuted"
      @status-change="onStatusChange"
      @alert-arrived="onAlertArrived"
      @alert-acked="onAlertHandled"
    />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { BellFilled, DataAnalysis, MuteNotification } from '@element-plus/icons-vue'
import GlobalAlertPopup from './components/GlobalAlertPopup.vue'
import { alertAudio } from './utils/audio'
import { getOverviewApi } from './api'

const popupRef = ref(null)
const isMuted = ref(alertAudio.isMuted())
const lastAlertEvent = ref(null)
const commState = ref({
  pollActive: false
})

function toggleMute() {
  isMuted.value = alertAudio.toggleMute()
}

function onStatusChange(state) {
  commState.value = state
}

function onAlertArrived(alert) {
  lastAlertEvent.value = { ...alert, _ts: Date.now() }
}

function onAlertHandled() {
  lastAlertEvent.value = { _ts: Date.now(), acked: true }
}

function handleAlertCreated(alert) {
  if (alert) {
    popupRef.value?.processIncomingAlert(alert, { forcePopup: true })
  }
}

onMounted(() => {
  getOverviewApi().catch(() => {})
})
</script>

<style scoped>
.app-shell {
  min-height: 100vh;
  background: var(--shm-bg-primary);
  color: var(--shm-text-primary);
}

.app-header {
  position: sticky;
  top: 0;
  z-index: 1000;
  height: 62px;
  padding: 0 22px;
  display: flex;
  align-items: center;
  gap: 28px;
  background: var(--shm-bg-secondary);
  border-bottom: 1px solid var(--shm-border);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 300px;
}

.brand-mark {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  background: #1d4ed8;
  color: #fff;
}

.brand strong {
  display: block;
  font-size: 15px;
}

.brand small {
  display: block;
  margin-top: 2px;
  color: var(--shm-text-secondary);
  font-size: 11px;
}

.main-nav {
  display: flex;
  align-items: center;
  gap: 8px;
}

.main-nav a {
  padding: 8px 18px;
  border-radius: 6px;
  color: var(--shm-text-secondary);
  text-decoration: none;
  font-size: 14px;
}

.main-nav a.router-link-active {
  background: rgba(29, 78, 216, 0.14);
  color: #2563eb;
  font-weight: 600;
}

.header-status {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--shm-text-secondary);
  font-size: 12px;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #f59e0b;
}

.status-dot.online {
  background: #10b981;
}

.app-content {
  max-width: 1440px;
  margin: 0 auto;
  padding: 20px;
}

@media (max-width: 760px) {
  .app-header {
    height: auto;
    padding: 12px;
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 12px;
  }

  .brand {
    min-width: 0;
    width: 100%;
  }

  .header-status {
    min-width: 0;
    margin-left: 0;
    gap: 4px;
  }

  .poll-label {
    display: none;
  }

  .header-status :deep(.el-button) {
    padding: 4px 6px;
  }

  .app-content {
    padding: 12px;
  }
}
</style>
