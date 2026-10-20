# Axure 原型需求无损提取 — 完整处理流程与逻辑

> 以「连赢挑战.zip」为例总结，流程可复用于任何 Axure HTML 导出包。目标：**不丢失任何需求语句，并能用机器证明没有丢失**。

---

## 0. 总体思路

Axure 导出的 HTML 不是普通文档：每一段文字、表格单元格、流程框都是一个绝对定位的 `div` 控件，阅读顺序由坐标决定，不由 DOM 顺序决定；一部分示意内容只以图片形式存在。因此提取不能靠"复制网页文字"，而要：

1. 把每个控件当作最小单元，带坐标提取；
2. 按坐标还原阅读顺序；
3. 用两条独立的通道（HTML 文字 + 图片人工查看）覆盖全部内容；
4. 用多重集（multiset）字符比对和逐行包含比对两种方式做机器校验；
5. 需求语句逐字保留，只做归类，不做改写；疑点单列。

流程分六个阶段：**解包识别 → 结构分析 → 自动提取 → 多级校验 → 图片补充 → 组织成文 + 终验**。

---

## 1. 解包与识别（判断输入是什么）

```bash
unzip -o -q 连赢挑战.zip -d <隔离目录>
find . -type f | wc -l            # 1176 个文件
ls 连赢挑战/                       # *.html, files/, images/, data/, plugins/, resources/
```

判断依据：
- 根目录有 `start.html / index.html / data/document.js / plugins/` → Axure HTML 导出；
- `files/<页面名>/styles.css` 与 `<页面名>.html` 一一对应 → 每个页面一套样式；
- `images/<页面名>/uNNN.png|svg` → 控件渲染成的图片。
- `__MACOSX/`、`.DS_Store` 直接忽略。

安全处理：解压到独立空目录，脚本放在另一目录，Python 以 `-I` 运行，避免执行包内任何代码。

结果：6 个内容页面——`更新记录 / 需求说明 / pc-活动入口说明 / pc-活动详情页界面说明 / 移动-活动入口说明 / 移动-活动详情页界面说明`。

---

## 2. 结构分析（弄清文字藏在哪里）

抽样查看一个页面的 HTML 与 CSS 后确认 Axure 的控件模式：

```html
<div id="u33" class="ax_default _段落">
  <div id="u33_div"></div>
  <div id="u33_text" class="text"><p><span>……</span></p>…</div>
</div>
```
```css
#u33 { position:absolute; left:101px; top:987px; width:1227px; height:544px; }
```

关键结论：
| 事实 | 对提取的影响 |
|---|---|
| 文字都在 `#uNNN_text` 内的 `<p>` 里 | 以 `<p>` 为行单位取文本，保留原始分行 |
| 坐标在 `files/<页>/styles.css` 的 `#uNNN{left,top}` 里 | 必须解析 CSS 才能得到位置 |
| 表格 = 多个 `table_cell` 控件，流程图 = `box_1` + `_连接` | 表格/流程图需按坐标重组 |
| 子控件坐标相对父控件 | 绝对坐标 = 沿父链累加 |
| 图片控件 `<img id="uNNN_img" src=…>` | 记录 src，后续人工看图 |
| class 带 `_段落 / _三级标题 / table_cell / box_1 / label` 等 | 可用于区分需求段落与示例界面元素 |
| HTML 内有 `<!-- Unnamed (矩形) -->` 注释 | 校验时需排除注释节点 |
| 后台原型常有表单控件：`<select id="uNNN_input"><option>` 下拉框、`<input id="uNNN_input" value=…>` 输入框 | 文字不在 `_text` 里，必须单独抓取（第二个包首轮校验即因此漏掉 5 页的枚举值，修正后归零） |

---

## 3. 自动提取（extract.py）

对每个页面执行 `extract.py <page.html> <styles.css> <out.json>`，逻辑：

1. **解析 CSS**：正则 `#(u\d+)\s*\{…\}` 取 `left/top/width/height`，得到 `pos[uid]`。
2. **遍历控件**：BeautifulSoup 找所有 `id=^u\d+$` 的元素。
3. **绝对坐标**：从该元素向上遍历祖先，凡 id 在 `pos` 中的都把 `left/top` 累加。
4. **取文本**：找 `#uid_text`，对其中每个 `<p>` 取 `get_text()`，`\xa0`→空格，去首尾空白，丢弃空行；没有 `<p>` 时整体取文本。
5. **取图片**：找 `#uid_img` 的 `src`。
   5b. **取表单控件**：找 `select#uid_input` 的全部 `<option>` → 记为 `[下拉选项] a / b / c`；找 `input|textarea#uid_input` 的 `value`/`placeholder` → 记为 `[输入值]` / `[占位提示]`（过滤 `value="checkbox"` 噪声）。
6. **隐藏状态**：检查 inline style 的 `display:none / visibility:hidden` 并标记（本例无隐藏控件；若有动态面板隐藏态，也会被提取并标 HIDDEN，不会漏）。
7. **排序**：`sort(key=(round(y/4), x))` —— 先按行（4px 容差合并同一行），再按列，得到自上而下、自左而右的阅读顺序。
8. **输出**：JSON（结构化，供校验）+ TXT（`[uNNN class @(x,y) wxh]` + 文本行，供人阅读）。

