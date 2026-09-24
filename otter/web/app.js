// otter GUI v8 — 前端桥与渲染。
// 渲染层(2026-09-22 第四次修正):弃用全部手写 markdown
// 解析器(三轮启发式修补均有边界错误),改用 marked(MIT
// 标准库)+ breaks 单换行;代码框为 marked 输出的
// 后处理增强(GitHub 式头部条+复制键)。
// Python 侧通过 evaluate_js 调 window.otterUI.*;JS 通过 window.pywebview.api.* 调 Python。
"use strict";

const $ = (sel) => document.querySelector(sel);
const thread = $("#thread");
const input = $("#input");
const sendBtn = $("#send");
let busy = false;
let currentAssistant = null;
let pendingChars = 0;

// ── "思考中"指示 + 任务级计时 ────────────────────────────────────
// 修正(2026-09-22):submit 后立即显示(此前等首个 MODEL_STARTED,首字延迟期
// 3-10 秒空窗——正是用户盯着看的时候,以为秒表没跳)
let thinkingEl = null;
let taskStartTime = 0;
let thinkingTimer = null;
let rawBuf = "";           // 2026-09-22 流式渲染:当前块的原始 markdown 缓冲
let streamRenderTimer = null;

function fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}`;
}
function refreshThinking() {
  // 2026-09-23 用户定版:秒表=chat 内常驻行「otter 正在思考中(已思考 Xs·已生成 N 字)」,
  // 全程显示到任务结束(流式/工具轮期间也不消失);状态栏不再承担计时
  const elapsed = taskStartTime ? fmtElapsed(Date.now() - taskStartTime) : "0s";
  if (thinkingEl) {
    thinkingEl.textContent = `otter 正在思考中(已思考 ${elapsed} · 已生成 ${pendingChars} 字)`;
  }
}
function ensureThinking() {
  // 修复(2026-09-23 #43):thinkingEl 悬空(thread 被整体清空)时自愈重建——
  // 否则"正在思考中"行会写进已分离节点,界面看不到且永不恢复
  if (thinkingEl && thinkingEl.parentNode !== thread) thinkingEl = null;
  if (!thinkingEl) {
    thinkingEl = document.createElement("div");
    thinkingEl.className = "meta system thinking";
    thread.appendChild(thinkingEl);
  }
  if (!thinkingTimer) thinkingTimer = setInterval(refreshThinking, 1000);
  refreshThinking();
  scrollBottom();
}
function showThinking() { ensureThinking(); }
function hideThinkingKeepTimer() {  // 换指示不掐秒表
  if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
}
function hideThinking() {
  hideThinkingKeepTimer();
  if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
  pendingChars = 0;
}
// 2026-09-23:新内容永远插到思考行**上面**——思考行全程钉在对话流底部直到任务结束
// 修复(2026-09-23 #43):onHistory/#newConv 用 innerHTML="" 清空 thread 后 thinkingEl
// 仍指向已分离节点——悬空参照令 insertBefore 抛 NotFoundError,连锁丢掉整批后续事件
// (meta/卡片/气泡全部不上屏,且 pywebview 会静默吞错)。统一走 mountAboveThinking:
// 参照失效即自愈置空并降级 appendChild,任何路径不再抛
function mountAboveThinking(el) {
  if (thinkingEl && thinkingEl.parentNode !== thread) thinkingEl = null;  // 悬空守卫(自愈)
  if (thinkingEl) thread.insertBefore(el, thinkingEl);
  else thread.appendChild(el);
  scrollBottom();
}
function insertAboveThinking(el) { mountAboveThinking(el); }

// 流式渲染:delta 追加进缓冲,防抖后用 marked 增量重渲染当前块(格式渐现,无裸文本期)
function scheduleStreamRender() {
  if (streamRenderTimer) return;
  streamRenderTimer = setTimeout(() => {
    streamRenderTimer = null;
    if (currentAssistant) {
      renderMarkdown(currentAssistant.querySelector(".body"), rawBuf);
      scrollBottom();
    }
  }, 120);
}

// ── Python → JS 回调入口 ─────────────────────────────────────────
window.otterUI = {
  onBackendReady(payload) {
    $("#statusbar").textContent = `就绪 · 模型 ${payload.model}`;
    $("#modelBadge").textContent = payload.model;
    renderConvs(payload.conversations || []);
  },
  // 2026-09-23 用户要求改版:单键 Act/Plan → 「普通/计划」分段控件;状态单一事实源
  // 仍在 Python(toggle_mode 回推),此处只做渲染——激活段/placeholder/状态栏/chat 提示行
  onMode(payload) {
    const plan = payload.mode === "plan";
    document.querySelectorAll(".seg-btn").forEach((b) =>
      b.classList.toggle("active", plan ? b.dataset.mode === "plan" : b.dataset.mode === "normal"));
    input.placeholder = plan ? "计划模式:只读调查与规划,不修改文件…" : "告诉 Otter 你想完成什么…";
    $("#statusbar").textContent = plan ? "[PLAN] 只读模式" : "就绪";
    meta("system", plan
      ? "── 已切换到 PLAN 只读模式(检索与分析,不写文件)──"
      : "── 已切换到 ACT 执行模式(可写文件)──");
  },
  onDelta(text) {
    pendingChars += text.length;
    if (!currentAssistant) {          // 首 delta:开流式块(思考行保留,钉在底部)
      startAssistant();
      rawBuf = "";
    }
    rawBuf += text;
    scheduleStreamRender();
    refreshThinking();
  },
  onEvent(arg0, arg1) {
    // 修复(2026-09-23 #44,产物卡片问题真根因):Python 侧 _js("onEvent", {"type":...,**payload})
    // 只传**一个合并对象**——原签名 (type, p) 里 type 接到的是整个对象,所有字符串比较
    // 恒 false,事件 UI(step/tool/预算/PLAN/ARTIFACT meta+卡片)全部静默死亡且无任何报错。
    // 探针(--gui-artifact-probe)0/15 暴露:handler 正常返回、DOM 零变化。兼容两种调用形态。
    const type = typeof arg0 === "string" ? arg0 : (arg0 && arg0.type);
    const p = (typeof arg0 === "string" ? arg1 : arg0) || {};
    // 修复(2026-09-23 #43):整个分发包 try/catch——任一分支抛错会让本次 evaluate_js
    // 整体失败且被 pywebview 静默吞掉(前端表现为"事件随机丢"),现在至少落 __errLog 可查
    try {
    // 2026-09-23:PLAN 模式的 step 行加徽标(模式已常驻,run 级 mode 随事件携带)
    if (type === "MODEL_STARTED") { finalizeAssistant(); rawBuf = ""; ensureThinking(); meta("system", `── step ${p.step}${p.mode === "plan" ? " · PLAN" : ""} ──`); }
    else if (type === "TOOL_STARTED") {
      const args = JSON.stringify(p.arguments || {});
      meta("tool", `⚡ ${p.name} ${args.length > 130 ? args.slice(0, 130) + "…" : args}`);
    }
    else if (type === "TOOL_COMPLETED") { $("#statusbar").textContent = `工具 ${p.name} 完成`; }
    else if (type === "RUN_FINALIZING") { meta("system", "! 达到最大步数,收尾总结中"); }
    // 修正(2026-09-23):DIFF_PREVIEW 不在事件路径建卡——gate(onDiffPreview)路径会建,
    // 双路径导致重复卡片/竞态;此处仅留提示,卡片由 gate 统一创建
    else if (type === "DIFF_PREVIEW") { /* 卡片由 onDiffPreview(gate)创建 */ }
    else if (type === "ARTIFACT") {
      // 2026-09-23 调试:先用已验证的 meta() 显示一行,同时尝试渲染完整卡片
      meta("ok", `📦 产物已创建: ${p.name || p.path || "?"} (${fmtSize(p.size || 0)})`);
      try { showArtifactCard(p); } catch (e) {
        (window.__errLog = window.__errLog || []).push("showArtifactCard error: " + String(e));
      }
      railDot("artifacts", true);  // 2026-09-24 rail 徽标:有新交付物,artifacts 键亮点
    }
    // 2026-09-24 R5(用户要求):写类工具成功 → 📎 链接行(图标+文件名超链接,点击 chat 内预览)
    else if (type === "FILE_CHANGED") {
      try { showFileLink(p); } catch (e) {
        (window.__errLog = window.__errLog || []).push("showFileLink error: " + String(e));
      }
    }
    else if (type === "RUN_BUDGET_WARNING") { meta("system", `💰 预算提醒 ${p.used}/${p.budget} token`); }
    else if (type === "RUN_BUDGET_FINALIZING") { meta("system", "💰 预算收口:即将耗尽,已注入收口指令"); }
    else if (type === "RUN_BUDGET_EXCEEDED") { meta("err", `💰 预算硬停 ${p.used}/${p.budget} token`); }
    else if (type === "CONTEXT_COMPACTED") { meta("system", `⇩ 上下文已压缩(第 ${p.compressions} 次,已覆盖 ${p.covered} 条)`); }
    // 2026-09-23 常驻 PLAN 模式:计划产物落盘提示(loop 的 Plan Mode v2 既有事件)
    else if (type === "PLAN_RESULT") {
      if (p.valid && p.plan_file) meta("ok", `📋 计划已存盘:${p.plan_file}(切回 Act 后可要求按它执行)`);
      else meta("err", "⚠ 本次输出未包含'## 步骤'小节,计划格式不完整,未存盘");
    }
    } catch (e) {  // #43 兜底:错误落 __errLog(探针/E2E 可读),不再静默
      (window.__errLog = window.__errLog || []).push(`onEvent(${type}) error: ` + String(e && e.stack || e));
    }
  },
  onDone(payload) {
    hideThinking();
    // 修复(2026-09-23 重复回复):流式期间已有 assistant 块(finalize 会渲染剩余 buffer)
    // 不再另起新块——只在流式没产生块时(如无输出)才建终块
    if (currentAssistant) {
      finalizeAssistant();
    } else if ((payload.final_text || "").trim()) {
      startAssistant();
      renderMarkdown(currentAssistant.querySelector(".body"), payload.final_text);
      finalizeAssistant();
    }
    rawBuf = "";
    meta("ok", `✔ ${payload.summary}`);
    setBusy(false);
    railDot("runs", true);  // 2026-09-24 rail 徽标:run 落地,runs 键亮点(切页即清)
  },
  onError(text) {
    hideThinking();
    finalizeAssistant();
    meta("err", `❌ 出错:${text}`);
    setBusy(false);
    railDot("runs", true);  // 2026-09-24 rail 徽标:失败也是新历史
  },
  onStopped() {
    hideThinking();
    finalizeAssistant();
    meta("system", "⏹ 已停止");
    setBusy(false);
    railDot("runs", true);  // 2026-09-24 rail 徽标:中断也是新历史
  },
  onConversations(convs) { renderConvs(convs); },
  onDiffPreview(payload) { return showDiffCard(payload); },  // M3.5:返回 Promise,Python 侧等待采纳/拒绝
  onHistory(messages) {
    thread.innerHTML = "";
    currentAssistant = null;
    // 修复(2026-09-23 #43):清空 thread 后必须复位 thinkingEl——否则它指向已分离
    // 节点,后续 ensureThinking 不重建、insertBefore 抛 NotFoundError(事件整批丢失)
    hideThinkingKeepTimer();
    for (const m of messages) {
      if (m.role === "user") addUser(m.content || "");
      else if (m.role === "assistant") {
        if (!(m.content || "").trim()) continue;
        startAssistant();
        renderMarkdown(currentAssistant.querySelector(".body"), m.content);
        finalizeAssistant();
      } else if (m.role === "tool") {
        const firstLine = (m.content || "").split("\n").find((l) => l.trim()) || "";
        meta("tool", `⚡ ${m.name || "tool"} ${firstLine.slice(0, 80)}`);
      }
    }
    scrollBottom();
  },
};

window.addEventListener("pywebviewready", async () => {
  await window.pywebview.api.ready();
});

// ── Markdown 渲染(marked 标准库)──────────────────────────────────
const ICON_COPY = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const ICON_CHECK = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => true).catch(() => false);
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { /* 降级失败 */ }
  ta.remove();
  return Promise.resolve(ok);
}

function enhanceCodeBlocks(container) {
  // marked 输出 <pre><code class="language-java">:包 GitHub 式头部条(语言+复制键)
  container.querySelectorAll("pre").forEach((pre) => {
    if (pre.parentElement && pre.parentElement.classList.contains("code-wrap")) return;
    const code = pre.querySelector("code");
    const lang = ((code ? code.className : "") || "").replace(/^language-/, "");
    const wrap = document.createElement("div");
    wrap.className = "code-wrap";
    const head = document.createElement("div");
    head.className = "code-head";
    const label = document.createElement("span");
    label.className = "code-lang";
    label.textContent = lang || "code";
    const btn = document.createElement("button");
    btn.className = "copy-btn";
    btn.title = "复制代码";
    btn.innerHTML = ICON_COPY + "<i>复制</i>";
    btn.onclick = async () => {
      const ok = await copyText((code || pre).textContent);
      btn.classList.add("ok");
      btn.innerHTML = ICON_CHECK + `<i>${ok ? "已复制" : "失败"}</i>`;
      setTimeout(() => { btn.classList.remove("ok"); btn.innerHTML = ICON_COPY + "<i>复制</i>"; }, 1600);
    };
    head.append(label, btn);
    pre.replaceWith(wrap);
    wrap.append(head, pre);
  });
}

// 修正(2026-09-23):esc 函数在 v8 重写时被误删——diff/产物卡片的 esc(p.name) 抛
// ReferenceError → showDiffCard 崩 → Python 侧瞬时判"拒绝"(真机:diff 框不出现、写盘被拒)
function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function renderMarkdown(el, text) {
  el.innerHTML = "";
  if (!text) return;
  if (typeof marked === "undefined") {  // 极端兜底:库未加载时纯文本
    el.textContent = text;
    return;
  }
  // gfm=表格/删除线/任务列表;breaks=单换行即 <br>(解决"长文本不换行")
  let html = marked.parse(String(text), { gfm: true, breaks: true });
  html = html.replace(/<script[\s\S]*?<\/script>/gi, "");  // 轻量安全过滤
  el.innerHTML = html;
  enhanceCodeBlocks(el);
}

// ── 消息组件 ─────────────────────────────────────────────────────
function addUser(text) {
  const div = document.createElement("div");
  div.className = "msg-user";
  const b = document.createElement("div");
  b.className = "bubble";
  b.textContent = text;
  div.appendChild(b);
  insertAboveThinking(div);
}

function startAssistant() {
  finalizeAssistant();
  const div = document.createElement("div");
  div.className = "msg-assistant busy";
  div.innerHTML = `<div class="author"><div class="avatar"></div><span class="name">otter</span></div><div class="body"></div>`;
  insertAboveThinking(div);
  currentAssistant = div;
}

function finalizeAssistant() {
  if (!currentAssistant) return;
  currentAssistant.classList.remove("busy");
  if (streamRenderTimer) { clearTimeout(streamRenderTimer); streamRenderTimer = null; }
  if (rawBuf.trim()) renderMarkdown(currentAssistant.querySelector(".body"), rawBuf);  // 收尾兜底:缓冲若有残留立即终渲染
  currentAssistant = null;
}

// ── diff 预览卡片(M3.5):GUI 侧展示变更,采纳后写盘(preview_gate 在 Python 侧)──
let pendingDiffResolve = null;
function showDiffCard(p) {
  hideThinkingKeepTimer();
  const card = document.createElement("div");
  card.className = "msg-assistant diff-card";
  const head = document.createElement("div");
  head.className = "author";
  head.innerHTML = `<div class="avatar"></div><span class="name">📝 变更预览 · ${esc(p.name)} → ${esc(p.path || "")}</span>`;
  const body = document.createElement("div");
  body.className = "body";
  const pre = document.createElement("pre");
  pre.className = "diff-pre";
  pre.textContent = (p.diff || "").split("\n").slice(0, 120).join("\n");
  body.appendChild(pre);
  const actions = document.createElement("div");
  actions.className = "diff-actions";
  const ok = document.createElement("button");
  ok.className = "diff-btn ok"; ok.textContent = "✓ 采纳写入";
  const no = document.createElement("button");
  no.className = "diff-btn no"; no.textContent = "✗ 拒绝";
  actions.append(ok, no);
  card.append(head, body, actions);
  mountAboveThinking(card);  // #43:统一挂载(悬空 thinkingEl 时不再抛 NotFoundError)
  return new Promise((resolve) => {
    pendingDiffResolve = resolve;
    const finish = (v) => {
      card.querySelectorAll("button").forEach((b) => (b.disabled = true));
      actions.remove();
      card.style.opacity = v ? "1" : "0.55";
      if (!v) card.appendChild(Object.assign(document.createElement("div"),
        { className: "meta err", textContent: "已拒绝,文件未改动" }));
      if (pendingDiffResolve === resolve) pendingDiffResolve = null;
      resolve(v);
    };
    ok.onclick = () => finish(true);
    no.onclick = () => finish(false);
  });
}

// ── 产物卡片(类 Codex Desktop):内嵌预览 + 三通道打开 ──
const ICON_FILE = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/></svg>';
function fmtSize(bytes) {
  if (!bytes || bytes < 1024) return (bytes || 0) + "B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + "KB";
  return (bytes / 1048576).toFixed(1) + "MB";
}
// 2026-09-24 R5 拆分:buildArtifactCard 只构建返回元素(供 FILE_CHANGED 链接行
// 内联展开复用);showArtifactCard 保持原挂载行为(ARTIFACT 事件路径,探针回归依赖)
function buildArtifactCard(p) {
  const card = document.createElement("div");
  card.className = "artifact-preview-card";

  // 头部:图标 + 文件名 + 大小 + 类型
  const head = document.createElement("div");
  head.className = "apc-head";
  const typeLabel = p.preview_type === "image" ? "图片" :
                    p.preview_type === "pdf" ? "PDF" :
                    p.preview_type === "code" ? (p.lang || "代码") :
                    p.preview_type === "text" ? "文本" : "文件";
  head.innerHTML = `${ICON_FILE} <span class="apc-name">${esc(p.name || "")}</span>
    <span class="apc-meta">${fmtSize(p.size)} · ${esc(typeLabel)}</span>
    ${p.note ? `<span class="apc-note">${esc(p.note)}</span>` : ""}`;

  // 预览区:按类型
  const body = document.createElement("div");
  body.className = "apc-body";
  const t = p.preview_type;

  if (t === "image" && p.data) {
    const img = document.createElement("img");
    img.className = "apc-img";
    img.src = `data:${p.mime || "image/jpeg"};base64,${p.data}`;
    img.onclick = () => window.pywebview.api.open_artifact(p.name, "open");
    body.appendChild(img);
  } else if (t === "pdf" && p.file_url) {
    const emb = document.createElement("embed");
    emb.className = "apc-pdf";
    emb.src = p.file_url;
    emb.type = "application/pdf";
    body.appendChild(emb);
  } else if ((t === "code" || t === "text") && p.content) {
    const pre = document.createElement("pre");
    pre.className = "apc-code";
    pre.textContent = p.content;
    if (p.content.split("\n").length >= 40) pre.textContent += "\n…(截断)";
    body.appendChild(pre);
  } else {
    body.innerHTML = `<div class="apc-binary">📄<br>${esc(p.name || "文件")}<br>${fmtSize(p.size)}</div>`;
  }

  // 操作按钮(2026-09-24 R5:优先用 path 直连——普通写盘文件不在 artifact 索引里,
  // artifact.open_artifact 已加文件系统兜底;artifact_publish 场景 path 同样可解析)
  const actions = document.createElement("div");
  actions.className = "apc-actions";
  for (const [how, label] of [["open", "打开"], ["finder", "Finder"], ["vscode", "VS Code"]]) {
    const b = document.createElement("button");
    b.className = "apc-btn";
    b.textContent = label;
    b.onclick = async () => {
      b.textContent = "…";
      b.textContent = await window.pywebview.api.open_artifact(p.path || p.name, how);
      setTimeout(() => (b.textContent = label), 1800);
    };
    actions.appendChild(b);
  }

  card.append(head, body, actions);
  return card;
}

function showArtifactCard(p) {
  mountAboveThinking(buildArtifactCard(p));  // #43:统一挂载(含悬空守卫)
}

// ── 2026-09-24 R5:文件变更链接行(⚙/📎 行)──
// 「📎 已修改 product.py (+2/-1)」:文件名为超链接,点击在行下方内联展开/收起预览卡
function showFileLink(p) {
  const line = document.createElement("div");
  line.className = "meta file-changed";
  // 2026-09-24 R8(用户要求):超链接做成「文件图标+超链接」——图标复用产物卡的
  // 文件 SVG(与 📦 卡片同款),文件名为链接,点击行下方内联预览
  const ico = document.createElement("span");
  ico.className = "file-ico";
  ico.innerHTML = ICON_FILE;
  const act = document.createElement("span");
  act.textContent = ` ${p.action || "已修改"} `;
  const a = document.createElement("a");
  a.className = "file-link";
  a.href = "#";
  a.textContent = p.name || p.path || "?";
  a.onclick = (ev) => {
    if (ev && ev.preventDefault) ev.preventDefault();
    toggleFilePreview(p, line);
    return false;
  };
  line.append(ico, act, a);
  if (p.plus != null || p.minus != null) {
    const st = document.createElement("span");
    st.textContent = ` (+${p.plus || 0}/-${p.minus || 0})`;
    line.appendChild(st);
  }
  mountAboveThinking(line);
}

async function toggleFilePreview(p, anchor) {
  if (anchor.__card) { anchor.__card.remove(); anchor.__card = null; return; }
  // payload 缺预览字段时按需补拉(CLI 场景/预览生成曾失败)
  if (!p.preview_type && p.path && window.pywebview) {
    try { Object.assign(p, await window.pywebview.api.preview_file(p.path)); } catch (e) { /* 走 binary 卡 */ }
  }
  const card = buildArtifactCard(p);
  anchor.__card = card;
  anchor.parentNode.insertBefore(card, anchor.nextSibling);
  scrollBottom();
}

function meta(kind, text) {
  const div = document.createElement("div");
  div.className = `meta ${kind}`;
  div.textContent = text;
  mountAboveThinking(div);  // #43:统一挂载(含悬空守卫)
}

function scrollBottom() { thread.scrollTop = thread.scrollHeight; }

function setBusy(b) {
  busy = b;
  sendBtn.innerHTML = b ? ICON_STOP : ICON_SEND;
  sendBtn.title = b ? "停止" : "发送";
  input.disabled = b;
  // 修复(2026-09-23 用户报告"没有思考倒计时"):计时的生命周期绑定 busy 本身——
  // setBusy(true) 即启动秒表(此前依赖 submit/showThinking 的调用链,流式开始后
  // 思考条被移除、状态栏无人续写,看起来像"没有倒计时");setBusy(false) 停表并复位
  if (b) {
    if (!taskStartTime) taskStartTime = Date.now();
    if (!thinkingTimer) thinkingTimer = setInterval(refreshThinking, 1000);
    refreshThinking();
  } else {
    if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
    taskStartTime = 0;
    $("#statusbar").textContent = "就绪";
    input.focus();
  }
  railDot("chat", b);  // 2026-09-24 rail 徽标:运行中 chat 键亮点(切到别页也可见)
}

// ── rail 徽标(2026-09-24 新增):各 rail 键的小圆点提醒,纯前端 ──
// 点亮:任务运行中→chat;run 结束→runs(有新历史);ARTIFACT 事件→artifacts(有新交付物)。
// 熄灭:切到对应页(拉数据)即清除。实现为按键内增删 .nav-dot span(见 css)。
function railDot(page, on) {
  const btn = document.querySelector(`.rail-item[data-page="${page}"]`);
  if (!btn) return;
  let dot = btn.querySelector(".nav-dot");
  if (on && !dot) {
    dot = document.createElement("span");
    dot.className = "nav-dot";
    btn.appendChild(dot);
  } else if (!on && dot) {
    dot.remove();
  }
}

// ── 交互 ─────────────────────────────────────────────────────────
const ICON_SEND = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/></svg>';
const ICON_STOP = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" stroke="none"><rect width="14" height="14" x="5" y="5" rx="2.5"/></svg>';
sendBtn.addEventListener("click", () => {
  if (busy) { window.pywebview.api.stop_task(); }
  else { submit(); }
});

// 2026-09-23 用户要求改版:「普通/计划」分段控件——点已在激活态的段无操作,
// 点另一段才通知 Python 翻转(单一事实源仍在 Python,onMode 回推渲染;运行中切换不影响当前 run)
document.querySelectorAll(".seg-btn").forEach((b) => {
  b.addEventListener("click", () => {
    if (!b.classList.contains("active")) window.pywebview.api.toggle_mode();
  });
});

async function submit() {
  const text = input.value.trim();
  if (!text || busy) return;
  input.value = "";
  input.style.height = "auto";
  addUser(text);
  // 2026-09-23 E2E 暴露:气泡出现后任务未达 Python——async 异常 window.onerror 抓不到,
  // 此处显式 try/catch 记录到 __errLog(异步拒绝另由 unhandledrejection 捕获)
  try {
    taskStartTime = 0;  // 交给 setBusy(true) 统一起表(2026-09-23:计时生命周期归 busy)
    setBusy(true);
    showThinking();  // 聊天流内的思考条(状态栏⏱由 setBusy 驱动,双保险)
    await window.pywebview.api.submit(text);
  } catch (err) {
    (window.__errLog = window.__errLog || []).push("submit: " + String(err && err.stack || err));
    setBusy(false);
    hideThinking();
  }
}

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
});

// 页面切换
document.querySelectorAll(".rail-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".rail-item").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
    $(`#page-${btn.dataset.page}`).classList.add("active");
    if (btn.dataset.page === "runs") loadRuns();
    if (btn.dataset.page === "memory") loadMemory();  // 2026-09-24 长期记忆页:切页即拉快照
    if (btn.dataset.page === "artifacts") loadArtifacts();  // 2026-09-24 交付物页:切页即拉索引
    if (btn.dataset.page === "settings") loadSettings();
  });
});

