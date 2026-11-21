/**
 * WindowFrame — 桌面版的圆角窗体外框：标题栏 + 内容 + 八向缩放。
 * 浏览器里直接透传 children，不影响 e2e 与设计预览。
 */
import { useEffect } from 'react'
import { useApp } from '@/store/app'
import { isDesktop } from '@/window'
import ResizeGrips from './ResizeGrips'
import TitleBar from './TitleBar'
import styles from './WindowFrame.module.css'

export default function WindowFrame({ children }: { children: React.ReactNode }) {
  const desktop = isDesktop()
  const booting = useApp((s) => s.booting)

  useEffect(() => {
    document.documentElement.dataset.desktop = desktop ? '1' : '0'
    return () => {
      delete document.documentElement.dataset.desktop
    }
  }, [desktop])

  if (!desktop) return <>{children}</>

  return (
    <div className={styles.frame} data-booting={booting ? 'true' : 'false'}>
      {!booting && <TitleBar />}
      <div className={styles.body}>{children}</div>
      {!booting && <ResizeGrips />}
    </div>
  )
}
