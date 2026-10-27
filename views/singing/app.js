// 唱歌控制
// 主视图只有一个数字 + 四挡预设 + 一个统一按钮；其余全在抽屉里。
// 所有控件变更即提交，后端负责持久化并重建引擎。
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const INT_KEYS = [
  "singing_target_seconds",
  "singing_target_seconds_private",
  "singing_target_seconds_group",
];
const BOOL_KEYS = ["singing_fill_short", "singing_quiet_hours_enable"];
const STR_KEYS = [
  "singing_quiet_hours",
  "singing_quiet_hours_private",
  "singing_quiet_hours_group",
];

const PRESETS = ["short", "medium", "long", "xlong"];
const PRESET_SECONDS = { short: 20, medium: 40, long: 60, xlong: 90 };
const PRESET_NAME = { short: "短", medium: "中", long: "长", xlong: "很长" };

const $ = (id) => document.getElementById(id);
let lastState = null;
let lastShownSeconds = null;

/* ---------------- 请求 ---------------- */

function hasPost() {
  return (
    bridge &&
    (typeof bridge.apiPost === "function" ||
      typeof bridge.post === "function" ||
      typeof bridge.apiRequest === "function")
  );
}

async function call(path, params, method) {
  if (!bridge) throw new Error("没有检测到 AstrBot 插件视图 bridge");
  if (method === "POST") {
    for (const name of ["apiPost", "post", "apiRequest"]) {
      if (typeof bridge[name] === "function") return bridge[name](path, params);
    }
  }
  if (typeof bridge.apiGet === "function") return bridge.apiGet(path, params);
  if (typeof bridge.request === "function") return bridge.request(path, params);
  throw new Error("bridge 没有可用的请求方法");
}

/* ---------------- Toast ---------------- */

let toastTimer = null;

function toast(message, state) {
  const box = $("toast");
  box.textContent = message;
  box.dataset.state = state || "ok";
  box.hidden = false;
  requestAnimationFrame(() => {
    box.dataset.show = "1";
  });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(
    () => {
      box.dataset.show = "0";
      setTimeout(() => {
        box.hidden = true;
      }, 240);
    },
    state === "err" || state === "warn" ? 4200 : 1900
  );
}

/* ---------------- 渲染 ---------------- */

function fillForm(cfg) {
  for (const key of INT_KEYS) {
    const el = $(key);
    if (el) el.value = Number(cfg[key] ?? 0);
  }
  for (const key of BOOL_KEYS) {
    const el = $(key);
    if (el) el.checked = Boolean(cfg[key]);
  }
  for (const key of STR_KEYS) {
    const el = $(key);
    if (el) el.value = cfg[key] ?? "";
  }
  // 「允许私聊与群聊不同」是统一模式的取反，语义更好懂
  const split = $("allow-split");
  if (split) split.checked = !cfg.singing_unified_mode;
}

function renderHero(state) {
  const cfg = state.config || {};
  const eff = state.effective || {};
  const preset = cfg.singing_length_preset || "";
  const index = PRESETS.indexOf(preset);
  const seconds = eff.private ? eff.private.target_seconds : 0;
  const chars = eff.private ? eff.private.target_chars : 0;

  const segmented = $("segmented");
  segmented.style.setProperty("--seg-index", String(Math.max(0, index)));
  segmented.classList.toggle("is-custom", index < 0);
  for (const button of segmented.querySelectorAll("[data-preset]")) {
    button.setAttribute(
      "aria-pressed",
      button.dataset.preset === preset ? "true" : "false"
    );
  }

  const number = $("length-value");
  const next = String(seconds || "—");
  if (number.textContent !== next) {
    number.textContent = next;
    if (lastShownSeconds !== null) {
      number.classList.remove("bump");
      void number.offsetWidth;
      number.classList.add("bump");
    }
  }
  lastShownSeconds = seconds;

  $("length-caption").textContent = preset
    ? `${PRESET_NAME[preset]}挡 · 目标 ${chars} 字`
    : `自定义 · 目标 ${chars} 字`;
}

