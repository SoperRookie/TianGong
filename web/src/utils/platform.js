/* 平台差异：快捷键提示在 Mac 显示 ⌘，其余（Windows / Linux）显示 Ctrl+。按键处理本身两个都认（metaKey || ctrlKey）。 */
const ua = typeof navigator === 'undefined' ? '' : `${navigator.platform || ''} ${navigator.userAgent || ''}`
export const IS_MAC = /Mac|iPhone|iPad|iPod/i.test(ua)
export const MOD = IS_MAC ? '⌘' : 'Ctrl+'
/** 把文案里的 ⌘ 换成当前平台的修饰键写法（说明抽屉里的快捷键、规则文字用） */
export const modText = s => (IS_MAC || typeof s !== 'string' ? s : s.replace(/⌘/g, 'Ctrl+'))
