import { createRouter, createWebHashHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { PAGE_INFO, sectionOf } from '@/nav'

const page = (dir, file) => () => import(`../pages/${dir}/${file}.vue`)

const routes = [
  { path: '/login', name: 'login', component: () => import('../pages/LoginPage.vue'), meta: { bare: true } },
  // 工作台
  { path: '/', name: 'dashboard', component: page('home', 'Dashboard') },
  { path: '/mine', name: 'dash-mine', component: page('home', 'MyProjects') },
  { path: '/recent', name: 'dash-recent', component: page('home', 'RecentOps') },
  // 项目
  { path: '/projects', name: 'projects', component: page('project', 'ProjectList') },
  { path: '/projects/:name/:sub?', name: 'project-detail', component: page('project', 'ProjectDetail') },
  { path: '/lines', name: 'lines', component: page('project', 'BusinessLines') },
  { path: '/modules', name: 'modules', component: page('project', 'ModulesPage') },
  // 需求
  { path: '/requirements', name: 'requirements', component: page('requirement', 'RequirementList') },
  { path: '/requirements/:id/:step?', name: 'req-detail', component: page('requirement', 'RequirementDetail') },
  { path: '/questions', name: 'questions', component: page('requirement', 'QuestionsPage') },
  { path: '/req-eval', name: 'req-eval', component: page('requirement', 'AnalysisEval') },
  { path: '/req-eval/runs/:id', name: 'eval-run', component: page('requirement', 'AnalysisEvalRun') },
  // 测试设计
  { path: '/designs', name: 'designs', component: page('design', 'TaskList') },
  { path: '/designs/:id/:sub?', name: 'design-detail', component: page('design', 'TaskDetail') },
  { path: '/points/:tid?', name: 'points', component: page('design', 'PointsWorkbench') },
  { path: '/cases/:tid?', name: 'cases', component: page('design', 'CasesWorkbench') },
  { path: '/caselib', name: 'caselib', component: page('design', 'CaseLibrary') },
  { path: '/recycle', name: 'recycle', component: page('design', 'RecycleBin') },
  // 测试计划
  { path: '/plans', name: 'plans', component: page('plan', 'PlanList') },
  { path: '/plans/:id/run/:rid', name: 'exec-panel', component: page('plan', 'ExecPanel') },
  { path: '/plans/:id/:sub?', name: 'plan-detail', component: page('plan', 'PlanDetail') },
  { path: '/exec', name: 'exec', component: page('plan', 'MyExecution') },
  { path: '/reports', name: 'reports', component: page('plan', 'ExecReports') },
  // AI 中心
  { path: '/ai/tasks', name: 'aitasks', component: page('ai', 'AiTasks') },
  { path: '/ai/calls', name: 'calls', component: page('ai', 'CallLog') },
  { path: '/ai/calls/:id', name: 'call-detail', component: page('ai', 'CallLog') },
  { path: '/ai/usage', name: 'usage', component: page('ai', 'Usage') },
  { path: '/ai/knowledge', name: 'knowledge', component: page('ai', 'Knowledge') },
  { path: '/ai/prompts', name: 'prompts', component: page('ai', 'Prompts') },
  { path: '/ai/prompts/:key', name: 'prompt-detail', component: page('ai', 'PromptDetail') },
  { path: '/ai/rules', name: 'rules', component: page('ai', 'RulesMemories') },
  // 报表
  { path: '/report', name: 'report', component: page('report', 'ProjectReport') },
  // 设置
  { path: '/settings/users', name: 'users', component: page('setting', 'Users') },
  { path: '/settings/models', name: 'models', component: page('setting', 'Models') },
  { path: '/settings/templates', name: 'templates', component: page('setting', 'Templates') },
  { path: '/settings/access', name: 'access', component: page('setting', 'Access') },
  { path: '/settings/audit', name: 'audit', component: page('setting', 'Audit') },
  { path: '/settings/system', name: 'system', component: page('setting', 'System') },
  { path: '/settings/features', name: 'features', component: page('setting', 'Features') },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({ history: createWebHashHistory(), routes, scrollBehavior: () => ({ top: 0 }) })

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.ready) await auth.fetchMe()
  if (to.name === 'login') return auth.isLogged ? '/' : true
  if (!auth.isLogged) return { name: 'login', query: to.fullPath !== '/' ? { redirect: to.fullPath } : {} }
  return true
})
router.afterEach((to) => {
  const info = PAGE_INFO[sectionOf(to.name)]
  document.title = (info ? `${info.title} · ${info.group.name} · ` : '') + '天工'
})
export default router
