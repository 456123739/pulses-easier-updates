import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { useApp } from './store/app'
import { usePlayer } from './store/player'
import { useUi } from './store/ui'
import './styles/tokens.css'
import './styles/global.css'
import './styles/ui.css'
import { start as startE2E } from './e2e/harness'

// 自动化测试/截图脚本要能摆布界面状态：只暴露 store，不暴露业务。
;(window as unknown as { __pe?: unknown }).__pe = { useApp, usePlayer, useUi }

const root = document.getElementById('root')
if (!root) throw new Error('#root not found')

createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)

// 端到端：只有 URL 带 e2e 标记才启动页面内驱动。
// 这里用静态 import 而不是动态 import：真机上动态 chunk 加载失败时是"静默不跑"，
// 排查代价极高（900 秒零输出）。静态打进来多 ~19KB，但一定在、也一定报错。
const e2eFlag =
  new URLSearchParams(window.location.search).get('e2e') === '1' ||
  window.location.hash.includes('e2e=1')
if (e2eFlag) {
  void startE2E()
}
