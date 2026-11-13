<template>
  <section class="page" data-module="alertticket">
    <header class="page-head">
      <div>
        <h2>告警处置闭环</h2>
        <p class="page-desc">
          指标连续满足持续周期才触发；同一任务、指标与异常类型恢复前只保留一张告警单，
          重复触发只更新最后发生时间；恢复后单向流转到已关闭，并与诊断报告跨模块联动。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出告警单清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <section class="sim-panel">
      <h3>采集周期指标上报（模拟）</h3>
      <p class="page-desc">
        按 ALERT-06 口径：失败条数 大于 50、持续 3 个周期、严重、通知值班渠道、静默 15 分钟。
      </p>
      <form class="filter-bar" @submit.prevent="ingest">
        <label class="filter-item">
          <span>任务编号</span>
          <input v-model="simForm.task_code" placeholder="如 TASK-1042" />
        </label>
        <label class="filter-item">
          <span>监控指标</span>
          <input v-model="simForm.metric" placeholder="如 失败条数" />
        </label>
        <label class="filter-item">
          <span>本周期值</span>
          <input v-model="simForm.value" type="number" />
        </label>
        <label class="filter-item">
          <span>异常类型</span>
          <input v-model="simForm.exception_type" placeholder="如 HTTP 503" />
        </label>
        <label class="filter-item">
          <span>影响行数</span>
          <input v-model="simForm.affected_rows" type="number" />
        </label>
        <button class="btn primary" type="submit">上报本周期</button>
        <button class="btn ghost" type="button" @click="simRecover">模拟指标恢复</button>
      </form>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>告警单号/规则编号</span>
        <input v-model="filters.keyword" placeholder="按关键字检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>关联任务</span>
        <input v-model="filters.task_code" placeholder="如 TASK-1042" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <button
              v-if="column === '关联诊断报告' && row[column]"
              class="link"
              type="button"
              @click="openDiagnosis(row[column])"
            >
              {{ row[column] }}
            </button>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row.status)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <span v-if="!availableActions(row.status).length" class="page-desc">已闭环</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无告警单，指标连续越阈后会自动生成</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 张告警单</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="lastMessage" class="ok-text">{{ lastMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/alertticket'
const route = useRoute()
const router = useRouter()

const columns = [
  '告警单号', '规则编号', '关联任务', '监控指标', '异常类型', '告警级别',
  '首次发生时间', '最后发生时间', '恢复时间', '连续周期数', '触发次数',
  '影响行数', '关联诊断报告', '处理结论', 'status',
]
const statuses = ['待处理', '处理中', '已关闭']
const ACTION_BY_STATUS: Record<string, string[]> = {
  待处理: ['认领处置', '关闭告警单'],
  处理中: ['关闭告警单'],
  已关闭: [],
}

const rows = ref<Row[]>([])
const total = ref(0)
const stats = ref<Record<string, number>>({ total: 0, pending: 0, processing: 0, closed: 0 })
const errorMessage = ref('')
const lastMessage = ref('')
const filters = reactive({
  keyword: typeof route.query.keyword === 'string' ? route.query.keyword : '',
  status: '',
  task_code: '',
})
const simForm = reactive({
  task_code: 'TASK-1042',
  metric: '失败条数',
  value: 60,
  exception_type: 'HTTP 503',
  affected_rows: 100,
})
let eventSeq = 0

const statCards = computed(() => [
  { label: '告警单总数', value: stats.value.total ?? 0 },
  { label: '待处理', value: stats.value.pending ?? 0 },
  { label: '处理中', value: stats.value.processing ?? 0 },
  { label: '已关闭', value: stats.value.closed ?? 0 },
])

function availableActions(status: string | number | null): string[] {
  return ACTION_BY_STATUS[String(status ?? '')] ?? []
}

function resetFilters() {
  filters.keyword = ''
  filters.status = ''
  filters.task_code = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openDiagnosis(reportCode: string | number | null) {
  void router.push({ path: '/diagnose', query: { linked_alert: '', keyword: String(reportCode ?? '') } })
}

async function ingest() {
  errorMessage.value = ''
  eventSeq += 1
  try {
    const response = await request(`${ENDPOINT}/ingest`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          event_id: `ui-${Date.now()}-${eventSeq}`,
          task_code: simForm.task_code,
          metric: simForm.metric,
          value: Number(simForm.value),
          exception_type: simForm.exception_type,
          affected_rows: Number(simForm.affected_rows),
          stack_trace: `${simForm.exception_type} 触发于 ${simForm.task_code} 采集周期`,
        },
      }),
    })
    const body = await response.json()
    if (!response.ok || !body.ok) {
      throw new Error(body.message || '上报未被接受')
    }
    lastMessage.value = body.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标上报失败'
  }
}

async function simRecover() {
  errorMessage.value = ''
  eventSeq += 1
  try {
    const response = await request(`${ENDPOINT}/ingest`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          event_id: `ui-${Date.now()}-${eventSeq}`,
          task_code: simForm.task_code,
          metric: simForm.metric,
          value: 0,
          exception_type: simForm.exception_type,
          affected_rows: 0,
        },
      }),
    })
    const body = await response.json()
    lastMessage.value = body.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复上报失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  let conclusion: string | undefined
  if (action === '关闭告警单') {
    conclusion = window.prompt('请输入处置结论（可留空，系统会补默认结论）') ?? undefined
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action, conclusion } }),
    })
    const body = await response.json()
    if (!response.ok || !body.ok) {
      throw new Error(body.message || '动作未生效')
    }
    lastMessage.value = body.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '告警单操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(
    Object.entries(filters).filter(([, v]) => v) as [string, string][],
  ).toString()
  try {
    const [listResp, statsResp] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/stats`),
    ])
    if (!listResp.ok) {
      throw new Error('告警单列表读取失败')
    }
    const payload = await listResp.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = statsResp.ok ? await statsResp.json() : stats.value
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '告警单列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.sim-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 12px;
}
.sim-panel h3 {
  margin: 0 0 4px;
  font-size: 15px;
}
.ok-text {
  color: #067647;
}
</style>
