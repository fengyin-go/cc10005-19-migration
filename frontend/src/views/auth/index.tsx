import { useCallback, useEffect, useMemo, useState } from 'react'

import { readError, request } from '@/api/client'
import { CreateForm, type CreateField } from '@/components/CreateForm'

const ENDPOINT = '/api/auth'
const SITE_ENDPOINT = '/api/site'
const columns = [
  "凭证编号",
  "认证方式",
  "账号标识",
  "凭据有效天数",
  "会话保持",
  "刷新触发",
  "失效告警"
]
const actions = [
  "提交绑定",
  "启用凭证",
  "标记失效"
]
const statuses = [
  "草稿",
  "待绑定",
  "已生效",
  "已失效"
]

const createFields: CreateField[] = [
  { name: '凭证编号', label: '凭证编号', required: true, placeholder: '如 AUTH-1001' },
  { name: '认证方式', label: '认证方式', required: true, placeholder: '账号密码 / Cookie / Token' },
  { name: '账号标识', label: '账号标识' },
  { name: '凭据有效天数', label: '凭据有效天数', placeholder: '如 30' },
  { name: '会话保持', label: '会话保持' },
  { name: '刷新触发', label: '刷新触发' },
  { name: '失效告警', label: '失效告警' },
]

const emptyForm = (): Record<string, string> =>
  Object.fromEntries(createFields.map((field) => [field.name, '']))

type Row = Record<string, string | number | null | boolean>
type SiteRow = Record<string, string | number | null>
type Issue = {
  id: number
  issue_type: string
  business_no: string | null
  message: string
}

