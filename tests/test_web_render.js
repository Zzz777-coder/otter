// M3.5 前端冒烟测试:已知 markdown 样本 → 渲染函数 → 关键 DOM 断言。
// 运行:node tests/test_web_render.js(依赖 otter/web/marked.min.js,零浏览器)
"use strict";
const fs = require("fs");
const path = require("path");

const WEB = path.join(__dirname, "..", "otter", "web");

// ── 极简 DOM 桩:够 renderMarkdown/enhanceCodeBlocks 跑通即可 ──
// 2026-09-23 #43/#44 回归升级:补 parentNode/_parent 跟踪与 classList、class 解析——
// 否则测不了 mountAboveThinking 悬空守卫与 onEvent 事件渲染(卡片偶发缺失两根因)
class El {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this._html = ""; this.textContent = ""; this.style = {}; this.disabled = false; this.title = ""; this._parent = null; this._cl = new Set(); }
  set className(v) { this.attrs.class = v; v.split(/\s+/).filter(Boolean).forEach((c) => this._cl.add(c)); }
  get className() { return this.attrs.class || ""; }
  get parentNode() { return this._parent; }
  get classList() { const self = this; return { add: (c) => self._cl.add(c), remove: (c) => self._cl.delete(c), contains: (c) => self._cl.has(c), toggle: (c, force) => { const on = force !== undefined ? !!force : !self._cl.has(c); on ? self._cl.add(c) : self._cl.delete(c); return on; } }; }
  set innerHTML(v) {
    this._html = v; this.children = [];
    // 从 HTML 串里解析 class="..." 生成子桩(供 querySelector(".body") 等找到)
    const re = /class="([^"]*)"/g; let m;
    while ((m = re.exec(v)) !== null) {
      const el = new El("div"); el.className = m[1]; el._parent = this; this.children.push(el);
    }
  }
  get innerHTML() { return this._html; }
  appendChild(c) { c._parent = this; this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  replaceWith(w) { if (this._parent) { this._parent.children[this._parent.children.indexOf(this)] = w; w._parent = this._parent; } }
  querySelector(sel) {
    if (sel && sel.startsWith(".")) {
      // 2026-09-24 升级:递归 DFS 查找(运行历史块的 .run-sub 嵌在 .run-main 下,
      // 旧版只查直接子级测不到;现有用例均扁平,DFS 先序不改变命中结果)
      const cls = sel.slice(1);
      const walk = (node) => {
        for (const c of node.children) {
          if (c._cl.has(cls)) return c;
          const hit = walk(c);
          if (hit) return hit;
        }
        return null;
      };
      return walk(this);
    }
    return null;
  }
  querySelectorAll(sel) {
    if (sel === "pre") return this.children.filter((c) => c.tagName === "pre");
    if (sel && sel.startsWith(".")) return this.children.filter((c) => c._cl.has(sel.slice(1)));
    return [];
  }
  remove() { if (this._parent) { const i = this._parent.children.indexOf(this); if (i >= 0) this._parent.children.splice(i, 1); this._parent = null; } }
  addEventListener() {}   // 桩:交互绑定不执行
  insertBefore(c, ref) {
    c._parent = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i >= 0) this.children.splice(i, 0, c); else this.children.push(c);
    return c;
  }
  focus() {}
  closest() { return null; }
}
global.document = { createElement: (t) => new El(t) };
// 2026-09-24 记忆页用例配套:renderMemory 用 createTextNode 拼 M###+标题,桩补齐
global.document.createTextNode = (t) => { const e = new El("#text"); e.textContent = t; return e; };
// 补齐 app.js 顶层所需桩(必须在 eval 之前就位——顺序错误即桩失效)
global.document.querySelector = () => new El("div");
global.document.querySelectorAll = () => [];
global.document.body = new El("body");
global.window = global;
global.addEventListener = () => {};   // 桩:app.js 顶层绑定 pywebviewready
global.setInterval = () => 0;
global.clearInterval = () => {};
global.setTimeout = (fn) => { if (typeof fn === "function") fn(); return 0; };  // 防抖立即执行便于测试
global.clearTimeout = () => {};

