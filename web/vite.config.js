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
  build: { outDir: 'dist', emptyOutDir: true, chunkSizeWarningLimit: 1500 },
})