// 会话列表(2026-09-23 历史排版要求:两行卡片+搜索过滤;
// 副行 sub 由 Python 侧 _conv_payload 生成「9月23日 19:35 · 3 轮对话」)
let lastConvs = [];
function renderConvs(convs) {
  lastConvs = convs || [];
  const list = $("#convList");
  list.innerHTML = "";
  const q = ($("#convSearch") ? $("#convSearch").value : "").trim().toLowerCase();
  let shown = 0;
  for (const c of lastConvs) {
    if (q && !(c.title || "").toLowerCase().includes(q)) continue;  // 标题不匹配即滤掉
    shown++;
    const div = document.createElement("div");
    div.className = "conv-item" + (c.active ? " active" : "");
    const t = document.createElement("div");
    t.className = "conv-title";
    t.textContent = c.title || `会话 #${c.id}`;
    const sub = document.createElement("div");
    sub.className = "conv-sub";
    sub.textContent = c.sub || "";
    div.append(t, sub);
    div.onclick = () => window.pywebview.api.switch_conversation(c.id);
    list.appendChild(div);
  }
  if (!shown) {
    const empty = document.createElement("div");
    empty.className = "conv-empty";
    empty.textContent = q ? "无匹配会话" : "暂无会话";
    list.appendChild(empty);
  }
}
// 搜索框:输入即从缓存重渲染(不跨桥,纯前端过滤)
$("#convSearch").addEventListener("input", () => renderConvs(lastConvs));