function renderHeader(state) {
  const engine = state.engine || {};
  const gv = state.global_voice || {};
  const bindings = Array.isArray(state.bindings) ? state.bindings : [];
  const divergent = bindings.filter((b) => !b.same_as_global);
  const badge = $("badge");

  if (!engine.name) {
    badge.dataset.state = "off";
    badge.textContent = "无引擎";
  } else if (state.voice_unified) {
    badge.dataset.state = "ok";
    badge.textContent = "已统一";
  } else {
    badge.dataset.state = "warn";
    badge.textContent = `${divergent.length} 个不同`;
  }

  const eff = state.effective || {};
  const priv = eff.private ? eff.private.target_seconds : 0;
  const grp = eff.group ? eff.group.target_seconds : 0;
  const voice = `朗读 ${gv.voice || "默认"} · 唱歌 ${gv.singing_voice || "跟随朗读"}`;
  let note = state.voice_unified
    ? `${voice}。私聊 ${priv} 秒 · 群聊 ${grp} 秒`
    : `${divergent.length} 个会话例外 · 私聊 ${priv} 秒 · 群聊 ${grp} 秒`;
  if (state.config && state.config.singing_quiet_hours_enable) note += " · 已设夜间禁唱";

  $("unify-note").textContent = note;
  // 按钮只表达「动作」，统一与否由徽章表达；文案恒定，避免长度跳动
}

/* 引擎卡片：点一下切换，不用回配置页打字 */
function renderEngines(state) {
  const box = state && state.engine_choices;
  const note = $("engine-note");
  const speechBox = $("speech-cards");
  const singingBox = $("singing-cards");
  if (!box || !speechBox || !singingBox) return;

  const cards = Array.isArray(box.cards) ? box.cards : [];
  const speechNow = box.speech || "";
  const singingNow = box.singing || "";
  const followLabel = "跟随朗读引擎";

  if (!cards.length) {
    speechBox.innerHTML = "";
    singingBox.innerHTML = "";
    if (note) note.textContent = "还没有配置引擎";
    return;
  }

  const singable = cards.filter((c) => c.can_sing).length;
  if (note) {
    note.textContent = singingNow && singingNow !== followLabel
      ? `说话 ${speechNow} · 唱歌 ${singingNow}`
      : `说话与唱歌都用 ${speechNow}`;
  }

  const makeCard = (value, label, opts) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "engine-card";
    btn.textContent = label;
    btn.setAttribute("role", "radio");
    btn.dataset.value = value;
    btn.dataset.kind = opts.kind;

    if (opts.disabled) {
      btn.disabled = true;
      if (opts.reason) btn.title = opts.reason;
    }
    if (opts.icon) {
      const tag = document.createElement("span");
      tag.className = "engine-tag";
      tag.textContent = opts.icon;
      btn.appendChild(tag);
    }

    const checked = opts.checked;
    btn.setAttribute("aria-checked", checked ? "true" : "false");
    return btn;
  };

  // 说话：所有引擎都能当朗读引擎
  speechBox.innerHTML = "";
  for (const card of cards) {
    speechBox.appendChild(
      makeCard(card.name, card.name, {
        kind: "speech",
        checked: card.name === speechNow,
        icon: card.can_sing ? "可唱" : "",
      })
    );
  }

  // 唱歌：不支持唱歌的灰掉，并说明原因
  singingBox.innerHTML = "";
  singingBox.appendChild(
    makeCard(followLabel, followLabel, {
      kind: "singing",
      checked: !singingNow || singingNow === followLabel,
      icon: "",
    })
  );
  for (const card of cards) {
    singingBox.appendChild(
      makeCard(card.name, card.name, {
        kind: "singing",
        checked: singingNow === card.name,
        disabled: !card.can_sing,
        reason: card.reason || "该引擎不支持唱歌",
        icon: card.can_sing ? "可唱" : "",
      })
    );
  }
}

