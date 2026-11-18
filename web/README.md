# 天工前端（web/）

Vue 3 + Vite 5 + vue-router 4 + pinia 2，按《前端交互设计方案 v2.3》重写的工程化前端，替代旧的单文件页面 `app/web/index.html`。

## 开发

```bash
# 后端（任选其一）
bash scripts/dev.sh                      # 本地 MySQL，8000
# 前端
cd web && npm install && npm run dev     # http://localhost:5173，/api /health /vendor 代理到 TIANGONG_BACKEND（默认 http://127.0.0.1:8000）
TIANGONG_BACKEND=http://127.0.0.1:8011 npm run dev   # 指向其他后端
```

## 构建与部署

```bash
cd web && npm run build                  # 产出 web/dist
```

`app/main.py` 启动时若存在 `web/dist/index.html`，`/` 直接托管它并挂载 `/assets`；不存在则回退旧页面 `app/web/index.html`。Docker 镜像与部署脚本需在打包前执行一次 `npm run build`（或把 `web/dist` 一起发布）。

## 结构

见 `CONVENTIONS.md`：目录、页面写法、工作范围与模块树、组件库、权限显隐、验证方法。

- 路由：哈希路由（`#/requirements/<id>/<step>`），全部在 `src/router/index.js`；一级域与二级页在 `src/nav.js`。
- 登录态：Bearer Token 存 `localStorage.tg_token`；401 统一跳登录，403 `must_change_password` 弹改密框。
- 工作范围：顶栏产品线 / 项目是全局筛选（`src/stores/scope.js`），需求 / 设计类页面左侧有按端分组的模块树。
- 每页右上角「本页交互说明」（`?`）：目标 / 规则 / 交互 / 验收 / 快捷键 / 覆盖的功能编号（`src/specs.js` + `src/data/features.json`）。
