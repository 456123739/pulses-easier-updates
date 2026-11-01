import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { useApp } from './store/app'
import { usePlayer } from './store/player'
import { useUi } from './store/ui'
import './styles/tokens.css'
import './styles/global.css'
import './styles/ui.css'

// 自动化测试/截图脚本要能摆布界面状态：只暴露 store，不暴露业务。
;(window as unknown as { __pe?: unknown }).__pe = { useApp, usePlayer, useUi }

const root = document.getElementById('root')
if (!root) throw new Error('#root not found')

createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)

// 端到端：只有 ?e2e=1 才加载页面内驱动。产物里存在这个 chunk，
// 但玩家路径永远不会 import 它（也就不会下载）。
const e2eFlag =
  new URLSearchParams(window.location.search).get('e2e') === '1' ||
  window.location.hash.includes('e2e=1')
if (e2eFlag) {
  void import('./e2e/harness').then((mod) => mod.start())
}