// 加载 marked 与 app.js(间接 eval=全局作用域;function 声明进 global)
const markedSrc = fs.readFileSync(path.join(WEB, "marked.min.js"), "utf-8");
const appSrc = fs.readFileSync(path.join(WEB, "app.js"), "utf-8");
(0, eval)(markedSrc);
// app.js 首行 "use strict":严格模式下 eval 的 function 声明不外泄——
// 尾部追加显式导出(同作用域内赋值给 global)
(0, eval)(appSrc + "\n;global.__exports = { renderMarkdown, enhanceCodeBlocks, loadRuns, renderMemory, renderArtifacts, railDot };");
if (!global.__exports || typeof global.__exports.renderMarkdown !== "function") {
  console.error("renderMarkdown 导出失败");
  process.exit(1);
}
const renderMarkdown = global.__exports.renderMarkdown;

let failures = 0;
function check(name, cond) {
  if (cond) console.log(`  ✓ ${name}`);
  else { console.error(`  ✗ ${name}`); failures++; }
}

// 直接在沙箱里 eval app.js(桩 DOM 足够它顶层跑完),再调用全局可见的函数
try {
  eval(appSrc);
} catch (e) {
  console.error("app.js 加载失败(桩不足):", e.message);
  process.exit(1);
}

// marked 基本渲染
const el = () => new El("div");

// 1) GFM 表格
let t = el();
renderMarkdown(t, "| a | b |\n| --- | --- |\n| 1 | 2 |");
check("GFM 表格渲染为 <table>", t.innerHTML.includes("<table"));
check("表头单元格 <th>", t.innerHTML.includes("<th"));

// 2) 围栏代码块 → enhance 包 code-wrap(桩 querySelectorAll 返回空,验证 marked 输出即可)
t = el();
renderMarkdown(t, "```java\nimport java.util.Map;\n```\n");
check("围栏代码输出 <pre><code class=language-java>", t.innerHTML.includes('class="language-java"'));
check("marked 转义代码内容", !t.innerHTML.includes("<script"));

// 3) breaks:true 单换行
t = el();
renderMarkdown(t, "第一行\n第二行");
check("单换行即 <br>(breaks:true)", t.innerHTML.includes("<br"));

// 4) 标题/列表/粗体
t = el();
renderMarkdown(t, "## 标题\n- 项目一\n**粗体**");
check("## 渲染为 <h2>", t.innerHTML.includes("<h2"));
check("无序列表 <li>", t.innerHTML.includes("<li"));
check("粗体 <strong>/<b>", t.innerHTML.includes("<strong") || t.innerHTML.includes("<b"));

// 5) XSS 轻过滤
t = el();
renderMarkdown(t, "hi <script>alert(1)</script> ok");
check("script 标签被剥除", !t.innerHTML.includes("<script"));

// 6) diff 预览生成(Python 侧,顺带在此文件做跨层契约:DIFF 事件字段)
const { execSync } = require("child_process");
const py = (code) => execSync(
  `${path.join(__dirname, "..", ".venv", "bin", "python")} -c "${code.replace(/"/g, '\\"')}"`,
  { cwd: path.join(__dirname, "..") }).toString().trim();
const diffOut = py("from otter.diffpreview import preview_edit;"
  + "open('/tmp/_dp.txt','w').write('def a():\\n    return 1\\n');"
  + "print(preview_edit('/tmp/_dp.txt','return 1','return 2'))");
check("preview_edit 产出 unified diff(- 旧行)", diffOut.includes("-    return 1"));
check("preview_edit 产出 unified diff(+ 新行)", diffOut.includes("+    return 2"));

// 7) 「普通/计划」分段控件(2026-09-23 用户要求改版)+ 历史卡片
//    覆写 querySelector/querySelectorAll 返回持久桩,捕获分段激活态与状态栏写入值
const segNormal = new El("button"); segNormal.className = "seg-btn active"; segNormal.dataset = { mode: "normal" };
const segPlan = new El("button"); segPlan.className = "seg-btn"; segPlan.dataset = { mode: "plan" };
const statusEl = new El("footer");
const searchEl = new El("input"); searchEl.value = "";
const listEl = new El("div");
document.querySelectorAll = (sel) => (sel === ".seg-btn" ? [segNormal, segPlan] : []);
document.querySelector = (sel) =>
  sel === "#convSearch" ? searchEl :
  sel === "#convList" ? listEl :
  sel === "#statusbar" ? statusEl : new El("div");
check("otterUI.onMode 存在", typeof global.otterUI.onMode === "function");
global.otterUI.onMode({ mode: "plan" });
check("onMode(plan) 计划段激活", segPlan.classList.contains("active") && !segNormal.classList.contains("active"));
check("onMode(plan) 状态栏带 [PLAN]", String(statusEl.textContent).includes("[PLAN]"));
global.otterUI.onMode({ mode: "normal" });
check("onMode(normal) 普通段激活", segNormal.classList.contains("active") && !segPlan.classList.contains("active"));

