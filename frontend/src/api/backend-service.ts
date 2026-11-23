import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------------------------------------------------------------------------
// 清理作业：物理分区多方签认治理
// ---------------------------------------------------------------------------
export type SignoffScope = {
  ok: boolean
  message?: string
  job_id: number
  purpose: string
  total_physical_partitions: number
  effective_scope_count: number
  blocked_count: number
  completed_count: number
  partitions: Array<{
    partition_id: number
    partition_path: string
    dataset: string
    ownership: string
    owners: string[]
    allowed_purposes: string[]
    purpose_allowed: boolean
    owner_checks: Array<{
      owner: string
      has_valid_signoff: boolean
      signoff_id: number | null
      signoff_status: string
      conflict: boolean
      withdrawn: boolean
    }>
    in_effective_scope: boolean
    missing_signoffs: Array<{ owner: string; purpose: string; reason: string }>
    already_completed: boolean
  }>
  effective_partitions: unknown[]
  blocked_partitions: unknown[]
}

export type ExecutionRecord = {
  execution_id: number
  executed_at: string
  executed_partitions: Array<{
    partition_id: number
    partition_path: string
    ownership: string
    owners: string[]
    purpose: string
    validated_signoff_ids: number[]
  }>
  blocked_partitions: Array<{
    partition_id: number
    partition_path: string
    ownership: string
    owners: string[]
    missing_signoffs: Array<{ owner: string; reason: string }>
  }>
  executed_count: number
  blocked_count: number
  deleted_rows: number
  freed_space_gb: number
}

export type CleanupActionResult = {
  ok: boolean
  message?: string
  [key: string]: unknown
}

async function cleanupGet<T>(id: number, segment: string): Promise<T> {
  const response = await request(`/api/cleanup_job/${id}/${segment}`)
  if (!response.ok) throw new Error('清理作业治理数据读取失败')
  return (await response.json()) as T
}

async function cleanupPost<T = CleanupActionResult>(
  id: number,
  segment: string,
  body: unknown = {},
): Promise<T> {
  const response = await request(`/api/cleanup_job/${id}/${segment}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return (await response.json()) as T
}

export function fetchSignoffScope(id: number): Promise<SignoffScope> {
  return cleanupGet<SignoffScope>(id, 'signoff-scope')
}

export function fetchExecutionRecords(
  id: number,
): Promise<{ ok: boolean; execution_count: number; records: ExecutionRecord[] }> {
  return cleanupGet(id, 'execution-records')
}

export function revokeSignoff(id: number, signoffId: number) {
  return cleanupPost(id, `signoffs/${signoffId}/revoke`)
}

export function markSignoffConflict(id: number, signoffId: number, remark: string) {
  return cleanupPost(id, `signoffs/${signoffId}/conflict`, { remark })
}

export function executeCleanup(id: number, confirm: boolean) {
  return cleanupPost(id, 'execute', { confirm })
}

export function grantSignoff(
  id: number,
  payload: { partition_id: number; owner: string; purpose: string; authorized_path: string },
) {
  return cleanupPost(id, 'signoffs', payload)
}