function renderFacts(state) {
  const eff = state.effective || {};
  const engine = state.engine || {};
  const speed = state.singing_speed || {};
  const last = state.last || {};
  const bindings = Array.isArray(state.bindings) ? state.bindings : [];
  const divergent = bindings.filter((b) => !b.same_as_global);

  const rows = [];
  rows.push(["引擎", engine.name ? `${engine.name}（${engine.mode || "—"}）` : "未初始化"]);
  if (speed.cps) {
    const src = speed.source === "实测校准" ? `实测 ${speed.samples} 次` : speed.source;
    rows.push(["语速", `${Number(speed.cps).toFixed(2)} 字/秒（${src}）`]);
  }
  rows.push([
    "钩子",
    `${state.prompt_hook ? "提示词已注入" : "提示词未注入"} · ${
      state.web_api ? "面板接口正常" : "面板接口未注册"
    }`,
  ]);
  if (eff.private) {
    rows.push([
      "私聊",
      `${eff.private.target_seconds} 秒 · 目标 ${eff.private.target_chars} 字${
        eff.private.quiet_now ? " · 当前禁唱" : ""
      }`,
    ]);
  }
  if (eff.group) {
    rows.push([
      "群聊",
      `${eff.group.target_seconds} 秒 · 目标 ${eff.group.target_chars} 字${
        eff.group.quiet_now ? " · 当前禁唱" : ""
      }`,
    ]);
  }
  // 统一模式开着时，群聊/私聊的单独设置仍有值但不参与计算，必须露出来
  const ignored = Array.isArray(state.ignored_overrides) ? state.ignored_overrides : [];
  if (ignored.length) {
    rows.push([
      "被忽略",
      `${ignored.length} 项单独设置（统一模式开着）：${ignored.join("、")}`,
    ]);
  }
  if (last && last.chars) {
    const scope = last.kind === "group" ? "群聊" : last.kind === "private" ? "私聊" : "会话";
    rows.push([
      "最近一次",
      `${scope} ${last.chars} 字 ≈ ${Math.round(last.seconds)} 秒（${last.mode}${
        last.truncated ? "，已裁剪" : ""
      }）`,
    ]);
  }

  const dl = $("facts");
  dl.innerHTML = "";
  for (const [k, v] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    dl.append(dt, dd);
  }

  const group = $("divergence");
  const tbody = group.querySelector("tbody");
  tbody.innerHTML = "";
  if (!divergent.length) {
    group.hidden = true;
    return;
  }
  group.hidden = false;
  for (const b of divergent) {
    const tr = document.createElement("tr");
    tr.className = "divergent";
    const kind = b.kind === "group" ? "群聊" : b.kind === "private" ? "私聊" : "未知";
    const cells = [
      b.umo,
      kind,
      b.instance,
      b.voice || "默认",
      b.singing_voice || "跟随朗读",
      b.ignored ? "已忽略" : "不同",
    ];
    for (const text of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }
    tbody.append(tr);
  }
}

function render(state) {
  lastState = state;
  renderHeader(state);
  renderHero(state);
  renderFacts(state);
  renderEngines(state);
  fillForm(state.config || {});
  $("split-group").hidden = Boolean(state.unified_mode);

  if (!state.web_api) {
    toast("面板接口未注册，请改用聊天指令 /tts_set 与 /tts_unify", "warn");
  }
}

/* ---------------- 提交 ---------------- */

const pending = {};
let flushTimer = null;

function queue(changes, delay) {
  Object.assign(pending, changes);
  clearTimeout(flushTimer);
  flushTimer = setTimeout(flush, delay ?? 120);
}

async function flush() {
  const payload = { ...pending };
  for (const key of Object.keys(pending)) delete pending[key];
  if (!Object.keys(payload).length) return;
  await submit(payload);
}

function summarize(payload) {
  const errors = payload.errors || {};
  const errKeys = Object.keys(errors);
  if (errKeys.length) return `${errKeys[0]}：${errors[errKeys[0]]}`;

  const applied = payload.applied || {};
  const keys = Object.keys(applied);
  if (keys.length === 1) {
    const key = keys[0];
    const value = applied[key];
    if (key === "singing_length_preset") {
      return PRESET_SECONDS[value]
        ? `已设为${PRESET_NAME[value]} · 约 ${PRESET_SECONDS[value]} 秒`
        : "已设为自定义时长";
    }
    if (key === "singing_unified_mode") {
      return value ? "已禁止分开设置" : "已允许私聊与群聊不同";
    }
    if (key === "singing_quiet_hours_enable") {
      return value ? "已开启夜间禁唱" : "已关闭夜间禁唱";
    }
    if (key === "singing_fill_short") return value ? "已开启补唱" : "已关闭补唱";
  }
  return "已保存";
}

async function submit(changes) {
  let payload;
  try {
    if (hasPost()) {
      payload = await call("singing/set", { values: changes }, "POST");
    } else {
      for (const [key, value] of Object.entries(changes)) {
        payload = await call("singing/set", { key, value }, "GET");
        if (payload && payload.ok === false && payload.error) throw new Error(payload.error);
      }
    }
    if (!payload) throw new Error("后端没有返回结果");
  } catch (error) {
    toast(`没改成：${error.message || error}`, "err");
    return;
  }
  render(payload);
  const bad = payload.ok === false;
  const saved = payload.persisted !== false;
  const message = summarize(payload);
  if (bad) toast(message, "warn");
  else if (!saved) toast(`${message}（仅当前进程，重启会丢）`, "warn");
  else toast(message, "ok");
}

