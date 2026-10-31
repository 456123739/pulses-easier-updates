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
