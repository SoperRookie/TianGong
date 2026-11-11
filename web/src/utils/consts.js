/* 与后端常量同口径的枚举（app/tasks/points.py、app/plans.py）。 */
export const REJECT_TYPES = ['内容错误', '场景遗漏', '颗粒度过粗', '颗粒度过细', '重复', '步骤不可执行', '预期不可验证', '优先级错误', '关键词错误', '超出需求', '需求理解错误', '范围过大', '范围不足', '描述不清晰', '不符合项目规范', '其他']
export const FIX_SCOPES = ['当前项', '选中项', '只补遗漏', '当前项重生成', '整批重生成']
export const CASE_FIELD_LABELS = [['title', '标题'], ['precondition', '前置条件'], ['steps', '操作步骤'], ['expected', '预期结果'], ['priority', '优先级'], ['keywords', '标签'], ['module', '模块'], ['remark', '备注']]
export const FAIL_REASONS = ['用例步骤有误', '用例预期有误', '系统缺陷', '环境问题', '测试数据问题', '其他']
export const EXEC_STATUSES = [['pass', '通过'], ['fail', '失败'], ['blocked', '阻塞'], ['skipped', '跳过']]
export const PROJECT_STATUSES = [['active', '进行中'], ['paused', '暂停'], ['archived', '已归档']]
export const VERSION_STATUSES = [['not_started', '未开始'], ['in_progress', '进行中'], ['done', '已完成'], ['archived', '已归档']]
