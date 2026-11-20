import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './styles/app.css'
import './styles/extra.css'
import { MOD, modText } from './utils/platform'

const app = createApp(App)
app.use(createPinia())
app.use(router)
// 模板里的快捷键提示：{{ $mod }}⏎ → Mac 显示 ⌘⏎，Windows / Linux 显示 Ctrl+⏎
app.config.globalProperties.$mod = MOD
app.config.globalProperties.$modText = modText
app.mount('#app')
