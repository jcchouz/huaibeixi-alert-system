import { createRouter, createWebHashHistory } from 'vue-router'
import AlertManagementView from '../views/AlertManagementView.vue'
import AnalysisView from '../views/AnalysisView.vue'

const routes = [
  {
    path: '/',
    redirect: '/analysis'
  },
  {
    path: '/analysis',
    name: 'Analysis',
    component: AnalysisView,
    meta: { title: '数据分析' }
  },
  {
    path: '/alerts',
    name: 'AlertManagement',
    component: AlertManagementView,
    meta: { title: '预警管理' }
  },
  {
    path: '/extremes',
    redirect: '/analysis'
  },
  {
    path: '/wind-rose',
    redirect: '/analysis'
  },
  {
    path: '/thresholds',
    redirect: '/analysis'
  }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

export default router
