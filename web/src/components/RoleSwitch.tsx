import { useEffect, useRef } from 'react'
import { invoke } from '@/bridge'
import type { Role } from '@/bridge/protocol'
import { useApp } from '@/store/app'
import { useUi } from '@/store/ui'
import styles from './RoleSwitch.module.css'

const ITEMS: { id: Role; label: string }[] = [
  { id: 'player', label: '玩家' },
  { id: 'developer', label: '开发者' },
]

export default function RoleSwitch() {
  const role = useApp((s) => s.state?.role ?? 'player')
  const motionOff = useApp((s) => s.state?.settings.window_anim === 0)
  const connected = useApp((s) => s.connected)
  const patchState = useApp((s) => s.patchState)
  const pushToast = useApp((s) => s.pushToast)
  const setCovering = useUi((s) => s.setCovering)
  const switching = useRef(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      setCovering(false)
    }
  }, [setCovering])

  const pick = async (next: Role) => {
    if (switching.current || next === useApp.getState().state?.role) return
    switching.current = true
    setCovering(true)
    try {
      // 等遮罩真正画到不透明，再触发会推送 state 的桥调用。
      // 固定 260ms 退场会和慢桥接、新页挂载撞在一起。
      await waitForCover()
      if (!mounted.current) return
      if (connected) await invoke('app.role.set', { role: next })
      if (!mounted.current) return
      patchState({ role: next })
      // 给新页的 effects 与首次布局各一个绘制机会，之后只合成遮罩透明度。
      await paintedFrame()
      await paintedFrame()
    } catch (err) {
      if (mounted.current) pushToast(err instanceof Error ? err.message : '暂时不能切换角色', 'error')
    } finally {
      if (mounted.current) {
        setCovering(false)
        // 退场期间也不接受重复请求；避免旧动画的收尾清掉下一次遮罩。
        while (document.querySelector('[data-role-cover]')) await paintedFrame()
      }
      switching.current = false
    }
  }

  return (
    <div className={styles.seg} data-role={role} data-motion-off={motionOff || undefined}>
      <span className={styles.thumb} aria-hidden="true" />
      {ITEMS.map((it) => {
        const active = it.id === role
        return (
          <button
            key={it.id}
            className={styles.item}
            data-active={active}
            onClick={() => void pick(it.id)}
          >
            <span className={styles.label}>{it.label}</span>
          </button>
        )
      })}
    </div>
  )
}

const paintedFrame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))

async function waitForCover() {
  // 读取一次小遮罩的 opacity，不测量/投影整个工作区布局。
  // 即时模式同样先提交遮罩，重页面的同步挂载不会阻塞入场首帧。
  while (useUi.getState().covering) {
    await paintedFrame()
    const cover = document.querySelector('[data-role-cover]')
    if (cover && Number(getComputedStyle(cover).opacity) === 1) {
      await paintedFrame()
      return
    }
  }
}
