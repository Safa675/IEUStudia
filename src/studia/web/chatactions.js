/* IEUStudia — doing things from the chat: buttons for what the AI suggests (```ieustudia {json}``` blocks at the end of
 * an answer), "/" commands typed in the composer, and a row of quick actions above it. Nothing runs by itself:
 * every action opens the usual dialog or asks first. Plugs into app.js through window.Studia. */
(function () {
  "use strict";
  const St = window.Studia;
  const { S, call, esc, I, plural, toast, go, A } = St;
  const $ = (s, el = document) => el.querySelector(s);

  const ICON = { video_plan: "youtube", prep_pack: "cap", get_ready: "list-checks", transcribe: "wave", download_files: "download",
    study_notes: "ai", open_lecture: "file-text", weekly_digest: "calendar", calendar: "calendar", sync_course: "zap", search: "search" };
  const found = {};                     // "seq:i" -> action
  const chatCourse = () => (S.chat.draft || S.chat.data || {}).course_id || "";
  const course = (cid) => S.courses.find((c) => c.id === cid) || {};
  const weeksText = (w) => (!w || !w.length ? "" : w.length === 1 ? `week ${w[0]}` : `weeks ${Math.min(...w)}–${Math.max(...w)}`);
  const dayOf = (cid, date) => ((course(cid).days || []).find((d) => d.date === date && d.mine !== false) || (course(cid).days || []).find((d) => d.date === date) || {}).day_key;

  function describe(a, cid) {
    const code = course(cid).code || "this course";
    const w = weeksText(a.weeks);
    return ({
      video_plan: ["Video study plan", [code, w, a.focus && `for ${a.focus}`].filter(Boolean).join(" · "), "Set it up…"],
      prep_pack: ["Exam prep pack", [code, w, a.focus && `for ${a.focus}`].filter(Boolean).join(" · "), "Set it up…"],
      get_ready: ["Get ready", `${code} · ${a.focus || "the next quiz or exam"}`, "Open checklist"],
      transcribe: ["Transcribe lectures", `${code} · ${a.date || w || "missing lectures"}`, "Transcribe…"],
      download_files: ["Download files", `${code} · ${w || "all weeks"}`, "Download…"],
      study_notes: ["Study notes", `${code} · ${a.date || ""}`, "Open"],
      open_lecture: ["Open the lecture", `${code} · ${a.date || ""}${a.at ? ` at ${a.at}` : ""}`, "Open"],
      weekly_digest: ["Weekly digest", "What happened this week and what is coming", "Open"],
      calendar: ["Add to calendar", "The announced quizzes and exams as a calendar file", "Save"],
      sync_course: ["Sync this course", `${code}: check IEULearn, transcribe and download what is missing`, "Review…"],
      search: ["Search", a.query || "", "Search"],
    })[a.action];
  }

  // ---------------------------------------------------------------- AI-suggested actions in answers
  St.actionCards = (text, seq, streaming) => {
    if (!text || !text.includes("```ieustudia")) return { text, html: "" };
    const cards = [];
    const clean = text.replace(/```ieustudia\s*([\s\S]*?)(```|$)/g, (_, body) => {
      if (streaming) return "";
      try {
        const a = JSON.parse(body.trim());
        if (a && describe(a, chatCourse())) cards.push(a);
      } catch (e) { /* not an action */ }
      return "";
    });
    const cid = chatCourse();
    const html = cards.length ? `<div class="act-cards">${cards.map((a, i) => {
      found[`${seq}:${i}`] = a;
      const [t, d, label] = describe(a, cid);
      return `<div class="act-card"><span class="act-ico">${I(ICON[a.action] || "zap", "sm")}</span><div class="grow"><b>${esc(t)}</b><div class="d">${esc(d)}</div></div>
        <button class="btn primary sm" data-act="chat-action" data-key="${seq}:${i}">${esc(label)}</button></div>`;
    }).join("")}</div>` : "";
    return { text: clean.trim(), html };
  };

  async function detail(cid) {
    if (!S.cd[cid]) { const r = await call("course_detail", cid); if (r.ok) S.cd[cid] = r.course; }
    return S.cd[cid];
  }
  async function run(a, cid) {
    cid = cid || chatCourse();
    const d = ["transcribe", "download_files", "get_ready", "video_plan", "prep_pack"].includes(a.action) ? await detail(cid) : null;
    const assess = d && a.focus ? ((d.alerts || {}).plan || []).find((x) => x.name.toLowerCase() === String(a.focus).toLowerCase()) : null;
    switch (a.action) {
      case "video_plan": case "prep_pack":
        return A["plan-new"]({ dataset: { id: cid, kind: a.action === "prep_pack" ? "prep" : "videos", focus: assess ? assess.name : "", weeks: (a.weeks || []).join(",") } });
      case "get_ready": {
        const plan = ((d || {}).alerts || {}).plan || [];
        const e = assess || plan.find((x) => x.week && !(d.weeks.find((w) => w.no === x.week) || {}).past) || plan[0];
        if (!e) { toast("No quiz or exam has been announced for this course yet.", "info"); return; }
        return A["ready-open"]({ dataset: { cid, name: e.name } });
      }
      case "transcribe": {
        const weeks = new Set(a.weeks || []);
        const keys = a.date ? [dayOf(cid, a.date)].filter(Boolean)
          : (d ? d.weeks.filter((w) => weeks.has(w.no)).flatMap((w) => w.days.filter((x) => x.mine !== false && !x.transcribed).map((x) => x.day_key)) : []);
        if (!keys.length) { toast("Those lectures are already transcribed (or not recorded yet).", "info"); return; }
        if (!confirm(`Transcribe ${plural(keys.length, "class day")} of ${course(cid).code || "this course"} on this PC?`)) return;
        let n = 0;
        for (const k of keys) { const r = await call("transcribe_day", k); if (r.ok) n += r.queued || 0; }
        toast(`${plural(n, "recording")} queued`, "success");
        return;
      }
      case "download_files": {
        const weeks = new Set(a.weeks || []);
        const ids = d ? d.weeks.filter((w) => !weeks.size || weeks.has(w.no)).flatMap((w) => w.units.flatMap((u) => u.materials)).filter((m) => !m.downloaded && !m.gone).map((m) => m.id) : [];
        if (!ids.length) { toast("Those files are already on this PC.", "info"); return; }
        const r = await call("materials_download", ids);
        if (r.ok) toast(`Downloading ${plural(ids.length, "file")}`, "success");
        return;
      }
      case "study_notes": case "open_lecture": {
        const k = a.date && dayOf(cid, a.date);
        if (!k) { toast("That lecture was not found in this course.", "info"); return; }
        await St.openDay(k);
        if (a.action === "study_notes") setTimeout(() => A.claude(), 300);
        else if (a.at) { const [mm, ss] = String(a.at).split(":").map(Number); setTimeout(() => St.jumpTo(+a.part || 1, (mm || 0) * 60 + (ss || 0)), 300); }
        return;
      }
      case "weekly_digest": return go({ name: "digest" });
      case "calendar": return A["export-calendar"]();
      case "sync_course": return A["course-sync"]({ dataset: { id: cid } });
      case "search": go({ name: "library" }); setTimeout(() => { const box = $("[data-act='search']"); if (box) { box.value = a.query || ""; St.runSearch(a.query || ""); } }, 200); return;
      default: toast("IEUStudia cannot do that yet.", "info");
    }
  }
  St.runAction = run;

  // ---------------------------------------------------------------- "/" commands and quick actions
  const CMDS = [
    ["plan", "video_plan", "Video study plan for this course (add a quiz name or weeks)"],
    ["prep", "prep_pack", "Exam prep pack (add a quiz or exam name)"],
    ["ready", "get_ready", "Get-ready checklist for the next quiz or exam"],
    ["notes", "study_notes", "Study notes for this lecture"],
    ["transcribe", "transcribe", "Transcribe lectures (add week numbers)"],
    ["files", "download_files", "Download the files (add week numbers)"],
    ["sync", "sync_course", "Sync this course: transcribe and download what is missing"],
    ["digest", "weekly_digest", "This week in all your courses"],
    ["calendar", "calendar", "Put the announced quizzes and exams in your calendar"],
    ["search", "search", "Search your lectures and files"],
  ];
  function parse(text) {
    const m = /^\/(\w+)\s*(.*)$/s.exec(text.trim());
    if (!m) return null;
    const c = CMDS.find(([k]) => k === m[1].toLowerCase());
    if (!c) return null;
    const rest = m[2].trim();
    const weeks = (rest.match(/\b\d{1,2}\b/g) || []).map(Number).filter((n) => n > 0 && n < 20);
    const a = { action: c[1] };
    if (weeks.length && /week|hafta|^\d|\s\d/i.test(rest) && !/quiz|exam|midterm|final/i.test(rest)) a.weeks = weeks;
    if (/quiz|exam|midterm|final|project/i.test(rest)) a.focus = rest.replace(/\b(for|the)\b/gi, "").replace(/\s+/g, " ").trim().replace(/^\w/, (x) => x.toUpperCase());
    if (c[1] === "search") a.query = rest;
    if (c[1] === "study_notes") {
      const days = (S.chat.data || S.chat.draft || {}).days || [];
      const day = typeof days[0] === "string" ? days[0] : (days[0] || {}).key;
      if (day) a.date = String(day).split("|")[1];
    }
    return a;
  }
  St.slashCommand = (text) => {
    const a = parse(text);
    if (!a) return false;
    run(a);
    return true;
  };
  St.chatToolsHTML = () => {
    const cid = chatCourse(); if (!cid) return "";
    const ai = S.system.ai || {};
    const btn = (cmd, ic, label, title) => `<button type="button" class="btn ghost sm" data-act="chat-cmd" data-cmd="${cmd}" title="${esc(title)}">${I(ic, "sm")} ${label}</button>`;
    return `<div class="chat-tools">${ai.can_plans !== false ? btn("plan", "youtube", "Video plan", "Find videos that teach these weeks, checked through their transcripts") + btn("prep", "cap", "Prep pack", "A written exam prep pack") : ""}
      ${btn("ready", "list-checks", "Get ready", "What to do before the next quiz or exam")}${btn("sync", "zap", "Sync course", "Transcribe and download what is missing")}
      <span class="dim sm" style="align-self:center">or type / for more</span></div>`;
  };

  // the "/" menu while typing
  let menuIndex = 0;
  function menuItems(v) { const q = v.slice(1).toLowerCase(); return CMDS.filter(([k]) => k.startsWith(q.split(/\s/)[0])); }
  function drawMenu() {
    const ta = $("#chat-input"); const box = ta && ta.closest(".composer-inner"); if (!box) return;
    let m = $(".cmd-menu", box);
    const v = ta.value;
    const items = v.startsWith("/") && !/\s/.test(v) ? menuItems(v) : [];
    if (!items.length) { if (m) m.remove(); return; }
    if (!m) { m = document.createElement("div"); m.className = "cmd-menu"; box.appendChild(m); }
    menuIndex = Math.min(menuIndex, items.length - 1);
    m.innerHTML = items.map(([k, , d], i) => `<button type="button" class="${i === menuIndex ? "on" : ""}" data-act="chat-cmd-pick" data-cmd="${k}"><span class="k">/${k}</span><span class="d">${esc(d)}</span></button>`).join("");
  }
  document.addEventListener("input", (e) => { if (e.target && e.target.id === "chat-input") { menuIndex = 0; drawMenu(); } });
  document.addEventListener("keydown", (e) => {
    const m = $(".cmd-menu"); if (!m || !e.target || e.target.id !== "chat-input") return;
    const items = menuItems(e.target.value);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); menuIndex = (menuIndex + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length; drawMenu(); }
    else if (e.key === "Tab" || (e.key === "Enter" && items.length && !e.target.value.includes(" "))) {
      e.preventDefault(); e.stopImmediatePropagation();
      e.target.value = `/${items[menuIndex][0]} `; S.chat.input = e.target.value; drawMenu();
    } else if (e.key === "Escape") { m.remove(); }
  }, true);

  Object.assign(A, {
    "chat-action": (el) => { const a = found[el.dataset.key]; if (a) run(a); },
    "chat-cmd": (el) => { const a = parse(`/${el.dataset.cmd}`); if (a) run(a); },
    "chat-cmd-pick": (el) => { const ta = $("#chat-input"); if (ta) { ta.value = `/${el.dataset.cmd} `; S.chat.input = ta.value; ta.focus(); drawMenu(); } },
  });
})();
