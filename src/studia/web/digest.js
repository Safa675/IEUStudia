/* IEUStudia — the weekly digest: what happened this week in each course, what is coming, and what to do next.
 * Built without AI; the AI you chose can add a short written summary on request. Plugs into app.js through window.Studia. */
(function () {
  "use strict";
  const St = window.Studia;
  const { S, call, esc, I, SPIN, plural, toast, go, renderView, A } = St;
  const CAT = { final: "Final", midterm: "Midterm", quiz: "Quiz", exam: "Exam", project: "Project", homework: "Homework", schedule: "Schedule" };

  S.digest = { offset: 0, data: null, summary: null, running: false, text: "" };

  async function load() {
    const r = await call("digest", S.digest.offset);
    if (!r.ok) { toast(r.error, "error"); return; }
    S.digest.data = r.digest;
    S.digest.summary = r.summary && r.summary.text ? r.summary : null;
    if (S.view.name === "digest") renderView();
  }

  function courseHTML(c) {
    const lec = c.lectures.map((l) => `<div class="dg-lec"><span class="dg-ico">${I("video", "sm")}</span><div class="grow">
        <div><b>${esc(l.kind)} ${esc(l.label)}</b> ${l.any_done ? `<span class="pill done">${I("check")}Transcribed</span>` : `<button class="btn ghost sm" data-act="tx-day" data-day="${esc(l.day_key)}">${I("wave", "sm")} Transcribe</button>`}
          ${l.any_done ? `<button class="btn ghost sm" data-act="open-day" data-day="${esc(l.day_key)}">${I("file-text", "sm")} Read</button>` : ""}</div>
        ${l.stressed.length ? `<ul class="dg-stars">${l.stressed.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}</div></div>`).join("");
    return `<div class="card dg-course"><div class="dg-head" data-act="go" data-view="course" data-id="${esc(c.id)}">
        <span class="dg-code">${esc(c.code)}</span><div class="grow"><b>${esc(c.name)}</b>${c.week ? `<div class="dim">Week ${c.week.no} · ${esc(c.week.topic)}</div>` : ""}</div>${I("chevron-right", "sm")}</div>
      ${c.coming.length ? `<div class="dg-coming">${c.coming.map((e) => `<span class="plan-pill ${e.this_week ? "now" : ""}"><b>${esc(e.name)}</b><span>${e.this_week ? "this week" : `week ${e.week}`}</span>${e.los.length ? `<span class="lo">LO${e.los.join(", LO")}</span>` : ""}</span>`).join("")}</div>` : ""}
      ${lec ? `<div class="dg-sec">${lec}</div>` : ""}
      ${c.said.length ? `<div class="dg-sec"><div class="dg-lbl">${I("alert", "sm")} The lecturer said</div>${c.said.map((s) => `<div class="dg-said"><span class="cat-chip cat-${esc(s.category)}">${esc(CAT[s.category] || s.category)}</span><span>${esc(s.text)}</span></div>`).join("")}</div>` : ""}
      ${c.new_files.length || c.announcements.length ? `<div class="dg-sec dg-misc">
        ${c.new_files.length ? `<div>${I("layers", "sm")} New files: ${c.new_files.map((f) => esc(f.title)).join(", ")}</div>` : ""}
        ${c.announcements.length ? `<div>${I("megaphone", "sm")} Announcements: ${c.announcements.map((a) => esc(a.title)).join(" · ")}</div>` : ""}</div>` : ""}
    </div>`;
  }

  function digestView() {
    const g = S.digest, d = g.data;
    const ai = (S.system || {}).ai || {};
    if (!d) return `<div class="view-inner">${St.skeleton()}</div>`;
    const todo = d.todo.map((t) => {
      const act = t.kind === "transcribe" ? `<button class="btn ghost sm" data-act="tx-day" data-day="${esc(t.day_key)}">${I("wave", "sm")} Transcribe</button>`
        : t.kind === "download" ? `<button class="btn ghost sm" data-act="mat-get" data-id="${esc(t.resource_id)}">${I("download", "sm")} Download</button>`
          : t.kind === "prepare" ? `<button class="btn ghost sm" data-act="plan-new" data-id="${esc(t.course_id)}">${I("youtube", "sm")} Make a plan</button>` : "";
      return `<div class="dg-todo"><span class="dg-code">${esc(t.course)}</span><span class="grow">${esc(t.text)}</span>${act}</div>`;
    }).join("");
    const sum = g.running ? `<div class="md">${St.mdToHtml(g.text || "")}</div><div class="dim">${SPIN()} ${esc(St.aiName())} is writing…</div>`
      : g.summary ? `<div class="md">${St.mdToHtml(g.summary.text)}</div><div class="dim sm">Written by AI (${esc(g.summary.model || "")}) — can contain mistakes · ${esc((g.summary.at || "").replace("T", " "))}
          ${g.summary.file ? ` · <button class="link" data-act="dg-file" data-path="${esc(g.summary.file)}">open the Markdown file</button>` : ""}</div>` : "";
    return `<div class="view-inner digest">
      <div class="pl-head"><div><h1>Weekly digest</h1><div class="dim">${esc(d.week.label)}</div></div>
        <div class="dg-nav"><button class="btn ghost icon sm" data-act="dg-week" data-d="-1" title="Previous week">${I("chevron-left", "sm")}</button>
          <button class="btn ghost sm" data-act="dg-week" data-d="0" ${d.week.offset === 0 ? "disabled" : ""}>This week</button>
          <button class="btn ghost icon sm" data-act="dg-week" data-d="1" title="Next week">${I("chevron-right", "sm")}</button>
          <button class="btn primary sm" data-act="dg-summary" ${ai.available && !g.running ? "" : "disabled"} title="${ai.available ? `A short written summary by ${St.aiName()} (light model)` : "Install Claude Code to enable"}">${I("ai", "sm")} ${g.summary ? "Summarize again" : "Summarize with Claude"}</button></div></div>
      ${sum ? `<div class="card dg-summary">${sum}</div>` : ""}
      ${todo ? `<div class="section-title">${I("list-checks")} To do</div><div class="card dg-todos">${todo}</div>` : ""}
      <div class="section-title">${I("book")} Your courses this week</div>
      ${d.courses.length ? d.courses.map(courseHTML).join("") : St.emptyHTML("calendar", "A quiet week", "Nothing happened in your courses this week yet.")}
    </div>`;
  }

  St.views.digest = digestView;
  const navBefore = St.navExtra;
  St.navExtra = (v) => (navBefore ? navBefore(v) : "") +
    `<button class="nav-item ${v.name === "digest" ? "active" : ""}" data-act="go" data-view="digest">${I("calendar")}<span class="label"><span class="code">Weekly digest</span></span></button>`;
  St.crumbs.digest = () => `${I("calendar")} Weekly digest`;
  St.onGo.digest = () => load();
  St.onEvents.digest = (e) => {
    if (e.offset !== S.digest.offset) return;
    if (e.phase === "start" || e.phase === "progress") { S.digest.running = true; S.digest.text = e.text || ""; }
    else if (e.phase === "done") { S.digest.running = false; S.digest.summary = e; toast("Weekly digest written", "success"); }
    else if (e.phase === "error") { S.digest.running = false; toast(e.message || "The AI could not finish", "error"); }
    if (S.view.name === "digest") renderView();
  };
  Object.assign(A, {
    "dg-week": (el) => { const d = +el.dataset.d; S.digest.offset = d === 0 ? 0 : S.digest.offset + d; S.digest.data = null; renderView(); load(); },
    "dg-summary": async () => { const r = await call("digest_summary", S.digest.offset); if (!r.ok) toast(r.error, "error"); else { S.digest.running = true; S.digest.text = ""; renderView(); } },
    "dg-file": (el) => call("open_output_file", el.dataset.path),
  });

  // browser preview
  St.mocks.digest = (offset) => ({ ok: true, summary: {}, digest: { week: { start: "2026-10-19", end: "2026-10-25", offset, label: "19 Oct – 25 Oct 2026" },
    todo: [{ kind: "prepare", course: "MATH 240", course_id: "17933", text: "Quiz 2 is this week (LO3, LO4)" }, { kind: "transcribe", course: "EEE 242", course_id: "17653", day_key: "17653|x|lecture", text: "Transcribe the lecture of Friday 16 October" }],
    courses: [{ id: "17933", code: "MATH 240", name: "Probability for Engineers", week: { no: 5, topic: "Bayes' rule" },
      lectures: [{ day_key: "17933|x|lecture", date: "2026-10-21", label: "Wednesday 21 October", kind: "Lecture", transcribed: true, any_done: true, stressed: ["Next week, we have a quiz.", "This denominator is the total probability — be careful."] }],
      new_files: [{ id: "m1", title: "Week05_Student", downloaded: false }], announcements: [{ id: "a1", title: "Class Rescheduling Announcement" }],
      coming: [{ name: "Quiz 2", week: 5, start: "2026-10-19", end: "2026-10-25", los: [3, 4], this_week: true }, { name: "Midterm", week: 8, start: "2026-11-09", end: "2026-11-15", los: [], this_week: false }],
      said: [{ category: "schedule", text: "Our class on 28 Oct is moved to Saturday 24 Oct, 12:00, M01.", date: "2026-10-19", source: "announcement" }] }] } });
  St.mocks.digest_summary = () => { setTimeout(() => window.__lsEvent({ type: "digest", phase: "done", offset: 0, text: "**A busy week.** Quiz 2 in MATH 240 covers Bayes' rule (LO3–LO4)…", model: "haiku", at: "2026-10-21T12:00:00" }), 800); return { ok: true }; };
})();
