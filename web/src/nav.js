/* 信息架构：8 个一级域 → 二级页（路由名）。角标由 TopBar 按待审数填充。 */
export const NAV = [
  { id: 'home', name: '工作台', pages: [['dashboard', '我的待办'], ['dash-mine', '我的项目'], ['dash-recent', '最近操作']] },
  { id: 'project', name: '项目', pages: [['projects', '项目列表'], ['lines', '产品线'], ['modules', '模块与端']] },
  { id: 'requirement', name: '需求', pages: [['requirements', '需求列表'], ['questions', '待确认事项'], ['req-eval', '需求分析评测']] },
  { id: 'design', name: '测试设计', pages: [['designs', '设计任务'], ['points', '测试点审核'], ['cases', '用例审核'], ['caselib', '用例库'], ['recycle', '回收站']] },
  { id: 'plan', name: '测试计划', pages: [['plans', '计划列表'], ['exec', '分配给我'], ['reports', '执行报告']] },
  { id: 'ai', name: 'AI 中心', pages: [['aitasks', 'AI 任务'], ['calls', '调用日志'], ['usage', '用量统计'], ['knowledge', '知识库'], ['prompts', 'Prompt'], ['rules', '学习规则与记忆']] },
  { id: 'report', name: '报表', pages: [['report', '项目报表']] },
  { id: 'setting', name: '设置', pages: [['users', '用户与安全'], ['models', '模型配置'], ['templates', '用例模板'], ['access', '访问控制'], ['audit', '操作日志'], ['system', '系统与部署'], ['features', '功能清单']] },
]
export const PAGE_INFO = {}
NAV.forEach(g => g.pages.forEach(([k, n]) => { PAGE_INFO[k] = { group: g, title: n } }))
/** 显示模块树的二级页 */
export const MODULE_PAGES = new Set(['requirements', 'questions', 'designs', 'points', 'cases', 'caselib'])
/** 详情路由 → 所属二级页 */
export const SECTION_OF = {
  'req-detail': 'requirements', 'plan-detail': 'plans', 'exec-panel': 'plans', 'design-detail': 'designs',
  'project-detail': 'projects', 'prompt-detail': 'prompts', 'call-detail': 'calls', 'eval-run': 'req-eval',
}
export const sectionOf = name => SECTION_OF[name] || name
export const groupOf = name => (PAGE_INFO[sectionOf(name)] || PAGE_INFO.dashboard).group