$("#newConv").addEventListener("click", async () => {
  await window.pywebview.api.new_conversation();
  thread.innerHTML = "";
  currentAssistant = null;
  hideThinkingKeepTimer();  // 修复(2026-09-23 #43):同 onHistory,清屏必须复位 thinkingEl
  $("#statusbar").textContent = "新会话(发送第一条消息时创建)";
});

// 运行历史(2026-09-24 用户要求:整屏行分布的历史记录块,不再左右分栏;
// 块内=完成标志/会话id/内容摘要/模式/最后会话时间,中断如实标注——字段由 gui.py _runs_rows 生成)
async function loadRuns() {
  railDot("runs", false);  // 2026-09-24 rail 徽标:进页查看即熄灭
  const runs = await window.pywebview.api.get_runs();
  const list = $("#runsList");
  list.innerHTML = "";
  for (const r of runs) {
    const item = document.createElement("div");
    item.className = "run-item";
    // 2026-09-24 用户反馈第四轮:已完成放最左,会话id缩小垫在其下方(左侧两行小栏);
    // 行与行之间加灰色横线(见 css border-bottom)
    const left = document.createElement("div");
    left.className = "run-left";
    const flag = document.createElement("div");
    flag.className = "run-flag " + (r.done ? "ok" : "no");
    flag.textContent = r.done ? "已完成" : "未完成";
    const ids = document.createElement("div");
    ids.className = "run-ids";
    // 2026-09-24 R8(用户要求):一整个 chat 一条历史记录——行=会话,副列带运行次数;
    // 无会话归属的旧 run 保留单条兜底
    ids.textContent = r.conv
      ? `会话 #${r.conv}` + (r.count > 1 ? ` · ${r.count} 次运行` : "")
      : `Run #${r.id}`;
    left.append(flag, ids);
    const title = document.createElement("div");
    title.className = "run-title";
    title.textContent = r.summary || `Run #${r.id}`;
    const mode = document.createElement("div");
    mode.className = "run-mode";
    mode.textContent = r.mode;
    // 2026-09-24 用户反馈第五轮:普通/计划字样居中(绝对定位到行正中,见 css);
    // 时间下方加「会话详情」按钮 → 跳到对话页打开该会话
    const tm = document.createElement("div");
    tm.className = "run-time";
    tm.textContent = r.time;
    const right = document.createElement("div");
    right.className = "run-right";
    right.appendChild(tm);
    if (r.conv) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "run-detail-btn";
      btn.textContent = "会话详情";
      btn.onclick = async (ev) => {
        if (ev && ev.stopPropagation) ev.stopPropagation();  // 不触发所在行的 Trace 展开
        await window.pywebview.api.switch_conversation(r.conv);
        // 切到对话页(直接改类,不依赖合成 .click()——WKWebView 里合成点击会悬死)
        document.querySelectorAll(".rail-item").forEach((b) =>
          b.classList.toggle("active", b.dataset.page === "chat"));
        document.querySelectorAll(".page").forEach((p) =>
          p.classList.toggle("active", p.id === "page-chat"));
      };
      right.appendChild(btn);
    }
    item.append(left, title, mode, right);
    // 状态胶囊只在非正常完成时出现(正常完成已由左侧「已完成」表达;
    // 中断/失败/进行中仍如实标注在右侧)
    if (r.label && r.label !== "完成") {
      const pill = document.createElement("div");
      pill.className = "run-pill " + r.kind;
      pill.textContent = r.label;
      item.appendChild(pill);
    }
    // 2026-09-24 R8(用户要求:一整个 chat 一条历史记录):行=会话,点击整行直接
    // 跳到对话页打开;无会话归属的旧 run 保留原「行内展开 Trace」兜底
    if (r.conv) {
      item.onclick = async () => {
        await window.pywebview.api.switch_conversation(r.conv);
        document.querySelectorAll(".rail-item").forEach((b) =>
          b.classList.toggle("active", b.dataset.page === "chat"));
        document.querySelectorAll(".page").forEach((p) =>
          p.classList.toggle("active", p.id === "page-chat"));
      };
    } else {
      item.onclick = async () => {
        if (item.__detail) { item.__detail.remove(); item.__detail = null; return; }
        const det = document.createElement("pre");
        det.className = "run-detail";
        det.textContent = "加载 Trace…";
        item.appendChild(det);
        item.__detail = det;
        det.textContent = await window.pywebview.api.get_run_detail(r.id);
      };
    }
    list.appendChild(item);
  }
  if (!runs.length) {
    const empty = document.createElement("div");
    empty.className = "conv-empty";
    empty.textContent = "暂无运行记录";
    list.appendChild(empty);
  }
}
$("#reloadRuns").addEventListener("click", loadRuns);

