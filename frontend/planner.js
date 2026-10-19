/* exported planner */
// Plan view: ranked per-day course suggestions written by the /plan-courses
// Claude Code skill, with one pick per day and feedback on past picks. All
// state lives on the server (/api/planner/*); this file only renders it.
// Loaded before app.js, which calls planner.init() once the map exists and
// planner.show()/hide() from switchView().
const planner = (() => {
  const TOKEN_KEY = "turnbeutel.plannerToken";
  const TIME_FILTER_KEY = "turnbeutel.planTimeFilter";
  const SORT_KEY = "turnbeutel.planSort";
  const PAST_DAYS = 14;
  const FUTURE_DAYS = 13;

  const TIME_BUCKETS = [
    { id: "morning", label: "Morning", test: (m) => m < 12 * 60 },
    { id: "afternoon", label: "Afternoon", test: (m) => m >= 12 * 60 && m < 17 * 60 },
    { id: "evening", label: "Evening", test: (m) => m >= 17 * 60 },
  ];
  const WENT = [
    ["yes", "Went"],
    ["no", "Didn't go"],
    ["came_up", "Something came up"],
  ];
  const NO_REASONS = [
    ["cancelled", "Cancelled"],
    ["full", "Was full"],
    ["didnt_feel_like_it", "Didn't feel like it"],
    ["other", "Other"],
  ];
  const AGAIN = [
    ["no", "No"],
    ["maybe", "Maybe"],
    ["yes", "Yes"],
  ];
  const AGAIN_AFTER = [
    ["1w", "in ~1 week"],
    ["2-4w", "in 2–4 weeks"],
    ["1-3m", "in 1–3 months"],
    ["someday", "someday"],
  ];

  let ctx = null; // helpers from app.js, see init()
  let layer = null;
  let active = false;
  let suggestions = [];
  let selectedDate = null;
  let showDismissed = false;
  let openFeedbackId = null; // suggestion whose feedback form is expanded
  let expandedId = null; // suggestion whose course details are open
  let state = "idle"; // idle | loading | ready | error | token
  let errorMessage = "";
  const markers = new Map(); // suggestion id -> Leaflet marker

  // ── Persistence (per-browser conveniences only) ──

  function readStored(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw == null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  }

  function writeStored(key, value) {
    try {
      if (value == null) localStorage.removeItem(key);
      else localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* storage unavailable: the setting just isn't remembered */
    }
  }

  const timeFilter = new Set(readStored(TIME_FILTER_KEY, []));
  let sortBy = readStored(SORT_KEY, "rank") === "time" ? "time" : "rank";

  // ── API ──

  class TokenError extends Error {}

  async function api(path, options = {}) {
    const token = readStored(TOKEN_KEY, null);
    if (!token) throw new TokenError("A planner token is needed.");
    const res = await fetch(`${API_BASE}/api/planner${path}`, {
      ...options,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
      },
    });
    if (res.status === 401) {
      writeStored(TOKEN_KEY, null);
      throw new TokenError("The planner token was rejected.");
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch {
        /* keep the status text */
      }
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  }

  function askForToken() {
    const token = window.prompt("Planner token (PLANNER_TOKEN on the server):");
    if (token && token.trim()) {
      writeStored(TOKEN_KEY, token.trim());
      load();
    }
  }

  async function load() {
    state = "loading";
    render();
    const today = ctx.todayIso();
    try {
      const data = await api(
        `/suggestions?from=${ctx.addDaysIso(today, -PAST_DAYS)}&to=${ctx.addDaysIso(today, FUTURE_DAYS)}`,
      );
      suggestions = data.suggestions;
      state = "ready";
      if (!selectedDate || !daysWithContent().includes(selectedDate)) selectedDate = defaultDate();
    } catch (err) {
      state = err instanceof TokenError ? "token" : "error";
      errorMessage = err.message;
    }
    render();
  }

  async function mutate(path, options) {
    try {
      await api(path, options);
    } catch (err) {
      if (err instanceof TokenError) {
        state = "token";
        errorMessage = err.message;
        render();
        return;
      }
      window.alert(`Could not save: ${err.message}`);
    }
    await load();
  }

  // ── Derived data ──

  // The shape app.js's course helpers (badges, details block) expect.
  function asCourse(s) {
    return { ...s, id: s.course_id };
  }

  function minutesOf(startTime) {
    const [h, m] = (startTime || "0:0").split(":").map(Number);
    return h * 60 + m;
  }

  function daysWithContent() {
    return [...new Set(suggestions.map((s) => s.date))].sort();
  }

  function defaultDate() {
    const today = ctx.todayIso();
    const days = daysWithContent();
    return days.find((d) => d >= today) || days[days.length - 1] || null;
  }

  // Days before today can no longer be picked; a pick can get feedback from
  // its own day on (the server allows date <= today), but only nags afterwards.
  function isPast(s) {
    return s.date < ctx.todayIso();
  }

  function canGiveFeedback(s) {
    return s.picked && s.date <= ctx.todayIso();
  }

  function feedbackDue() {
    return suggestions.filter((s) => s.picked && isPast(s) && !s.feedback);
  }

  function passesTimeFilter(s) {
    if (timeFilter.size === 0) return true;
    const m = minutesOf(s.start_time);
    return TIME_BUCKETS.some((b) => timeFilter.has(b.id) && b.test(m));
  }

  function dayCards(date) {
    const cards = suggestions.filter((s) => s.date === date && passesTimeFilter(s));
    const key = sortBy === "time" ? (s) => [minutesOf(s.start_time), s.rank] : (s) => [s.rank, minutesOf(s.start_time)];
    return cards.sort((a, b) => {
      const ka = key(a);
      const kb = key(b);
      return ka[0] - kb[0] || ka[1] - kb[1];
    });
  }

  // How many picks fall at this venue in the suggestion's calendar month,
  // counting only picks up to (and including) its date.
  function visitNumber(s) {
    const month = s.date.slice(0, 7);
    return suggestions.filter(
      (o) => o.picked && o.venue_id === s.venue_id && o.date.slice(0, 7) === month && o.date <= s.date,
    ).length;
  }

  function ordinal(n) {
    const suffix = n % 10 === 1 && n % 100 !== 11 ? "st" : n % 10 === 2 && n % 100 !== 12 ? "nd" : n % 10 === 3 && n % 100 !== 13 ? "rd" : "th";
    return `${n}${suffix}`;
  }

  function formatDay(iso, opts = { weekday: "short", day: "numeric", month: "short" }) {
    return new Date(iso + "T00:00:00").toLocaleDateString("en-GB", opts);
  }

  // ── Rendering ──

  function render() {
    if (!active) return;
    const container = document.getElementById("plan-view");
    const esc = ctx.esc;
    updateStats();

    if (state === "loading" && suggestions.length === 0) {
      container.innerHTML = `<div class="loading">Loading plan…</div>`;
      renderMarkers([]);
      return;
    }
    if (state === "token") {
      container.innerHTML = `
        <div class="plan-empty">
          <p>${esc(errorMessage)}</p>
          <button type="button" class="plan-button primary" data-action="token">Enter token</button>
        </div>`;
      renderMarkers([]);
      return;
    }
    if (state === "error") {
      container.innerHTML = `
        <div class="plan-empty">
          <p>Could not load the plan: ${esc(errorMessage)}</p>
          <button type="button" class="plan-button" data-action="retry">Retry</button>
        </div>`;
      renderMarkers([]);
      return;
    }
    if (suggestions.length === 0) {
      container.innerHTML = `
        <div class="plan-empty">
          <p>No suggestions yet.</p>
          <p class="plan-hint">Run <code>/plan-courses</code> in Claude Code to get ranked suggestions for the next two weeks.</p>
        </div>`;
      renderMarkers([]);
      return;
    }

    const due = feedbackDue();
    const bannerHtml = due.length
      ? `<div class="plan-banner"><strong>Feedback due:</strong> ${due
          .map(
            (s) =>
              `<button type="button" class="plan-link" data-action="goto-feedback" data-id="${s.id}">${esc(s.title)} (${esc(formatDay(s.date))})</button>`,
          )
          .join(", ")}</div>`
      : "";

    const chipsHtml = TIME_BUCKETS.map(
      (b) =>
        `<button type="button" class="plan-chip${timeFilter.has(b.id) ? " on" : ""}" data-action="time" data-bucket="${b.id}">${b.label}</button>`,
    ).join("");
    const toolbarHtml = `
      <div class="plan-toolbar">
        <div class="plan-chips">${chipsHtml}</div>
        <button type="button" class="plan-chip" data-action="sort" title="Change sort order">Sort: ${sortBy === "rank" ? "best first" : "by time"}</button>
      </div>`;

    const today = ctx.todayIso();
    const stripHtml = daysWithContent()
      .map((d) => {
        const ofDay = suggestions.filter((s) => s.date === d);
        const pick = ofDay.find((s) => s.picked);
        const needsFeedback = pick && isPast(pick) && !pick.feedback;
        const classes = ["plan-day"];
        if (d === selectedDate) classes.push("selected");
        if (d === today) classes.push("today");
        if (d < today) classes.push("past");
        if (pick) classes.push("has-pick");
        return `
          <button type="button" class="${classes.join(" ")}" data-action="day" data-date="${d}">
            <span class="dow">${esc(formatDay(d, { weekday: "short" }))}${needsFeedback ? '<i class="plan-dot" title="Feedback due"></i>' : ""}</span>
            <span class="num">${esc(formatDay(d, { day: "numeric", month: "short" }))}</span>
            <span class="what">${pick ? esc(pick.venue_name) : `${ofDay.filter((s) => !s.not_interested).length} options`}</span>
          </button>`;
      })
      .join("");

    const cards = selectedDate ? dayCards(selectedDate) : [];
    const visible = cards.filter((s) => showDismissed || !s.not_interested);
    const dismissedCount = cards.length - cards.filter((s) => !s.not_interested).length;
    const cardsHtml = visible.length
      ? visible.map(cardHtml).join("")
      : `<div class="plan-empty"><p>No suggestions match the time filter on this day.</p></div>`;
    const dismissedHtml = dismissedCount
      ? `<button type="button" class="show-more" data-action="toggle-dismissed">${showDismissed ? "Hide" : "Show"} ${dismissedCount} not interested</button>`
      : "";

    container.innerHTML = `
      ${bannerHtml}
      ${toolbarHtml}
      <div class="plan-days">${stripHtml}</div>
      ${selectedDate ? `<div class="course-date-header">${esc(formatDay(selectedDate, { weekday: "long", day: "numeric", month: "long" }))}</div>` : ""}
      <div class="plan-cards">${cardsHtml}</div>
      ${dismissedHtml}`;

    container.querySelectorAll("form.plan-feedback").forEach(syncFeedbackForm);
    // Travel times for every suggestion: precomputed when the run was written,
    // so this is normally a cache hit; chips fill in place.
    ctx.requestTravel(suggestions.map((s) => s.venue_id));
    const expanded = visible.find((s) => s.id === expandedId);
    const expandedCard = expanded && container.querySelector(`.plan-card[data-id="${expanded.id}"]`);
    if (expandedCard) ctx.expandCourseDetails(expandedCard, asCourse(expanded));
    container.querySelector(".plan-day.selected")?.scrollIntoView({ block: "nearest", inline: "center" });
    renderMarkers(visible);
  }

  function cardHtml(s) {
    const esc = ctx.esc;
    const spots =
      s.free_spots == null
        ? ""
        : `<span class="course-spots ${s.free_spots > 0 ? "available" : "full"}">${s.free_spots > 0 ? `${s.free_spots} left` : "full"}</span>`;
    const tags = s.tags.length ? `<div class="plan-tags">${s.tags.map((t) => `<span class="plan-tag">${esc(t)}</span>`).join("")}</div>` : "";
    const limit = ctx.planVisitLimit(s.venue_id);
    const limitText = limit != null ? ` (limit ${limit}/month)` : "";
    // Picked: this pick's visit number. Not picked: picks already at the venue.
    const n = s.picked
      ? visitNumber(s)
      : suggestions.filter((o) => o.picked && o.venue_id === s.venue_id && o.date.slice(0, 7) === s.date.slice(0, 7)).length;
    const overLimit = limit != null && (s.picked ? n > limit : n >= limit);
    let visitNote = "";
    if (s.picked && (n > 1 || overLimit)) {
      visitNote = `${ordinal(n)} visit this month at ${esc(s.venue_name)}${limitText}`;
    } else if (!s.picked && n > 0) {
      visitNote = `Already picked ${n}× this month at ${esc(s.venue_name)}${limitText}`;
    }
    const classes = ["plan-card"];
    if (s.picked) classes.push("picked");
    if (s.not_interested) classes.push("dismissed");
    const past = isPast(s);
    return `
      <div class="${classes.join(" ")}" data-id="${s.id}">
        <div class="plan-card-head">
          <span class="plan-rank">#${s.rank}</span>
          <span class="course-time">${esc(s.start_time)} – ${esc(s.end_time)}</span>
          ${spots}
        </div>
        <div class="course-title">${esc(s.title)}${ctx.courseBadgesHtml(asCourse(s))}</div>
        <div class="course-venue">${esc(s.venue_name)}${s.district ? " · " + esc(s.district) : ""}${s.category ? " · " + esc(s.category) : ""}</div>
        ${ctx.travelChipsHtml(s.venue_id)}
        ${s.reason ? `<div class="plan-reason">${esc(s.reason)}</div>` : ""}
        ${tags}
        ${visitNote ? `<div class="plan-visit-note${overLimit ? " over" : ""}">${visitNote}</div>` : ""}
        <div class="plan-actions">
          ${past ? "" : `<button type="button" class="plan-button${s.picked ? " primary" : ""}" data-action="pick">${s.picked ? "Picked ✓" : "Pick"}</button>`}
          ${s.picked || past ? "" : `<button type="button" class="plan-button" data-action="dismiss">${s.not_interested ? "Undo not interested" : "✕ Not interested"}</button>`}
        </div>
        ${canGiveFeedback(s) ? feedbackHtml(s) : ""}
      </div>`;
  }

  function label(options, value) {
    return (options.find(([v]) => v === value) || [null, value])[1];
  }

  function feedbackHtml(s) {
    const esc = ctx.esc;
    const fb = s.feedback;
    if (fb && openFeedbackId !== s.id) {
      const parts = [label(WENT, fb.went)];
      if (fb.no_reason) parts.push(label(NO_REASONS, fb.no_reason).toLowerCase());
      if (fb.rating) parts.push("★".repeat(fb.rating) + "☆".repeat(5 - fb.rating));
      if (fb.again) parts.push(`again: ${label(AGAIN, fb.again).toLowerCase()}${fb.again_after ? " " + label(AGAIN_AFTER, fb.again_after) : ""}`);
      return `
        <div class="plan-feedback-summary">
          <span>${esc(parts.join(" · "))}</span>
          <button type="button" class="plan-link" data-action="edit-feedback">Edit</button>
          ${fb.note ? `<div class="plan-reason">“${esc(fb.note)}”</div>` : ""}
        </div>`;
    }
    const f = fb || {};
    const radios = (name, options, current) =>
      options
        .map(
          ([v, text]) =>
            `<label class="plan-chip-radio"><input type="radio" name="${name}-${s.id}" value="${v}"${current === v ? " checked" : ""}><span>${esc(text)}</span></label>`,
        )
        .join("");
    // Reverse DOM order + row-reverse layout, so CSS "~" can light the lower stars.
    const stars = [5, 4, 3, 2, 1]
      .map(
        (n) =>
          `<label class="plan-star"><input type="radio" name="rating-${s.id}" value="${n}"${f.rating === n ? " checked" : ""}><span title="${n}">★</span></label>`,
      )
      .join("");
    return `
      <form class="plan-feedback" data-id="${s.id}">
        <div class="plan-field">How did it go?<div class="plan-radios">${radios("went", WENT, f.went)}</div></div>
        <div class="plan-field" data-when="went=no">Why not?<div class="plan-radios">${radios("no_reason", NO_REASONS, f.no_reason)}</div></div>
        <div class="plan-field" data-when="went=yes">How much did you like it?<div class="plan-stars">${stars}</div></div>
        <div class="plan-field" data-when="went=any">Try it again?<div class="plan-radios">${radios("again", AGAIN, f.again)}</div></div>
        <div class="plan-field" data-when="again=maybe,yes">When?<div class="plan-radios">${radios("again_after", AGAIN_AFTER, f.again_after)}</div></div>
        <label class="plan-field">Note <textarea name="note" rows="2" placeholder="Teacher, crowd, level, anything worth remembering">${esc(f.note || "")}</textarea></label>
        <div class="plan-actions">
          <button type="submit" class="plan-button primary">Save feedback</button>
          ${fb ? '<button type="button" class="plan-button" data-action="cancel-feedback">Cancel</button>' : ""}
        </div>
      </form>`;
  }

  // Show only the feedback fields that apply to the current answers.
  function syncFeedbackForm(form) {
    const id = form.dataset.id;
    const value = (name) => form.querySelector(`input[name="${name}-${id}"]:checked`)?.value || null;
    const answers = { went: value("went"), again: value("again") };
    for (const field of form.querySelectorAll("[data-when]")) {
      const [key, allowed] = field.dataset.when.split("=");
      const current = answers[key];
      field.hidden = allowed === "any" ? current == null : !allowed.split(",").includes(current);
    }
    form.querySelector('button[type="submit"]').disabled = !answers.went;
  }

  function readFeedbackForm(form) {
    const id = form.dataset.id;
    const value = (name) => form.querySelector(`input[name="${name}-${id}"]:checked`)?.value || null;
    const went = value("went");
    const again = value("again");
    const rating = value("rating");
    return {
      went,
      no_reason: went === "no" ? value("no_reason") : null,
      rating: went === "yes" && rating ? Number(rating) : null,
      again,
      again_after: again === "maybe" || again === "yes" ? value("again_after") : null,
      note: form.querySelector("textarea[name=note]").value.trim(),
    };
  }

  function renderMarkers(cards) {
    layer.clearLayers();
    markers.clear();
    for (const s of cards) {
      if (s.lat == null || s.lng == null) continue;
      const marker = L.marker([s.lat, s.lng], {
        icon: L.divIcon({
          className: `plan-marker${s.picked ? " picked" : ""}`,
          html: `<span>${s.rank}</span>`,
          iconSize: [26, 26],
          iconAnchor: [13, 13],
        }),
      });
      marker.bindPopup(
        `<div class="popup-name">${ctx.esc(s.title)}</div>
         <div class="popup-address">${ctx.esc(s.start_time)} – ${ctx.esc(s.end_time)} · ${ctx.esc(s.venue_name)}</div>`,
        ctx.popupOptions(),
      );
      marker.addTo(layer);
      markers.set(s.id, marker);
    }
  }

  function fitToDay() {
    const points = [...markers.values()].map((m) => m.getLatLng());
    if (points.length) ctx.map.fitBounds(L.latLngBounds(points), { padding: [40, 40], maxZoom: 14 });
  }

  function updateStats() {
    const stats = document.getElementById("stats");
    if (state !== "ready") {
      stats.textContent = "Plan";
      return;
    }
    const upcoming = suggestions.filter((s) => s.date >= ctx.todayIso());
    const pickedDays = upcoming.filter((s) => s.picked).length;
    const days = new Set(upcoming.map((s) => s.date)).size;
    stats.textContent = `${pickedDays} of ${days} upcoming days planned`;
  }

  // ── Events (delegated, bound once) ──

  function onClick(e) {
    const actionEl = e.target.closest("[data-action]");
    const card = e.target.closest(".plan-card");
    const sid = Number(actionEl?.dataset.id || card?.dataset.id);
    const s = suggestions.find((x) => x.id === sid);
    const action = actionEl?.dataset.action;

    switch (action) {
      case "token":
        askForToken();
        return;
      case "retry":
        load();
        return;
      case "time": {
        const bucket = actionEl.dataset.bucket;
        if (timeFilter.has(bucket)) timeFilter.delete(bucket);
        else timeFilter.add(bucket);
        writeStored(TIME_FILTER_KEY, [...timeFilter]);
        render();
        return;
      }
      case "sort":
        sortBy = sortBy === "rank" ? "time" : "rank";
        writeStored(SORT_KEY, sortBy);
        render();
        return;
      case "day":
        selectedDate = actionEl.dataset.date;
        render();
        fitToDay();
        return;
      case "toggle-dismissed":
        showDismissed = !showDismissed;
        render();
        return;
      case "goto-feedback":
        if (!s) return;
        selectedDate = s.date;
        openFeedbackId = s.id;
        render();
        document.querySelector(`.plan-card[data-id="${s.id}"]`)?.scrollIntoView({ block: "center" });
        return;
      case "pick":
        if (s) mutate(`/suggestions/${s.id}`, { method: "PATCH", body: JSON.stringify({ picked: !s.picked }) });
        return;
      case "dismiss":
        if (s) mutate(`/suggestions/${s.id}`, { method: "PATCH", body: JSON.stringify({ not_interested: !s.not_interested }) });
        return;
      case "edit-feedback":
        openFeedbackId = sid;
        render();
        return;
      case "cancel-feedback":
        openFeedbackId = null;
        render();
        return;
    }

    // A plain tap on a card (not on its controls) opens its course details
    // and shows it on the map, like a course in the Courses list. On mobile,
    // focusing the map would drop the sheet over the details just opened, so
    // there the first tap only expands and a second one collapses + focuses.
    if (card && s && !e.target.closest("form, button, label, textarea, input")) {
      if (expandedId !== s.id) {
        expandedId = s.id;
        ctx.expandCourseDetails(card, asCourse(s));
        if (ctx.isMobileLayout()) return;
      } else {
        expandedId = null;
        ctx.collapseCourseItem(card);
      }
      focusOnMap(s);
    }
  }

  function focusOnMap(s) {
    const marker = markers.get(s.id);
    if (!marker) return;
    ctx.collapseSheetForMap();
    ctx.map.setView(marker.getLatLng(), Math.max(ctx.map.getZoom(), 15));
    marker.openPopup();
  }

  function onChange(e) {
    const form = e.target.closest("form.plan-feedback");
    if (form) syncFeedbackForm(form);
  }

  function onSubmit(e) {
    const form = e.target.closest("form.plan-feedback");
    if (!form) return;
    e.preventDefault();
    const sid = Number(form.dataset.id);
    openFeedbackId = null;
    mutate(`/suggestions/${sid}/feedback`, { method: "PUT", body: JSON.stringify(readFeedbackForm(form)) });
  }

  // ── Public interface ──

  // ctx: helpers from app.js (map, esc, date helpers, sheet helpers, course
  // badges/details and travel chips), see the planner.init() call there.
  async function init(context) {
    ctx = context;
    layer = L.layerGroup();
    const container = document.getElementById("plan-view");
    container.addEventListener("click", onClick);
    container.addEventListener("change", onChange);
    container.addEventListener("submit", onSubmit);
    try {
      const res = await fetch(`${API_BASE}/api/planner/status`);
      const { enabled } = await res.json();
      document.querySelector('.view-tab[data-view="plan"]').hidden = !enabled;
    } catch {
      /* planner unavailable: keep the tab hidden */
    }
  }

  function show() {
    active = true;
    layer.addTo(ctx.map);
    if (!readStored(TOKEN_KEY, null)) {
      state = "token";
      errorMessage = "A planner token is needed.";
      render();
      askForToken();
      return;
    }
    load().then(fitToDay);
  }

  function hide() {
    active = false;
    if (layer) ctx.map.removeLayer(layer);
  }

  return { init, show, hide };
})();
