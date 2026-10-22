/* IEUStudia — the first-start setup: welcome and notice, this PC's check, the speech model, AI (Claude / ChatGPT /
 * offline / none), the background helper and notifications, the downloads (with sizes), then signing in.
 * Also the AI, background, storage and about cards of Settings. Plugs into app.js through window.Studia. */
(function () {
  "use strict";
  const St = window.Studia;
  const { S, call, esc, I, SPIN, toast, A } = St;
  const $ = (s, el = document) => el.querySelector(s);
  const gb = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`);
  const bytes = (n) => gb(n / 1048576);

  const STEPS = [["welcome", "Welcome"], ["pc", "This PC"], ["speech", "Transcription"], ["ai", "AI"],
    ["background", "Background"], ["download", "Download"], ["signin", "Sign in"]];
  const U = St.setup = { step: 0, st: null, ai: null, ch: {}, plan: null, dl: {}, running: false, done: false, accepted: false, error: "", timer: null,
    busy: {}, autoTested: {} };

  // ------------------------------------------------------------------ the notice (also in Settings → About)
  const NOTICE = `<ul class="notice">
      <li><b>Independent student project.</b> IEUStudia is not made, endorsed or supported by İzmir University of Economics, IEULearn or Panopto. Use it at your own risk; it comes without any warranty and the authors take no responsibility for how it is used.</li>
      <li><b>Your EkoID password stays with you.</b> It is sent only to learn.ieu.edu.tr to sign you in and is stored encrypted on this PC (Windows DPAPI). It is never sent to any other server — there is no IEUStudia server.</li>
      <li><b>Your lectures stay on this PC.</b> Audio is transcribed on your own computer. Recordings, slides and materials belong to your lecturers and the university: keep them for your own study and do not share them.</li>
      <li><b>AI is optional and can make mistakes.</b> Only if you use an AI feature is the lecture text you chose sent to the AI service you picked (Anthropic or OpenAI, under your own account). Always check AI answers against your lectures and materials.</li></ul>`;
  St.NOTICE = NOTICE;

  // ------------------------------------------------------------------ flow
  St.startSetup = async () => {
    S.mode = "setup"; U.step = 0; U.error = "";
    St.render();
    const r = await call("setup_state");
    if (!r.ok) { U.error = r.error; St.render(); return; }
    U.st = r;
    const v = r.verdict, app = r.app;
    const ready = (id) => (r.catalog.speech.find((x) => x.id === id) || {}).ready;
    U.ch = {
      speech: app.onboarded || ready(r.model) ? r.model : v.recommended_model,
      ai: app.ai_provider || "",
      llm: app.local_model && app.local_model !== "qwen3-1.7b" ? app.local_model : (v.recommended_llm || "qwen3-1.7b"),
      tray: app.onboarded ? app.tray : true, startup: app.onboarded ? r.startup : true,
      notify_recordings: app.notify_recordings, notify_announcements: app.notify_announcements, notify_materials: app.notify_materials,
    };
    U.accepted = !!app.terms_accepted;
    St.render();
    loadAi();
  };
  async function loadAi(which) {
    const r = await call("ai_status", which || "");
    if (!r.ok) return;
    const before = JSON.stringify(U.ai);
    U.ai = which && U.ai ? { ...U.ai, ...r } : r;
    if (!U.ch.ai && !which) U.ch.ai = r.claude.logged_in ? "claude" : r.codex.logged_in ? "codex" : "";
    for (const key of ["claude", "codex"]) {
      const a = U.ai[key];
      if (a && a.logged_in) delete U.busy["login:" + key];
    }
    autoTest();
    if (JSON.stringify(U.ai) !== before) rerenderAi();     // nothing new: leave the page alone
  }
  function rerenderAi() {
    if (S.mode === "setup" && STEPS[U.step][0] === "ai") St.render();
    if (S.view.name === "settings" && S.mode === "app") St.renderView();
  }
  // the AI being set up: the one picked in the setup, or the one chosen in Settings
  const aiKey = () => (S.mode === "setup" ? U.ch.ai : ((S.boot || {}).app || {}).ai_provider);
  function pollAi(on) {
    clearInterval(U.timer);
    U.timer = null;
    if (!on) return;
    U.timer = setInterval(() => {           // catches the sign-in finishing in the browser, an install, Git appearing
      const key = aiKey();
      const inSetup = S.mode === "setup" && STEPS[U.step][0] === "ai";
      if (!inSetup && !(S.mode === "app" && S.view.name === "settings")) { pollAi(false); return; }
      if (!(key === "claude" || key === "codex") || (U.ai && U.ai[key] && aiReady(key, U.ai[key]))) return;
      // in Settings only while something is under way (a sign-in or an install), so other settings aren't disturbed
      const a = (U.ai || {})[key] || {};
      const busy = U.busy["login:" + key] || (a.job && a.job.state === "running") || ((a.native || {}).sign_in || {}).state === "waiting";
      if (inSetup || busy) loadAi(key);
    }, 4000);
  }

  function go(n) {
    U.step = Math.max(0, Math.min(STEPS.length - 1, n));
    pollAi(STEPS[U.step][0] === "ai");
    if (STEPS[U.step][0] === "download") loadPlan();
    St.render();
    const box = $(".su-body"); if (box) box.scrollTop = 0;
  }
  async function loadPlan() {
    U.plan = null; St.render();
    const r = await call("setup_plan", U.ch.speech, U.ch.ai === "local" ? U.ch.llm : "");
    U.plan = r.ok ? r : { items: [], total_mb: 0, blockers: [r.error], free_gb: 0 };
    U.done = U.plan.items.length === 0;
    St.render();
  }

  // ------------------------------------------------------------------ steps
  const pcRows = () => (U.st ? U.st.verdict.summary : []).map((x) => `<div class="su-row ${x.ok}"><span class="su-ico">${I(x.ok === "yes" ? "check" : x.ok === "warn" ? "alert" : "x", "sm")}</span>
      <b>${esc(x.label)}</b><span>${esc(x.value)}</span></div>`).join("");
  const speedHint = (id, gpu) => {
    const f = gpu ? { "large-v3-turbo": 22, "large-v3": 12, medium: 16, small: 25 }[id] : { "large-v3-turbo": 1, "large-v3": 0.4, medium: 0.2, small: 2 }[id];     // measured on a laptop CPU (small 15–30 min, medium 4 h+)
    const min = Math.round(60 / (f || 1));
    return min <= 1 ? "about a minute per lecture hour" : `about ${min >= 60 ? `${(min / 60).toFixed(1).replace(".0", "")} h` : `${min} min`} per lecture hour`;
  };
  function stepWelcome() {
    return `<div class="su-hero"><img class="su-logo" src="../assets/logo.svg" alt=""><div><h1>Welcome to IEUStudia</h1>
        <p class="lead">Your IEULearn courses, Panopto lectures, materials and announcements in one place — lectures transcribed on this PC (English with Turkish passages), the lecturer's heads-ups about quizzes and exams, and optional AI help with your own Claude or ChatGPT plan.</p></div></div>
      <div class="card su-notice"><h3>${I("shield", "sm")} Before you start</h3>${NOTICE}
        <label class="su-accept"><input type="checkbox" class="check" data-act="su-accept" ${U.accepted ? "checked" : ""}> I have read this and want to continue</label></div>`;
  }
  function stepPc() {
    if (!U.st) return U.error ? `<div class="callout error">${I("x-circle")}<div>${esc(U.error)}</div></div>` : St.skeleton();
    const v = U.st.verdict;
    return `<h2>This PC</h2><p class="lead">IEUStudia checked whether this computer can run it.</p>
      <div class="card su-rows">${pcRows()}</div>
      ${v.blockers.length ? `<div class="callout error su-block">${I("x-circle")}<div><b>IEUStudia cannot run on this PC.</b><ul>${v.blockers.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>
        <div class="dim">Nothing was installed or changed. You can close IEUStudia and uninstall it from Windows Settings → Apps.</div></div></div>` : ""}
      ${v.warnings.length ? `<div class="callout warning">${I("alert")}<div><ul>${v.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div></div>` : ""}
      ${!v.blockers.length ? `<div class="callout info">${I("check-circle")}<div>${v.gpu ? `Your NVIDIA GPU will transcribe lectures quickly.` : `Everything works; transcription runs on the processor, so it is slower.`}</div></div>` : ""}`;
  }
  function stepSpeech() {
    const v = U.st.verdict, gpu = v.gpu;
    const cards = U.st.catalog.speech.map((m) => {
      const ok = (v.models[m.id] || {}).ok !== false;
      return `<button class="preset su-pick ${U.ch.speech === m.id ? "on" : ""}" data-act="su-speech" data-id="${m.id}" ${ok ? "" : "disabled"}>
        <div class="n">${esc(m.label)} ${m.id === v.recommended_model ? `<span class="pill now">Recommended</span>` : ""}<span class="sz">${gb(m.size_mb)}${m.ready ? " · on this PC" : ""}</span></div>
        <div class="d">${esc(m.description)}</div><div class="d su-speed">${ok ? `${I("clock", "xs")} ${speedHint(m.id, gpu)}` : esc(v.models[m.id].why)}</div></button>`;
    }).join("");
    return `<h2>Transcription</h2><p class="lead">Lectures are transcribed on this PC by an open speech model (Whisper). It is downloaded once; you can change it later in Settings.</p>
      <div class="presets su-presets">${cards}</div>
      ${gpu && !U.st.catalog.cuda.ready ? `<div class="callout info">${I("cpu")}<div>To use your NVIDIA GPU, IEUStudia also downloads NVIDIA's CUDA libraries (${gb(U.st.catalog.cuda.size_mb)}, from NVIDIA's official package).</div></div>` : ""}
      <div class="set-row"><div class="txt"><div class="t">Save transcripts and notes in</div><div class="d"><span class="path">${esc(S.settings.output_dir || U.st.output_dir)}</span></div></div>
        <button class="btn sm" data-act="pick-folder">Change…</button></div>`;
  }
  function aiCard(id, title, sub, body) {
    return `<div class="su-ai ${U.ch.ai === id ? "on" : ""}"><button class="su-ai-head" data-act="su-ai" data-id="${id}"><span class="su-radio"></span><div><b>${title}</b><div class="d">${sub}</div></div></button>
      ${U.ch.ai === id && body ? `<div class="su-ai-body">${body}</div>` : ""}</div>`;
  }
  // ------------------------------------------------------------------ connecting Claude or ChatGPT, one step at a time
  const VENDOR = {
    claude: { name: "Claude", tool: "Claude Code", maker: "Anthropic", plans: "Claude Pro or Max", upgrade: "https://claude.ai/upgrade",
      intro: "IEUStudia talks to Claude through <b>Claude Code</b>, Anthropic's official command-line tool. It's free to install and works with your Claude Pro or Max subscription. It isn't the Claude desktop app, and you won't need to open it yourself.",
      manual: [["Install Claude Code (in PowerShell)", "irm https://claude.ai/install.ps1 | iex"], ["Sign in", "claude auth login"]] },
    codex: { name: "ChatGPT", tool: "Codex CLI", maker: "OpenAI", plans: "ChatGPT Plus or Pro", upgrade: "https://chatgpt.com/pricing",
      intro: "You sign in on OpenAI's own page and allow IEUStudia to use your ChatGPT Plus or Pro plan. There's nothing to install, and IEUStudia never sees your password. In ChatGPT's settings you can see what it uses, set a limit, or disconnect it.",
      manual: [["Install the Codex CLI (needs Node.js)", "npm install -g @openai/codex"], ["Sign in", "codex login"]] },
  };
  // ChatGPT: "Continue with ChatGPT" (chatgpt.py), or the Codex CLI for people who already use it.
  function chatgptSteps(a) {
    const n = a.native || {}, si = n.sign_in || {}, t = a.test || {}, viaCli = a.method === "cli";
    const waiting = si.state === "waiting" || (U.busy["login:codex"] && !n.signed_in && si.state !== "error");
    const noPlanUse = n.signed_in && !n.plan_allowed;
    const notEligible = t.ok === false && /Plus or Pro/.test(t.error || "");
    return [
      { id: "signin", ok: a.method === "app" || viaCli, title: "Continue with ChatGPT",
        okText: viaCli ? "Using your Codex CLI sign-in" : `Signed in${n.email ? ` as ${esc(n.email)}` : ""}`,
        text: "Opens OpenAI's sign-in page in your browser. Sign in, then allow IEUStudia to use your ChatGPT plan.",
        busy: waiting && !noPlanUse ? "Finish on the page that opened in your browser. This list notices when you're done." : "",
        error: si.state === "error" ? esc(si.error) : noPlanUse ? "You're signed in, but IEUStudia may not use your ChatGPT plan. That needs ChatGPT Plus or Pro (the free plan can't be used in apps). If you have one, click Allow it and accept on OpenAI's page." : "",
        btn: noPlanUse ? `<button class="btn sm" data-act="ai-login" data-id="codex" data-consent="1">Allow it</button>`
          : `<button class="btn sm" data-act="ai-login" data-id="codex">Continue with ChatGPT</button>` },
      { id: "plan", ok: t.ok === true, title: "Your plan", okText: "It works with your plan",
        text: "Needs ChatGPT Plus or Pro.", error: notEligible ? esc(t.error) : "",
        unknown: (a.method && t.ok !== true && !notEligible) ? "Plus or Pro is needed. The test message shows whether your plan can be used in apps." : "",
        btn: notEligible ? `<button class="btn sm" data-act="url" data-url="https://chatgpt.com/pricing">${I("external", "sm")} See plans</button>` : "" },
      { id: "test", ok: t.ok === true, title: "Test message", okText: `ChatGPT answered in ${t.seconds} s`,
        text: "One short message to check that everything works. It uses a little of your plan.",
        busy: U.busy["test:codex"] ? "Sending a short test message…" : "", error: t.ok === false && !notEligible ? esc(t.error) : "",
        btn: `<button class="btn sm" data-act="ai-test" data-id="codex">${t.ok === false ? "Try again" : "Send a test message"}</button>` },
    ];
  }
  // The checklist for one AI: each step is done, busy, failed, unknown, the one to do now, or later.
  function connectSteps(key, a) {
    if (key === "codex") return chatgptSteps(a);
    const v = VENDOR[key], out = [];
    const running = (j) => j && j.state === "running" ? (j.line || "Installing…") : "";
    const failed = (j) => j && j.state === "error" ? j.error : "";
    out.push({ id: "tool", ok: a.available, title: v.tool, okText: `Version ${esc(a.version)}`,
      text: `Free, installed from ${v.maker}'s official source.`, busy: running(a.job), error: failed(a.job),
      btn: `<button class="btn sm" data-act="ai-install" data-id="${key}">${I("download", "sm")} Install ${v.tool}</button>` });
    out.push({ id: "signin", ok: a.logged_in, title: `Sign in to ${v.name}`, okText: a.email ? `Signed in as ${esc(a.email)}` : "Signed in",
      text: `Opens ${v.maker}'s sign-in page in your browser. IEUStudia never sees your password.`,
      busy: U.busy["login:" + key] && !a.logged_in ? `Finish signing in on the page that opened in your browser. A small ${v.tool} window opens too and closes by itself when you're done; this list notices it.` : "",
      btn: `<button class="btn sm" data-act="ai-login" data-id="${key}">Sign in with ${v.name}</button>` });
    const ps = a.plan_status || {}, t = a.test || {};
    out.push({ id: "plan", ok: ps.ok === true || (ps.ok == null && a.logged_in && t.ok === true), title: "Your plan",
      okText: ps.label ? esc(ps.label) + (ps.note ? `. ${esc(ps.note)}` : "") : "It works with your plan",
      text: `Needs ${v.plans}.`, error: ps.ok === false ? esc(ps.note) : "", unknown: ps.ok == null && a.logged_in && t.ok !== true ? esc(ps.note || "") : "",
      btn: ps.ok === false ? `<button class="btn sm" data-act="url" data-url="${v.upgrade}">${I("external", "sm")} See plans</button>` : "" });
    out.push({ id: "test", ok: t.ok === true, title: "Test message", okText: `${v.name} answered in ${t.seconds} s`,
      text: "One short message to check that everything works. It uses a few tokens of your plan.",
      busy: U.busy["test:" + key] ? "Sending a short test message…" : "", error: t.ok === false ? esc(t.error) : "",
      btn: `<button class="btn sm" data-act="ai-test" data-id="${key}">${t.ok === false ? "Try again" : "Send a test message"}</button>` });
    return out;
  }
  const aiReady = (key, a) => connectSteps(key, a).every((s) => s.ok);
  function connectHTML(key) {
    const a = U.ai && U.ai[key], v = VENDOR[key];
    if (!a) return `<div class="dim">${SPIN()} Checking this PC…</div>`;
    const steps = connectSteps(key, a);
    const now = steps.findIndex((s) => !s.ok && !s.unknown);
    const rows = steps.map((s, i) => {
      const st = s.ok ? "ok" : s.busy ? "busy" : s.error ? "err" : s.unknown ? "unknown" : i === now ? "now" : "later";
      const ico = st === "ok" ? I("check", "sm") : st === "busy" ? SPIN() : st === "err" ? I("x", "sm") : st === "unknown" ? "?" : i + 1;
      const detail = st === "ok" ? s.okText : st === "busy" ? esc(s.busy) : st === "err" ? s.error : st === "unknown" ? s.unknown : s.text;
      const btn = (st === "now" || st === "err") && s.btn ? s.btn : "";
      return `<div class="cn-step ${st}"><span class="cn-ico">${ico}</span><div class="grow"><b>${s.title}</b><div class="d">${detail}</div>
        ${st === "now" && s.hint ? `<div class="d dim">${esc(s.hint)}</div>` : ""}</div>${btn}</div>`;
    }).join("");
    const ready = steps.every((s) => s.ok);
    return `<p class="cn-intro">${v.intro}</p><div class="cn-steps">${rows}</div>
      ${ready ? `<div class="callout success">${I("check-circle")}<div><b>${v.name} is ready.</b> AI features in IEUStudia will use your ${v.name} plan.</div></div>`
        : `<div class="dim su-note">${I("info", "xs")} You can also continue now and finish this later in Settings → AI.</div>`}
      ${key === "codex" && a.method === "app" && S.mode === "app" ? `<div class="dim su-note">${I("lock", "xs")} Signed in with ChatGPT. <button class="link" data-act="ai-signout" data-id="codex">Sign out</button> · <button class="link" data-act="url" data-url="https://chatgpt.com/settings/usage">Usage in ChatGPT's settings</button></div>` : ""}
      <details class="cn-manual"><summary>${key === "codex" ? "Already use the Codex CLI?" : "Rather do it yourself?"}</summary>
        <div class="dim">${key === "codex" ? "IEUStudia can use OpenAI's Codex CLI instead of the sign-in above. Install it, or let IEUStudia install it, and sign in with <code>codex login</code>; this list notices it."
          : "Run these in PowerShell, then come back: this list updates by itself."}</div>
        ${key === "codex" ? `<div class="cn-cmd"><span class="dim">${a.available ? `Codex CLI ${esc(a.version)} is installed` : "Install it for you"}</span><span class="dim">${esc(((a.job || {}).state === "running" && a.job.line) || ((a.job || {}).state === "error" && a.job.error) || "")}</span>
          ${a.available ? `<button class="btn ghost sm" data-act="ai-login" data-id="codex-cli">codex login</button>` : `<button class="btn ghost sm" data-act="ai-install" data-id="codex" ${(a.job || {}).state === "running" ? "disabled" : ""}>${I("download", "sm")} Install</button>`}</div>` : ""}
        ${v.manual.map(([l, cmd]) => `<div class="cn-cmd"><span class="dim">${esc(l)}</span><code>${esc(cmd)}</code><button class="btn ghost sm" data-act="ai-copy" data-text="${esc(cmd)}">${I("copy", "sm")}</button></div>`).join("")}</details>`;
  }
  async function runTest(key) {
    U.busy["test:" + key] = true; rerenderAi();
    const r = await call("ai_test", key);
    delete U.busy["test:" + key];
    if (U.ai && U.ai[key]) U.ai[key].test = r.ok ? r.test : { ok: false, error: r.error, seconds: 0 };
    rerenderAi();
  }
  function autoTest() {               // once everything else is done, check it really works (once per session)
    const key = aiKey();
    const a = U.ai && U.ai[key];
    if (!a || !(key === "claude" || key === "codex") || U.autoTested[key] || U.busy["test:" + key] || (a.test && a.test.at)) return;
    const before = connectSteps(key, a).filter((s) => s.id !== "test");
    if (before.every((s) => s.ok || s.unknown)) { U.autoTested[key] = true; runTest(key); }
  }
  function stepAi() {
    const loc = (U.ai && U.ai.local.models) || [];
    const rec = U.st.verdict.recommended_llm;
    const llmBody = rec ? `<div class="presets">${loc.map((m) => `<button class="preset ${U.ch.llm === m.id ? "on" : ""}" data-act="su-llm" data-id="${m.id}" ${m.id === "qwen3-4b" && rec !== "qwen3-4b" ? "disabled" : ""}>
        <div class="n">${esc(m.label)} ${m.id === rec ? `<span class="pill now">Fits this PC</span>` : ""}<span class="sz">${gb(m.size_mb)}${m.ready ? " · on this PC" : ""}</span></div><div class="d">${esc(m.description)}</div></button>`).join("")}</div>
        <div class="dim su-note">Runs entirely on this PC. Fine for short notes and simple questions; study plans and prep packs need Claude or ChatGPT.${U.st.verdict.gpu ? "" : " Without an NVIDIA GPU it writes slowly (notes for a lecture take several minutes)."}</div>`
      : `<div class="callout warning">${I("alert")}<div>This PC has too little memory for an offline model (8 GB needed).</div></div>`;
    return `<h2>AI help (optional)</h2><p class="lead">Study notes, chat about your lectures, video study plans and exam prep packs. Pick what you have — everything else in IEUStudia works without AI.</p>
      ${aiCard("claude", "Claude", "Best results. Needs a Claude Pro or Max subscription.", connectHTML("claude"))}
      ${aiCard("codex", "ChatGPT", "Needs a ChatGPT Plus or Pro subscription.", connectHTML("codex"))}
      ${aiCard("local", "Offline model", "No subscription: a small model on this PC — basic notes and questions.", llmBody)}
      ${aiCard("none", "No AI", "Transcripts, heads-ups, search, calendar and everything else still work.", "")}
      <div class="callout info su-ai-note">${I("info")}<div><b>AI can make mistakes.</b> Answers are generated from your lectures, but check anything important against the lecture and your materials. Nothing is sent anywhere unless you use an AI feature.</div></div>`;
  }
  function stepBackground() {
    const sw = (key, label, d, dis) => `<div class="set-row"><div class="txt"><div class="t">${label}</div><div class="d">${d}</div></div>
      <input type="checkbox" class="switch" data-act="su-bool" data-key="${key}" ${U.ch[key] ? "checked" : ""} ${dis ? "disabled" : ""}></div>`;
    return `<h2>In the background</h2><p class="lead">A small helper can stay in the notification area (tray) and tell you when something new appears on IEULearn — even when the IEUStudia window is closed.</p>
      <div class="card set-card">${sw("tray", "Keep the helper running in the tray", "About 50 MB of memory. Checks IEULearn every 30 minutes; it never transcribes or downloads anything by itself.")}
        ${sw("startup", "Start it when I sign in to Windows", "So notifications arrive without opening IEUStudia first.", !U.ch.tray)}</div>
      <div class="card set-card"><h3>Notify me about</h3>
        ${sw("notify_recordings", "New lecture recordings", "Of your own section.", !U.ch.tray)}
        ${sw("notify_announcements", "New announcements", "Quiz, exam and homework news is marked as a heads-up.", !U.ch.tray)}
        ${sw("notify_materials", "New files", "Slides and PDFs added to a course.", !U.ch.tray)}</div>
      <div class="dim su-note">${I("info", "xs")} You can change all of this later in Settings, or from the tray icon's menu.</div>`;
  }
  function stepDownload() {
    const p = U.plan;
    if (!p) return `<h2>Download</h2>${St.skeleton()}`;
    const rows = p.items.map((it) => {
      const d = U.dl[it.id] || {};
      const pct = d.total ? Math.round((100 * d.done) / d.total) : d.state === "done" ? 100 : 0;
      const state = d.state === "done" ? `<span class="pill done">${I("check")}Done</span>` : d.state === "error" ? `<span class="pill failed">Failed</span>`
        : d.state === "downloading" ? `<span class="dim">${d.total ? `${bytes(d.done)} of ${bytes(d.total)}` : "Starting…"}</span>` : `<span class="dim">${gb(it.size_mb)}</span>`;
      return `<div class="su-dl"><div class="grow"><b>${esc(it.label)}</b>${d.error ? `<div class="err-txt">${esc(d.error)}</div>` : ""}<div class="bar"><i style="width:${pct}%"></i></div></div>${state}</div>`;
    }).join("");
    const total = p.total_mb;
    return `<h2>Download</h2><p class="lead">${p.items.length ? "These are downloaded once now, so IEUStudia works right away." : "Everything IEUStudia needs is already on this PC."}</p>
      ${p.items.length ? `<div class="card su-dls">${rows}</div>` : ""}
      <div class="card su-space"><h3>${I("hard-drive", "sm")} Disk space</h3>
        <div class="su-row"><span class="grow">Downloads now</span><b>${gb(total)}</b></div>
        <div class="su-row"><span class="grow">Each transcribed lecture hour (text, slides from the screen recording)</span><b>≈ 5–30 MB</b></div>
        <div class="su-row"><span class="grow">Course files you download</span><b>as large as the files</b></div>
        <div class="su-row"><span class="grow">Free on ${esc((U.st.facts.disk.data_dir || "C:").slice(0, 3))}</span><b>${p.free_gb.toFixed(0)} GB</b></div></div>
      ${(p.blockers || []).length ? `<div class="callout error">${I("x-circle")}<div>${p.blockers.map(esc).join("<br>")}</div></div>` : ""}`;
  }
  function stepSignin() {
    return `<h2>Almost done</h2><p class="lead">${S.boot && S.boot.signed_in ? `You are signed in as ${esc((S.profile || {}).name || "")}. Finish to open IEUStudia.` : "Last step: sign in with your IEULearn (EkoID) account."}</p>
      <div class="card su-summary">${[["Transcription", (U.st.catalog.speech.find((m) => m.id === U.ch.speech) || {}).label || U.ch.speech],
        ["AI", { claude: "Claude", codex: "ChatGPT", local: "Offline model", none: "No AI", "": "No AI" }[U.ch.ai]],
        ["Background helper", U.ch.tray ? `On${U.ch.startup ? ", starts with Windows" : ""}` : "Off"]].map(([k, v]) => `<div class="su-row"><span class="grow">${k}</span><b>${esc(v)}</b></div>`).join("")}</div>`;
  }

  // ------------------------------------------------------------------ frame
  function canNext() {
    const k = STEPS[U.step][0];
    if (k === "welcome") return U.accepted;
    if (k === "pc") return U.st && !U.st.verdict.blockers.length;
    if (k === "speech") return !!U.ch.speech;
    if (k === "ai") return true;
    if (k === "download") return U.done && !(U.plan && (U.plan.blockers || []).length);
    return true;
  }
  St.setupHTML = () => {
    const k = STEPS[U.step][0];
    const body = !U.st && k !== "welcome" ? stepPc() : { welcome: stepWelcome, pc: stepPc, speech: stepSpeech, ai: stepAi, background: stepBackground, download: stepDownload, signin: stepSignin }[k]();
    const dl = k === "download" && U.plan && U.plan.items.length && !U.done;
    const next = k === "signin" ? `<button class="btn primary" data-act="su-finish">${S.boot && S.boot.signed_in ? "Open IEUStudia" : "Continue to sign in"} ${I("arrow-right", "sm")}</button>`
      : dl ? `<button class="btn primary" data-act="su-download" ${U.running || (U.plan.blockers || []).length ? "disabled" : ""}>${U.running ? `${SPIN()} Downloading…` : `${I("download", "sm")} Download ${gb(U.plan.total_mb)}`}</button>`
        : `<button class="btn primary" data-act="su-next" ${canNext() ? "" : "disabled"}>Continue ${I("arrow-right", "sm")}</button>`;
    return `<div class="setup"><aside class="su-rail"><div class="brand"><img class="logo-img" src="../assets/logo.svg" alt="">IEUStudia</div>
        <ol>${STEPS.map(([key, label], i) => `<li class="${i === U.step ? "on" : i < U.step ? "done" : ""}"><span>${i < U.step ? I("check", "xs") : i + 1}</span>${label}</li>`).join("")}</ol>
        <div class="su-rail-foot">${I("lock", "xs")} No account with us, no tracking.</div></aside>
      <main class="su-main"><div class="su-body">${body}</div>
        <div class="su-foot">${U.step > 0 && !U.running ? `<button class="btn ghost" data-act="su-back">${I("arrow-left", "sm")} Back</button>` : `<span></span>`}
          ${U.running ? `<button class="btn ghost" data-act="su-cancel">Cancel download</button>` : ""}${next}</div></main></div>`;
  };

  // ------------------------------------------------------------------ actions
  Object.assign(A, {
    "su-accept": (el) => { U.accepted = el.checked; St.render(); },
    "su-next": async () => {
      const k = STEPS[U.step][0];
      if (k === "speech") { await call("update_settings", { model: U.ch.speech }); S.settings.model = U.ch.speech; }
      if (k === "ai") {
        const r = await call("app_config", { ai_provider: U.ch.ai || "none", local_model: U.ch.llm });
        if (!r.ok) { toast(r.error, "error"); return; }
      }
      if (k === "background") {
        const r = await call("app_config", { tray: U.ch.tray, start_with_windows: U.ch.tray && U.ch.startup, notify_recordings: U.ch.notify_recordings,
          notify_announcements: U.ch.notify_announcements, notify_materials: U.ch.notify_materials });
        if (!r.ok) { toast(r.error, "error"); return; }
      }
      go(U.step + 1);
    },
    "su-back": () => go(U.step - 1),
    "su-speech": (el) => { U.ch.speech = el.dataset.id; St.render(); },
    "su-llm": (el) => { U.ch.llm = el.dataset.id; St.render(); },
    "su-ai": (el) => { U.ch.ai = el.dataset.id; St.render(); if (VENDOR[U.ch.ai]) { loadAi(U.ch.ai); pollAi(true); } },
    "su-bool": (el) => { U.ch[el.dataset.key] = el.checked; if (el.dataset.key === "tray" && !el.checked) U.ch.startup = false; St.render(); },
    "su-download": async () => {
      U.running = true; U.dl = {}; St.render();
      const r = await call("setup_download", U.plan.items.map((x) => x.id));
      if (!r.ok) { U.running = false; toast(r.error, "error"); St.render(); }
    },
    "su-cancel": () => call("setup_cancel"),
    "su-finish": async () => {
      const r = await call("finish_setup");
      if (!r.ok) { toast(r.error, "error"); return; }
      pollAi(false);
      S.boot.app = r.app;
      if (S.boot.signed_in) { S.mode = "app"; St.render(); St.afterBoot(); }
      else { S.mode = "signin"; St.render(); }
    },
    "setup-again": () => St.startSetup(),
  });
  // checkbox inputs of the setup (app.js lets these reach their handlers)
  St.inputActs = (St.inputActs || []).concat(["su-accept", "su-bool"]);

  // installing Claude Code or the Codex CLI (ai_setup.py)
  St.onEvents.ai_setup = (e) => {
    const key = e.which;
    if (U.ai && U.ai[key]) U.ai[key].job = e;
    if (e.state === "error") toast(e.error, "error");
    if (e.state !== "running") { if (U.ai && U.ai[key]) delete U.ai[key].test; U.autoTested[key] = false; loadAi(key); }
    rerenderAi();
  };
  // "Continue with ChatGPT" finished in the browser (chatgpt.py)
  St.onEvents.chatgpt = (e) => {
    delete U.busy["login:codex"];
    if (e.state === "error") toast(e.error, "error");
    if (U.ai && U.ai.codex && U.ai.codex.native) U.ai.codex.native.sign_in = { state: e.state, error: e.error || "" };
    U.autoTested.codex = false;
    loadAi("codex");
    rerenderAi();
  };
  Object.assign(A, {
    "ai-install": async (el) => {
      const which = el.dataset.id;
      const r = await call("ai_install", which);
      if (!r.ok) { toast(r.error, "error"); return; }
      if (U.ai && U.ai[which]) U.ai[which].job = r.job;
      pollAi(true); rerenderAi();
    },
    "ai-login": async (el) => {
      const which = el.dataset.id, key = which === "codex-cli" ? "codex" : which;
      const r = await call("ai_login", which, !!el.dataset.consent);
      if (!r.ok) { toast(r.error, "error"); return; }
      U.busy["login:" + key] = Date.now(); U.autoTested[key] = false;
      if (U.ai && U.ai[key]) {
        delete U.ai[key].test;
        if (which === "codex" && U.ai.codex.native) U.ai.codex.native.sign_in = { state: "waiting", error: "" };
      }
      setTimeout(() => { if (U.busy["login:" + key]) { delete U.busy["login:" + key]; rerenderAi(); } }, 10 * 60000);
      pollAi(true); rerenderAi();
    },
    "ai-signout": async (el) => {
      if (!confirm("Sign out of ChatGPT in IEUStudia? OpenAI ends the session; you can sign in again any time.")) return;
      const r = await call("ai_sign_out", el.dataset.id);
      if (!r.ok) { toast(r.error, "error"); return; }
      toast(r.revoked ? "Signed out of ChatGPT." : "Signed out here. OpenAI didn't confirm it, so also disconnect IEUStudia in ChatGPT's settings.", r.revoked ? "success" : "info");
      U.autoTested.codex = false; loadAi("codex");
    },
    "ai-test": (el) => runTest(el.dataset.id),
    "ai-copy": async (el) => { const r = await call("copy_text", el.dataset.text); toast(r.ok ? "Copied" : r.error, r.ok ? "success" : "error"); },
  });

  St.onEvents.setup = (e) => {
    if (e.id) U.dl[e.id] = { ...(U.dl[e.id] || {}), ...e };
    if (e.state === "idle") {
      U.running = false;
      U.done = !!e.ok && U.plan && U.plan.items.every((it) => (U.dl[it.id] || {}).state === "done");
      if (!e.ok && Object.values(U.dl).some((d) => d.state === "error")) toast("A download failed — check your connection and try again.", "error");
    }
    if (S.mode === "setup") St.render();
    else if (S.view.name === "settings") St.renderView();
  };

  // ------------------------------------------------------------------ Settings cards
  St.settingsAI = () => {
    const app = (S.boot || {}).app || {};
    const a = U.ai;
    if (!a) { setTimeout(loadAi, 0); }
    const label = { claude: "Claude (your Claude Pro or Max plan)", codex: "ChatGPT (your ChatGPT Plus or Pro plan)", local: "Offline model on this PC", none: "No AI" };
    const status = !a ? `${SPIN()} Checking…` : app.ai_provider === "claude" ? (a.claude.logged_in ? `Claude Code ${esc(a.claude.version)} · signed in${a.claude.plan ? ` (${esc(a.claude.plan)})` : ""}` : a.claude.available ? "Claude Code is installed but not signed in." : "Claude Code is not installed.")
      : app.ai_provider === "codex" ? (a.codex.method === "app" ? `Signed in with ChatGPT${(a.codex.native || {}).email ? ` as ${a.codex.native.email}` : ""}.` : a.codex.method === "cli" ? `Using the Codex CLI ${a.codex.version}, signed in with ChatGPT.` : "Not connected yet.")
        : app.ai_provider === "local" ? ((a.local.models.find((m) => m.id === app.local_model) || {}).ready ? `${esc(app.local_model)} is on this PC.` : "The offline model is not downloaded yet.") : "AI features are hidden.";
    const needs = a && a.local && app.ai_provider === "local" && !(a.local.models.find((m) => m.id === app.local_model) || {}).ready;
    const cli = (app.ai_provider === "claude" || app.ai_provider === "codex") && a && a[app.ai_provider];
    if (cli && !U.timer) pollAi(true);
    return `<div class="set-row"><div class="txt"><div class="t">AI service</div><div class="d">${esc(status)}</div></div>
        <select class="input" style="width:260px" data-act="ai-provider">${Object.entries(label).map(([v, l]) => `<option value="${v}" ${(app.ai_provider || "none") === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      ${cli ? `<div class="set-connect">${connectHTML(app.ai_provider)}</div>` : ""}
      ${needs ? `<div class="set-row"><div class="txt"><div class="t">Download the offline model</div><div class="d">It is downloaded in the setup's AI step.</div></div><button class="btn sm" data-act="setup-ai">${I("arrow-right", "sm")} Set up AI</button></div>` : ""}
      ${app.ai_provider === "codex" && a && a.codex.models.length ? `<div class="set-row"><div class="txt"><div class="t">ChatGPT model</div><div class="d">The default is usually right; pick another only if your plan offers it.</div></div>
        <select class="input" style="width:200px" data-act="codex-model"><option value="">Default</option>${a.codex.models.map((m) => `<option ${app.codex_model === m ? "selected" : ""}>${esc(m)}</option>`).join("")}</select></div>` : ""}
      <div class="callout info">${I("info")}<div><b>AI can make mistakes.</b> Check important details against your lectures and materials. AI only runs when you ask.</div></div>`;
  };
  St.settingsMore = () => {
    const app = (S.boot || {}).app || {};
    const row = (t, d, ctl) => `<div class="set-row"><div class="txt"><div class="t">${t}</div><div class="d">${d}</div></div>${ctl}</div>`;
    const sw = (key, dis) => `<input type="checkbox" class="switch" data-act="app-bool" data-key="${key}" ${app[key] ? "checked" : ""} ${dis ? "disabled" : ""}>`;
    const st = U.storage;
    if (!st) setTimeout(loadStorage, 0);
    const use = st ? Object.entries({ speech_models: "Speech models", gpu_libraries: "NVIDIA GPU libraries", offline_ai: "Offline AI model", library: "Library and transcripts", cache: "Caches", your_files: "Your files (output folder)" })
      .filter(([k]) => st.usage[k]).map(([k, l]) => `<div class="su-row"><span class="grow">${l}</span><b>${bytes(st.usage[k])}</b></div>`).join("") : St.skeleton();
    const removable = st ? [...st.catalog.speech.filter((m) => m.ready && m.id !== S.settings.model).map((m) => [`speech:${m.id}`, `Speech model “${m.label}”`, m.size_mb]),
      ...st.catalog.llm.filter((m) => m.ready && !(app.ai_provider === "local" && app.local_model === m.id)).map((m) => [`llm:${m.id}`, `Offline model “${m.label}”`, m.size_mb])] : [];
    return `<div class="card set-card"><h3>Background and notifications</h3>
        ${row("Tray helper", "Keeps running when the window is closed (about 50 MB of memory), checks IEULearn every 30 minutes and shows notifications. It never transcribes or downloads by itself.", sw("tray"))}
        ${row("Start with Windows", "Start the helper when you sign in to Windows.", sw("start_with_windows", !app.tray))}
        ${row("New lecture recordings", "Of your own section.", sw("notify_recordings", !app.tray))}
        ${row("New announcements", "Quiz, exam and homework news is marked as a heads-up.", sw("notify_announcements", !app.tray))}
        ${row("New files", "Slides and PDFs added to a course.", sw("notify_materials", !app.tray))}</div>
      <div class="card set-card"><h3>Storage</h3>${use}
        ${removable.map(([id, l, mb]) => row(l, `${gb(mb)} · not in use`, `<button class="btn ghost sm danger" data-act="del-component" data-id="${esc(id)}">${I("x", "sm")} Remove</button>`)).join("")}</div>
      <div class="card set-card"><h3>About</h3>
        ${row(`IEUStudia ${esc((S.boot || {}).version || "")}`, "Open source (MIT). Transcription runs on this PC with faster-whisper; AI only through the service you chose.",
          `<div style="display:flex;gap:6px"><button class="btn ghost sm" data-act="logs">${I("terminal", "sm")} Logs</button><button class="btn sm" data-act="setup-again">${I("refresh", "sm")} Run setup again</button></div>`)}
        <div class="su-notice-small">${NOTICE}</div></div>`;
  };
  async function loadStorage() {
    const r = await call("storage");
    if (r.ok) { U.storage = r; if (S.view.name === "settings") St.renderView(); }
  }
  Object.assign(A, {
    "ai-provider": async (el) => {
      const v = el.value;
      const r = await call("app_config", { ai_provider: v });
      if (!r.ok) { toast(r.error, "error"); return; }
      S.boot.app = r.app; await loadAi();
      if (v === "local") U.storage = null;
      St.renderView();
    },
    "codex-model": async (el) => { const r = await call("app_config", { codex_model: el.value }); if (r.ok) S.boot.app = r.app; },
    "app-bool": async (el) => {
      const patch = { [el.dataset.key]: el.checked };
      if (el.dataset.key === "tray" && !el.checked) patch.start_with_windows = false;
      const r = await call("app_config", patch);
      if (!r.ok) { toast(r.error, "error"); return; }
      S.boot.app = r.app; St.renderView();
      if (el.dataset.key === "tray") toast(el.checked ? "The helper is now in the tray (bottom right)." : "The helper was turned off.", "info");
    },
    "setup-ai": () => { St.startSetup().then(() => go(STEPS.findIndex((s) => s[0] === "ai"))); },
    "del-component": async (el) => {
      if (!confirm("Remove it from this PC? You can download it again later.")) return;
      const r = await call("delete_component", el.dataset.id);
      if (!r.ok) { toast(r.error, "error"); return; }
      U.storage = r; St.renderView();
    },
  });
  St.inputActs = St.inputActs.concat(["app-bool"]);

  // ------------------------------------------------------------------ browser preview (mock)
  St.mocks.setup_state = () => ({ ok: true, model: "large-v3-turbo", output_dir: "C:\\Users\\student\\Documents\\IEUStudia", frozen: true, startup: false,
    app: { onboarded: false, terms_accepted: "", ai_provider: "", codex_model: "", local_model: "qwen3-1.7b", tray: false, start_with_windows: false, notify_recordings: true, notify_announcements: true, notify_materials: false },
    facts: { disk: { data_dir: "C:\\Users\\student\\AppData\\Local\\IEUStudia", data_free_gb: 182.4 } },
    verdict: { ok: true, blockers: location.hash.includes("weak") ? ["This processor does not support AVX instructions, which the speech recognition needs."] : [], warnings: [], gpu: true, recommended_model: "large-v3-turbo", recommended_llm: "qwen3-4b",
      models: { "large-v3-turbo": { ok: true }, "large-v3": { ok: true }, medium: { ok: true }, small: { ok: true } },
      summary: [{ label: "Windows", value: "Windows 11 24H2 (build 26100)", ok: "yes" }, { label: "Processor", value: "Intel Core i7-13700H · 20 threads", ok: "yes" },
        { label: "Memory", value: "32 GB", ok: "yes" }, { label: "Graphics", value: "NVIDIA GeForce RTX 4070 Laptop GPU · 8 GB · driver 566.14", ok: "yes" }, { label: "Free disk space", value: "182 GB on C:\\", ok: "yes" }] },
    catalog: { speech: [{ id: "large-v3-turbo", label: "Fast & accurate", description: "Recommended. Near large-v3 quality at a fraction of the time on a GPU.", size_mb: 1620, ready: false },
      { id: "large-v3", label: "Maximum accuracy", description: "Slowest, best for hard audio and long Turkish passages.", size_mb: 3090, ready: false },
      { id: "medium", label: "Medium", description: "More accurate than Small, but very slow without an NVIDIA GPU (hours per lecture).", size_mb: 1530, ready: false },
      { id: "small", label: "Small", description: "The one for PCs without an NVIDIA GPU: fast enough on the processor, a bit less accurate.", size_mb: 484, ready: false }],
    cuda: { needed: true, ready: false, size_mb: 560 }, llm: [] } });
  // the preview walks through a fresh PC: Claude Code, then signing in (#ready: all done already)
  const M = St.mockAi = { claude: location.hash.includes("ready"), codex: false,
    login: { claude: location.hash.includes("ready"), codex: false, chatgpt: false }, test: {} };
  const mockJob = (which) => ({ state: M[which] ? "done" : "", line: "", error: "" });
  St.mocks.ai_status = (which) => {
    const all = { ok: true, provider: "none", chosen: "",
      claude: { available: M.claude, version: M.claude ? "2.1.290" : "", logged_in: M.login.claude, plan: M.login.claude ? "pro" : "", email: M.login.claude ? "student@example.com" : "",
        needs: "Claude Pro or Max subscription", plan_status: M.login.claude ? { ok: true, label: "Claude Pro", note: "" } : { ok: null, label: "", note: "" },
        job: mockJob("claude"), test: M.test.claude || {} },
      codex: { available: M.codex, version: M.codex ? "0.157.0" : "", logged_in: M.login.codex, plan: M.login.codex ? "chatgpt" : "", email: "", ours: M.codex, model: "",
        models: M.login.chatgpt ? ["gpt-6.1-sol", "gpt-6-sol-mini"] : [], method: M.login.chatgpt ? "app" : M.login.codex ? "cli" : "",
        native: { signed_in: M.login.chatgpt, email: M.login.chatgpt ? "student@example.com" : "", plan_allowed: M.login.chatgpt, registered: M.login.chatgpt, sign_in: {} },
        needs: "ChatGPT Plus or Pro subscription", plan_status: { ok: null, label: "", note: "" }, job: mockJob("codex"), test: M.test.codex || {} },
      local: { model: "qwen3-1.7b", models: [{ id: "qwen3-1.7b", label: "Basic", description: "Qwen3 1.7B: short notes and simple questions; runs on most PCs (8 GB memory).", size_mb: 1745, ready: false },
        { id: "qwen3-4b", label: "Better", description: "Qwen3 4B Instruct: clearly better notes and answers; needs a GPU with 8 GB or 16 GB of memory.", size_mb: 4045, ready: false }] } };
    if (which) { delete all.local; delete all[which === "claude" ? "codex" : "claude"]; }
    return all;
  };
  St.mocks.ai_install = (which) => {
    const lines = { claude: ["Downloading Claude Code from claude.ai...", "Setting up Claude Code...", "Claude Code successfully installed!"],
      codex: ["Looking up the latest Codex CLI on npm…", "Downloading Codex 0.157.0… 31 of 58 MB", "Unpacking…"] }[which];
    lines.forEach((line, i) => setTimeout(() => window.__lsEvent({ type: "ai_setup", which, state: "running", line, error: "" }), 400 + 700 * i));
    setTimeout(() => { M[which] = true; window.__lsEvent({ type: "ai_setup", which, state: "done", line: "Installed", error: "" }); }, 400 + 700 * lines.length);
    return { ok: true, job: { state: "running", line: "Starting…", error: "" } };
  };
  St.mocks.ai_login = (which) => {
    if (which === "codex") {            // "Continue with ChatGPT": the browser comes back after a while
      setTimeout(() => { M.login.chatgpt = true; window.__lsEvent({ type: "chatgpt", state: "done", error: "", email: "student@example.com" }); }, 2500);
      return { ok: true, state: "waiting" };
    }
    setTimeout(() => { M.login[which === "codex-cli" ? "codex" : which] = true; }, 3000);
    return { ok: true };
  };
  St.mocks.ai_sign_out = () => { M.login.chatgpt = false; delete M.test.codex; return { ok: true, revoked: true }; };
  St.mocks.ai_test = (which) => new Promise((res) => setTimeout(() => { M.test[which] = { ok: true, seconds: 3.4, error: "", at: Date.now() / 1000 }; res({ ok: true, test: M.test[which] }); }, 1500));
  St.mocks.setup_plan = (speech, llm) => {
    const items = [{ id: `speech:${speech}`, label: "Speech model (Fast & accurate)", size_mb: 1620 }, { id: "cuda", label: "NVIDIA GPU libraries (CUDA 12 cuBLAS)", size_mb: 560 }];
    if (llm) items.push({ id: `llm:${llm}`, label: "Offline AI model", size_mb: llm === "qwen3-4b" ? 4045 : 1745 });
    return { ok: true, items, total_mb: items.reduce((a, b) => a + b.size_mb, 0), free_gb: 182.4, blockers: [] };
  };
  St.mocks.setup_download = (ids) => {
    let i = 0, done = 0;
    const tick = setInterval(() => {
      const id = ids[i]; if (!id) { clearInterval(tick); window.__lsEvent({ type: "setup", id: "", state: "idle", ok: true }); return; }
      done += 40; window.__lsEvent({ type: "setup", id, state: done >= 100 ? "done" : "downloading", done: Math.min(done, 100) * 1048576, total: 100 * 1048576 });
      if (done >= 100) { i++; done = 0; }
    }, 120);
    return { ok: true };
  };
  St.mocks.setup_cancel = () => ({ ok: true });
  St.mocks.app_config = (patch) => { S.boot.app = { ...(S.boot.app || {}), ...(patch || {}) }; return { ok: true, app: S.boot.app, startup: !!S.boot.app.start_with_windows, helper: !!S.boot.app.tray }; };
  St.mocks.finish_setup = () => ({ ok: true, app: { ...(S.boot.app || {}), onboarded: true } });
  St.mocks.storage = () => ({ ok: true, usage: { speech_models: 1.7e9, gpu_libraries: 7.8e8, library: 1.2e8, cache: 6e7, your_files: 2.4e9 }, free_gb: 182.4, data_dir: "C:\\Users\\student\\AppData\\Local\\IEUStudia",
    catalog: { speech: [{ id: "large-v3-turbo", label: "Fast & accurate", size_mb: 1620, ready: true }, { id: "small", label: "Small", size_mb: 484, ready: true }], cuda: {}, llm: [] } });
  St.mocks.delete_component = () => St.mocks.storage();
})();
