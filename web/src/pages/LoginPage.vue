<script setup>
// 登录页：沿用旧单页（app/web/index.html）的「深空科技风」——左侧宣传区 + 右侧玻璃卡片，
// 背景粒子星网 canvas；仅把表单逻辑接到 pinia auth store 与 vue-router。
import { reactive, ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
const auth = useAuthStore(), router = useRouter(), route = useRoute()
const f = reactive({ username: '', password: '', otp: '' })
const otpStep = ref(false), busy = ref(false), err = ref('')
const fx = ref(null), otpInput = ref(null)

async function submit() {
  err.value = ''; busy.value = true
  try {
    const r = await auth.login({ username: f.username.trim(), password: f.password, otp: otpStep.value ? f.otp.trim() : '' })
    if (r.otpRequired) {  // 口令正确，需补充动态验证码
      otpStep.value = true; f.otp = ''
      await nextTick(); otpInput.value?.focus()
      return
    }
    router.replace(route.query.redirect || '/')
  } catch (e) { err.value = e.message } finally { busy.value = false }
}

/* 登录页背景：粒子星网 + 缓慢漂移，离开页面时停止渲染；尊重减少动效偏好 */
let raf = null, onResize = null
function startFx() {
  const c = fx.value; if (!c || raf) return
  const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  const ctx = c.getContext('2d'); let W, H; const pts = []
  const dpr = window.devicePixelRatio || 1
  onResize = () => { W = c.width = c.clientWidth * dpr; H = c.height = c.clientHeight * dpr }
  onResize(); window.addEventListener('resize', onResize)
  const N = Math.min(140, Math.floor(W * H / (28000 * dpr * dpr)))
  for (let i = 0; i < N; i++) pts.push({ x: Math.random() * W, y: Math.random() * H, vx: (Math.random() - .5) * .25 * dpr, vy: (Math.random() - .5) * .25 * dpr, r: (Math.random() * 1.4 + .6) * dpr, h: Math.random() < .2 ? 270 : 222 })
  const LINK = 150 * dpr
  let mouse = { x: -9999, y: -9999 }
  c.addEventListener('mousemove', e => { const b = c.getBoundingClientRect(); mouse = { x: (e.clientX - b.left) * dpr, y: (e.clientY - b.top) * dpr } })
  const frame = () => {
    ctx.clearRect(0, 0, W, H)
    for (const p of pts) {
      if (!reduce) { p.x += p.vx; p.y += p.vy; if (p.x < 0 || p.x > W) p.vx *= -1; if (p.y < 0 || p.y > H) p.vy *= -1 }
    }
    for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) {
      const a = pts[i], b = pts[j], dx = a.x - b.x, dy = a.y - b.y, d = Math.hypot(dx, dy)
      if (d < LINK) { ctx.strokeStyle = `hsla(${a.h},90%,72%,${(1 - d / LINK) * .28})`; ctx.lineWidth = dpr * .8; ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke() }
    }
    for (const p of pts) {
      const md = Math.hypot(p.x - mouse.x, p.y - mouse.y), glow = md < 160 * dpr ? 1 : 0
      ctx.fillStyle = `hsla(${p.h},90%,${glow ? 85 : 72}%,${glow ? .95 : .7})`
      ctx.beginPath(); ctx.arc(p.x, p.y, p.r * (glow ? 1.8 : 1), 0, Math.PI * 2); ctx.fill()
    }
    if (reduce) { raf = 1; return }
    raf = requestAnimationFrame(frame)
  }
  frame()
}
function stopFx() {
  if (raf && raf !== 1) cancelAnimationFrame(raf)
  raf = null
  if (onResize) { window.removeEventListener('resize', onResize); onResize = null }
}
onMounted(startFx)
onBeforeUnmount(stopFx)
</script>

