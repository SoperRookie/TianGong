# 天工前端（web/）开发约定 · Vue 3 + Vite 5 · 设计方案 v2.3

本目录是对旧单文件前端 `app/web/index.html` 的 Vue 重写，视觉与交互按 `docs/前端交互设计_v2.3/前端交互设计方案_v2.3.md`，
演示版原型 `docs/前端交互设计_v2.3/演示版源码/tpl_pages*.js`（纯 JS 字符串模板）是每个页面的布局与交互参考（数据是假的，接口以后端为准）。

## 运行

- 后端：`bash scripts/dev.sh`（8000）或本地 SQLite 调试实例；`vite.config.js` 把 `/api /health /vendor` 代理到 `TIANGONG_BACKEND`（默认 8000）。
- 前端：`npm run dev`（5173）；`npm run build` 产出 `dist/`，`app/main.py` 优先托管 `web/dist/index.html`。
- 验证页面：用 Chrome 打开 `http://localhost:5173/#/<route>`，看控制台无错误；或 `npm run build` 必须通过。

## 目录

```
src/main.js            入口（pinia + router + 样式）
src/App.vue            登录页（meta.bare）/ AppShell
src/router/index.js    全部路由（哈希），name 与 nav.js 的页面 key 一致
src/nav.js             NAV（8 域 → 二级页）、MODULE_PAGES（显示模块树的页）、SECTION_OF（详情路由 → 二级页）
src/specs.js           每页「本页交互说明」(SPECS) 与功能编号映射 (FEATURE_MAP)
src/api/client.js      api.get/post/put/del/upload/download/text，seg() 路径段编码（项目名含 /）
src/stores/auth.js     token / user / can(project, action) / isAdmin / roleIn
src/stores/scope.js    工作范围：project / line / modules 树 / module / terminal 筛选 / moduleCounts / query
src/stores/ui.js       ui.toast(msg,{kind,undo}) / ui.error(e) / await ui.confirm(title, body, {ok,danger}) / ui.setCrumb()
src/stores/badge.js    顶栏角标（工作台聚合接口），写操作后可 badge.refresh(true)
src/components/ui/     TgIcon TgBadge TgPrio TgPanel TgStat TgTabs TgSeg TgDrawer TgDialog TgEmpty TgPager TgKv TgMd TgDropdown TgField TgTable PageHeading（从 '@/components/ui' 具名导入）
src/components/shell/  顶栏 / 子导航 / 模块树 / 搜索 / 说明抽屉 / toast / 确认框 / 个人资料
src/utils/             format(fmtTime fmtAgo fmtSize num pct shortId mdLite copyText) status(STATUS statusOf) consts(REJECT_TYPES FIX_SCOPES FAIL_REASONS…) permissions terminals useLoad useKeys
src/pages/<域>/        页面组件（<script setup>）
src/styles/app.css     v2.3 视觉 token 与全部基础类（来自演示版）；extra.css 补充类
```

## 页面写法

```vue
<script setup>
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, seg } from '@/api/client'
import { useLoad } from '@/utils/useLoad'
import { useScopeStore } from '@/stores/scope'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { fmtTime } from '@/utils/format'
import { PageHeading, TgPanel, TgTable, TgBadge, TgPrio, TgSeg, TgDialog, TgDropdown, TgField, TgPager, TgEmpty } from '@/components/ui'
const route = useRoute(), router = useRouter(), scope = useScopeStore(), auth = useAuthStore(), ui = useUiStore()
const status = ref(route.query.status || '')
const { data, loading, error, reload } = useLoad(() => api.get('/api/v1/requirements', { ...scope.query, status: status.value }), { deps: [() => scope.project, () => scope.line, status] })
async function remove(r) {
  if (!await ui.confirm('删除需求？', `「${r.title}」将进入回收站，可恢复。`, { ok: '删除', danger: true })) return
  try { await api.del(`/api/v1/requirements/${r.req_id}`); ui.toast('已删除'); reload(true) } catch (e) { ui.error(e) }
}
</script>
<template>
  <PageHeading title="需求列表" :sub="`${data?.total ?? 0} 条`">
    <template #actions><button class="btn primary" @click="open = true">录入需求</button><TgDropdown :items="[{ label: '导出', action: exportAll }]" /></template>
  </PageHeading>
  <div v-if="error" class="err-box">{{ error.message }}</div>
  <TgPanel :pad="false">
    <div class="filters"><TgSeg v-model="status" :items="REQ_STATUSES" /><input v-model="kw" class="field" placeholder="搜索" /></div>
    <TgTable :cols="cols" :rows="data?.items || []" row-key="req_id" :loading="loading" clickable @row-click="r => router.push({ name: 'req-detail', params: { id: r.req_id } })">
      <template #cell-status="{ row }"><TgBadge :status="row.status" /></template>
    </TgTable>
  </TgPanel>
</template>
```

规则：

1. **工作范围是全局筛选**：列表接口加 `...scope.query`（`project` / `business_line`）；`deps` 里监听 `() => scope.project`。需求 / 设计 / 测试点 / 用例 / 用例库页还要用 `scope.module`、`scope.terminal` 过滤（前端过滤或传参均可），并在有数据时写 `scope.moduleCounts = { 模块名: 数量 }` 给树计数（离开页面时置 null）。
2. **筛选 / 页签 / 选中项进 URL query**（`router.replace({ query })`），刷新后停在原处。
3. **每页一个主按钮**（页头右上，`btn primary`），危险 / 低频动作收进 `TgDropdown`。删除类动作先 `await ui.confirm(...)`。
4. **反馈**：成功 `ui.toast('已通过', { undo })`，失败 `ui.error(e)`；审核工作台内不弹 toast，只更新底部状态条。
5. **权限显隐**：`auth.can(project, 'case.review')`；管理员 `auth.isAdmin`。后端仍校验，前端只隐藏。
6. **徽标即状态**：`<TgBadge :status="x" />`（未知状态自动灰色显示原值）、`<TgPrio :p="c.priority" />`。
7. **时间** `fmtTime(iso)`；ID 用 `shortId()`；项目名进路径用 `seg(name)`。
8. **接口错误 401** 由 client 统一跳登录；**403 must_change_password** 弹改密框；页面不需要处理。
9. 文件下载走 `api.download(path, filename, query)`（Authorization 头 + blob）；上传用 `FormData` + `api.upload`。
10. 键盘流用 `useKeys({ j, k, a, r, e, space, u, '?': ... })`；在输入框内不触发。
11. 样式只用 `app.css` / `extra.css` 已有的类；页面私有样式写 `<style scoped>`，不要新建全局类名；不要引入第三方 UI 库。
12. 「本页交互说明」：在 `src/specs.js` 的 `SPECS` 为页面路由名加 `{ title, goal, rules, inter, accept, keys }`；FEATURE_MAP 已按 docs/xt/00 编号映射。
13. 长文本（整理稿、报告）用 `<TgMd :text="…" />`；代码 / JSON 用 `.code-block`。
14. 组件命名：页面文件已由路由指定（见 router/index.js），不要改路由名；页面内子组件放同目录 `components/` 子文件夹（如 `src/pages/design/components/CaseCard.vue`）。
15. 旧前端 `app/web/index.html` 是功能行为的权威参考：grep 对应 `render*` 函数或接口路径即可看到请求体字段与响应用法；接口定义在 `app/api/routes.py`（`@router.*("/api/v1/...")`），Pydantic 请求体 `class XxxBody`。
16. 所有文案中文；不要在代码或提交里留下 AI 署名。
