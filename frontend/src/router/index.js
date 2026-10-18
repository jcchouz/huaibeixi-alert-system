import { createRouter, createWebHashHistory } from 'vue-router'
import AlertCenterView from '../views/AlertCenterView.vue'
import ExtremesDashboardView from '../views/ExtremesDashboardView.vue'
import WindRoseView from '../views/WindRoseView.vue'
import ThresholdWorkbenchView from '../views/ThresholdWorkbenchView.vue'

const routes = [
  {
    path: '/',
    redirect: '/alerts'
  },
  {
    path: '/alerts',
    name: 'AlertCenter',
    component: AlertCenterView,
    meta: { title: '综合预警监控中心' }
  },
  {
    path: '/extremes',
    name: 'ExtremesDashboard',
    component: ExtremesDashboardView,
    meta: { title: '传感器定时最大最小值看板' }
  },
  {
    path: '/wind-rose',
    name: 'WindRose',
    component: WindRoseView,
    meta: { title: '桥梁风玫瑰图与风场分析' }
  },
  {
    path: '/thresholds',
    name: 'ThresholdWorkbench',
    component: ThresholdWorkbenchView,
    meta: { title: '三级阈值调试与历史回放试算台' }
  }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

export default router
