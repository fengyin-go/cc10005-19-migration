/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export interface ApiConflict {
  message: string
  currentVersion: number | null
}

export class ApiError extends Error {
  status: number
  conflict: ApiConflict | null

  constructor(message: string, status: number, conflict: ApiConflict | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.conflict = conflict
  }
}

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

/** 统一解析失败响应：409 时抽出冲突回执（含服务端当前版本），其余取 detail/message。 */
export async function readError(response: Response): Promise<ApiError> {
  let message = `接口返回 ${response.status}，数据未更新`
  let conflict: ApiConflict | null = null
  try {
    const payload = (await response.json()) as Record<string, unknown>
    const detail = payload.detail
    if (response.status === 409 && detail && typeof detail === 'object') {
      const d = detail as { message?: string; current_version?: number | null }
      message = d.message ?? message
      conflict = {
        message,
        currentVersion: d.current_version ?? null,
      }
    } else if (typeof detail === 'string') {
      message = detail
    } else if (typeof payload.message === 'string') {
      message = payload.message
    }
  } catch {
    // 响应体不是 JSON 时保留默认提示
  }
  return new ApiError(message, response.status, conflict)
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw await readError(response)
  }
  return (await response.json()) as T
}