// ── 长期记忆页(2026-09-24 新增):查看+搜索,只读;编辑能力留待后续轮次 ──
// 数据快照经 gui.py _memory_payload 纯读生成(Core=JSON 条目,Ordinary=front matter 解析);
// 搜索为纯前端过滤(从缓存重渲染,不跨桥),与历史页 convSearch 同一模式
let lastMemory = null;
function _memEmpty(list, text) {
  const empty = document.createElement("div");
  empty.className = "conv-empty";
  empty.textContent = text;
  list.appendChild(empty);
}
function renderMemory(mem, q = "") {
  const ql = (q || "").toLowerCase();
  const match = (...fields) =>
    !ql || fields.some((f) => (f || "").toLowerCase().includes(ql));
  // Core 区:key: value 主行 + 依据/日期副行,点击展开出处原话
  const coreList = $("#coreList");
  coreList.innerHTML = "";
  let shownCore = 0;
  for (const c of mem.core || []) {
    if (!match(c.key, c.value, c.reason)) continue;
    shownCore++;
    const item = document.createElement("div");
    item.className = "mem-item";
    const title = document.createElement("div");
    title.className = "mem-title";
    title.textContent = `${c.key}:${c.value}`;
    const meta = document.createElement("div");
    meta.className = "mem-meta";
    meta.textContent = `依据:${c.reason || "—"} · ${c.updated_at || ""}`;
    const body = document.createElement("div");
    body.className = "mem-body";
    body.textContent = c.source_quote || "(无出处原话)";
    item.append(title, meta, body);
    item.onclick = () => item.classList.toggle("open");
    coreList.appendChild(item);
  }
  if (!shownCore) _memEmpty(coreList, "无 Core 记忆");
  // 普通记忆区:M### 标题 + 摘要 + rev/访问/时间副行,点击展开正文
  const list = $("#memList");
  list.innerHTML = "";
  let shown = 0;
  for (const e of mem.entries || []) {
    if (!match(e.mid, e.title, e.summary, e.content)) continue;
    shown++;
    const item = document.createElement("div");
    item.className = "mem-item";
    const title = document.createElement("div");
    title.className = "mem-title";
    const id = document.createElement("span");
    id.className = "mem-id";
    id.textContent = e.mid;
    title.append(id, document.createTextNode(e.title || ""));
    const sub = document.createElement("div");
    sub.className = "mem-sub";
    sub.textContent = e.summary || "";
    const meta = document.createElement("div");
    meta.className = "mem-meta";
    meta.textContent = `rev ${e.revision} · 访问 ${e.access_count} · ${e.mtime || ""}`;
    const body = document.createElement("div");
    body.className = "mem-body";
    body.textContent = e.content || "";
    item.append(title, sub, meta, body);
    item.onclick = () => item.classList.toggle("open");
    list.appendChild(item);
  }
  if (!shown) _memEmpty(list, "无普通记忆");
}
async function loadMemory() {
  lastMemory = await window.pywebview.api.get_memory();
  renderMemory(lastMemory, $("#memSearch").value || "");
}
$("#memSearch").addEventListener("input", () =>
  renderMemory(lastMemory || { core: [], entries: [] }, $("#memSearch").value));
