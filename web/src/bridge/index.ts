/* pywebview 桥：前端只在这里碰 Python。 */
import type { ServerEvent } from './protocol'

declare global {
  interface Window {
    pywebview?: {
      api: {
        invoke: (method: string, params?: Record<string, unknown>) => Promise<InvokeResult>
        pick_path?: (kind: string, filters?: string) => Promise<InvokeResult>
      }
    }
    __PE?: { push: (events: ServerEvent[]) => void }
  }
}

export type InvokeResult =
  | { ok: true; data: unknown }
  | { ok: false; error: { code: string; message: string; detail?: string } }

export class BridgeError extends Error {
  code: string
  detail: string
  constructor(code: string, message: string, detail = '') {
    super(message)
    this.name = 'BridgeError'
    this.code = code
    this.detail = detail
  }
}

/** 等桥就绪。不缓存 false：WebView2 冷启动时注入可能晚于首帧。 */
export function whenReady(timeout = 4000): Promise<boolean> {
  if (window.pywebview?.api) return Promise.resolve(true)
  return new Promise((resolve) => {
    let settled = false
    const done = () => {
      if (settled) return
      settled = true
      resolve(Boolean(window.pywebview?.api))
    }
    window.addEventListener('pywebviewready', done, { once: true })
    window.setTimeout(done, timeout)
  })
}

export function hasBridge(): boolean {
  return Boolean(window.pywebview?.api)
}

export async function invoke<T = unknown>(
  method: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  const api = window.pywebview?.api
  if (!api) throw new BridgeError('NO_BRIDGE', '桌面桥接尚未就绪')
  const result = await api.invoke(method, params)
  if (!result || result.ok !== true) {
    const err = (result as { error?: { code: string; message: string; detail?: string } }).error
    throw new BridgeError(err?.code ?? 'ERROR', err?.message ?? '操作失败', err?.detail ?? '')
  }
  return result.data as T
}

export async function pickPath(
  kind: 'file' | 'folder' | 'save',
  filters = '',
): Promise<string> {
  const api = window.pywebview?.api
  if (!api?.pick_path) throw new BridgeError('NO_BRIDGE', '桌面桥接尚未就绪')
  const result = await api.pick_path(kind, filters)
  if (!result || result.ok !== true) {
    const err = (result as { error?: { message: string } }).error
    throw new BridgeError('DIALOG_ERROR', err?.message ?? '无法打开选择器')
  }
  return String((result.data as { path?: string }).path ?? '')
}

/** 订阅 Python 推送。返回取消函数。 */
export function subscribe(handler: (events: ServerEvent[]) => void): () => void {
  const previous = window.__PE
  window.__PE = {
    ...(previous ?? {}),
    push(events) {
      if (!Array.isArray(events) || events.length === 0) return
      try {
        handler(events)
      } catch (err) {
        console.error('event handler failed', err)
      }
    },
  }
  return () => {
    if (window.__PE) window.__PE.push = () => {}
  }
}
