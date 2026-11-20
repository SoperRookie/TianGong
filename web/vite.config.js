import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 开发时后端由 uvicorn 起在 8000（bash scripts/dev.sh）；/api、/health、/vendor 走代理。
// 构建产物在 web/dist，由 FastAPI 直接托管（app/main.py 优先 web/dist/index.html）。
const BACKEND = process.env.TIANGONG_BACKEND || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/health': { target: BACKEND, changeOrigin: true },
      '/vendor': { target: BACKEND, changeOrigin: true },
    },
  },
  // 浏览器基线：Chrome / Edge 88+、Firefox 78+、Safari 14.1+（及同内核的 360 / QQ / 搜狗极速模式）。
  // target 让 esbuild 把更新的 JS 语法降级；cssTarget 让 CSS 自动补 -webkit- 前缀、把 inset / #rrggbbaa 等新写法展开成老写法。
  // 样式里大量用到 flex gap，这是基线定在上述版本的原因，再低的版本布局间距会丢。
  build: {
    outDir: 'dist', emptyOutDir: true, chunkSizeWarningLimit: 1500,
    target: ['es2019', 'chrome80', 'edge80', 'firefox78', 'safari13.1'],
    cssTarget: ['chrome80', 'edge80', 'firefox78', 'safari13.1'],
  },
})