// 7b) 历史卡片渲染(两行卡片+搜索过滤;sub 副行来自 Python _conv_payload)
global.otterUI.onConversations([
  { id: 1, title: "为产品添加库存预警功能并覆盖测试", active: true, sub: "9月23日 19:35 · 3 轮对话" },
  { id: 2, title: "写周报", active: false, sub: "9月22日 10:00 · 1 轮对话" },
]);
check("历史渲染 2 张卡片", listEl.children.length === 2);
check("卡片两行结构(标题+副行)", listEl.children[0].querySelector(".conv-title") !== null
  && listEl.children[0].querySelector(".conv-sub") !== null);
check("副行含轮对话文案", (listEl.children[0].querySelector(".conv-sub") || {}).textContent === "9月23日 19:35 · 3 轮对话");
searchEl.value = "周报";
global.otterUI.onConversations([
  { id: 1, title: "为产品添加库存预警功能并覆盖测试", active: true, sub: "9月23日 19:35 · 3 轮对话" },
  { id: 2, title: "写周报", active: false, sub: "9月22日 10:00 · 1 轮对话" },
]);
check("搜索『周报』过滤到 1 张", listEl.children.length === 1
  && listEl.children[0].querySelector(".conv-title").textContent === "写周报");
searchEl.value = "不存在的关键词";
global.otterUI.onConversations([{ id: 3, title: "别的", active: false, sub: "" }]);
check("无匹配显示空态", listEl.children.length === 1 && listEl.children[0]._cl.has("conv-empty"));
searchEl.value = "";

// 8) #44 回归(2026-09-23,产物卡片偶发缺失真根因):Python _js 只传一个合并对象
//    {"type":...,**payload},旧签名 onEvent(type,p) 里 type=整个对象 → 所有分支恒
//    false → 事件 UI 全部静默死亡。锁死:单对象调用必须渲染出对应 DOM。
//    同时锁 #43:onHistory 清屏后 thinkingEl 悬空必须自愈,事件继续可渲染。
const threadEl = new El("div");
document.querySelector = (sel) =>
  sel === "#thread" ? threadEl :
  sel === "#convSearch" ? searchEl :
  sel === "#convList" ? listEl :
  sel === "#statusbar" ? statusEl : new El("div");
// 重跑 app.js:让闭包捕获本段的单例 thread(旧桩每次 querySelector 返回新对象测不了)
(0, eval)(appSrc + "\n;global.__x = { getThread: () => thread };");
const otterUI2 = global.otterUI;  // 重跑后 window.otterUI 指向新对象
check("app.js 可加载且 otterUI 在", typeof otterUI2.onEvent === "function");

const cardsInThread = () => threadEl.querySelectorAll(".artifact-preview-card").length;
// 思考行 className 含 "meta system thinking",数提示行须排除它
const metasInThread = () => threadEl.children.filter((c) => c._cl.has("meta") && !c._cl.has("thinking")).length;

// 8a. 单对象契约(#44):MODEL_STARTED 渲染 step 行 + 思考行
const m0 = metasInThread();
otterUI2.onEvent({ type: "MODEL_STARTED", step: 1, mode: "normal" });
check("#44 单对象 onEvent:MODEL_STARTED 渲染了 meta 行", metasInThread() === m0 + 1);
check("#44 单对象 onEvent:思考行在 thread 中", threadEl.querySelectorAll(".thinking").length === 1);

// 8b. 单对象契约(#44):ARTIFACT 渲染 meta 行 + 完整卡片
otterUI2.onEvent({ type: "ARTIFACT", path: "demo/sample.py", name: "sample.py", note: "回归",
                   preview_type: "code", lang: "python", content: "print(1)\nprint(2)", size: 18 });
check("#44 ARTIFACT:meta 兜底行渲染", metasInThread() === m0 + 2);
check("#44 ARTIFACT:完整卡片渲染", cardsInThread() === 1);

// 8c. 双参形态兼容(#44 修复采用双形态兼容,别把旧调用方断掉)
otterUI2.onEvent("RUN_BUDGET_WARNING", { used: 10, budget: 100 });
check("#44 双参形态仍兼容", metasInThread() === m0 + 3);

