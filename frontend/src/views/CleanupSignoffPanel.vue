<template>
  <section class="signoff-panel" data-testid="cleanup-signoff-panel">
    <header class="signoff-head">
      <div>
        <h3>物理分区签认治理</h3>
        <p class="page-desc">
          按实际物理分区核对独占/共同归属的有效签认范围，共同分区需每个所有方对同一用途签认后方可删除。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="refresh-scope" @click="reload">刷新签认范围</button>
        <button class="btn primary" type="button" data-testid="execute-cleanup" @click="onExecute">
          按有效签认范围确认删除
        </button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">到期物理分区</span>
        <strong class="stat-value">{{ scope?.total_physical_partitions ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">有效签认范围内</span>
        <strong class="stat-value">{{ scope?.effective_scope_count ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">签认缺失被拦截</span>
        <strong class="stat-value">{{ scope?.blocked_count ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已完成（不重复安排）</span>
        <strong class="stat-value">{{ scope?.completed_count ?? '—' }}</strong>
      </article>
    </div>

    <table v-if="scope" class="data-table">
      <thead>
        <tr>
          <th>物理分区</th>
          <th>归属</th>
          <th>相关所有方签认</th>
          <th>有效签认范围</th>
          <th>缺失签认 / 操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="partition in scope.partitions" :key="partition.partition_id"
            :data-testid="`scope-row-${partition.partition_id}`"
            :data-scope="partition.already_completed ? 'completed' : (partition.in_effective_scope ? 'effective' : 'blocked')">
          <td>
            <div>{{ partition.partition_path }}</div>
            <small>数据集 {{ partition.dataset }} · 获准用途：{{ partition.allowed_purposes.join('、') }}</small>
          </td>
          <td>
            <span class="tag">{{ partition.ownership }}</span>
            <div><small>{{ partition.owners.join('、') }}</small></div>
          </td>
          <td>
            <div v-for="check in partition.owner_checks" :key="check.owner" class="owner-check">
              <span :class="['dot', check.has_valid_signoff ? 'ok' : 'bad']"></span>
              <strong>{{ check.owner }}</strong>
              <small>{{ check.signoff_status }}<template v-if="check.withdrawn">（已撤回）</template><template v-else-if="check.conflict">（授权冲突）</template></small>
              <template v-if="check.signoff_id">
                <button class="link" type="button"
                  :data-testid="`conflict-${check.signoff_id}`"
                  @click="onConflict(check.signoff_id!)">标记冲突</button>
                <button class="link danger" type="button"
                  :data-testid="`revoke-${check.signoff_id}`"
                  @click="onRevoke(check.signoff_id!)">撤回</button>
              </template>
            </div>
          </td>
          <td>
            <span v-if="partition.already_completed" class="tag done">已完成</span>
            <span v-else-if="partition.in_effective_scope" class="tag ok">可删除</span>
            <span v-else class="tag bad">拦截</span>
          </td>
          <td>
            <ul v-if="partition.missing_signoffs.length" class="missing-list">
              <li v-for="(missing, idx) in partition.missing_signoffs" :key="idx">
                {{ missing.owner }}：{{ missing.reason }}
              </li>
            </ul>
            <span v-else-if="!partition.already_completed" class="ok-text">签认齐全</span>
            <span v-else class="muted">不再进入下一份计划</span>
          </td>
        </tr>
      </tbody>
    </table>

    <section class="record-section">
      <h4>执行记录核对</h4>
      <p v-if="!records?.execution_count" class="muted">暂无执行记录。</p>
      <article v-for="record in records?.records ?? []" :key="record.execution_id" class="record-card"
               :data-testid="`record-${record.execution_id}`">
        <header>
          <strong>第 {{ record.execution_id }} 次执行 · {{ record.executed_at }}</strong>
          <span>已删除 {{ record.executed_count }} 个 / 拦截 {{ record.blocked_count }} 个
            · {{ record.deleted_rows }} 行 · 释放 {{ record.freed_space_gb }} GB</span>
        </header>
        <div class="record-cols">
          <div>
            <h5 class="ok-text">已确认并执行的分区</h5>
            <ul>
              <li v-for="item in record.executed_partitions" :key="item.partition_id">
                {{ item.partition_path }}（{{ item.ownership }}，签认ID {{ item.validated_signoff_ids.join('、') }}）
              </li>
              <li v-if="!record.executed_partitions.length" class="muted">无</li>
            </ul>
          </div>
          <div>
            <h5 class="error-text">因签认缺失未执行的分区</h5>
            <ul>
              <li v-for="item in record.blocked_partitions" :key="item.partition_id">
                {{ item.partition_path }}
                <ul>
                  <li v-for="(missing, idx) in item.missing_signoffs" :key="idx">
                    {{ missing.owner }}：{{ missing.reason }}
                  </li>
                </ul>
              </li>
              <li v-if="!record.blocked_partitions.length" class="muted">无</li>
            </ul>
          </div>
        </div>
      </article>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  executeCleanup,
  fetchExecutionRecords,
  fetchSignoffScope,
  markSignoffConflict,
  revokeSignoff,
  type ExecutionRecord,
  type SignoffScope,
} from '@/api/backend-service'

const props = defineProps<{ jobId: number }>()

const scope = ref<SignoffScope | null>(null)
const records = ref<{ ok: boolean; execution_count: number; records: ExecutionRecord[] } | null>(null)
const errorMessage = ref('')

async function reload() {
  errorMessage.value = ''
  try {
    const [scopeData, recordData] = await Promise.all([
      fetchSignoffScope(props.jobId),
      fetchExecutionRecords(props.jobId),
    ])
    scope.value = scopeData
    records.value = recordData
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '签认数据读取失败'
  }
}

async function onRevoke(signoffId: number) {
  errorMessage.value = ''
  try {
    const result = await revokeSignoff(props.jobId, signoffId)
    if (!result.ok) throw new Error(result.message as string)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '撤回失败'
  }
}

async function onConflict(signoffId: number) {
  errorMessage.value = ''
  try {
    const result = await markSignoffConflict(props.jobId, signoffId, '预览后所有方授权范围存在冲突')
    if (!result.ok) throw new Error(result.message as string)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '冲突标记失败'
  }
}

async function onExecute() {
  errorMessage.value = ''
  try {
    const result = await executeCleanup(props.jobId, true)
    if (!result.ok) throw new Error(result.message as string)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '执行失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.signoff-panel {
  margin-top: 24px;
  padding: 20px;
  border: 1px solid var(--border, #e3e6ee);
  border-radius: 12px;
  background: #fff;
}
.signoff-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 12px;
}
.owner-check {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 4px 0;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.dot.ok {
  background: #16a34a;
}
.dot.bad {
  background: #dc2626;
}
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  background: #eef2ff;
  color: #3730a3;
  font-size: 12px;
}
.tag.ok {
  background: #dcfce7;
  color: #166534;
}
.tag.bad {
  background: #fee2e2;
  color: #991b1b;
}
.tag.done {
  background: #f1f5f9;
  color: #475569;
}
.missing-list {
  margin: 0;
  padding-left: 16px;
  color: #b91c1c;
}
.link.danger {
  color: #dc2626;
}
.ok-text {
  color: #166534;
}
.error-text {
  color: #b91c1c;
}
.muted {
  color: #64748b;
}
.record-section {
  margin-top: 20px;
}
.record-card {
  border: 1px solid var(--border, #e3e6ee);
  border-radius: 10px;
  padding: 14px 16px;
  margin: 12px 0;
}
.record-card header {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.record-cols {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.record-cols ul {
  margin: 4px 0;
  padding-left: 18px;
}
</style>
