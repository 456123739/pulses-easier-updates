/**
 * window.ts — 桌面窗口（pywebview）与浏览器的差别都收在这里。
 *
 * 只有真桌面版（window.pywebview.platform 存在）才启用无边框标题栏、
 * 拖拽/缩放/展开；浏览器里（e2e、设计预览）一律当成普通网页。
 */
import { invoke } from '@/bridge'

type NativeBridge = {
  platform?: string
  api?: { invoke: (method: string, params?: unknown) => Promise<unknown> }
}

export function nativeBridge(): NativeBridge | null {
  if (typeof window === 'undefined') return null
  const w = window as unknown as { pywebview?: NativeBridge }
  return w.pywebview?.platform ? w.pywebview : null
}

export const isDesktop = (): boolean => nativeBridge() !== null

/** 调 window.* 方法；浏览器/不支持时安静失败。 */
export async function win<T>(method: string, params?: Record<string, unknown>): Promise<T | null> {
  if (!isDesktop()) return null
  try {
    return await invoke<T>(method, params)
  } catch {
    return null
  }
}

export const expandWindow = async (): Promise<void> => {
  await win('window.expand')
}
