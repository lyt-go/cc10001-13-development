<template>
  <section class="page" data-module="diagnose">
    <header class="page-head">
      <div>
        <h2>诊断记录管理</h2>
        <p class="page-desc">
          告警单进入待处理时自动生成关联报告，记录影响行数、异常堆栈与修复建议；
          报告结案后处理结论固化，不会被下一次重复告警覆盖。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出诊断记录清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>报告编号</span>
        <input v-model="filters.keyword" placeholder="按报告编号检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>关联告警单</span>
        <input v-model="filters.linked_alert" placeholder="按告警单号检索" />
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
          <td v-for="column in columns" :key="column" :title="column === '异常堆栈' ? String(row[column] ?? '') : ''">
            <button
              v-if="column === '关联告警单' && row[column]"
              class="link"
              type="button"
              @click="openTicket(row[column])"
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
            <span v-if="!availableActions(row.status).length" class="page-desc">结论已固化</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无诊断记录，告警触发后会自动生成关联报告</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条诊断记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="lastMessage" class="ok-text">{{ lastMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/diagnose'
const route = useRoute()
const router = useRouter()

const columns = [
  '报告编号', '关联任务', '异常类型', '异常堆栈', '发生时间', '影响行数',
  '根因分类', '修复建议', '关联告警单', '处理结论', 'status',
]
const statuses = ['草稿', '分析中', '已结案', '已归档']
const ACTION_BY_STATUS: Record<string, string[]> = {
  草稿: ['开始分析'],
  分析中: ['提交结论', '标记结案', '归档报告'],
  已结案: ['归档报告'],
  已归档: [],
}

const rows = ref<Row[]>([])
const total = ref(0)
const stats = ref([
  { label: '诊断报告总数', value: 0 },
  { label: '分析中报告', value: 0 },
  { label: '已结案报告', value: 0 },
])
const errorMessage = ref('')
const lastMessage = ref('')
const filters = reactive({
  keyword: typeof route.query.keyword === 'string' ? route.query.keyword : '',
  status: '',
  linked_alert: typeof route.query.linked_alert === 'string' ? route.query.linked_alert : '',
})

function availableActions(status: string | number | null): string[] {
  return ACTION_BY_STATUS[String(status ?? '')] ?? []
}

function resetFilters() {
  filters.keyword = ''
  filters.status = ''
  filters.linked_alert = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openTicket(ticketCode: string | number | null) {
  void router.push({ path: '/alertticket', query: { keyword: String(ticketCode ?? '') } })
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  let conclusion: string | undefined
  if (action === '提交结论') {
    conclusion = window.prompt('请输入诊断处理结论（结案后不可覆盖）') ?? undefined
    if (!conclusion) {
      errorMessage.value = '提交结论必须填写处理结论'
      return
    }
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action, conclusion } }),
    })
    const body = await response.json()
    if (!response.ok || !body.ok) {
      throw new Error(body.message || '诊断动作未生效')
    }
    lastMessage.value = body.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '诊断记录操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(
    Object.entries(filters).filter(([, v]) => v) as [string, string][],
  ).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('诊断记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '诊断报告总数', value: total.value },
      { label: '分析中报告', value: rows.value.filter((r) => r.status === '分析中').length },
      { label: '已结案报告', value: rows.value.filter((r) => r.status === '已结案' || r.status === '已归档').length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '诊断记录列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.ok-text {
  color: #067647;
}
</style>
