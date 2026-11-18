/* 简单逐行 diff（LCS）：返回 [{ type: 'same' | 'add' | 'del', text }]，用于 Prompt 版本对比。 */
export function lineDiff(a, b) {
  const A = String(a || '').split('\n'), B = String(b || '').split('\n')
  const n = A.length, m = B.length
  // 超长文本退化为逐行比较，避免 O(n·m) 内存爆炸
  if (n * m > 4_000_000) {
    const out = []
    for (let i = 0; i < Math.max(n, m); i++) {
      if (A[i] === B[i]) out.push({ type: 'same', text: A[i] })
      else { if (i < n) out.push({ type: 'del', text: A[i] }); if (i < m) out.push({ type: 'add', text: B[i] }) }
    }
    return out
  }
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1))
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  const out = []
  let i = 0, j = 0
  while (i < n && j < m) {
    if (A[i] === B[j]) { out.push({ type: 'same', text: A[i] }); i++; j++ }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ type: 'del', text: A[i] }); i++ }
    else { out.push({ type: 'add', text: B[j] }); j++ }
  }
  while (i < n) out.push({ type: 'del', text: A[i++] })
  while (j < m) out.push({ type: 'add', text: B[j++] })
  return out
}