<template>
  <div class="login-wrap">
    <!-- 天工吉祥物「夜枭」：戴扫描护目镜的机械猫头鹰（源文件 /brand/owl*.svg） -->
    <svg width="0" height="0" style="position:absolute" aria-hidden="true">
      <defs>
        <linearGradient id="owlSkin" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#25335e"/><stop offset="1" stop-color="#121b38"/></linearGradient>
        <filter id="owlGlow" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="1.4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
        <symbol id="owlMark" viewBox="0 0 64 64">
          <path d="M13 22 L20 12 L26 20 H38 L44 12 L51 22 V42 A19 19 0 0 1 13 42 Z" fill="url(#owlSkin)" stroke="#5fe3ff" stroke-width="2" stroke-linejoin="round"/>
          <path d="M20 12 L26 20 M44 12 L38 20" stroke="#b79cff" stroke-width="2" stroke-linecap="round"/>
          <rect x="15" y="25" width="34" height="14" rx="7" fill="#0b1226" stroke="#5fe3ff" stroke-width="1.6"/>
          <circle cx="24" cy="32" r="5.2" fill="#0b1226" stroke="#5fe3ff" stroke-width="1.6"/>
          <circle cx="40" cy="32" r="5.2" fill="#0b1226" stroke="#5fe3ff" stroke-width="1.6"/>
          <g class="owl-eye"><circle cx="24" cy="32" r="2.4" fill="#7cf5c2" filter="url(#owlGlow)"/><circle cx="40" cy="32" r="2.4" fill="#7cf5c2" filter="url(#owlGlow)"/></g>
          <rect class="owl-scan" x="17" y="31" width="30" height="1.6" fill="#5fe3ff" opacity=".35"/>
          <path d="M29.5 41 L32 46 L34.5 41 Z" fill="#ff7ad9"/>
          <path d="M22 48 L26 52 L30 48 M34 48 L38 52 L42 48" fill="none" stroke="#5fe3ff" stroke-opacity=".6" stroke-width="1.6" stroke-linecap="round"/>
        </symbol>
      </defs>
    </svg>
    <canvas ref="fx"></canvas>
    <div class="grid"></div>
    <div class="login-stage">
      <div class="login-hero">
        <svg class="hero-owl" viewBox="0 0 64 64" width="132" height="132"><use href="#owlMark"/></svg>
        <div class="eyebrow"><i></i>AI Test Design Platform</div>
        <h1>让测试设计<br><span>从需求直达执行</span></h1>
        <p>多 Agent 协作拆解需求、生成与评审用例，知识库沉淀团队规则，人工评审留痕反哺下一次生成。需求、测试点、用例、计划、执行全链路可追溯。</p>
        <div class="flow">
          <div class="node on"><div class="dot"></div><div class="lab">需求</div></div><div class="link"></div>
          <div class="node on"><div class="dot"></div><div class="lab">测试点</div></div><div class="link"></div>
          <div class="node on"><div class="dot"></div><div class="lab">用例</div></div><div class="link"></div>
          <div class="node on"><div class="dot"></div><div class="lab">计划</div></div><div class="link"></div>
          <div class="node on"><div class="dot"></div><div class="lab">执行</div></div>
        </div>
        <div class="login-stats">
          <div><b>17</b>可管理 Prompt</div>
          <div><b>3 层</b>知识库隔离</div>
          <div><b>4 类</b>项目角色</div>
          <div><b>≤3 轮</b>生成评审回环</div>
        </div>
      </div>
      <form class="login-card" @submit.prevent="submit">
        <div class="brand">
          <span class="mark"><svg viewBox="0 0 64 64"><use href="#owlMark"/></svg></span>
          <div><b>天工</b><div class="sub">AI 测试用例平台</div></div>
        </div>
        <template v-if="!otpStep">
          <label class="f" for="loginUser">用户名</label>
          <input id="loginUser" v-model="f.username" type="text" autocomplete="username" placeholder="登录账号" autofocus required />
          <label class="f" for="loginPwd">密码</label>
          <input id="loginPwd" v-model="f.password" type="password" autocomplete="current-password" placeholder="••••••••" required />
        </template>
        <template v-else>
          <label class="f" for="loginOtp">动态验证码<span class="hint">（Google Authenticator / 海月盾等验证器中的 6 位数字）</span></label>
          <input id="loginOtp" ref="otpInput" v-model="f.otp" class="otp" type="text" inputmode="numeric" maxlength="6" autocomplete="one-time-code" required />
        </template>
        <div class="login-err">{{ err || (auth.expired ? '会话已过期或账号已禁用，请重新登录。' : '') }}</div>
        <button type="submit" class="btn block" :disabled="busy"><span v-if="busy" class="spin"></span>{{ busy ? ' 登录中' : (otpStep ? '验证并登录' : '登 录') }}</button>
        <button v-if="otpStep" type="button" class="back" @click="otpStep = false; f.otp = ''">返回重新输入账号</button>
        <div class="login-foot"><span class="live"><i></i>内网部署 · 数据不出域</span><span>OpenAI GPT</span></div>
      </form>
    </div>
  </div>
