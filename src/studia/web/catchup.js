/* IEUStudia — full sync (every course) and course sync: sync, then transcribe the lectures of your sections that
 * aren't transcribed yet (newest first, so the lecturer's latest heads-ups appear first) and download the materials.
 * No AI is used here: notes, plans and chats stay on demand. Plugs into app.js through window.Studia. */
(function () {
  "use strict";
  const St = window.Studia;
  const { S, call, esc, I, SPIN, plural, toast, modal, closeModal, renderTop, fmtDur, A } = St;

  S.catchup = { running: null, est: null, picked: null, scope: null, mats: true };

  const speed = (est) => est.speed || (est.gpu ? 12 : 0.6);        // from catchup.py
  const mb = (n) => (n >= 1024 ? `${(n / 1024).toFixed(1)} GB` : `${Math.max(1, Math.round(n))} MB`);

  function totals() {
    const { est, picked } = S.catchup;
    const rows = est.courses.filter((c) => picked.has(c.course_id));
    const audio = rows.reduce((a, c) => a + c.audio_s, 0);
    return { rows, lectures: rows.reduce((a, c) => a + c.lectures, 0), audio, materials: rows.reduce((a, c) => a + c.materials, 0),
      work: audio / speed(est), audioMb: (audio / 60) * 0.75 };
  }

  function rowHTML(c) {
    const what = c.needs_section && c.waiting ? `<span class="r" style="color:var(--amber)">choose your section first (${plural(c.waiting, "lecture")})</span>`
      : c.lectures || c.materials ? `<span class="r">${c.lectures ? `${plural(c.lectures, "lecture")} · ${fmtDur(c.audio_s)}` : "lectures done"}${c.materials ? ` · ${plural(c.materials, "file")}` : ""}</span>`
        : `<span class="r" style="color:var(--green)">${I("check", "xs")} up to date</span>`;
    return `<label class="sync-row"><input type="checkbox" class="check" data-act="cu-pick" data-id="${esc(c.course_id)}" ${S.catchup.picked.has(c.course_id) ? "checked" : ""}>
      <span class="t"><span class="code">${esc(c.code)}</span>${esc(St.findCourse(c.course_id) ? St.courseSub(St.findCourse(c.course_id)) : c.name)}</span>${what}</label>`;
  }

  function bodyHTML() {
    const { est, scope } = S.catchup;
    const one = scope !== "all";
    const t = totals();
    const c0 = one ? est.courses[0] : null;
    const mats = S.catchup.mats;
    const big = t.work > 3600 || t.lectures > 8;
    const summary = t.lectures
      ? `<b>${plural(t.lectures, "lecture")}</b> to transcribe (≈ ${fmtDur(t.audio)} of audio) — roughly <b>${fmtDur(t.work)}</b> ${est.gpu ? "with your graphics card" : "on the processor, which is slow"}; about ${mb(t.audioMb)} of audio is downloaded and deleted afterwards.`
      : "Every lecture of your sections is transcribed already.";
    const section = one && c0 && c0.needs_section && c0.waiting
      ? `<div class="sync-note">${I("users")}<div>This course publishes the lectures of several sections and yours isn’t set yet, so its ${plural(c0.waiting, "lecture")} are held back. Choose your section on the course page first.</div></div>` : "";
    const others = (one ? [c0] : t.rows).reduce((a, c) => a + (c ? c.other_sections : 0), 0);
    return `<p class="lead">${one ? `Checks IEULearn and Panopto, then transcribes ${esc(c0 ? c0.code : "this course")}’s lectures that aren’t transcribed yet (your section, newest first)` : "Checks IEULearn and Panopto, then transcribes every lecture of your sections that isn’t transcribed yet (newest first, across all courses)"}${mats ? " and downloads the materials" : ""}. Anything new the sync finds is included.</p>
      ${one ? "" : `<div class="sync-pick">${est.courses.map(rowHTML).join("")}</div>`}
      ${section}
      <div class="sync-note${big ? "" : " calm"}">${I(big ? "alert" : "info")}<div>${summary}${t.lectures ? ` ${big ? "Your PC works hard while this runs, and its fans may get loud. " : ""}You can keep using IEUStudia and cancel any time from the bar at the bottom of the window.` : ""}</div></div>
      <div class="sync-note calm">${I("megaphone")}<div>Heads-ups — quizzes, exams, homework the lecturer mentions — appear as each lecture is transcribed. No AI is used here: notes, study plans and chats run only when you ask for them.${others ? ` Lectures of other sections are skipped (${others}).` : ""}</div></div>
      <label class="mini-toggle" style="margin:10px 0 0"><input type="checkbox" class="switch sm" data-act="cu-mats" ${mats ? "checked" : ""}>Also download materials${t.materials ? ` (${plural(t.materials, "file")})` : " (none missing)"}</label>
      <div class="actions"><button class="btn" data-act="modal-close">Cancel</button>
        <button class="btn primary" data-act="cu-go" ${!one && !t.rows.length ? "disabled" : ""}>${I("zap", "sm")} ${t.lectures || (mats && t.materials) ? (one ? "Sync course" : "Start full sync") : "Sync now"}</button></div>`;
  }
  function draw() {
    const box = document.querySelector("#cu-body");
    if (box) box.innerHTML = bodyHTML();
  }

  async function dialog(courseId) {
    if (S.catchup.running) { toast("A full sync is already starting. Once it has checked IEULearn, its lectures appear in the bar at the bottom of the window.", "info"); return; }
    const one = !!courseId;
    modal(`<h2>${I("zap")} ${one ? "Sync this course" : "Full sync — every course"}</h2><div id="cu-body">${St.skeleton ? St.skeleton() : SPIN()}</div>`);
    const r = await call("catch_up_estimate", courseId || null);
    if (!document.querySelector("#cu-body")) return;           // closed meanwhile
    if (!r.ok) { closeModal(); toast(r.error, "error"); return; }
    S.catchup.est = r;
    S.catchup.scope = courseId || "all";
    S.catchup.picked = new Set(r.courses.filter((c) => one || c.lectures || c.materials || !c.needs_section).map((c) => c.course_id));
    draw();
  }

  async function start() {
    const { est, picked, scope } = S.catchup;
    const all = scope === "all" && picked.size === est.courses.length;
    const ids = scope === "all" ? (all ? null : [...picked]) : [scope];
    closeModal();
    S.catchup.running = scope;
    renderTop();
    const r = await call("catch_up", ids, S.catchup.mats);
    if (!r.ok) { S.catchup.running = null; toast(r.error, "error"); renderTop(); return; }
    toast(scope === "all" ? "Full sync started — syncing first, then the lectures are queued." : "Course sync started — syncing first.", "info");
  }

  St.onEvents.catchup = (e) => {
    if (e.phase === "sync") return;
    S.catchup.running = null;
    renderTop();
    if (e.phase === "error") { toast(e.message || "Full sync failed", "error"); return; }
    const parts = [];
    if (e.lectures) parts.push(`${plural(e.lectures, "lecture")} queued (≈ ${fmtDur(e.audio_s)} of audio)`);
    if (e.materials) parts.push(`${plural(e.materials, "material")} downloading`);
    toast(parts.length ? parts.join(" · ") : "Everything is synced and transcribed.", parts.length ? "success" : "info");
    (e.waiting || []).forEach((code) => toast(`${code}: choose your section to transcribe its lectures.`, "warning"));
  };

  Object.assign(A, {
    "full-sync": () => dialog(null),
    "course-sync": (el) => dialog(el.dataset.id),
    "cu-go": () => start(),
    "cu-pick": (el) => { const p = S.catchup.picked; if (el.checked) p.add(el.dataset.id); else p.delete(el.dataset.id); draw(); },
    "cu-mats": (el) => { S.catchup.mats = el.checked; draw(); },
  });

  // ------------------------------------------------------------------ browser preview (mock)
  St.mocks.catch_up_estimate = (cid) => {
    const courses = S.courses.filter((c) => !cid || c.id === cid).map((c, i) => {
      const left = Math.max(0, (c.counts.total || 0) - (c.counts.done || 0));
      return { course_id: c.id, code: c.code, name: c.display_name, lectures: left, audio_s: left * 2900, materials: i % 2 ? 0 : 4,
        other_sections: i === 0 ? 3 : 0, needs_section: false, waiting: 0 };
    });
    const audio = courses.reduce((a, c) => a + c.audio_s, 0);
    return { ok: true, courses, lectures: courses.reduce((a, c) => a + c.lectures, 0), audio_s: audio,
      materials: courses.reduce((a, c) => a + c.materials, 0), work_s: audio / 12, speed: 12, gpu: true, audio_mb: (audio / 60) * 0.75, needs_section: [] };
  };
  St.mocks.catch_up = () => {
    setTimeout(() => window.__lsEvent({ type: "catchup", phase: "done", scope: "all", lectures: 3, materials: 4, audio_s: 8700, waiting: [] }), 700);
    return { ok: true };
  };
})();
