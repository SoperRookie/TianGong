import { ref, watch, onMounted } from 'vue'
/**
 * 异步加载：const { data, loading, error, reload } = useLoad(() => api.get(...), { deps: [() => route.params.id], immediate: true })
 * 过期响应（deps 已变）自动丢弃。
 */
export function useLoad(fn, { deps = [], immediate = true, initial = null } = {}) {
  const data = ref(initial), loading = ref(false), error = ref(null)
  let seq = 0
  async function reload(silent = false) {
    const my = ++seq
    if (!silent) loading.value = true
    error.value = null
    try {
      const r = await fn()
      if (my === seq) data.value = r
    } catch (e) {
      if (my === seq) error.value = e
    } finally {
      if (my === seq) loading.value = false
    }
    return data.value
  }
  if (deps.length) watch(deps, () => reload(), { flush: 'post' })
  if (immediate) onMounted(() => reload())
  return { data, loading, error, reload }
}