export default function AuthView() {
  const [rows, setRows] = useState<Row[]>([])
  const [sites, setSites] = useState<SiteRow[]>([])
  const [issues, setIssues] = useState<Issue[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [successMessage, setSuccessMessage] = useState('')
  const [conflictMessage, setConflictMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [formOpen, setFormOpen] = useState(false)
  const [formValues, setFormValues] = useState<Record<string, string>>(emptyForm)
  const [submitting, setSubmitting] = useState(false)
  const [bindingRow, setBindingRow] = useState<Row | null>(null)
  const [targetSiteId, setTargetSiteId] = useState('')
  const filterFields = useMemo(() => ['凭证编号', '认证方式'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [credResp, siteResp, issueResp] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${SITE_ENDPOINT}?size=200`),
        request(`${ENDPOINT}/migration-issues`),
      ])
      if (!credResp.ok) {
        throw new Error('凭证配置列表读取失败')
      }
      const payload = await credResp.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      if (siteResp.ok) {
        const sitePayload = await siteResp.json()
        setSites(sitePayload.items ?? [])
      }
      if (issueResp.ok) {
        const issuePayload = await issueResp.json()
        setIssues(issuePayload.items ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '登录凭证列表读取失败')
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
        throw new Error(payload.message ?? '登录凭证动作未生效，请稍后重试')
      }
      setSuccessMessage(payload.message ?? '动作已生效')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '登录凭证操作失败')
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
      setSuccessMessage(payload.message ?? '凭证配置已登记，已写入 SQLite，重启后仍可核对')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '凭证配置登记失败')
    } finally {
      setSubmitting(false)
    }
  }

  function openBinding(row: Row) {
    setBindingRow(row)
    setTargetSiteId(row.bound_site_id != null ? String(row.bound_site_id) : '')
    setConflictMessage('')
    setErrorMessage('')
  }

  async function submitBinding() {
    if (!bindingRow) {
      return
    }
    const siteId = Number(targetSiteId)
    if (!Number.isInteger(siteId)) {
      setErrorMessage('请选择目标站点')
      return
    }
    setSubmitting(true)
    setErrorMessage('')
    setConflictMessage('')
    try {
      const response = await request(`${ENDPOINT}/${bindingRow.id}/bindings`, {
        method: 'POST',
        body: JSON.stringify({ site_id: siteId, version: bindingRow.version ?? null }),
      })
      if (response.status === 409) {
        const apiError = await readError(response)
        setConflictMessage(apiError.message)
        setBindingRow(null)
        await reload()
        return
      }
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '绑定未生效')
      }
      setSuccessMessage(payload.message ?? '绑定已生效')
      setBindingRow(null)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '凭证绑定失败')
    } finally {
      setSubmitting(false)
    }
  }

  async function unbind(row: Row) {
    setErrorMessage('')
    setConflictMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/bindings`, {
        method: 'DELETE',
        body: JSON.stringify({ version: row.version ?? null }),
      })
      if (response.status === 409) {
        const apiError = await readError(response)
        setConflictMessage(apiError.message)
        await reload()
        return
      }
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '解绑未生效')
      }
      setSuccessMessage(payload.message ?? '已解除绑定')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '凭证解绑失败')
    }
  }

  return (
    <section className="page" data-module="auth">
      <header className="page-head">
        <div>
          <h2>登录凭证管理</h2>
          <p className="page-desc">登记凭证并绑定/转交到采集站点；站点与凭证关系在同一事务落 SQLite，重启后仍可核对。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => { setFormOpen(true); setErrorMessage('') }}>
            登记凭证配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出登录凭证清单
          </button>
        </div>
      </header>

      {issues.length > 0 && (
        <div className="issue-banner" role="alert">
          <strong>迁移遗留 {issues.length} 条待处理问题</strong>
          <span>（重复业务编号、无法匹配目标站点的凭证未被自动合并，需人工处理）</span>
          <ul>
            {issues.slice(0, 5).map((issue) => (
              <li key={issue.id}>
                {issue.business_no ? `[${issue.business_no}] ` : ''}{issue.message}
              </li>
            ))}
            {issues.length > 5 && <li>其余 {issues.length - 5} 条请通过接口 GET {ENDPOINT}/migration-issues 查看</li>}
          </ul>
        </div>
      )}

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
            <th>绑定站点</th>
            <th>状态</th>
            <th>版本</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>
                <div className="bound-cell">
                  {row.bound_site_id != null ? (
                    <>
                      <span className="bound-no">{String(row.bound_site_no ?? '')}</span>
                      <span className="unbound">{String(row.bound_domain ?? '')}</span>
                    </>
                  ) : (
                    <span className="unbound">未绑定</span>
                  )}
                </div>
              </td>
              <td>{String(row.status ?? '')}</td>
              <td>v{String(row.version ?? 1)}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => openBinding(row)}>
                  {row.bound_site_id != null ? '转交站点' : '绑定站点'}
                </button>
                {row.bound_site_id != null && (
                  <button className="link" type="button" onClick={() => void unbind(row)}>
                    解除绑定
                  </button>
                )}
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
              <td className="empty-state" colSpan={columns.length + 4}>暂无登录凭证数据，可先登记凭证配置</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条登录凭证记录（状态：{statuses.join(' / ')}）</span>
        {successMessage && <span className="success-text">{successMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>

      <CreateForm
        title="登记凭证配置"
        open={formOpen}
        fields={createFields}
        values={formValues}
        submitting={submitting}
        onChange={(name, value) => setFormValues((prev) => ({ ...prev, [name]: value }))}
        onSubmit={() => void submitCreate()}
        onClose={() => setFormOpen(false)}
      />

      {bindingRow && (
        <div className="modal-mask" role="presentation" onMouseDown={() => setBindingRow(null)}>
          <form
            className="modal-card"
            onSubmit={(event) => { event.preventDefault(); void submitBinding() }}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <header className="modal-head">
              <strong>
                {bindingRow.bound_site_id != null ? '转交凭证到其他站点' : '绑定凭证到站点'}
                ：{String(bindingRow['凭证编号'])}
              </strong>
              <button className="link" type="button" onClick={() => setBindingRow(null)}>关闭</button>
            </header>
            <label className="form-item">
              <span>目标站点<em className="required">*</em></span>
              <select value={targetSiteId} onChange={(event) => setTargetSiteId(event.target.value)}>
                <option value="">请选择站点编号</option>
                {sites.map((site) => (
                  <option key={String(site.id)} value={String(site.id)}>
                    {String(site['站点编号'])} — {String(site['域名地址'])}
                  </option>
                ))}
              </select>
            </label>
            <p className="form-hint">
              当前版本 v{String(bindingRow.version ?? 1)}；提交期间若有其他请求改动了该凭证，
              服务端会返回冲突回执，本次绑定不会覆盖对方修改。
            </p>
            <footer className="modal-foot">
              <button className="btn" type="button" onClick={() => setBindingRow(null)}>取消</button>
              <button className="btn primary" type="submit" disabled={submitting}>
                {submitting ? '提交中…' : '确认绑定'}
              </button>
            </footer>
          </form>
        </div>
      )}
    </section>
  )
}