// 8d. 悬空 thinkingEl 自愈(#43):onHistory 清屏(innerHTML="")后事件照常渲染
otterUI2.onHistory([]);
check("#43 onHistory 清屏后 thread 空", threadEl.children.length === 0);
otterUI2.onEvent({ type: "MODEL_STARTED", step: 2, mode: "normal" });
check("#43 清屏后 MODEL_STARTED 不丢(自愈)", threadEl.children.length >= 2
      && threadEl.querySelectorAll(".thinking").length === 1);
otterUI2.onEvent({ type: "ARTIFACT", path: "demo/b.bin", name: "b.bin", note: "",
                   preview_type: "binary", size: 7 });
check("#43 清屏后 ARTIFACT 卡片照常", cardsInThread() === 1);

// 8e. 流式期间落产物(卡片常见时序):卡片不被流式渲染冲掉
otterUI2.onDelta("分析中");
otterUI2.onEvent({ type: "ARTIFACT", path: "demo/c.txt", name: "c.txt", note: "",
                   preview_type: "text", content: "hello", size: 5 });
otterUI2.onDelta("收尾");
otterUI2.onDone({ summary: "done", final_text: "" });
check("流式中 ARTIFACT 卡片不被冲掉", cardsInThread() === 2);

// 8f. FILE_CHANGED(R5,2026-09-24 用户要求):📎 链接行渲染(图标+文件名超链接+
//     diffstat),点击行下方内联展开预览卡,再点收起
otterUI2.onEvent({ type: "FILE_CHANGED", path: "demo/sample.py", name: "sample.py",
                   action: "已修改", plus: 2, minus: 1,
                   preview_type: "code", lang: "python", content: "print(1)", size: 8 });
const linkLine = threadEl.children.find((c) => c._cl.has("file-changed"));
check("R8 链接行=文件图标+文案", linkLine !== undefined
  && linkLine.querySelector(".file-ico") !== null
  && linkLine.children[1] && linkLine.children[1].textContent.includes("已修改"));
const linkEl2 = linkLine && linkLine.querySelector(".file-link");
check("R5 文件名为超链接", linkEl2 !== null && linkEl2.textContent === "sample.py");
const r5Base = cardsInThread();
linkEl2.onclick();
check("R5 点击文件名内联展开预览卡 +1", cardsInThread() === r5Base + 1);
check("R8 diffstat (+2/-1) 在行内(第4子元素)", linkLine.children[3]
  && linkLine.children[3].textContent === " (+2/-1)");
linkEl2.onclick();
check("R5 再点收起", cardsInThread() === r5Base);