产出规模：6 页共 1770 条文字行，HTML 文字约 20,400 个非空字符。

---

## 4. 第一级校验：HTML 原文 vs 提取结果（verify.py）

目的：证明"脚本没有漏掉任何 HTML 里的文字"。

方法（与提取逻辑**相互独立**，不复用控件解析）：
1. 原始 HTML 去掉 `script/style/title`，`get_text()` 得到全页文字；
2. 统计非空白字符的 `Counter`（多重集）；
3. 对提取 JSON 的全部文本行做同样统计；
4. `raw - extracted` 为缺失，`extracted - raw` 为多余；
5. 另外列出所有不在 `*_text` 容器内的文本节点，检查是否有被遗漏的文字位置。

结果：6/6 页面缺失集合均为 `{'﻿':1}`（文件头 BOM，非内容），多余为空；`*_text` 外的文本节点全部是 Axure 注释 `Unnamed (矩形)`（注释不属于内容）。→ **HTML 文字 0 遗漏**。

---

## 5. 通读与理解（人工阅读 1770 行）

逐页完整阅读 TXT 输出，目的有三：
- 确认排序结果符合原型视觉布局（例如表格行列能对上、流程图框与 Y/N 连接线能对上）；
- 区分三类内容：**需求语句**（段落、规则说明、注释）、**界面示例元素**（标题/按钮/占位数据 ¥888,888、XXXXXXX）、**结构标注**（placeholder 区域说明）；
- 记录原文疑点（错别字、PC/移动口径不一致、占位符 `****`）。

此步不做任何改写，只做标记。

---

## 6. 第二通道：图片内容核查

HTML 校验只能证明"文字层"完整，示意图可能带文字。处理：

1. 从 JSON 收集所有被引用的 PNG（93 张，去重后仍 93 张），按文件大小/尺寸排序；
2. 用 PIL 把图片按主题拼成 6 张 contact sheet（每张 ≤ 2160px 宽，带文件名标签）；
3. 逐张查看，对照 HTML 文字：
   - 与 HTML 一致的 → 不重复记录；
   - 只在图里出现的（弹窗提示语、按钮名、B 面展开示例文案、额度记录筛选项）→ 单独列入"仅图片文案"一节；
   - 纯界面截图（大厅定位图、手机壳）→ 只记录 HTML 中对应的标注框文字。
4. 表格单元格背景小图（`u86.png` 等）判定为合并单元格的空白渲染，无文字。

---

## 7. 组织成文（按主题归类，逐字保留）

结构设计原则：
- **正文 1–8 节**：按"背景 → 全局规则 → PC 入口 → PC 详情 → 移动入口 → 移动详情 → 后台范围"归类；每条需求语句原样复制（含错别字），只调整编号样式与层级；表格/流程图用 Markdown 表格 + 文本流程还原。
- **第 9 节**：仅图片文案。
- **第 10 节**：疑点/不一致清单（供需求澄清与测试用例设计）。
- **第 11 节附录**：脚本从 JSON 自动生成的逐控件原文（带 `[uNNN]`，重复控件合并标 ×N），作为可追溯底稿。

附录自动生成避免了手工誊写造成的二次丢失。

---

## 8. 终验：原文 vs 最终文档（三轮）

### 8.1 逐行包含校验（最严格）
对 1770 条原文行，去空白后检查是否出现在最终文档中：**0 条缺失**。

### 8.2 字符多重集校验
原始 HTML 字符集 − 文档字符集 = 只剩重复控件被 ×N 合并后的数字/占位符差异，符合预期。

### 8.3 正文覆盖校验（判断归类时是否漏了需求语句）
只用正文（去掉附录）做逐行包含，并对编号前缀做归一化（原文 `1、` / 正文 `1.`；`注：1、`；`1️⃣`）：残差 68 条，逐条人工分类——
- 章节标题（已转为 Markdown 标题）
- 示例表格数据行（`XXXXXXX 11 150 ¥8,888`）
- 多空格排版的示例数值（`#7  #3` / `¥368  ¥688`）
→ 全部为非需求内容，**正文无需求语句遗漏**。

---

## 9. 关键决策与取舍

| 决策 | 原因 |
|---|---|
| 以控件而非 DOM 顺序为单位 | Axure DOM 顺序 ≠ 视觉顺序，直接复制会打乱表格与流程图 |
| 行容差 4px | 同一行控件 top 常相差 1–3px；过大则串行 |
| 校验用独立方法（整页 get_text） | 避免"用同一段逻辑验证自己"的盲区 |
| 需求语句逐字保留错别字 | 用户要求与原文一致；改写会引入语义风险，疑点改为单列 |
| 附录由脚本生成 | 人工誊写是丢失的主要来源 |
| 图片人工查看而非 OCR | 图片数量可控，人工识别对设计稿的准确率更高；OCR 会引入噪声 |
| 重复控件 ×N 合并 | 同一示例在多张示意图中重复，合并不影响完整性且便于阅读 |