$("#reloadMemory").addEventListener("click", loadMemory);

// ── 交付物页(2026-09-24 新增):artifact_publish 发布历史,最新在前 ──
// 预览/打开全复用产物卡链路(buildArtifactCard 内含三通道按钮,preview_file 按需补拉)
let lastArtifacts = null;
function renderArtifacts(arts, q = "") {
  const ql = (q || "").toLowerCase();
  const list = $("#artList");
  list.innerHTML = "";
  let shown = 0;
  for (const a of arts || []) {
    if (ql && ![a.name, a.note, a.id].some((f) => (f || "").toLowerCase().includes(ql))) continue;
    shown++;
    const item = document.createElement("div");
    item.className = "mem-item";  // 复用记忆页条目排版(同族观感)
    const title = document.createElement("div");
    title.className = "mem-title";
    title.textContent = a.name || a.id;
    const sub = document.createElement("div");
    sub.className = "mem-sub";
    sub.textContent = a.note || "";
    const meta = document.createElement("div");
    meta.className = "mem-meta";
    meta.textContent = `${fmtSize(a.size)} · ${a.created || ""}`
      + (a.run_id ? ` · Run #${a.run_id}` : "")
      + (a.sha256 ? ` · sha ${a.sha256}` : "");
    item.append(title, sub, meta);
    item.onclick = async () => {
      if (item.__card) { item.__card.remove(); item.__card = null; return; }
      // 与 R5 链接行同法:payload 无预览字段,点击时按需补拉(失败走 binary 卡兜底)
      const p = { name: a.name, path: a.path, size: a.size, note: a.note };
      if (window.pywebview) {
        try { Object.assign(p, await window.pywebview.api.preview_file(a.path)); } catch (e) { /* binary 卡 */ }
      }
      const card = buildArtifactCard(p);
      card.onclick = (ev) => { if (ev && ev.stopPropagation) ev.stopPropagation(); };  // 卡内按钮不触发行的收起
      item.__card = card;
      item.appendChild(card);
    };
    list.appendChild(item);
  }
  if (!shown) _memEmpty(list, "暂无交付物");
}
async function loadArtifacts() {
  railDot("artifacts", false);  // 2026-09-24 rail 徽标:进页查看即熄灭
  lastArtifacts = await window.pywebview.api.get_artifacts();
  renderArtifacts(lastArtifacts, $("#artSearch").value || "");
}
$("#artSearch").addEventListener("input", () => renderArtifacts(lastArtifacts || [], $("#artSearch").value));
$("#reloadArtifacts").addEventListener("click", loadArtifacts);

async function loadSettings() {
  $("#settingsBox").textContent = await window.pywebview.api.get_settings();
}