// 9) 运行历史块渲染(2026-09-24 用户要求:整屏行分布记录块,取消左右分栏)。
//    覆写 querySelector 返回持久 #runsList 桩 + 假 pywebview.api,断言块结构/如实中断标注/行内 Trace。
//    loadRuns 是 async,收尾汇总挪进 IIFE 等 microtask 跑完再打印。
(async () => {
  const runsList = new El("div");
  const _q = document.querySelector;
  document.querySelector = (s) => (s === "#runsList" ? runsList : _q(s));
  global.window.pywebview = { api: {
    get_runs: async () => [
      { id: 9, conv: 9, summary: "在桌面创建 test3.txt 写入 hello 然后发布它", mode: "普通",
        time: "9月24日 10:11", label: "完成", kind: "ok", done: true, count: 3 },
      { id: 8, conv: 8, summary: "写一首十四行诗鼓励我", mode: "计划",
        time: "9月23日 20:25", label: "中断", kind: "warn", done: false, count: 1 },
    ],
    get_run_detail: async () => "Run #12 — Trace\n==============================================\n20:11:01  ▶ step 1 模型调用",
    switch_conversation: async (cid) => { global.__switchedCid = cid; },
  } };
  await global.__exports.loadRuns();
  check("历史按会话渲染 2 条", runsList.children.length === 2);
  const b1 = runsList.children[0], b2 = runsList.children[1];
  check("条1 左侧=已完成 汉字", b1.querySelector(".run-flag") !== null
    && b1.querySelector(".run-flag").textContent === "已完成");
  check("条2 左侧=未完成 汉字", b2.querySelector(".run-flag").textContent === "未完成");
  check("条1 正常完成不出右侧胶囊(已完成已表达)", b1.querySelector(".run-pill") === null);
  check("条2 胶囊=中断(会话级如实标注)", b2.querySelector(".run-pill").textContent === "中断");
  check("最左为左栏(已完成+下方小号会话id+次数)", b1.children[0]._cl.has("run-left")
    && b1.children[0].querySelector(".run-flag").textContent === "已完成"
    && b1.children[0].querySelector(".run-ids").textContent === "会话 #9 · 3 次运行");
  check("右侧模式列", b1.querySelector(".run-mode").textContent === "普通"
    && b2.querySelector(".run-mode").textContent === "计划");
  check("右侧时间列", b1.querySelector(".run-time").textContent === "9月24日 10:11");
  // 会话详情按钮:时间下方,点击调 switch_conversation 跳对话页(不触发行的 Trace 展开)
  const right1 = b1.querySelector(".run-right");
  check("右列=时间+下方会话详情按钮", right1 !== null
    && right1.querySelector(".run-time") !== null
    && right1.querySelector(".run-detail-btn") !== null
    && right1.querySelector(".run-detail-btn").textContent === "会话详情");
  const beforeTrace = b1.querySelector(".run-detail");
  await right1.querySelector(".run-detail-btn").onclick();
  check("按钮触发 switch_conversation(会话9)", global.__switchedCid === 9);
  check("按钮未触发行的 Trace 展开", b1.querySelector(".run-detail") === null && beforeTrace === null);
  check("摘要为标题行", b1.querySelector(".run-title").textContent === "在桌面创建 test3.txt 写入 hello 然后发布它");
  // R8:行=会话,点击整行也跳转到该会话
  await b1.onclick();
  check("R8 点击整行跳转会话(switch_conversation)", global.__switchedCid === 9);
  check("R8 会话行不展开 Trace(跳转取代)", b1.querySelector(".run-detail") === null);

  // 10) 长期记忆页(2026-09-24 新增):renderMemory 两区块渲染+搜索过滤+空态
  const coreListEl = new El("div"), memListEl = new El("div"), memSearchEl = new El("input");
  const _qm = document.querySelector;
  document.querySelector = (s) => (s === "#coreList" ? coreListEl
    : s === "#memList" ? memListEl : s === "#memSearch" ? memSearchEl : _qm(s));
  const { renderMemory } = global.__exports;
  renderMemory({
    core: [{ key: "偏好", value: "结论先行", reason: "用户要求", source_quote: "给我结论先行", updated_at: "2026-09-24" }],
    entries: [{ mid: "M001", title: "排版偏好", summary: "要简洁", content: "正文内容", revision: 2, access_count: 3, mtime: "09月24日 10:00" }],
  }, "");
  check("记忆页 Core 条目主行 key:value", coreListEl.children.length === 1
    && coreListEl.children[0].children[0].textContent === "偏好:结论先行");
  check("记忆页 Core 副行=依据+日期", coreListEl.children[0].children[1].textContent.includes("用户要求")
    && coreListEl.children[0].children[1].textContent.includes("2026-09-24"));
  check("记忆页普通记忆 M###+标题", memListEl.children.length === 1
    && memListEl.children[0].children[0].children[0].textContent === "M001"
    && memListEl.children[0].children[0].children[1].textContent === "排版偏好");
  check("记忆页摘要/元数据/正文在结构内", memListEl.children[0].children[1].textContent === "要简洁"
    && memListEl.children[0].children[2].textContent.includes("rev 2")
    && memListEl.children[0].children[3].textContent === "正文内容");
  renderMemory({ core: [], entries: [] }, "");
  check("记忆页空数据显示空态", coreListEl.children.length === 1
    && coreListEl.children[0]._cl.has("conv-empty") && memListEl.children[0]._cl.has("conv-empty"));
  renderMemory({ core: [{ key: "偏好", value: "x", reason: "", source_quote: "", updated_at: "" }], entries: [] }, "不存在词");
  check("记忆页搜索无命中→空态", coreListEl.children[0]._cl.has("conv-empty"));
  renderMemory({ core: [{ key: "偏好", value: "x", reason: "", source_quote: "", updated_at: "" }],
                 entries: [{ mid: "M009", title: "别的", summary: "", content: "", revision: 1, access_count: 0, mtime: "" }] }, "M009");
  check("记忆页搜索命中 mid 字段", coreListEl.children[0]._cl.has("conv-empty")
    && memListEl.children.length === 1
    && memListEl.children[0].children[0].children[0].textContent === "M009");

  // 11) 交付物页(2026-09-24 新增):renderArtifacts 列表/空态/搜索/点击内联展开预览卡
  const artListEl = new El("div"), artSearchEl = new El("input");
  document.querySelector = (s) => (s === "#artList" ? artListEl
    : s === "#memSearch" ? memSearchEl : s === "#artSearch" ? artSearchEl : _qm(s));
  // 桩:preview_file 按需补拉 + open_artifact(卡片三通道按钮)
  global.window.pywebview.api.preview_file = async () => ({ preview_type: "text", content: "预览正文", size: 9 });
  global.window.pywebview.api.open_artifact = async () => "[otter] 已打开";
  const { renderArtifacts } = global.__exports;
  const sampleArts = [
    { id: "20260924-2", name: "report.pdf", path: "/tmp/report.pdf", size: 1024, note: "周报", run_id: 7, sha256: "abc123", created: "2026-09-24 10:00" },
    { id: "20260924-1", name: "data.csv", path: "/tmp/data.csv", size: 2048, note: "", run_id: 6, sha256: "def456", created: "2026-09-24 09:00" },
  ];
  renderArtifacts(sampleArts, "");
  check("交付物页渲染 2 条(最新在前)", artListEl.children.length === 2
    && artListEl.children[0].children[0].textContent === "report.pdf");
  check("交付物副行=note", artListEl.children[0].children[1].textContent === "周报");
  check("交付物元数据含大小/时间/run/sha", artListEl.children[0].children[2].textContent.includes("1.0KB")
    && artListEl.children[0].children[2].textContent.includes("Run #7")
    && artListEl.children[0].children[2].textContent.includes("sha abc123"));
  const artItem = artListEl.children[0];
  await artItem.onclick();  // 点击行 → 按需补拉预览 → 内联展开产物卡
  check("点击交付物行内联展开预览卡", artItem.__card !== null && artItem.__card._cl.has("artifact-preview-card"));
  await artItem.onclick();
  check("再点收起预览卡", artItem.__card === null);
  renderArtifacts(sampleArts, "data");
  check("交付物搜索过滤命中 1 条", artListEl.children.length === 1
    && artListEl.children[0].children[0].textContent === "data.csv");
  renderArtifacts([], "");
  check("交付物空数据显示空态", artListEl.children.length === 1
    && artListEl.children[0]._cl.has("conv-empty"));

  // 12) rail 徽标(2026-09-24 新增):railDot 点亮/熄灭/幂等;onDone→runs 亮;loadRuns→熄
  const chatBtn = new El("button"), runsBtnEl = new El("button"), artsBtn = new El("button");
  document.querySelector = (s) => {
    if (s.includes('data-page="chat"')) return chatBtn;
    if (s.includes('data-page="runs"')) return runsBtnEl;
    if (s.includes('data-page="artifacts"')) return artsBtn;
    return (s === "#artList") ? artListEl : (s === "#memSearch") ? memSearchEl
      : (s === "#artSearch") ? artSearchEl : _qm(s);
  };
  const { railDot } = global.__exports;
  railDot("chat", true);
  check("railDot 点亮:chat 键出现 nav-dot", chatBtn.children[0] !== undefined
    && chatBtn.children[0]._cl.has("nav-dot"));
  railDot("chat", true);
  check("railDot 幂等:重复点亮不重复加", chatBtn.querySelectorAll(".nav-dot").length === 1);
  railDot("chat", false);
  check("railDot 熄灭:移除 nav-dot", chatBtn.querySelectorAll(".nav-dot").length === 0);
  railDot("runs", false);  // 无 dot 时熄灭不炸
  check("railDot 无 dot 时熄灭安全", runsBtnEl.children.length === 0);
  // 联动:onDone → runs 键亮;loadRuns → 熄
  otterUI2.onDone({ summary: "完成", final_text: "答案" });
  check("onDone 后 runs 键点亮", runsBtnEl.children[0] !== undefined
    && runsBtnEl.children[0]._cl.has("nav-dot"));
  await global.__exports.loadRuns();
  check("loadRuns 后 runs 键熄灭", runsBtnEl.children.length === 0);
  // 联动:ARTIFACT 事件 → artifacts 键亮
  otterUI2.onEvent({ type: "ARTIFACT", path: "demo/x.txt", name: "x.txt", note: "",
                     preview_type: "text", content: "hi", size: 2 });
  check("ARTIFACT 事件后 artifacts 键点亮", artsBtn.children[0] !== undefined
    && artsBtn.children[0]._cl.has("nav-dot"));

  console.log(failures === 0 ? "\n全部通过 ✓" : `\n${failures} 项失败 ✗`);
  process.exit(failures === 0 ? 0 : 1);
})();