</template>

<style scoped>
/* ================= 登录页：深空科技风（与旧单页 app/web/index.html 一致） ================= */
.login-wrap { position:fixed; inset:0; z-index:200; overflow:hidden; color:#dfe6f5;
  background:radial-gradient(1200px 700px at 15% 10%, #16244a 0%, transparent 60%),
             radial-gradient(900px 600px at 90% 90%, #241c4d 0%, transparent 55%), #0a0f1f; }
.login-wrap canvas { position:absolute; inset:0; width:100%; height:100%; }
.login-wrap .grid { position:absolute; inset:-1px; pointer-events:none;
  background-image:linear-gradient(rgba(96,140,255,.07) 1px, transparent 1px), linear-gradient(90deg, rgba(96,140,255,.07) 1px, transparent 1px);
  background-size:56px 56px; mask-image:radial-gradient(ellipse at 50% 40%, #000 20%, transparent 75%); -webkit-mask-image:radial-gradient(ellipse at 50% 40%, #000 20%, transparent 75%); }
.login-stage { position:relative; z-index:2; height:100%; display:grid; grid-template-columns:minmax(0,1.15fr) minmax(400px,.85fr);
  align-items:center; gap:40px; padding:0 8vw; max-width:1400px; margin:0 auto; }
.login-hero { max-width:560px; }
.hero-owl { display:block; margin:0 0 18px -6px; filter:drop-shadow(0 0 26px rgba(95,227,255,.28)); }
.login-hero .eyebrow { display:inline-flex; align-items:center; gap:8px; font-size:12px; letter-spacing:.22em; color:#8fb0ff;
  text-transform:uppercase; margin-bottom:22px; }
.login-hero .eyebrow i { width:26px; height:1px; background:linear-gradient(90deg,#8fb0ff,transparent); }
.login-hero h1 { font-size:48px; line-height:1.12; margin:0 0 14px; font-weight:750; letter-spacing:-.5px; color:#fff; text-wrap:balance; }
.login-hero h1 span { background:linear-gradient(92deg,#8fb0ff 0%,#c2a6ff 50%,#79e0ff 100%); -webkit-background-clip:text; background-clip:text; color:transparent; }
.login-hero p { color:#9aa9c7; font-size:15px; line-height:1.75; margin:0 0 30px; max-width:46ch; }
.flow { display:flex; align-items:center; gap:0; margin-top:6px; }
.flow .node { position:relative; display:flex; flex-direction:column; align-items:center; gap:8px; min-width:74px; }
.flow .node .dot { width:12px; height:12px; border-radius:50%; background:#1b2a52; border:2px solid #3f66d9; box-shadow:0 0 0 0 rgba(120,160,255,.6); }
.flow .node .lab { font-size:12px; color:#9aa9c7; letter-spacing:.04em; white-space:nowrap; }
.flow .link { flex:1; height:2px; min-width:34px; margin-bottom:24px; background:linear-gradient(90deg,#2b4290,#2b4290); position:relative; overflow:hidden; }
.flow .link::after { content:""; position:absolute; top:0; left:-40%; width:40%; height:100%; background:linear-gradient(90deg,transparent,#8fb0ff,transparent); animation:flowdash 2.4s linear infinite; }
.flow .node.on .dot { background:#4a6ff0; border-color:#8fb0ff; animation:pulse 2.4s ease-out infinite; }
@keyframes flowdash { to { left:100%; } }
@keyframes pulse { 0% { box-shadow:0 0 0 0 rgba(120,160,255,.55); } 100% { box-shadow:0 0 0 14px rgba(120,160,255,0); } }
.login-stats { display:flex; gap:26px; margin-top:34px; }
.login-stats div { font-size:12px; color:#7f8fb0; }
.login-stats b { display:block; font-size:22px; color:#e9eefc; font-variant-numeric:tabular-nums; letter-spacing:-.3px; }
.login-card { position:relative; width:420px; max-width:92vw; margin-left:auto; padding:36px 36px 30px; border-radius:20px;
  background:linear-gradient(160deg, rgba(20,29,54,.82), rgba(12,18,38,.88)); backdrop-filter:blur(18px); -webkit-backdrop-filter:blur(18px);
  border:1px solid rgba(143,176,255,.18); box-shadow:0 30px 80px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.06); }
.login-card::before { content:""; position:absolute; inset:-1px; border-radius:21px; padding:1px; pointer-events:none;
  background:linear-gradient(135deg, rgba(143,176,255,.55), transparent 35%, transparent 65%, rgba(194,166,255,.5));
  -webkit-mask:linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0); -webkit-mask-composite:xor; mask-composite:exclude; }
.login-card .brand { display:flex; align-items:center; gap:12px; margin-bottom:26px; }
.login-card .brand .mark { width:42px; height:42px; border-radius:12px; flex:none; display:grid; place-items:center;
  background:#0b1226; border:1px solid #2b3d6b; box-shadow:0 8px 24px rgba(95,227,255,.18); }
.login-card .brand .mark svg { width:100%; height:100%; display:block; }
.login-card .brand b { font-size:18px; color:#fff; background:none; border:0; padding:0; margin:0; }  /* 覆盖 app.css 顶栏 .brand b 的小标签样式 */
.login-card .brand .sub { color:#8b9ac0; font-size:12px; margin-top:2px; }
.login-card label.f { display:block; margin:12px 0 6px; color:#aebbd9; font-size:12.5px; letter-spacing:.04em; }
.login-card label.f:first-of-type { margin-top:0; }
.login-card label.f .hint { color:#6f7fa3; }
.login-card input[type=text], .login-card input[type=password] { width:100%; box-sizing:border-box; background:rgba(9,14,30,.7);
  border:1px solid rgba(143,176,255,.18); color:#eef2ff; border-radius:10px; padding:11px 13px; font-size:14px; transition:border-color .2s, box-shadow .2s; }
.login-card input:focus { outline:none; border-color:#6f93ff; box-shadow:0 0 0 3px rgba(111,147,255,.18), 0 0 24px rgba(111,147,255,.15); }
.login-card input::placeholder { color:#5a6a90; }
.login-card input.otp { letter-spacing:6px; font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; text-align:center; }
.login-card .btn.block { width:100%; height:auto; margin-top:6px; padding:12px 14px; font-size:14.5px; font-weight:650; letter-spacing:.06em; border:none; border-radius:11px;
  background:linear-gradient(92deg,#3b63f0,#6b4de8 60%,#3b63f0); background-size:200% 100%; color:#fff; box-shadow:0 10px 28px rgba(80,100,240,.4);
  transition:transform .15s, box-shadow .2s, background-position .5s; }
.login-card .btn.block:hover:not(:disabled) { background:linear-gradient(92deg,#3b63f0,#6b4de8 60%,#3b63f0); background-size:200% 100%; background-position:100% 0; color:#fff; transform:translateY(-1px); box-shadow:0 14px 34px rgba(80,100,240,.5); }
.login-card .btn.block:disabled { opacity:.7; transform:none; }
.login-card .spin { width:14px; height:14px; border-radius:50%; border:2px solid rgba(255,255,255,.35); border-top-color:#fff; animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.login-card .back { display:block; width:100%; margin-top:10px; padding:6px; background:none; border:none; color:#8fb0ff; font-size:12.5px; cursor:pointer; }
.login-card .back:hover { text-decoration:underline; }
.login-err { color:#ff8b8b; font-size:12.5px; margin-top:10px; min-height:18px; }
.login-foot { margin-top:18px; display:flex; justify-content:space-between; font-size:11.5px; color:#5f6f95; }
.login-foot .live { display:inline-flex; align-items:center; gap:6px; }
.login-foot .live i { width:7px; height:7px; border-radius:50%; background:#37d39a; box-shadow:0 0 10px #37d39a; animation:pulse 2s infinite; }
/* 夜枭眨眼与扫描线 */
.owl-eye { transform-origin:center; animation:owlblink 5.5s infinite; }
@keyframes owlblink { 0%,92%,100% { transform:scaleY(1); } 95% { transform:scaleY(.15); } }
.owl-scan { animation:owlscan 2.6s ease-in-out infinite; }
@keyframes owlscan { 0%,100% { transform:translateY(-4px); opacity:.25; } 50% { transform:translateY(4px); opacity:.9; } }
@media (max-width:900px) { .login-stage { grid-template-columns:1fr; padding:24px 6vw; align-content:center; } .login-hero { display:none; } .login-card { margin:0 auto; } }
@media (prefers-reduced-motion: reduce) { .flow .link::after, .flow .node.on .dot, .login-foot .live i, .owl-eye, .owl-scan { animation:none; } }
</style>