---

## 10. 复用清单（做成智能体时的步骤模板）

1. 解压到隔离目录 → 识别导出类型 → 列出页面与资源映射。
2. 抽样一页，确认控件/坐标/文本/图片的模式。
3. 运行 `extract.py` 得到 JSON + TXT。
4. 运行 `verify.py`：HTML 字符多重集 vs JSON，必须为 0 缺失（允许 BOM）。
5. 通读 TXT，标记需求/示例/结构三类，记录疑点。
6. 图片去重 → contact sheet → 人工核查，补充"仅图片文案"。
7. 写正文（逐字）+ 脚本生成附录。
8. 终验三轮：逐行包含、字符多重集、正文覆盖（编号归一化后人工分类残差）。
9. 交付文档 + 校验报告 + 疑点清单。

---

## 附：脚本

### extract.py
```python
#!/usr/bin/env python3
import sys, re, json
from bs4 import BeautifulSoup

html_path, css_path, out_path = sys.argv[1:4]
html = open(html_path, encoding='utf-8').read()
css = open(css_path, encoding='utf-8').read()

pos = {}
for m in re.finditer(r'#(u\d+)\s*\{([^}]*)\}', css):
    wid, body = m.group(1), m.group(2)
    d = {}
    for k in ('left', 'top', 'width', 'height'):
        mm = re.search(rf'(?<![\w-]){k}:\s*(-?[\d.]+)px', body)
        if mm: d[k] = float(mm.group(1))
    if 'left' in d and 'top' in d: pos[wid] = d

soup = BeautifulSoup(html, 'html.parser')

def abs_pos(el):
    x = y = 0.0; cur = el
    while cur is not None and getattr(cur, 'get', None):
        wid = cur.get('id')
        if wid in pos: x += pos[wid]['left']; y += pos[wid]['top']
        cur = cur.parent
    return x, y

def text_of(el):
    lines = [p.get_text('').replace('\xa0', ' ').strip() for p in el.find_all('p')]
    if not lines:
        t = el.get_text('\n', strip=True).replace('\xa0', ' ')
        lines = [t] if t else []
    return [l for l in lines if l]

items = []
for el in soup.find_all(id=re.compile(r'^u\d+$')):
    wid = el['id']; cls = ' '.join(el.get('class', []))
    x, y = abs_pos(el)
    w = pos.get(wid, {}).get('width'); h = pos.get(wid, {}).get('height')
    tdiv = el.find(id=f'{wid}_text'); text = text_of(tdiv) if tdiv else []
    img = el.find('img', id=f'{wid}_img'); src = img['src'] if img else None
    style = el.get('style', '').replace(' ', '')
    hidden = 'display:none' in style or 'visibility:hidden' in style
    if text or src:
        items.append(dict(id=wid, cls=cls, x=x, y=y, w=w, h=h, text=text, img=src, hidden=hidden))

items.sort(key=lambda d: (round(d['y'] / 4), d['x']))
json.dump(items, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
for it in items:
    tag = f"[{it['id']} {it['cls']} @({int(it['x'])},{int(it['y'])}) {int(it['w'] or 0)}x{int(it['h'] or 0)}{' HIDDEN' if it['hidden'] else ''}]"
    if it['text']:
        print(tag); [print('   ', l) for l in it['text']]
    elif it['img']:
        print(tag, 'IMG', it['img'])
```

### verify.py
```python
#!/usr/bin/env python3
import sys, json, re
from collections import Counter
from bs4 import BeautifulSoup

html_path, json_path = sys.argv[1:3]
soup = BeautifulSoup(open(html_path, encoding='utf-8').read(), 'html.parser')
for t in soup(['script', 'style', 'title']): t.decompose()
raw_chars = Counter(c for c in soup.get_text(' ') if not c.isspace())
items = json.load(open(json_path, encoding='utf-8'))
ext_chars = Counter(c for l in (x for it in items for x in it['text']) for c in l if not c.isspace())
print("raw:", sum(raw_chars.values()), "extracted:", sum(ext_chars.values()))
print("MISSING:", dict(raw_chars - ext_chars) or "none")
print("EXTRA:", dict(ext_chars - raw_chars) or "none")
```

### 终验（逐行包含 + 编号归一化）
```python
import json, re
doc = open('最终文档.md', encoding='utf-8').read()
body = doc.split('## 11. 附录')[0]
def norm(s):
    s = re.sub(r'\s+', '', s)
    return re.sub(r'^(注[:：])?(\d+|[①②③④]|[1-9]️⃣)[、.．]', '', s)
docn = re.sub(r'\s+', '', doc); bodyn = re.sub(r'\s+', '', body)
miss_doc, miss_body = [], []
for p in pages:
    for it in json.load(open(f'out/{p}.json', encoding='utf-8')):
        for l in it['text']:
            if re.sub(r'\s+', '', l) not in docn: miss_doc.append((p, it['id'], l))
            if norm(l) not in bodyn: miss_body.append((p, it['id'], l))
# miss_doc 必须为空；miss_body 逐条人工判定为标题/示例数据
```