async function unify() {
  const button = $("unify");
  button.disabled = true;
  try {
    const payload = await call("singing/unify", {}, "POST");
    if (!payload) throw new Error("后端没有返回结果");
    render(payload);
    const parts = [];
    if (payload.cleared_bindings) parts.push(`清掉 ${payload.cleared_bindings} 个单独配置`);
    if (Array.isArray(payload.cleared_overrides) && payload.cleared_overrides.length) {
      parts.push(`清掉 ${payload.cleared_overrides.length} 项覆盖`);
    }
    toast(parts.length ? `已统一，${parts.join("，")}` : "已统一", "ok");
  } catch (error) {
    toast(`统一失败：${error.message || error}`, "err");
  } finally {
    button.disabled = false;
  }
}

async function resetAll() {
  try {
    const payload = await call("singing/reset", {}, "POST");
    if (!payload) throw new Error("后端没有返回结果");
    render(payload);
    toast("已恢复默认", "ok");
  } catch (error) {
    toast(`恢复失败：${error.message || error}`, "err");
  }
}

/* ---------------- 抽屉 ---------------- */

function toggleDrawer(which) {
  const map = {
    more: { panel: $("drawer-more"), trigger: $("open-more") },
    diag: { panel: $("drawer-diag"), trigger: $("open-diag") },
  };
  const other = which === "more" ? map.diag : map.more;
  other.panel.hidden = true;
  other.trigger.setAttribute("aria-expanded", "false");

  const self = map[which];
  const open = self.panel.hidden;
  self.panel.hidden = !open;
  self.trigger.setAttribute("aria-expanded", open ? "true" : "false");
}

/* ---------------- 事件 ---------------- */

/* 引擎卡片点击 */
function bindEngineEvents() {
  for (const id of ["speech-cards", "singing-cards"]) {
    const box = $(id);
    if (!box) continue;
    box.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".engine-card");
      if (!btn || btn.disabled) return;
      const kind = btn.dataset.kind;
      const value = btn.dataset.value;
      queue(kind === "speech" ? { active_engine: value } : { singing_engine: value });
      // 立即在界面上反映选中态，等后端回执再校正
      box.querySelectorAll(".engine-card").forEach((el) => {
        el.setAttribute("aria-checked", el === btn ? "true" : "false");
      });
    });
  }
}

function bindEvents() {
  bindEngineEvents();

  for (const button of document.querySelectorAll("[data-preset]")) {
    button.addEventListener("click", () => {
      queue({ singing_length_preset: button.dataset.preset }, 0);
    });
  }

  $("unify").addEventListener("click", unify);
  $("reset").addEventListener("click", resetAll);
  $("open-more").addEventListener("click", () => toggleDrawer("more"));
  $("open-diag").addEventListener("click", () => toggleDrawer("diag"));

  for (const button of document.querySelectorAll("[data-step]")) {
    button.addEventListener("click", () => {
      const input = $(button.dataset.step);
      if (!input) return;
      const min = Number(input.min || 0);
      const max = Number(input.max || 300);
      const next = Math.min(
        max,
        Math.max(min, Number(input.value || 0) + Number(button.dataset.delta))
      );
      input.value = next;
      queue({ [button.dataset.step]: next }, 60);
    });
  }

  for (const key of INT_KEYS) {
    const el = $(key);
    if (!el) continue;
    el.addEventListener("change", () => {
      const value = Number(el.value);
      if (!Number.isFinite(value)) return;
      const changes = { [key]: value };
      if (lastState && lastState.config && lastState.config.singing_length_preset) {
        changes.singing_length_preset = "";
      }
      queue(changes, 60);
    });
  }

  for (const key of BOOL_KEYS) {
    const el = $(key);
    if (!el) continue;
    el.addEventListener("change", () => queue({ [key]: el.checked }, 60));
  }

  for (const key of STR_KEYS) {
    const el = $(key);
    if (!el) continue;
    el.addEventListener("input", () => queue({ [key]: el.value.trim() }, 700));
    el.addEventListener("blur", () => queue({ [key]: el.value.trim() }, 0));
  }

  // 「允许私聊与群聊不同」= 统一模式取反
  const split = $("allow-split");
  if (split) {
    split.addEventListener("change", () => {
      queue({ singing_unified_mode: !split.checked }, 0);
    });
  }
}

/* ---------------- 启动 ---------------- */

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  if (!bridge) {
    $("badge").dataset.state = "off";
    $("badge").textContent = "未连接";
    $("length-caption").textContent = "请从 AstrBot WebUI 的插件详情页打开本页面。";
    return;
  }
  try {
    const state = await call("singing/state", {}, "GET");
    if (!state || state.ok === false) throw new Error((state && state.error) || "读取失败");
    render(state);
  } catch (error) {
    $("badge").dataset.state = "warn";
    $("badge").textContent = "读取失败";
    $("length-caption").textContent = `读取状态失败：${error.message || error}`;
  }
});
