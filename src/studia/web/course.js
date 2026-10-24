/* IEUStudia — Home dashboard and course pages (Weeks · Heads-up · Overview · Lectures · Materials · Announcements).
 * Plugs into app.js through window.Studia (views, events, actions). */
(function () {
  "use strict";
  const St = window.Studia;
  const { S, call, esc, I, SPIN, plural, toast, go, renderView, renderSidebar, emptyHTML, skeleton, openDay, jumpTo, A } = St;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const day = (iso) => (iso ? new Date(iso.slice(0, 10) + "T00:00:00") : null);
  const dm = (iso) => { const d = day(iso); return d ? `${d.getDate()} ${MON[d.getMonth()]}` : ""; };
  const dmw = (iso) => { const d = day(iso); return d ? `${DAYS[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]}` : ""; };
  const range = (a, b) => (a ? (b && b !== a ? `${dm(a)} – ${dm(b)}` : dm(a)) : "");
  const daysAgo = (iso) => (iso ? (Date.now() - new Date(iso).getTime()) / 864e5 : Infinity);
  const fmtSize = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n ? `${Math.max(1, Math.round(n / 1024))} KB` : "");
  const when = (iso) => { const d = iso ? new Date(iso) : null; return d && !isNaN(d) ? `${DAYS[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]}, ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}` : ""; };
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { /* private mode */ } return null; };

  S.cd = {};                 // course details by id (from course_detail)
  const loadedAt = {};       // when each course's details arrived
  S.mat = {};                // download state by material id: {state, done, total, error, open}
  S.loLang = "en";
  S.ann = new Set();         // expanded announcements
  S.alertCat = "key";        // Heads-up tab filter: "key" (all but "stressed"), a category, or "all"
  S.headsOpen = store("studia-heads") !== "0";
  S.weeksOpen = {};          // past weeks the user expanded, by course: {cid: Set(week numbers)}

  // ------------------------------------------------------------------ data
  const loading = {};
  async function loadCourse(cid, quiet) {
    if (loading[cid]) return loading[cid];
    loading[cid] = (async () => {
      const r = await call("course_detail", cid);
      delete loading[cid];
      if (!r.ok) { if (!quiet) toast(r.error, "error"); return; }
      S.cd[cid] = r.course;
      loadedAt[cid] = Date.now();
      if (R.cid === cid && $(".modal.rd")) drawReady();         // the "get ready" checklist is open: keep it current
      const v = S.view;
      if ((v.name === "course" && v.courseId === cid) || v.name === "home") renderView();
    })();
    return loading[cid];
  }
  const reloadTimers = {};
  function reloadSoon(cid) { clearTimeout(reloadTimers[cid]); reloadTimers[cid] = setTimeout(() => loadCourse(cid, true), 250); }
  function allMaterials(d) { return d ? [...d.general.flatMap((u) => u.materials), ...d.weeks.flatMap((w) => w.units.flatMap((u) => u.materials))] : []; }
  function findMaterial(id) {
    for (const cid in S.cd) { const m = allMaterials(S.cd[cid]).find((x) => x.id === id); if (m) return { cid, m }; }
    return null;
  }

  // ------------------------------------------------------------------ pieces
  const EXT_ICON = { pdf: "file", pptx: "presentation", ppt: "presentation", docx: "file-text", doc: "file-text" };
  function matRowHTML(m) {
    const st = S.mat[m.id] || {};
    const busy = st.state === "queued" || st.state === "downloading";
    let status = "";
    if (busy) status = st.total ? `<span class="bar"><i style="width:${Math.round((100 * st.done) / st.total)}%"></i></span>` : `${SPIN()} ${st.state === "queued" ? "Waiting" : "Downloading"}`;
    else if (st.state === "error") status = `<span class="err-txt" title="${esc(st.error)}">${I("alert-circle", "sm")} Failed</span>`;
    else if (m.downloaded) status = m.outdated ? `<span class="pill processing">Updated on IEULearn</span>` : `<span class="dim">${m.pages ? plural(m.pages, m.ext === "pptx" ? "slide" : "page") : ""} ${fmtSize(m.size)}</span>`;
    else if (m.is_new) status = `<span class="pill new">New</span>`;
    const acts = busy ? "" : m.downloaded
      ? `${m.outdated ? `<button class="btn ghost sm" data-act="mat-get" data-id="${m.id}" title="Download the new version">${I("download", "sm")}</button>` : ""}
         <button class="btn ghost icon sm" data-act="mat-reveal" data-id="${m.id}" title="Show in folder">${I("folder-open", "sm")}</button>`
      : `<button class="btn ghost sm" data-act="mat-get" data-id="${m.id}">${I("download", "sm")} Download</button>`;
    return `<div class="mat ${m.downloaded ? "have" : ""} ${m.gone ? "gone" : ""}" data-mid="${esc(m.id)}">
      <span class="mat-ico ext-${esc(m.ext)}">${I(EXT_ICON[m.ext] || "file", "sm")}</span>
      <button class="mat-name" data-act="mat-open" data-id="${esc(m.id)}" title="${esc(m.file_name || m.title)} — ${m.downloaded ? "open" : "download and open"}">${esc(m.title)}</button>
      <span class="mat-ext">${esc((m.ext || "").toUpperCase())}</span><span class="mat-st">${status}</span><span class="mat-acts">${acts}</span></div>`;
  }
  function updateMatRow(id) {
    const hit = findMaterial(id);
    if (!hit) return;
    $$(`.mat[data-mid="${CSS.escape(id)}"]`).forEach((el) => { el.outerHTML = matRowHTML(hit.m); });
  }
  function lectureChipHTML(dd) {
    const d = day(dd.date);
    const label = `${DAYS[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]} · ${esc(dd.kind_label)}${dd.parts > 1 ? ` · ${dd.parts} parts` : ""}`;
    const state = dd.transcribed ? `<span class="pill done">${I("check")}Transcribed</span>` : dd.any_done ? `<span class="pill">Partly</span>` : dd.is_new ? `<span class="pill new">New</span>` : "";
    return `<div class="lec ${dd.transcribed ? "done" : ""}">
      <span class="lec-ico">${I("video", "sm")}</span><span class="lec-name">${label}</span>${state}
      <span class="lec-acts">${dd.any_done ? `<button class="btn ghost sm" data-act="open-day" data-day="${esc(dd.day_key)}">${I("file-text", "sm")} Read</button>` : ""}
      ${dd.transcribed ? "" : `<button class="btn ghost sm" data-act="tx-day" data-day="${esc(dd.day_key)}">${I("wave", "sm")} Transcribe</button>`}</span></div>`;
  }

  // ------------------------------------------------------------------ heads-up: what the lecturer said
  const CATS = { final: ["Final", "cap"], midterm: ["Midterm", "cap"], quiz: ["Quiz", "target"], exam: ["Exam", "cap"], project: ["Project", "layers"],
    homework: ["Homework", "file-text"], schedule: ["Schedule", "calendar"], important: ["Stressed", "alert"] };
  const KEY_ORDER = ["quiz", "midterm", "final", "exam", "project", "homework", "schedule"];
  const isKey = (a) => a.category !== "important";
  const RECENT_DAYS = 14;
  function markHL(text, hl) {
    if (!hl || !(hl[1] > hl[0])) return esc(text);
    return esc(text.slice(0, hl[0])) + `<mark>${esc(text.slice(hl[0], hl[1]))}</mark>` + esc(text.slice(hl[1]));
  }
  function catChip(k, n, act) {
    const [label, icon] = CATS[k] || [k, "alert"];
    const attrs = act ? `data-act="alert-cat" data-cat="${k}"` : "";
    return `<${act ? "button" : "span"} class="cat-chip cat-${k}" ${attrs}>${I(icon, "xs")}${esc(label)}${n != null ? ` <b>${n}</b>` : ""}</${act ? "button" : "span"}>`;
  }
  function alertHTML(a, cid, course) {
    const others = a.categories.filter((k) => k !== a.category && k !== "important").slice(0, 3);
    const lecture = a.source === "lecture";
    const src = lecture
      ? `${I("video", "xs")} Lecture · ${esc(dmw(a.date))}${a.part ? ` · part ${a.part}` : ""} · ${esc(a.ts)}${a.other_section ? ` <span class="pill other-sec" title="Heard in another section's lecture: dates and details may differ for your section">${esc(a.other_section)}</span>` : ""}`
      : `${I("megaphone", "xs")} Announcement · ${esc(dmw(a.date))}${a.is_new ? ` <span class="pill new">New</span>` : ""}`;
    const target = lecture ? `data-day="${esc(a.day_key)}" data-part="${a.part}" data-t="${a.t}"` : `data-cid="${esc(cid)}" data-ann="${esc(a.announcement_id)}"`;
    return `<button class="alert-item" data-act="alert-open" ${target} title="${lecture ? "Read it in the transcript" : "Open the announcement"}">
      ${catChip(a.category)}
      <span class="alert-main">
        <span class="alert-text">${course ? `<b class="alert-course">${esc(course.code)}</b>` : ""}${lecture ? "" : `<b>${esc(a.title)}</b> — `}${lecture ? "“" : ""}${markHL(a.text, a.hl)}${lecture ? "”" : ""}${a.lang === "tr" ? ` <span class="tag-tr">TR</span>` : ""}</span>
        <span class="alert-meta">${src}${others.map((k) => `<span class="cat-mini cat-${k}">${esc(CATS[k][0])}</span>`).join("")}</span>
      </span>
      <span class="alert-go">${I("chevron-right", "sm")}</span></button>`;
  }
  // the assessment plan the lecturer announced: which week, which learning outcomes
  const pad2 = (n) => String(n).padStart(2, "0");
  const localToday = () => { const t = new Date(); return `${t.getFullYear()}-${pad2(t.getMonth() + 1)}-${pad2(t.getDate())}`; };
  const monday = (iso) => { const d = day(iso); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d; };
  // A quiz announced for a week is held in one of the course's classes that week (the backend lists them from the
  // lecture days): it is done once they are all over. Midterms and finals follow the exam schedule, so for them only
  // the week counts. Before that, how far away it is counts calendar weeks.
  function weekRel(d, no, kind = "quiz") {
    const w = no && d.weeks.find((x) => x.no === no);
    if (!w || !w.start) return { w, rel: "", past: false };
    const now = new Date(), today = localToday(), hm = `${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
    const classes = kind === "quiz" ? w.classes || [] : [], left = classes.filter((c) => c.date > today || (c.date === today && c.end > hm));
    if (w.past || (classes.length && !left.length)) return { w, rel: "done", past: true };
    const next = left.length ? left[0].date : w.start > today ? w.start : today;
    const n = Math.round((monday(next) - monday(today)) / 6048e5);
    const rel = next === today && left.length ? "today" : n <= 0 ? "this week" : n === 1 ? "next week" : `in ${n} weeks`;
    return { w, rel, past: false, now: n <= 0, days: left.map((c) => c.date) };
  }
  // when it is: the class day(s) still ahead if the course's schedule is known, else the week's dates
  const relWhen = (r) => (r.days && r.days.length && r.days.length <= 2 ? r.days.map(dmw).join(" or ") : range(r.w.start, r.w.end));
  function loLabel(los) {
    const runs = [];
    for (const n of los) { const r = runs[runs.length - 1]; if (r && n === r[1] + 1) r[1] = n; else runs.push([n, n]); }
    return runs.map(([a, b]) => (a === b ? `LO${a}` : `LO${a}–LO${b}`)).join(", ");
  }
  function loTitle(d, los) {
    return los.map((n) => [`LO${n}: ${d.outcomes_en[n - 1] || "?"}`, d.outcomes_tr[n - 1] && d.outcomes_tr[n - 1] !== d.outcomes_en[n - 1] ? `   IEULearn LO${n}: ${d.outcomes_tr[n - 1]}` : ""].filter(Boolean).join("\n")).join("\n");
  }
  function upcoming(d) {
    return ((d && d.alerts && d.alerts.plan) || []).filter((e) => !weekRel(d, e.week, e.kind).past);
  }
  function planPill(c, d, e) {
    const r = weekRel(d, e.week, e.kind);
    return `<button class="plan-pill ${r.now ? "now" : ""} kind-${e.kind}" data-act="ready-open" data-cid="${esc(c.id)}" data-name="${esc(e.name)}" title="${esc(`Get ready: what to transcribe, download and read${e.los.length ? "\n" + loTitle(d, e.los) : ""}`)}">
      <b>${esc(e.name)}</b>${e.week ? `<span>week ${e.week}${r.rel ? ` · ${r.rel}` : ""}</span>` : ""}${e.los.length ? `<span class="lo">${esc(loLabel(e.los))}</span>` : ""}</button>`;
  }
  function planHTML(c, d) {
    const plan = (d.alerts && d.alerts.plan) || [];
    if (!plan.length) return "";
    return `<div class="card plan"><div class="mg-head"><b>${I("cap", "sm")} Assessment plan</b><span class="dim">as announced on IEULearn — hover an outcome for its text</span></div>
      ${plan.map((e) => {
        const r = weekRel(d, e.week, e.kind);
        return `<div class="plan-row ${r.past ? "past" : ""} ${r.now ? "now" : ""}">
          <span class="plan-name kind-${e.kind}">${esc(e.name)}</span>
          <span class="plan-when">${e.week ? `Week ${e.week}${r.w && r.w.start ? ` · ${esc(range(r.w.start, r.w.end))}` : ""}` : `<span class="dim">week not announced</span>`}${r.rel ? ` <span class="plan-rel">${esc(r.rel)}</span>` : ""}</span>
          <span class="plan-los">${e.los.length ? e.los.map((n) => `<span class="lo-chip" title="${esc(loTitle(d, [n]))}">LO${n}</span>`).join("") : `<span class="dim">no outcomes listed</span>`}</span>
          <span class="plan-acts">${e.week && !r.past ? `<button class="btn sm" data-act="ready-open" data-cid="${esc(c.id)}" data-name="${esc(e.name)}">${I("list-checks", "sm")} Get ready</button>` : ""}
          <button class="btn ghost sm plan-src" data-act="plan-ann" data-cid="${esc(c.id)}" data-ann="${esc(e.sources[0].id)}" title="${esc(e.sources.map((s) => s.title).join("\n"))}">${I("megaphone", "xs")}<span>${esc(e.sources[0].title)}</span></button></span></div>`;
      }).join("")}</div>`;
  }
  function headsUpHTML(c, d) {
    const al = d && d.alerts;
    const key = al ? al.items.filter(isKey) : [];
    const next = upcoming(d).slice(0, 3);
    if (!key.length && !next.length) return "";
    const chips = KEY_ORDER.filter((k) => al.counts[k]).map((k) => catChip(k, al.counts[k], true)).join("");
    return `<div class="card heads ${S.headsOpen ? "" : "closed"}">
      <div class="heads-head"><span class="heads-ico">${I("alert", "sm")}</span><b>Heads-up from the lecturer</b><span class="heads-chips">${chips}</span><span class="spacer"></span>
        <button class="btn ghost sm" data-act="course-tab" data-tab="alerts">See all ${I("chevron-right", "sm")}</button>
        <button class="btn ghost icon sm" data-act="heads-toggle" title="${S.headsOpen ? "Collapse" : "Expand"}">${I(S.headsOpen ? "chevron-up" : "chevron-down", "sm")}</button></div>
      ${next.length ? `<div class="plan-strip"><span class="plan-strip-label">${I("cap", "xs")} Coming up</span>${next.map((e) => planPill(c, d, e)).join("")}</div>` : ""}
      ${S.headsOpen && key.length ? `<div class="heads-list">${key.slice(0, 3).map((a) => alertHTML(a, c.id)).join("")}</div>` : ""}</div>`;
  }
  function alertsHTML(c, d) {
    const al = d.alerts || { items: [], counts: {}, total: 0 };
    if (!al.items.length && !(al.plan || []).length) {
      return emptyHTML("alert", "Nothing flagged yet", c.counts.done
        ? "When a transcribed lecture or an announcement mentions a quiz, the midterm, the final, a project or homework, it shows up here."
        : "Transcribe lectures: whenever the lecturer mentions a quiz, the midterm, the final, a project or homework, it shows up here (announcements too).");
    }
    const f = S.alertCat;
    const keyN = al.items.filter(isKey).length;
    const chips = [["key", "Assessments & deadlines", keyN], ...KEY_ORDER.filter((k) => al.counts[k]).map((k) => [k, CATS[k][0], al.counts[k]]),
      ...(al.counts.important ? [["important", "Stressed in class", al.counts.important]] : []), ["all", "All", al.items.length]];
    const list = al.items.filter((a) => (f === "all" ? true : f === "key" ? isKey(a) : a.category === f));
    const groups = [];
    for (const a of list) {
      const dd = (a.date || "").slice(0, 10);
      const w = d.weeks.find((x) => x.start && dd >= x.start && dd <= (x.end || x.start));
      const label = w ? `Week ${w.no} · ${range(w.start, w.end)}` : "Other";
      let g = groups[groups.length - 1];
      if (!g || g.label !== label) groups.push((g = { label, items: [] }));
      g.items.push(a);
    }
    return `${planHTML(c, d)}<div class="fchips">${chips.map(([k, l, n]) => `<button class="fchip ${f === k ? "on" : ""} ${CATS[k] ? `cat-${k}` : ""}" data-act="alert-cat" data-cat="${k}">${esc(l)} <span>${n}</span></button>`).join("")}</div>
      <div class="note-sm">${I("info", "sm")}<span>Found automatically in your transcribed lectures and the IEULearn announcements — click one to read it in context. Lectures you haven’t transcribed aren’t included.</span></div>
      ${list.length ? groups.map((g) => `<div class="card alert-group"><div class="mg-head"><b>${esc(g.label)}</b><span class="dim">${plural(g.items.length, "item")}</span></div>${g.items.map((a) => alertHTML(a, c.id)).join("")}</div>`).join("")
        : `<div class="dim" style="padding:16px 4px">Nothing in this category.</div>`}`;
  }

  // ------------------------------------------------------------------ sections (some lecturers publish every section's lectures)
  const manySections = (c) => (c.available_sections || []).length > 1;
  const mineOnly = (c) => manySections(c) && c.show_mode === "mine" && !!c.my_section;
  const sectionOf = (c) => c.my_section || (manySections(c) ? "" : c.section);
  function sectionChip(c) {
    if (manySections(c)) {
      return `<button class="chip-btn ${c.my_section ? "" : "ask"}" data-act="section-menu" data-id="${esc(c.id)}" title="Lectures of several sections are published for this course — choose yours">
        ${I("users", "sm")}${c.my_section ? `Section ${esc(c.my_section)}` : "Your section?"}${I("chevron-down", "xs")}</button>`;
    }
    return sectionOf(c) ? `<span>${I("users", "sm")}Section ${esc(sectionOf(c))}</span>` : "";
  }
  function sectionAsk(c) {
    if (!manySections(c) || c.my_section) return "";
    return `<div class="card section-ask">${I("users")}<div class="grow"><b>Which section are you in?</b>
      <div class="dim">This course publishes the lectures of sections ${c.available_sections.join(", ")}. IEUStudia shows and suggests your section's lectures first; the others stay one click away.</div></div>
      <div class="sa-btns">${c.available_sections.map((x) => `<button class="btn sm" data-act="set-section" data-id="${esc(c.id)}" data-sec="${esc(x)}">Section ${esc(x)}</button>`).join("")}</div></div>`;
  }
  async function setSection(cid, patch) {
    const r = await call("set_course_prefs", cid, patch);
    if (!r.ok) { toast(r.error, "error"); return; }
    St.setCourses(r.courses); renderSidebar(); renderView();
    reloadSoon(cid);                                   // the weeks' lectures depend on it
  }

  // ------------------------------------------------------------------ course page
  function courseHero(c, d) {
    const cur = d && d.weeks.find((w) => w.current);
    const ects = d && d.syllabus.ects ? `<span>${I("cap", "sm")}${esc(d.syllabus.ects)} ECTS</span>` : "";
    const ai = S.system.ai || {};
    return `<div class="card hero course-hero" style="--h:${hue(c)}"><div class="badge">${esc(c.code).replace(" ", "<br>")}</div><div class="grow">
      <h1>${esc(c.display_name)}</h1>
      <div class="meta">${c.instructor ? `<span>${I("user", "sm")}${esc(c.instructor)}</span>` : ""}${c.term ? `<span>${I("calendar", "sm")}${esc(c.term)}</span>` : ""}
        ${sectionChip(c)}${ects}</div>
      <div class="stats">${cur ? `<button class="stat now" data-act="goto-week" data-no="${cur.no}" title="Show this week">${I("sparkle", "xs")}<b>Week ${cur.no}</b>${cur.exam ? "exam week" : "this week"}</button>` : ""}
        <div class="stat"><b>${d ? d.materials.total : "…"}</b>materials${d && d.materials.downloaded ? ` · ${d.materials.downloaded} saved` : ""}</div>
        <div class="stat"><b>${c.counts.done}</b>of ${c.counts.total} lectures transcribed</div>
        ${d && d.outcomes_en.length ? `<div class="stat"><b>${d.outcomes_en.length}</b>learning outcomes</div>` : ""}</div></div>
      <div class="hero-acts"><button class="btn grad sm" data-act="ask-course" data-id="${c.id}" ${ai.available && (c.counts.done_any ?? c.counts.done) ? "" : "disabled"} title="${ai.available ? ((c.counts.done_any ?? c.counts.done) ? `Chat with ${St.aiName()} about this course` : "Transcribe a lecture first") : St.aiHint()}">${I("chat", "sm")} Ask ${esc(St.aiName())}</button>
        <button class="btn sm" data-act="course-sync" data-id="${c.id}" title="Sync, then transcribe this course's lectures that aren't transcribed yet (your section) and download its materials">${I("zap", "sm")} Sync course</button>
        <button class="btn sm" data-act="plan-new" data-id="${c.id}" ${ai.available && ai.can_plans !== false ? "" : "disabled"} title="${ai.available ? "Find YouTube videos that teach this course's weeks, checked through their captions" : St.aiHint()}">${I("youtube", "sm")} Video plan</button>
        <button class="btn sm" data-act="open-folder" data-id="${c.id}" title="Open this course's folder">${I("folder-open", "sm")} Folder</button></div></div>`;
  }
  function tabsHTML(c, d, tab) {
    const cn = c.content || {};
    const heads = d && d.alerts ? d.alerts.items.filter((a) => isKey(a) && daysAgo(a.date) <= RECENT_DAYS).length : 0;
    const tabs = [["weeks", "Weeks", "calendar", 0], ["alerts", "Heads-up", "alert", heads], ["overview", "Overview", "target", 0], ["lectures", "Lectures", "video", c.counts.new],
      ["materials", "Materials", "layers", cn.materials_new], ["announcements", "Announcements", "megaphone", cn.announcements_new]];
    return `<div class="tabs" role="tablist">${tabs.map(([k, l, ic, n]) => `<button role="tab" class="tab ${tab === k ? "on" : ""}" data-act="course-tab" data-tab="${k}">${I(ic, "sm")}${l}${n ? `<span class="count ${k === "alerts" ? "warn" : "new"}" ${k === "alerts" ? `title="In the last ${RECENT_DAYS} days"` : ""}>${n}</span>` : ""}</button>`).join("")}</div>`;
  }
  function coursePage(v) {
    const c = St.findCourse(v.courseId);
    if (!c) return `<div class="view-inner">${emptyHTML("book", "Course not found", "")}</div>`;
    const d = S.cd[c.id];
    const tab = v.tab || "weeks";
    let body;
    if (tab === "lectures") body = St.courseHTML(c, { bare: true });
    else if (!d) body = skeleton();
    else if (tab === "alerts") body = alertsHTML(c, d);
    else if (tab === "overview") body = overviewHTML(c, d);
    else if (tab === "materials") body = materialsHTML(c, d);
    else if (tab === "announcements") body = announcementsHTML(c, d);
    else body = weeksHTML(c, d);
    const gone = c.inactive ? `<div class="callout info">${I("info")}<div>This course is no longer on your IEULearn dashboard (dropped, or from an earlier term). Its transcripts, files, chats and plans stay here.</div></div>` : "";
    return `<div class="view-inner">${courseHero(c, d)}${gone}${sectionAsk(c)}${tab === "alerts" ? "" : headsUpHTML(c, d)}${tabsHTML(c, d, tab)}<div class="tab-body">${body}</div></div>`;
  }

  function weeksHTML(c, d) {
    if (!d.weeks.length && !d.general.length) {
      return emptyHTML("calendar", d.content_at ? "No weekly plan on IEULearn" : "Loading the weekly plan…",
        d.content_at ? "This course has no unit plan on IEULearn yet." : "It appears after the next sync.");
    }
    let h = "";
    if (d.general.length) {
      h += `<div class="card week general"><div class="week-head"><div class="week-no gen">${I("layers")}</div><div class="week-title"><div class="t">Course-wide</div>
        <div class="s">${esc(d.general.map((u) => u.title).join(" · "))}</div></div></div>
        <div class="week-body one">${d.general.flatMap((u) => u.materials).map(matRowHTML).join("") || '<div class="dim">No files</div>'}</div></div>`;
    }
    for (const w of d.weeks) h += weekCard(c, d, w);
    return h;
  }
  function weekCard(c, d, w) {
    const mats = w.units.flatMap((u) => u.materials);
    w = { ...w, days: mineOnly(c) ? w.days.filter((x) => x.mine !== false) : w.days };
    const unitTitle = w.units.map((u) => u.title).join(" · ");
    const title = w.topic_en || unitTitle || w.topic_tr || `Week ${w.no}`;
    const sub = [range(w.start, w.end), unitTitle && unitTitle !== title ? `IEULearn: ${unitTitle}` : (w.topic_tr && w.topic_tr !== title ? w.topic_tr : "")].filter(Boolean).join(" · ");
    const due = ((d.alerts && d.alerts.plan) || []).filter((e) => e.week === w.no);
    const duePills = due.map((e) => `<span class="pill due kind-${e.kind}" title="${esc(e.los.length ? loTitle(d, e.los) : `From “${e.sources[0].title}”`)}">${I("cap")}${esc(e.name)}${e.los.length ? ` · ${esc(loLabel(e.los))}` : ""}</span>`).join("");
    const quiet = !w.current && !mats.length && !w.days.length && !due.length;
    const foldable = w.past && !w.current && !quiet;
    const shut = foldable && !(S.weeksOpen[c.id] && S.weeksOpen[c.id].has(w.no));
    const saved = mats.filter((m) => m.downloaded).length, tx = w.days.filter((x) => x.transcribed).length;
    const summary = [mats.length ? `${plural(mats.length, "file")}${saved ? ` (${saved} saved)` : ""}` : "", w.days.length ? `${plural(w.days.length, "lecture")}${tx ? ` (${tx} transcribed)` : ""}` : ""].filter(Boolean).join(" · ");
    return `<div class="card week ${w.current ? "current" : ""} ${w.past ? "past" : ""} ${w.exam ? "exam" : ""} ${quiet ? "quiet" : ""} ${shut ? "shut" : ""}" id="week-${w.no}">
      <div class="week-head" ${foldable ? `data-act="week-toggle" data-no="${w.no}" title="${shut ? "Show" : "Hide"} this week's files and lectures"` : ""}>
        <div class="week-no"><b>${w.no}</b><span>week</span></div>
        <div class="week-title"><div class="t">${esc(title)} ${w.current ? `<span class="pill now">${I("sparkle")}This week</span>` : ""} ${duePills || (w.exam ? `<span class="pill processing">${I("cap")}Exam</span>` : "")}</div>
          <div class="s">${esc(sub)}${shut && summary ? ` · <span class="week-sum">${esc(summary)}</span>` : ""}</div></div>
        ${foldable ? `<span class="week-fold">${I(shut ? "chevron-down" : "chevron-up", "sm")}</span>` : ""}
      </div>
      ${w.readings && !quiet && !shut ? `<div class="week-read">${I("book", "sm")}<span>${esc(w.readings)}</span></div>` : ""}
      ${quiet || shut ? "" : `<div class="week-body">
        <div class="week-col"><div class="col-title">${I("layers", "sm")} Materials</div>${mats.map(matRowHTML).join("") || '<div class="dim none">No files for this week</div>'}</div>
        <div class="week-col"><div class="col-title">${I("video", "sm")} Lectures</div>${w.days.map(lectureChipHTML).join("") || `<div class="dim none">${w.past || w.current ? "No recordings" : "Not yet"}</div>`}</div>
      </div>`}</div>`;
  }

  function overviewHTML(c, d) {
    const los = S.loLang === "tr" ? d.outcomes_tr : d.outcomes_en;
    const total = d.grading.reduce((n, g) => n + (+g.weight || 0), 0) || 100;
    const info = [["Lecturers", d.syllabus.lecturers], ["Coordinator", d.syllabus.coordinator], ["Assistants", d.syllabus.assistants],
      ["Prerequisites", d.syllabus.prerequisites], ["Language", d.syllabus.language], ["Semester", d.syllabus.semester],
      ["ECTS", d.syllabus.ects], ["Textbook", d.syllabus.textbooks], ["Suggested readings", d.syllabus.readings]].filter(([, v]) => v && v.trim());
    const colors = [1, 2, 3, 4, 5, 6].map((n) => `var(--gc${n})`);   // grading parts: tones from styles.css
    return `<div class="ov-grid">
      <div class="card ov-card">
        <h3>${I("target")} Learning outcomes <span class="count">${los.length}</span>
          ${d.outcomes_tr.length && d.outcomes_en.length ? `<span class="spacer"></span><div class="seg sm"><button class="${S.loLang === "en" ? "on" : ""}" data-act="lo-lang" data-val="en">English</button><button class="${S.loLang === "tr" ? "on" : ""}" data-act="lo-lang" data-val="tr">Türkçe</button></div>` : ""}</h3>
        ${los.length ? `<ol class="lo-list">${los.map((t, i) => `<li><span class="lo-no">LO${i + 1}</span><span>${esc(t)}</span></li>`).join("")}</ol>
          ${d.outcomes_tr.length && d.outcomes_en.length ? `<div class="note-sm">${I("info", "sm")} The English syllabus and IEULearn number some outcomes differently; when a lecturer says “LO5”, check which list they mean.</div>` : ""}`
        : `<div class="dim">No learning outcomes found for this course.</div>`}
      </div>
      <div class="ov-side">
        ${d.grading.length ? `<div class="card ov-card"><h3>${I("cap")} Grading</h3>
          <div class="grade-bar">${d.grading.map((g, i) => `<i style="width:${(100 * g.weight) / total}%;background:${colors[i % colors.length]}" title="${esc(g.activity)} ${g.weight}%"></i>`).join("")}</div>
          <div class="grade-list">${d.grading.map((g, i) => `<div><span class="dot" style="background:${colors[i % colors.length]}"></span><span class="g-name">${esc(g.activity)}${g.number > 1 ? ` <span class="dim">× ${g.number}</span>` : ""}</span><b>${g.weight}%</b></div>`).join("")}</div></div>` : ""}
        ${d.description || d.objectives ? `<div class="card ov-card"><h3>${I("book")} About</h3>${d.objectives ? `<p>${esc(d.objectives)}</p>` : ""}${d.description && d.description !== d.objectives ? `<p class="dim">${esc(d.description)}</p>` : ""}</div>` : ""}
        ${info.length ? `<div class="card ov-card"><h3>${I("info")} Course details</h3><dl class="info-list">${info.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
          ${d.syllabus.url ? `<button class="btn ghost sm" data-act="url" data-url="${esc(d.syllabus.url)}">${I("external", "sm")} Full syllabus</button>` : ""}</div>` : ""}
      </div></div>`;
  }

  function materialsHTML(c, d) {
    const groups = [...(d.general.length ? [{ label: "Course-wide", mats: d.general.flatMap((u) => u.materials) }] : []),
      ...d.weeks.filter((w) => w.units.some((u) => u.materials.length)).map((w) => ({ label: `Week ${w.no} · ${w.topic_en || w.units.map((u) => u.title).join(" · ")}`, sub: range(w.start, w.end), mats: w.units.flatMap((u) => u.materials) }))];
    const all = groups.flatMap((g) => g.mats);
    const missing = all.filter((m) => !m.downloaded && !(S.mat[m.id] && ["queued", "downloading"].includes(S.mat[m.id].state)));
    let h = `<div class="toolbar"><span class="dim">${plural(all.length, "file")} on IEULearn · ${all.filter((m) => m.downloaded).length} on this PC</span><span class="spacer"></span>
      ${d.materials.new ? `<button class="btn ghost sm" data-act="course-seen" data-id="${c.id}" data-what="materials">Mark as seen</button>` : ""}
      <button class="btn sm" data-act="mat-all" data-id="${c.id}" ${missing.length ? "" : "disabled"}>${I("download", "sm")} Download ${missing.length ? `all ${missing.length}` : "all"}</button></div>`;
    if (!all.length) return h + emptyHTML("layers", "No materials yet", "Slides and documents your lecturer posts on IEULearn appear here.");
    for (const g of groups) h += `<div class="card mat-group"><div class="mg-head"><b>${esc(g.label)}</b>${g.sub ? `<span class="dim">${esc(g.sub)}</span>` : ""}</div>${g.mats.map(matRowHTML).join("")}</div>`;
    return h;
  }

  function announcementsHTML(c, d) {
    if (!d.announcements.length) return emptyHTML("megaphone", "No announcements", "Announcements your lecturer posts on IEULearn appear here.");
    const anyNew = d.announcements.some((a) => a.is_new);
    return `<div class="toolbar"><span class="spacer"></span>${anyNew ? `<button class="btn ghost sm" data-act="course-seen" data-id="${c.id}" data-what="announcements">Mark as seen</button>` : ""}</div>` +
      d.announcements.map((a) => annCard(a)).join("");
  }
  function annCard(a, course) {
    const open = S.ann.has(a.id) || a.body.length < 420;
    const flash = S.flashAnn && S.flashAnn.id === a.id && Date.now() < S.flashAnn.until;
    return `<div class="card ann ${a.is_new ? "is-new" : ""} ${flash ? "flash" : ""}" data-ann-id="${esc(a.id)}">
      <div class="ann-head">${I("megaphone")}<div class="grow"><div class="t">${esc(a.title)} ${a.is_new ? `<span class="pill new">New</span>` : ""}</div>
        <div class="s">${course ? `<b>${esc(course.code)}</b> · ` : ""}${esc(a.author)}${a.posted_at ? ` · ${esc(when(a.posted_at))}` : ""}</div></div></div>
      <div class="ann-body ${open ? "" : "clamp"}">${esc(a.body)}</div>
      ${open ? "" : `<button class="btn ghost sm" data-act="ann-more" data-id="${esc(a.id)}">Show all</button>`}</div>`;
  }

  // ------------------------------------------------------------------ home
  // a course's colour (Home cover, course page stripe and badge): clear, well-separated hues (no muddy yellows),
  // handed out in course-id order so this term's courses all differ
  const HUES = [217, 330, 158, 262, 20, 188, 290, 142];
  function hue(c) {
    const ids = S.courses.map((x) => x.id).sort();
    return HUES[Math.max(0, ids.indexOf(c.id)) % HUES.length];
  }
  function courseCard(c) {
    const d = S.cd[c.id];
    const w = d && (d.weeks.find((x) => x.current) || d.weeks.find((x) => x.start && !x.past));
    const cn = c.content || {};
    const fresh = (c.counts.new || 0) + (cn.materials_new || 0) + (cn.announcements_new || 0);
    const heads = d && d.alerts ? d.alerts.items.filter((a) => isKey(a) && daysAgo(a.date) <= RECENT_DAYS).length : 0;
    const exam = d && d.weeks.find((x) => x.exam && !x.past);
    const next = upcoming(d)[0];
    const done = c.counts.total ? Math.round((100 * c.counts.done) / c.counts.total) : 0;
    const topic = w ? (w.topic_en || w.units.map((u) => u.title).join(" · ") || w.topic_tr || "") : "";
    return `<button class="cc" data-act="go" data-view="course" data-id="${esc(c.id)}" title="Open ${esc(c.code)}">
      <span class="cc-cover" style="--h:${hue(c)}${c.cover ? `;background-image:url('${esc(c.cover)}')` : ""}">
        <span class="cc-scrim"></span><span class="cc-code">${esc(c.code)}${sectionOf(c) ? ` · ${esc(sectionOf(c))}` : ""}</span>
        ${fresh ? `<span class="cc-new">${fresh} new</span>` : ""}
        <span class="cc-name">${esc(c.display_name)}</span></span>
      <span class="cc-body">
        <span class="cc-info">${c.instructor ? `<span title="Instructor">${I("user", "xs")}${esc(c.instructor)}</span>` : ""}${c.term ? `<span title="Term">${I("calendar", "xs")}${esc(c.term)}</span>` : ""}</span>
        <span class="cc-week">${w ? `<b>${w.current ? "This week" : "Next"} · Week ${w.no}</b><span>${esc(topic)}</span>` : `<span class="dim">${d ? "No weekly plan yet" : "Loading…"}</span>`}</span>
        <span class="cc-bars">
          ${c.has_panopto && c.counts.total ? `<span class="cc-bar" title="Lectures transcribed in IEUStudia"><span>${I("wave", "xs")}${c.counts.done}/${c.counts.total} lectures transcribed</span><i><b style="width:${done}%"></b></i></span>` : ""}
          ${c.lms_progress != null ? `<span class="cc-bar lms" title="Your progress on IEULearn (İlerleme)"><span>${I("zap", "xs")}IEULearn progress ${c.lms_progress}%</span><i><b style="width:${Math.min(100, c.lms_progress)}%"></b></i></span>` : ""}
        </span>
        <span class="cc-foot">${heads ? `<span class="cc-heads">${I("alert", "xs")}${heads} heads-up</span>` : ""}
          ${next ? `<span class="exam-chip" title="${esc(next.los.length ? loTitle(d, next.los) : next.sources[0].title)}">${I("cap", "xs")}${esc(next.name)}${next.week ? ` · ${esc(weekRel(d, next.week, next.kind).rel || `week ${next.week}`)}` : ""}${next.los.length ? ` · ${esc(loLabel(next.los))}` : ""}</span>`
            : exam ? `<span class="exam-chip">${I("cap", "xs")}${esc(exam.exam_label || exam.topic_en || exam.topic_tr || "Exam")} · week ${exam.no}</span>` : ""}
          <span class="spacer"></span><span class="cc-go">${I("arrow-right", "sm")}</span></span>
      </span></button>`;
  }
  function homeView() {
    const now = new Date(), hr = now.getHours();
    const first = ((S.profile || {}).name || "").split(" ")[0];
    const greet = hr < 5 ? "Good night" : hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
    const courses = S.courses.filter((c) => c.code);
    const heads = courses.flatMap((c) => (((S.cd[c.id] || {}).alerts || {}).items || []).filter((a) => isKey(a) && daysAgo(a.date) <= RECENT_DAYS).map((a) => ({ a, c })))
      .sort((x, y) => (y.a.date || "").localeCompare(x.a.date || ""));
    const newMats = courses.flatMap((c) => allMaterials(S.cd[c.id]).filter((m) => m.is_new).map((m) => ({ c, m })));
    const anns = courses.flatMap((c) => ((S.cd[c.id] || {}).announcements || []).map((a) => ({ a, c })))
      .sort((x, y) => (y.a.posted_at || "").localeCompare(x.a.posted_at || ""));
    const newAnns = anns.filter((x) => x.a.is_new);
    const latest = (newAnns.length ? newAnns : anns).slice(0, 3);
    const recs = St.newHTML({ embedded: true });
    const soon = courses.flatMap((c) => {
      const d = S.cd[c.id];
      return d ? upcoming(d).map((e) => ({ c, d, e, r: weekRel(d, e.week, e.kind) })).filter((x) => x.r.now || x.r.rel === "next week") : [];
    }).sort((x, y) => (y.r.now ? 1 : 0) - (x.r.now ? 1 : 0));
    let h = `<div class="view-inner home">
      <div class="home-hero"><div><h1>${greet}${first ? `, ${esc(first)}` : ""}</h1><div class="dim">${DAYS[now.getDay()]}, ${now.getDate()} ${MON[now.getMonth()]} ${now.getFullYear()}${S.sync.running ? ` · ${SPIN()} ${esc(S.sync.message || "Syncing…")}` : ""}</div>
        <div class="home-acts"><button class="btn sm" data-act="go" data-view="digest">${I("calendar", "sm")} Weekly digest</button>
          <button class="btn sm" data-act="plan-new">${I("youtube", "sm")} New study plan</button>
          <button class="btn sm" data-act="full-sync" title="Sync every course, then transcribe your untranscribed lectures and download the materials (asks first)">${I("zap", "sm")} Full sync</button>
          <button class="btn sm" data-act="export-calendar" title="Save the announced quizzes and exams as a calendar file (.ics) for Google Calendar, Outlook or your phone">${I("calendar", "sm")} Add to calendar</button></div></div>
        <img class="home-logo" src="../assets/logo.svg" alt=""></div>`;
    if (soon.length) {
      h += `<div class="section-title">${I("cap")} Coming up <span class="dim sm">this week and next, as announced</span></div>
        <div class="soon">${soon.map(({ c, d, e, r }) => `<button class="soon-card ${r.now ? "now" : ""}" data-act="ready-open" data-cid="${esc(c.id)}" data-name="${esc(e.name)}" title="${esc(`Get ready: what to transcribe, download and read${e.los.length ? "\n" + loTitle(d, e.los) : ""}`)}">
          <span class="soon-code">${esc(c.code)}</span><b>${esc(e.name)}</b><span class="soon-when">${esc(r.rel)}${r.w && r.rel !== "today" ? ` · ${esc(relWhen(r))}` : ""}</span>
          ${e.los.length ? `<span class="lo">${esc(loLabel(e.los))}</span>` : ""}</button>`).join("")}</div>`;
    }
    h += `<div class="section-title">${I("book")} My courses <span class="dim sm">${courses.length}</span></div>
      <div class="cc-grid">${courses.map(courseCard).join("") || skeleton()}</div>`;
    if (heads.length) {
      h += `<div class="section-title">${I("alert")} Heads-up <span class="dim sm">from your lecturers · last ${RECENT_DAYS} days</span></div>
        <div class="card heads home-heads"><div class="heads-list">${heads.slice(0, 5).map(({ a, c }) => alertHTML(a, c.id, c)).join("")}</div>
        ${heads.length > 5 ? `<div class="heads-more dim">${heads.length - 5} more on the course pages</div>` : ""}</div>`;
    }
    if (recs) h += `<div class="section-title">${I("sparkle")} New recordings</div>${recs}`;
    if (newMats.length) {
      h += `<div class="section-title">${I("layers")} New materials <span class="spacer"></span><button class="btn ghost sm" data-act="course-seen" data-id="" data-what="materials">Mark as seen</button></div>
        <div class="card mat-group">${newMats.map(({ c, m }) => `<div class="mg-course"><span class="pill new">${esc(c.code)}</span></div>${matRowHTML(m)}`).join("")}</div>`;
    }
    if (latest.length) {
      h += `<div class="section-title">${I("megaphone")} ${newAnns.length ? "New announcements" : "Latest announcements"}
        ${newAnns.length ? `<span class="spacer"></span><button class="btn ghost sm" data-act="course-seen" data-id="" data-what="announcements">Mark as seen</button>` : ""}</div>` +
        latest.map(({ a, c }) => annCard(a, c)).join("");
    }
    return h + `</div>`;
  }

  // ------------------------------------------------------------------ get ready for a quiz or exam
  // A checklist built from what IEUStudia already knows, without AI: the weeks the assessment covers, your
  // section's lectures and the files of those weeks, what the lecturer said about it and stressed in class.
  const R = { cid: null, name: null };
  const readDays = new Set((() => { try { return JSON.parse(store("studia-read") || "[]"); } catch (e) { return []; } })());
  function setRead(dayKey, on) {
    if (on) readDays.add(dayKey); else readDays.delete(dayKey);
    store("studia-read", JSON.stringify([...readDays].slice(-3000)));
  }
  St.after.day = (v) => { if (S.day && v.dayKey && !readDays.has(v.dayKey)) setRead(v.dayKey, true); };   // opened = read

  function coveredWeeks(d, e) {     // as the study-plan dialog: a quiz covers the weeks since the previous assessment
    const plan = ((d.alerts || {}).plan || []).filter((x) => x.week).sort((a, b) => a.week - b.week);
    const i = plan.findIndex((x) => x.name === e.name);
    const midBefore = plan.slice(0, Math.max(0, i)).filter((x) => x.kind === "midterm").pop();   // Midterm 2: since Midterm 1
    const start = e.kind === "final" ? 1 : e.kind === "midterm" ? (e.no >= 2 && midBefore ? midBefore.week + 1 : 1)
      : (i > 0 ? plan[i - 1].week : 0) + 1;
    const out = [];
    for (let n = Math.min(start, e.week); n <= e.week; n++) out.push(n);
    return out;
  }
  function readiness(c, d, e) {
    const nos = new Set(e.week ? coveredWeeks(d, e) : []);
    const weeks = d.weeks.filter((w) => nos.has(w.no));
    const days = weeks.flatMap((w) => w.days.filter((x) => x.mine !== false && x.parts));
    const mats = weeks.flatMap((w) => w.units.flatMap((u) => u.materials)).filter((m) => !m.gone);
    const inWeeks = (a) => { const t = (a.date || "").slice(0, 10); return weeks.some((w) => w.start && t >= w.start && t <= (w.end || w.start)); };
    const kinds = e.kind === "quiz" ? ["quiz"] : [e.kind, "exam"];
    const sources = new Set(e.sources.map((s) => s.id));
    const items = (d.alerts || {}).items || [];
    const said = items.filter((a) => (a.source === "announcement" ? sources.has(a.announcement_id)
      : inWeeks(a) && (a.categories || [a.category]).some((k) => kinds.includes(k)))).slice(0, 6);
    const stressed = items.filter((a) => a.source === "lecture" && a.category === "important" && inWeeks(a)).slice(0, 6);
    const cv = St.findCourse(c.id) || { days: [] };
    const todo = days.filter((x) => !x.transcribed).flatMap((x) => ((cv.days.find((y) => y.day_key === x.day_key) || {}).recordings || [])
      .filter((r) => r.part && r.mine !== false && r.status !== "done" && !["queued", "downloading", "transcribing"].includes(r.status)).map((r) => r.id));
    const busy = days.filter((x) => ((cv.days.find((y) => y.day_key === x.day_key) || {}).recordings || []).some((r) => ["queued", "downloading", "transcribing"].includes(r.status)));
    return { weeks, days, mats, said, stressed, todo, busy: busy.length,
      transcribed: days.filter((x) => x.transcribed).length, read: days.filter((x) => x.any_done && readDays.has(x.day_key)).length,
      readable: days.filter((x) => x.any_done).length, downloaded: mats.filter((m) => m.downloaded).length,
      missing: mats.filter((m) => !m.downloaded && !(S.mat[m.id] && ["queued", "downloading"].includes(S.mat[m.id].state))).map((m) => m.id) };
  }
  function step(done, title, detail, action) {
    return `<div class="rd-step ${done ? "done" : ""}"><span class="rd-tick">${I(done ? "check" : "minus", "sm")}</span>
      <div class="grow"><b>${title}</b>${detail ? `<div class="dim">${detail}</div>` : ""}</div>${action || ""}</div>`;
  }
  function readyHTML() {
    const c = St.findCourse(R.cid), d = S.cd[R.cid];
    const e = d && ((d.alerts || {}).plan || []).find((x) => x.name === R.name);
    if (!c || !d || !e) return `<h2>${I("cap")} Get ready</h2><p class="lead">This assessment is no longer in the course's plan.</p>
      <div class="actions"><button class="btn" data-act="modal-close">Close</button></div>`;
    const r = readiness(c, d, e), rel = weekRel(d, e.week, e.kind);
    const plans = (R.plans || []).filter((p) => p.course_id === c.id && (p.scope || {}).focus === e.name);
    const prep = plans.find((p) => (p.scope || {}).kind === "prep"), video = plans.find((p) => (p.scope || {}).kind !== "prep");
    const steps = [
      [r.days.length && r.transcribed === r.days.length, `Lectures transcribed: ${r.transcribed} of ${r.days.length}`,
        r.busy ? `${plural(r.busy, "class day")} transcribing now` : r.days.length ? "your section, in the weeks it covers" : "no recorded lectures in these weeks yet",
        r.todo.length ? `<button class="btn sm" data-act="rd-transcribe">${I("wave", "sm")} Transcribe ${r.todo.length}</button>` : ""],
      [r.mats.length && !r.missing.length, `Files on this PC: ${r.downloaded} of ${r.mats.length}`, r.mats.length ? "slides and PDFs of those weeks" : "no files listed for these weeks",
        r.missing.length ? `<button class="btn sm" data-act="rd-download">${I("download", "sm")} Download ${r.missing.length}</button>` : ""],
      [r.readable && r.read === r.readable, `Lectures read: ${r.read} of ${r.readable}`, "ticked when you open a transcript, or tick them below", ""],
      [!!(prep && prep.status === "done") || !!(video && video.status === "done"), "A prep pack or video plan for it",
        plans.length ? plans.map((p) => `${(p.scope || {}).kind === "prep" ? "prep pack" : "video plan"}: ${esc(p.status === "done" ? "ready" : p.status)}`).join(" · ") : "made by AI when you ask (ChatGPT or Claude)",
        `<span class="rd-acts">${plans.length ? `<button class="btn sm" data-act="plan-open" data-id="${esc(plans[0].id)}">${I("arrow-right", "sm")} Open</button>`
          : `<button class="btn sm" data-act="plan-new" data-id="${esc(c.id)}" data-kind="prep" data-focus="${esc(e.name)}">${I("cap", "sm")} Prep pack</button>
             <button class="btn sm" data-act="plan-new" data-id="${esc(c.id)}" data-kind="videos" data-focus="${esc(e.name)}">${I("youtube", "sm")} Video plan</button>`}</span>`],
    ];
    const done = steps.filter((s) => s[0]).length;
    const read = r.days.filter((x) => x.any_done);
    return `<h2>${I("cap")} Get ready: ${esc(e.name)} <span class="dim sm">${esc(c.code)}</span></h2>
      <p class="lead">${e.week ? `Week ${e.week}${rel.w && rel.w.start ? ` · ${esc(range(rel.w.start, rel.w.end))}` : ""}${rel.rel ? ` · <b>${esc(rel.rel)}</b>` : ""}` : "Week not announced"}
        ${r.weeks.length ? ` · covers ${r.weeks.length === 1 ? `week ${r.weeks[0].no}` : `weeks ${r.weeks[0].no}–${r.weeks[r.weeks.length - 1].no}`}` : ""}
        · <button class="link" data-act="plan-ann" data-cid="${esc(c.id)}" data-ann="${esc(e.sources[0].id)}">as announced</button></p>
      ${e.los.length ? `<div class="rd-los">${e.los.map((n) => `<div><span class="lo-chip">LO${n}</span> ${esc(d.outcomes_en[n - 1] || d.outcomes_tr[n - 1] || "")}</div>`).join("")}</div>` : ""}
      <div class="rd-meter"><div class="bar"><i style="width:${Math.round((100 * done) / steps.length)}%"></i></div><span>${done} of ${steps.length} ready</span></div>
      <div class="rd-steps">${steps.map((s) => step(...s)).join("")}</div>
      ${read.length ? `<div class="rd-sec"><div class="col-title">${I("file-text", "xs")} Lectures to read</div>${read.map((x) => `<div class="rd-day">
          <input type="checkbox" class="check" data-act="rd-read" data-day="${esc(x.day_key)}" ${readDays.has(x.day_key) ? "checked" : ""} title="Read">
          <button class="link" data-act="open-day" data-day="${esc(x.day_key)}">${esc(x.kind_label || "Lecture")} · ${esc(dmw(x.date))}</button>
          <span class="dim">${x.transcribed ? "" : "partly transcribed"}</span></div>`).join("")}</div>` : ""}
      ${r.weeks.length ? `<div class="rd-sec"><div class="col-title">${I("list-checks", "xs")} Topics</div>${r.weeks.map((w) => `<div class="rd-topic"><b>Week ${w.no}</b> ${esc(w.topic_en || w.topic_tr || w.units.map((u) => u.title).join(" · ") || "—")}</div>`).join("")}</div>` : ""}
      ${r.said.length ? `<div class="rd-sec"><div class="col-title">${I("megaphone", "xs")} What the lecturer said about it</div>${r.said.map((a) => alertHTML(a, c.id)).join("")}</div>` : ""}
      ${r.stressed.length ? `<div class="rd-sec"><div class="col-title">${I("star", "xs")} Stressed in class in those weeks</div>${r.stressed.map((a) => alertHTML(a, c.id)).join("")}</div>` : ""}
      <div class="actions"><button class="btn" data-act="modal-close">Close</button></div>`;
  }
  function drawReady() {
    const box = $(".modal.rd");
    if (box) box.innerHTML = readyHTML();
  }
  // leaving the checklist for a transcript, an announcement or a plan closes it (the app's click handler then acts)
  document.addEventListener("click", (e) => {
    const el = e.target.closest(".modal.rd [data-act]");
    if (el && ["open-day", "plan-ann", "alert-open", "plan-open"].includes(el.dataset.act)) St.closeModal();
  }, true);
  async function openReady(cid, name) {
    R.cid = cid; R.name = name; R.plans = null;
    St.modal(`<h2>${I("cap")} Get ready</h2>${skeleton()}`);
    $(".modal").classList.add("rd");
    if (!S.cd[cid]) await loadCourse(cid, true);
    const pl = await call("plan_list", cid);
    R.plans = pl.ok ? pl.plans : [];
    drawReady();
  }

  // ------------------------------------------------------------------ wiring
  St.views.course = coursePage;
  St.views.home = homeView;
  St.onGo.course = (v) => { if (!S.cd[v.courseId] || Date.now() - (loadedAt[v.courseId] || 0) > 20000) loadCourse(v.courseId); };
  St.onGo.home = () => S.courses.forEach((c) => { if (!S.cd[c.id]) loadCourse(c.id, true); });
  St.after.course = (v) => {
    if (v.annFocus) {          // opened from a heads-up item: scroll to that announcement once it is on screen
      const el = $(`.ann[data-ann-id="${CSS.escape(v.annFocus)}"]`);
      if (el) { v.annFocus = null; el.scrollIntoView({ block: "center", behavior: "instant" }); }
      return;
    }
    if (v.jumpWeek && (v.tab || "weeks") === "weeks") {
      const el = $(`#week-${v.jumpWeek}`);
      if (el) { v.jumpWeek = null; el.scrollIntoView({ block: "start", behavior: "instant" }); }
    }
  };
  St.onEvents.courses = () => {
    const v = S.view;
    if (v.name === "course") reloadSoon(v.courseId);
    else if (v.name === "home") { S.courses.forEach((c) => reloadSoon(c.id)); renderView(); }
    else if (v.name === "new") renderView();
  };
  St.onEvents.content = (e) => {
    if (e.phase !== "done") return;
    const parts = [];
    if (e.new_materials) parts.push(plural(e.new_materials, "new material"));
    if (e.new_announcements) parts.push(plural(e.new_announcements, "new announcement"));
    if (parts.length) toast(parts.join(" and ") + " on IEULearn", "success");
    S.courses.forEach((c) => { if (S.cd[c.id] || S.view.name === "home") reloadSoon(c.id); });
  };
  St.onEvents.material = (e) => {
    if (!e.id) return;
    const prev = S.mat[e.id] || {};
    S.mat[e.id] = { ...prev, ...e };
    const hit = findMaterial(e.id);
    if (e.state === "done" && hit) {
      hit.m.downloaded = true; hit.m.outdated = false; hit.m.is_new = false; hit.m.pages = e.pages || hit.m.pages;
      if (prev.open) call("material_open", e.id);
      delete S.mat[e.id];
      reloadSoon(hit.cid);
    } else if (e.state === "error") {
      toast(`${hit ? hit.m.title : "Download"}: ${e.error || "failed"}`, "error");
    }
    updateMatRow(e.id);
  };

  function download(ids, open) {
    ids.forEach((id) => { S.mat[id] = { ...(S.mat[id] || {}), state: "queued", open: open || (S.mat[id] || {}).open }; updateMatRow(id); });
    return call("materials_download", ids).then((r) => { if (!r.ok) toast(r.error, "error"); });
  }
  Object.assign(A, {
    "course-tab": (el) => {
      const v = S.view, scrolled = $("#view").scrollTop > 0;
      go({ name: "course", courseId: v.courseId, tab: el.dataset.tab });
      // the tab's content starts below the course header: on a small screen, or when you had scrolled down to the
      // tabs, show the tabs at the top so the click visibly does something
      const tabs = $(".tabs");
      if (tabs && (scrolled || tabs.getBoundingClientRect().bottom > window.innerHeight - 160)) tabs.scrollIntoView({ block: "start", behavior: "instant" });
    },
    "goto-week": (el) => {
      const v = S.view;
      if ((v.tab || "weeks") === "weeks") { v.jumpWeek = +el.dataset.no; renderView(); }
      else go({ name: "course", courseId: v.courseId, tab: "weeks", jumpWeek: +el.dataset.no });
    },
    "week-toggle": (el) => {
      const cid = S.view.courseId, no = +el.dataset.no;
      const open = S.weeksOpen[cid] || (S.weeksOpen[cid] = new Set());
      if (open.has(no)) open.delete(no); else open.add(no);
      renderView();
    },
    "lo-lang": (el) => { S.loLang = el.dataset.val; renderView(); },
    "mat-get": (el) => download([el.dataset.id], false),
    "mat-open": (el) => {
      const hit = findMaterial(el.dataset.id); if (!hit) return;
      if (hit.m.downloaded) call("material_open", el.dataset.id).then((r) => { if (!r.ok) toast(r.error, "error"); });
      else download([el.dataset.id], true);
    },
    "mat-reveal": (el) => call("material_open", el.dataset.id, true),
    "mat-all": (el) => {
      const d = S.cd[el.dataset.id]; if (!d) return;
      const ids = allMaterials(d).filter((m) => !m.downloaded && !(S.mat[m.id] && ["queued", "downloading"].includes(S.mat[m.id].state))).map((m) => m.id);
      if (ids.length) download(ids, false);
      renderView();
    },
    "course-seen": async (el) => {
      const r = await call("course_mark_seen", el.dataset.id || "", el.dataset.what || "all");
      if (r.ok) { St.setCourses(r.courses); renderSidebar(); }
      (el.dataset.id ? [el.dataset.id] : Object.keys(S.cd)).forEach((cid) => reloadSoon(cid));
    },
    "ann-more": (el) => { S.ann.add(el.dataset.id); renderView(); },
    "alert-cat": (el) => {
      S.alertCat = el.dataset.cat;
      const v = S.view;
      if (v.name === "course" && v.tab === "alerts") renderView();
      else go({ name: "course", courseId: v.courseId, tab: "alerts" });
    },
    "alert-open": async (el) => {
      if (el.dataset.day) {
        const part = +el.dataset.part || null, t = +el.dataset.t || 0;
        await openDay(el.dataset.day);
        setTimeout(() => jumpTo(part, t), 60);
        return;
      }
      S.ann.add(el.dataset.ann);
      S.flashAnn = { id: el.dataset.ann, until: Date.now() + 1500 };
      go({ name: "course", courseId: el.dataset.cid, tab: "announcements", annFocus: el.dataset.ann });
    },
    "heads-toggle": () => { S.headsOpen = !S.headsOpen; store("studia-heads", S.headsOpen ? "1" : "0"); renderView(); },
    "plan-ann": (el) => A["alert-open"](el),            // an assessment's announcement ("plan-open" opens a study plan)
    "set-section": (el) => setSection(el.dataset.id, { my_section: el.dataset.sec, show_mode: "mine" }),
    "ready-open": (el) => openReady(el.dataset.cid, el.dataset.name),
    "export-calendar": async () => {
      const r = await call("export_calendar");
      if (!r.ok) { toast(r.error, "info"); return; }
      toast(`Saved ${r.count === 1 ? "1 quiz or exam" : `${r.count} quizzes and exams`} to a calendar file. Double-click it to add them to Outlook or Windows Calendar, or import it in Google Calendar (Settings → Import).`, "success");
    },
    "rd-read": (el) => { setRead(el.dataset.day, el.checked); drawReady(); },
    "rd-transcribe": async () => {
      const c = St.findCourse(R.cid), d = S.cd[R.cid];
      const e = d && ((d.alerts || {}).plan || []).find((x) => x.name === R.name);
      const ids = c && e ? readiness(c, d, e).todo : [];
      if (!ids.length) return;
      const r = await call("transcribe", ids);
      if (!r.ok) { toast(r.error, "error"); return; }
      toast(`${plural(r.queued, "lecture")} queued for transcription`, "success");
      reloadSoon(R.cid);
    },
    "rd-download": () => {
      const c = St.findCourse(R.cid), d = S.cd[R.cid];
      const e = d && ((d.alerts || {}).plan || []).find((x) => x.name === R.name);
      const ids = c && e ? readiness(c, d, e).missing : [];
      if (ids.length) download(ids, false).then(drawReady);
    },
    "section-menu": (el) => {
      const c = St.findCourse(el.dataset.id); if (!c) return;
      St.menu(el, [...c.available_sections.map((x) => ({ id: "s" + x, icon: x === c.my_section ? "check" : "users", label: `Section ${x}`,
        desc: x === c.my_section ? "your section" : "", run: () => setSection(c.id, { my_section: x, show_mode: "mine" }) })), "-",
        { id: "all", icon: "layers", label: c.show_mode === "all" ? "Showing all sections" : "Show all sections' lectures",
          desc: "other sections cover the same material", run: () => setSection(c.id, { show_mode: c.show_mode === "all" ? "mine" : "all" }) }]);
    },
  });

  // ------------------------------------------------------------------ browser preview (mock)
  const mockWeeks = [["Sample Spaces, Events & Counting", "Örnek uzay ve olaylar", "2026-09-23"], ["Combinations & the First Probability Rules", "Olaylar ve sayma metotları", "2026-09-30"],
    ["Independence, Reliability & Bayes", "Sayma metotları, bir olayın olasılığı", "2026-10-07"], ["Random Variables, PMF & CDF", "Ekleme kuralları, koşullu olasılık", "2026-10-14"],
    ["Mean, Variance & the Named Distributions", "Bayes’ kuralı", "2026-10-21"], ["Poisson & the Continuous Half", "Rassal değişken konsepti", "2026-10-28"],
    ["The Uniform & the Normal", "Kesikli rassal değişkenler", "2026-11-04"], ["Midterm", "Ara Sınav", "2026-11-11"]];
  const today = new Date().toISOString().slice(0, 10);
  const hlOf = (text, cue) => { const i = text.indexOf(cue); return i < 0 ? [0, 0] : [i, i + cue.length]; };
  function mockAlerts(c) {
    const lect = (c.days || []).filter((dd) => dd.any_done);
    const ago = (n, hm) => { const d = new Date(); d.setDate(d.getDate() - n); return `${d.toISOString().slice(0, 10)}T${hm}`; };
    const items = [
      { category: "schedule", categories: ["schedule"], source: "announcement", title: "Class Rescheduling Announcement", text: "Our class originally scheduled for October 28 has been rescheduled to Saturday, October 24, at 12:00 in classroom M01.", cue: "rescheduled", lang: "en", announcement_id: "a1", date: ago(2, "15:53"), is_new: true },
      ...(lect.length ? [
        { category: "quiz", categories: ["quiz"], source: "lecture", text: "I think time is up. So please continue to study. Next week, we have a quiz.", cue: "quiz", lang: "en", day_key: lect[0].day_key, part: 3, t: 2609, ts: "43:29", date: `${lect[0].date}T10:58` },
        { category: "homework", categories: ["homework"], source: "lecture", text: "So, let's take a break and the other tools are homework for you.", cue: "homework", lang: "en", day_key: lect[0].day_key, part: 2, t: 1717, ts: "28:37", date: `${lect[0].date}T09:58` },
        { category: "final", categories: ["final", "midterm", "quiz"], source: "lecture", text: "…And the midterm or quizzes doesn't matter in final exam. If you see your result, accept this.", cue: "final exam", lang: "en", day_key: lect[0].day_key, part: 2, t: 931, ts: "15:31", date: `${lect[0].date}T09:44` },
        { category: "important", categories: ["important"], source: "lecture", text: "This is very important: the events must be mutually exclusive before you add the probabilities.", cue: "important", lang: "en", day_key: lect[0].day_key, part: 1, t: 610, ts: "10:10", date: `${lect[0].date}T08:38` }] : []),
      { category: "quiz", categories: ["quiz", "midterm", "exam"], source: "announcement", title: "Quiz Takvimi ve Uygulama Kuralları", text: "Quiz ve ara sınav takvimi aşağıdaki gibidir. Quizlerin kapsadığı öğrenme çıktıları (LO) tabloda belirtilmiştir.", cue: "Quiz", lang: "tr", announcement_id: "a2", date: ago(16, "10:05"), is_new: false },
    ].map((a) => ({ ...a, hl: hlOf(a.text, a.cue) })).sort((x, y) => y.date.localeCompare(x.date));
    const counts = {};
    items.forEach((a) => { counts[a.category] = (counts[a.category] || 0) + 1; });
    const src = [{ id: "a2", title: "Quiz Takvimi ve Uygulama Kuralları", date: ago(16, "10:05") }];
    const plan = [{ kind: "quiz", no: 1, name: "Quiz 1", week: 3, los: [1, 2], sources: src }, { kind: "quiz", no: 2, name: "Quiz 2", week: 5, los: [3], sources: src },
      { kind: "midterm", no: 0, name: "Midterm", week: 8, los: [], sources: src }, { kind: "quiz", no: 3, name: "Quiz 3", week: 10, los: [5, 6, 7], sources: src }];
    return { items, counts, total: items.length, plan };
  }
  St.mocks.course_detail = (cid) => {
    const c = St.findCourse(cid) || {};
    const wds = new Set((c.days || []).map((dd) => day(dd.date).getDay()));        // the weekdays it meets on
    const weeks = mockWeeks.map(([en, tr, start], i) => {
      const end = new Date(Date.parse(start + "T00:00:00Z") + 6 * 864e5).toISOString().slice(0, 10);
      const classes = [0, 1, 2, 3, 4, 5, 6].map((k) => new Date(Date.parse(start + "T00:00:00Z") + k * 864e5).toISOString().slice(0, 10))
        .filter((x) => wds.has(day(x).getDay())).map((x) => ({ date: x, end: "11:20" }));
      const mats = i === 7 ? [] : [{ id: `m${cid}${i}`, title: `Week${String(i + 1).padStart(2, "0")}_Student`, file_name: `Week${i + 1}.pdf`, ext: "pdf", kind: "pdf", size: 460000, downloaded: i < 2, outdated: false, is_new: i === 4, pages: i < 2 ? 15 : 0, text: "", gone: false }];
      if (i === 2) mats.push({ id: `p${cid}${i}`, title: "Bayes worked examples", file_name: "bayes.pptx", ext: "pptx", kind: "presentation", size: 2300000, downloaded: false, outdated: false, is_new: false, pages: 0, text: "", gone: false });
      return { no: i + 1, start, end, topic_en: en, topic_tr: tr, readings: i === 7 ? "" : `Montgomery & Runger, Sections 2.${i + 1}`, exam: i === 7,
        units: [{ id: `u${i}`, title: tr, readings: "", materials: mats }], current: start <= today && today <= end, past: end < today, classes,
        days: (c.days || []).filter((dd) => dd.date >= start && dd.date <= end).map((dd) => ({ day_key: dd.day_key, date: dd.date, label: dd.label, kind_label: dd.kind_label, parts: dd.parts, transcribed: dd.transcribed, any_done: dd.any_done, is_new: dd.is_new, mine: dd.mine, duration_s: dd.duration_s })) };
    });
    return { ok: true, course: { id: cid, code: c.code, name: c.name, display_name: c.display_name, instructor: c.instructor, term: c.term, section: c.section, has_panopto: c.has_panopto, counts: c.counts, my_section: c.my_section, cover: c.cover || "", lms_progress: c.lms_progress,
      description: "In this course some important theorems about probability are investigated.", objectives: "This course aims to introduce students the theory of probability and its applications to engineering problems.",
      outcomes_en: ["use fundamental concepts such as sample space, events and counting techniques.", "explain concepts of probability.", "use conditional probability, the total probability rule and Bayes' theorem.", "compute discrete and continuous random variables.", "investigate the advantages of joint probability distributions.", "apply discrete and continuous distributions.", "examine the relationship between two random variables.", "find mean and variance of random variables."],
      outcomes_tr: ["örnek uzay, olay ve sayma teknikleri gibi olasılığın temel kavramlarını kullanabilecektir.", "olasılık konseptini inceleyebilecektir.", "koşullu olasılık, toplam olasılık formülü ve Bayes teoremini kullanabilecektir."],
      grading: [{ activity: "Quizzes", number: 4, weight: 20 }, { activity: "Midterm", number: 1, weight: 30 }, { activity: "Final Exam", number: 1, weight: 50 }],
      syllabus: { url: "https://ce.ieu.edu.tr/en/syllabus/type/read/id/MATH+240", ects: "6", semester: "Fall", prerequisites: "MATH 154 (at least FD)", language: "English", coordinator: c.instructor, lecturers: c.instructor, assistants: "", textbooks: "Montgomery & Runger, Applied Statistics and Probability for Engineers, 7th ed.", readings: "" },
      weeks, general: [{ id: "g", title: "Formula Sheet", readings: "", materials: [{ id: `f${cid}`, title: "Formula Sheet 2026-27", file_name: "formula.pdf", ext: "pdf", kind: "pdf", size: 120000, downloaded: true, outdated: false, is_new: false, pages: 2, text: "done", gone: false }] }],
      announcements: [{ id: "a1", title: "Class Rescheduling Announcement", author: c.instructor, posted_at: "2026-10-19T15:53", body: "Dear Students,\n\nOur class originally scheduled for October 28 has been rescheduled to Saturday, October 24, at 12:00 in classroom M01.\n\nBest regards", is_new: true },
        { id: "a2", title: "Quiz Takvimi ve Uygulama Kuralları", author: c.instructor, posted_at: "2026-10-05T10:05", body: "Sevgili Öğrenciler,\n\nQuiz ve ara sınav takvimi aşağıdaki gibidir. Quizlerin kapsadığı öğrenme çıktıları (LO) tabloda belirtilmiştir.\n\nQuiz 1 — 3. hafta — LO1, LO2\nQuiz 2 — 5. hafta — LO3\nAra sınav — 8. hafta — LO1–LO4", is_new: false }],
      materials: { total: 9, downloaded: 3, new: 1 }, content_at: "2026-10-21T08:00:00", alerts: mockAlerts(c) } };
  };
  St.mocks.export_calendar = () => ({ ok: true, count: 7, path: "C:\\Users\\student\\Documents\\IEUStudia\\IEUStudia - quizzes and exams.ics" });
  St.mocks.materials_download = (ids) => {
    ids.forEach((id, k) => {
      let done = 0;
      const total = 460000;
      setTimeout(() => {
        const iv = setInterval(() => {
          done += 92000;
          if (done >= total) { clearInterval(iv); window.__lsEvent({ type: "material", id, state: "done", pages: 15, path: "x.pdf" }); return; }
          window.__lsEvent({ type: "material", id, state: "downloading", done, total });
        }, 150);
      }, k * 900);
    });
    return { ok: true, queued: ids.length };
  };
  St.mocks.material_open = () => ({ ok: true });
  St.mocks.course_mark_seen = () => ({ ok: true, courses: S.courses });
})();
