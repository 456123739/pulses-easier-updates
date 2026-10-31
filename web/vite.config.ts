import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 产物给 pywebview 用：相对 base（由内置 HTTP server 提供），
// 输出到 web/dist，PyInstaller 当 datas 收进 _internal/web/dist。
export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {
    alias: { '@': new URL('./src', import.meta.url).pathname },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // WebView2 是 Chromium 内核，目标可以放高一点
    target: 'chrome114',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  server: { host: '127.0.0.1', port: 5273, strictPort: true },
})
