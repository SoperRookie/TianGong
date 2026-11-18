<script setup>
/* 8 节点流水线（5.4–5.8）：完成绿、进行蓝、失败红、跳过灰；执行中显示进展摘要与检查点。 */
import { computed } from 'vue'
import { fmtAgo } from '@/utils/format'
import { TgIcon } from '@/components/ui'
import { pipelineNodes, progressText } from './taskUtils'
const props = defineProps({ task: { type: Object, required: true } })
const nodes = computed(() => pipelineNodes(props.task))
const running = computed(() => ['queued', 'running'].includes(props.task.status))
const pd = computed(() => props.task.progress_detail || {})
const elapsed = computed(() => {
  const since = (props.task.context || {}).retried_at || props.task.created_at
  const s = since ? Math.max(0, Math.floor((Date.now() - new Date(since).getTime()) / 1000)) : 0
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
})
</script>
<template>
  <div class="pipeline">
    <div v-for="(n, i) in nodes" :key="n.name" class="pnode" :class="n.state">
      <span>{{ n.state === 'done' ? '✓' : n.state === 'fail' ? '!' : i + 1 }}</span><b>{{ n.name }}</b><small>{{ n.sub }}</small>
    </div>
  </div>
  <div v-if="running" class="information" style="margin-top:8px">
    <span class="spinner"></span>
    <span>{{ task.progress === 'parsing' ? '正在解析需求材料（PDF 提取与图片理解按页 / 按图进行，大文件需要几分钟）' : `正在执行：${progressText(task.progress || 'queued')}` }} · 已耗时 {{ elapsed }}
      <template v-if="pd.job_active === false"><br /><b style="color:#9a5a1e">后台没有这个任务的执行句柄</b>（取消 / 重启打断，或投入后台时出错），不会再有进展；用「标记失败」后可「继续完成」<template v-if="pd.modules_done">，已完成的 {{ pd.modules_done }} 个模块会保留</template>。</template>
      <template v-else-if="pd.modules_done || pd.analysis_done"><br />检查点：{{ pd.analysis_done ? '拆解已完成' : '' }}{{ pd.analysis_done && pd.modules_done ? ' · ' : '' }}{{ pd.modules_done ? `已生成 ${pd.modules_done} 个模块` : '' }}{{ pd.checkpoint_at ? ` · 最近落盘 ${fmtAgo(pd.checkpoint_at)}` : '' }}</template>
    </span>
  </div>
  <div v-if="task.status === 'awaiting_confirmation'" class="information" style="margin-top:8px"><TgIcon name="shield" :size="14" /> <span>拆解后停在「待确认测试点」：人工审核门禁清零并确认后才生成用例；已完成任务上再次确认为增量生成并入现有用例。{{ task.progress === 'quality_check' ? '查漏 / 组合延伸 / 查重 / 独立评审仍在后台补充。' : '' }}</span></div>
</template>
<style scoped>
.pnode.fail span { background: #e44f5a; color: #fff; }
.pnode.skip span { background: #f1f3f7; color: #c3ccd8; border-style: dashed; }
.pnode.skip b, .pnode.skip small { color: #b4bcc9; }
</style>
