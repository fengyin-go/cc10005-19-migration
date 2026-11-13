import { useCallback, useEffect, useMemo, useState } from 'react'

import { readError, request } from '@/api/client'
import { CreateForm, type CreateField } from '@/components/CreateForm'

const ENDPOINT = '/api/site'
const columns = [
  "站点编号",
  "域名地址",
  "站点类型",
  "请求方式",
  "字符编码",
  "请求头模板",
  "Cookie配置",
  "站点负责人"
]
const actions = [
  "提交校验",
  "激活站点",
  "暂停站点"
]
const statuses = [
  "草稿",
  "待校验",
  "已激活",
  "已暂停"
]

const createFields: CreateField[] = [
  { name: '站点编号', label: '站点编号', required: true, placeholder: '如 SITE-1001' },
  { name: '域名地址', label: '域名地址', required: true, placeholder: '如 www.example.com' },
  { name: '站点类型', label: '站点类型', required: true, placeholder: '如 门户 / 论坛' },
  { name: '请求方式', label: '请求方式', placeholder: 'GET / POST' },
  { name: '字符编码', label: '字符编码', placeholder: '如 UTF-8' },
  { name: '站点负责人', label: '站点负责人' },
]

const emptyForm = (): Record<string, string> =>
  Object.fromEntries(createFields.map((field) => [field.name, '']))

type Row = Record<string, string | number | null | boolean>

export default function SiteView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [successMessage, setSuccessMessage] = useState('')
  const [conflictMessage, setConflictMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [formOpen, setFormOpen] = useState(false)
  const [formValues, setFormValues] = useState<Record<string, string>>(emptyForm)
  const [submitting, setSubmitting] = useState(false)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('采集站点列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '站点管理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setConflictMessage('')
    setSuccessMessage('')
    try {
      // 带上行版本号：并发修改后服务端会回 409，提示刷新而不是静默覆盖。
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action }, version: row.version ?? null }),
      })
      if (response.status === 409) {
        const apiError = await readError(response)
        setConflictMessage(apiError.message)
        await reload()
        return
      }
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '站点管理动作未生效，请稍后重试')
      }
      setSuccessMessage(payload.message ?? '动作已生效')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '站点管理操作失败')
    }
  }

  async function submitCreate() {
    setErrorMessage('')
    setConflictMessage('')
    setSubmitting(true)
    try {
      const values = Object.fromEntries(
        Object.entries(formValues).filter(([, value]) => value.trim() !== '')
      )
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      if (response.status === 409) {
        const apiError = await readError(response)
        setConflictMessage(apiError.message)
        return
      }
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '登记失败')
      }
      setFormOpen(false)
      setFormValues(emptyForm())
      setSuccessMessage(payload.message ?? '采集站点已登记，已写入 SQLite，重启后仍可核对')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采集站点登记失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section className="page" data-module="site">
      <header className="page-head">
        <div>
          <h2>站点管理管理</h2>
          <p className="page-desc">维护采集站点的输入、执行约束与结果记录；登记数据落在 SQLite，服务重启后仍可核对。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => { setFormOpen(true); setErrorMessage('') }}>
            登记采集站点
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出站点管理清单
          </button>
        </div>
      </header>

      {conflictMessage && (
        <div className="conflict-box" role="alert">
          <strong>并发冲突：</strong>{conflictMessage}
          <button className="link" type="button" style={{ marginLeft: 8 }} onClick={() => void reload()}>
            刷新列表
          </button>
        </div>
      )}

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => {
                const next = { ...prev }
                if (event.target.value) {
                  next[field] = event.target.value
                } else {
                  delete next[field]
                }
                return next
              })}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>版本</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{String(row.status ?? '')}</td>
              <td>v{String(row.version ?? 1)}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无站点管理数据，可先登记采集站点</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条站点管理记录{statuses.length > 0 ? `（状态：${statuses.join(' / ')}）` : ''}</span>
        {successMessage && <span className="success-text">{successMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      <CreateForm
        title="登记采集站点"
        open={formOpen}
        fields={createFields}
        values={formValues}
        submitting={submitting}
        onChange={(name, value) => setFormValues((prev) => ({ ...prev, [name]: value }))}
        onSubmit={() => void submitCreate()}
        onClose={() => setFormOpen(false)}
      />
    </section>
  )
}
