// FastAPI mounts the frontend itself, so the page is always same-origin with
// the API. Relative URLs work for both `make serve` (dev) and the production
// container. Set this to a full URL only if you ever serve the frontend from
// a different origin during development.
const API_BASE = "";

let tierConfig = {};
let map, markerCluster, allVenues, filteredVenues;

// Multi-city state
let allCities = [];
let defaultCityId = 1;
const loadedVenueCities = new Set();
const loadedCourseCities = new Map(); // city_id -> Set<date-string>
const MIN_FETCH_ZOOM = 9;
const CENTROID_BBOX_HALF_DEG = 0.18; // ~20 km fallback until real bbox is known
const VIEWPORT_DEBOUNCE_MS = 200;
const LAST_VIEW_KEY = "usc.lastView";
// Time-of-day slider value domain: 0 = "Any" (no lower bound),
// 33 = "24:00" (no upper bound), 1..32 = 08:00..23:30 in 30-min steps.
const TIME_MIN_INDEX = 0;
const TIME_MAX_INDEX = 33;
let cityPinLayer = null;
let viewportDebounceTimer = null;

// Per-venue UI state, kept off the venue objects so the data layer stays pure.
const venueMarkers = new WeakMap();        // venue -> Leaflet marker
const venueDetailFetched = new Set();      // address_id whose detail has been fetched
const venueDetailInFlight = new Set();     // address_id currently being fetched

// Courses view state
let currentView = "venues";
let allCourses = [];
let filteredCourses = [];
let coursesLoaded = false;
let coursesLoadToken = 0;
let courseMarkers = new Map();

let courseStartDate = null; // ISO YYYY-MM-DD, selected range start
let courseEndDate = null;   // ISO YYYY-MM-DD, selected range end
const DATE_STRIP_DAYS = 14; // chips shown: today .. today+13

// Date-strip drag selection: a plain tap picks one day; pressing and dragging
// across chips picks a range. These track an in-progress drag.
let dateDragAnchor = null;  // ISO of the chip the current drag started on
let dateDragChanged = false; // did the selection change during this drag?
let dateDragPrevStart = null; // selection to restore if the drag is cancelled
let dateDragPrevEnd = null;

// Current state
function getMembershipType() {
  return document.querySelector('#membership-toggle input:checked').value;
}

function getTierOrder() {
  const type = getMembershipType();
  return tierConfig[type]?.order || [];
}

function getTierColors() {
  const type = getMembershipType();
  return tierConfig[type]?.colors || {};
}

function getTierDisplay(tierName) {
  const type = getMembershipType();
  const display = tierConfig[type]?.display;
  return display ? (display[tierName] || tierName) : tierName;
}

function getVenueTiers(venue) {
  const type = getMembershipType();
  return type === "corporate" ? venue.tiers_corporate : venue.tiers_private;
}

function getVenueMinTier(venue) {
  const type = getMembershipType();
  return type === "corporate" ? venue.min_tier_corporate : venue.min_tier_private;
}

function getTierIndex(tierName) {
  return getTierOrder().indexOf(tierName);
}

function loadLastView() {
  try {
    const raw = localStorage.getItem(LAST_VIEW_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (
      typeof parsed.lat === "number" &&
      typeof parsed.lng === "number" &&
      typeof parsed.zoom === "number"
    ) {
      return parsed;
    }
  } catch {
    /* ignore malformed stored view */
  }
  return null;
}

function saveLastView() {
  const c = map.getCenter();
  localStorage.setItem(
    LAST_VIEW_KEY,
    JSON.stringify({ lat: c.lat, lng: c.lng, zoom: map.getZoom() }),
  );
}

// ── Init ──

async function init() {
  document.getElementById("venue-list").innerHTML =
    '<div class="loading">Loading city index...</div>';

  try {
    const citiesResp = await fetch(`${API_BASE}/api/cities`);
    if (!citiesResp.ok) throw new Error(`/api/cities ${citiesResp.status}`);
    const citiesData = await citiesResp.json();
    allCities = citiesData.cities || [];
    defaultCityId = citiesData.default_city_id ?? 1;
    tierConfig = {}; // filled on first /api/venues response
  } catch (err) {
    document.getElementById("venue-list").innerHTML =
      `<div class="loading">Error loading cities: ${esc(err.message)}</div>`;
    return;
  }

  const lastView = loadLastView();
  const defaultCity = allCities.find((c) => c.id === defaultCityId);
  const fallbackCenter =
    defaultCity && defaultCity.centroid_lat != null
      ? [defaultCity.centroid_lat, defaultCity.centroid_lng]
      : [52.52, 13.405];

  map = L.map("map").setView(
    lastView ? [lastView.lat, lastView.lng] : fallbackCenter,
    lastView ? lastView.zoom : 11,
  );
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(map);

  markerCluster = L.markerClusterGroup({
    maxClusterRadius: 50,
    spiderfyOnMaxZoom: true,
    disableClusteringAtZoom: 15,
  });
  map.addLayer(markerCluster);

  cityPinLayer = L.layerGroup();

  allVenues = [];
  bindFilterEvents();
  rebuildFilterOptions(allVenues);
  updateSliderLabels();
  updateSliderFill();

  map.on("moveend zoomend", scheduleViewportUpdate);

  // Fire the handler once synchronously for the initial view.
  onMapViewportChange();

  // Courses view is independent from the venues fetch — always wire it up.
  initCoursesView();
}

function scheduleViewportUpdate() {
  if (viewportDebounceTimer) clearTimeout(viewportDebounceTimer);
  viewportDebounceTimer = setTimeout(onMapViewportChange, VIEWPORT_DEBOUNCE_MS);
}

// ── Courses view ──

function initCoursesView() {
  // Default selection = today.
  courseStartDate = todayIso();
  courseEndDate = courseStartDate;
  renderDateStrip();

  // A date-strip drag can end (or be cancelled by a scroll) anywhere on the
  // page, so the release listeners live on the document.
  document.addEventListener("pointerup", endDateDrag);
  document.addEventListener("pointercancel", cancelDateDrag);

  // Bind view tabs
  document.querySelectorAll(".view-tab").forEach((btn) =>
    btn.addEventListener("click", () => switchView(btn.dataset.view))
  );

  // Bind client-side filter events
  document.getElementById("category-filter").addEventListener("change", applyCourseFilters);
  document.getElementById("course-spots-filter").addEventListener("change", applyCourseFilters);
  document.getElementById("course-plus-filter").addEventListener("change", applyCourseFilters);
  document.getElementById("course-search-filter").addEventListener("input", applyCourseFilters);

  initTimeSlider();
}

// Format a Date as a local YYYY-MM-DD. Unlike toISOString(), this does NOT
// shift into UTC, so "today" stays today in timezones ahead of UTC.
function localIso(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function todayIso() {
  return localIso(new Date());
}

// Add `n` days to an ISO date string, returning a new ISO date string.
function addDaysIso(iso, n) {
  const d = new Date(iso + "T00:00:00");
  d.setDate(d.getDate() + n);
  return localIso(d);
}

function renderDateStrip() {
  const strip = document.getElementById("date-strip");
  const today = todayIso();
  strip.innerHTML = "";
  for (let i = 0; i < DATE_STRIP_DAYS; i++) {
    const iso = addDaysIso(today, i);
    const d = new Date(iso + "T00:00:00");
    const dow = d.toLocaleDateString(undefined, { weekday: "short" });
    const chip = document.createElement("div");
    chip.className = "date-chip";
    chip.dataset.date = iso;
    chip.innerHTML =
      `<span class="dow">${dow}</span><span class="num">${d.getDate()}</span>`;
    chip.addEventListener("pointerdown", (e) => startDateDrag(e, iso));
    chip.addEventListener("pointerenter", () => extendDateDrag(iso));
    strip.appendChild(chip);
  }
  updateDateChipStates();
}

// A plain tap selects a single day; pressing and dragging across chips selects
// a range. Selection updates live during the drag and only triggers a fetch
// once, on release.
function startDateDrag(e, iso) {
  if (e.button !== undefined && e.button !== 0) return; // primary button only
  dateDragAnchor = iso;
  dateDragChanged = false;
  dateDragPrevStart = courseStartDate;
  dateDragPrevEnd = courseEndDate;
  setDateSelection(iso, iso);
}

function extendDateDrag(iso) {
  if (dateDragAnchor === null) return;
  const start = iso < dateDragAnchor ? iso : dateDragAnchor;
  const end = iso < dateDragAnchor ? dateDragAnchor : iso;
  setDateSelection(start, end);
}

function endDateDrag() {
  if (dateDragAnchor === null) return;
  dateDragAnchor = null;
  if (dateDragChanged) fetchCourses();
}

// Abort an in-progress drag (e.g. a touch that became a scroll) and restore the
// selection that was in effect before it started.
function cancelDateDrag() {
  if (dateDragAnchor === null) return;
  dateDragAnchor = null;
  if (dateDragChanged) {
    setDateSelection(dateDragPrevStart, dateDragPrevEnd);
    dateDragChanged = false;
  }
}

function setDateSelection(start, end) {
  if (start === courseStartDate && end === courseEndDate) return;
  courseStartDate = start;
  courseEndDate = end;
  dateDragChanged = true;
  updateDateChipStates();
}

function updateDateChipStates() {
  const today = todayIso();
  document.querySelectorAll("#date-strip .date-chip").forEach((chip) => {
    const iso = chip.dataset.date;
    chip.classList.toggle("today", iso === today);
    chip.classList.toggle("selected", iso === courseStartDate || iso === courseEndDate);
    chip.classList.toggle(
      "inrange",
      iso > courseStartDate && iso < courseEndDate,
    );
  });
}

function switchView(view) {
  if (view === currentView) return;
  currentView = view;

  document.querySelectorAll(".view-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === view)
  );
  document.getElementById("filters").style.display = view === "venues" ? "" : "none";
  document.getElementById("courses-filters").style.display = view === "courses" ? "" : "none";
  document.getElementById("venue-list").style.display = view === "venues" ? "" : "none";
  document.getElementById("course-list").style.display = view === "courses" ? "" : "none";

  if (view === "venues") {
    applyFilters();
  } else if (!coursesLoaded) {
    fetchCourses();
  } else {
    applyCourseFilters();
  }
}

function daysBetween(start, end) {
  const s = new Date(start);
  const e = new Date(end);
  return Math.round((e - s) / 86400000);
}

async function fetchCourses() {
  const startDate = courseStartDate;
  let endDate = courseEndDate;
  if (!startDate) return;
  if (!endDate || endDate < startDate) endDate = startDate;

  const days = Math.min(14, Math.max(1, daysBetween(startDate, endDate) + 1));
  const dateList = [];
  for (let i = 0; i < days; i++) {
    const d = new Date(startDate + "T00:00:00");
    d.setDate(d.getDate() + i);
    dateList.push(localIso(d));
  }

  const zoom = map.getZoom();
  const visible = zoom >= MIN_FETCH_ZOOM ? citiesInViewport(map.getBounds()) : [];

  const listEl = document.getElementById("course-list");
  if (visible.length === 0) {
    listEl.innerHTML =
      '<div class="loading">Zoom in on a city to load courses.</div>';
    allCourses = [];
    applyCourseFilters();
    return;
  }

  // Decide which (city_id, date) pairs still need fetching.
  const missingByCity = new Map();
  for (const cid of visible) {
    const cached = loadedCourseCities.get(cid) ?? new Set();
    const missing = dateList.filter((d) => !cached.has(d));
    if (missing.length > 0) missingByCity.set(cid, missing);
  }

  listEl.innerHTML = '<div class="loading">Loading courses...</div>';
  const token = ++coursesLoadToken;

  try {
    // One /api/courses call per city, covering that city's missing dates.
    // `missing` may be non-contiguous (e.g. the user shifted the date range
    // after a partial load), so request the whole [first..last] span — the
    // backend's per-(city,date) cache makes re-covering the middle cheap — and
    // filter the response to the dates we actually lacked so already-loaded
    // courses are never double-added.
    for (const [cid, missing] of missingByCity.entries()) {
      const spanStart = missing[0];
      const spanEnd = missing[missing.length - 1];
      const spanDays = daysBetween(spanStart, spanEnd) + 1;
      const missingSet = new Set(missing);
      const resp = await fetch(
        `${API_BASE}/api/courses?start_date=${spanStart}&days=${spanDays}&city_ids=${cid}`,
      );
      const data = await resp.json().catch(() => ({}));
      if (token !== coursesLoadToken) return;
      if (!resp.ok) throw new Error(data.detail || `API ${resp.status}`);
      const cached = loadedCourseCities.get(cid) ?? new Set();
      for (const d of missing) cached.add(d);
      loadedCourseCities.set(cid, cached);
      // `data.cities` is always length 1 (we queried a single city).
      for (const entry of data.cities || []) {
        for (const c of entry.courses || []) {
          if (!missingSet.has(c.date)) continue; // already loaded — skip
          c.city_id = entry.city_id;
          allCourses.push(c);
        }
      }
    }
    coursesLoaded = true;
    populateCategoryFilter(allCourses);
    applyCourseFilters();
  } catch (err) {
    if (token !== coursesLoadToken) return;
    coursesLoaded = true;
    listEl.innerHTML = `<div class="loading">Error loading courses: ${esc(err.message)}</div>`;
  }
}

function populateCategoryFilter(courses) {
  const sel = document.getElementById("category-filter");
  const current = sel.value;
  const cats = [...new Set(courses.map((c) => c.category).filter(Boolean))].sort();
  while (sel.firstChild) sel.removeChild(sel.firstChild);
  const allOpt = document.createElement("option");
  allOpt.value = "";
  allOpt.textContent = "All categories";
  sel.appendChild(allOpt);
  for (const c of cats) {
    const opt = document.createElement("option");
    opt.value = c;
    opt.textContent = c;
    sel.appendChild(opt);
  }
  if (cats.includes(current)) sel.value = current;
}

function applyCourseFilters() {
  const { lower: timeLower, upper: timeUpper } = timeFilterBounds();
  const category = document.getElementById("category-filter").value;
  const spotsOnly = document.getElementById("course-spots-filter").checked;
  const plusOnly = document.getElementById("course-plus-filter").checked;
  const search = document.getElementById("course-search-filter").value.toLowerCase();
  // Only show courses within the currently selected date range.
  const startDate = courseStartDate;
  const endDate = courseEndDate || courseStartDate;

  filteredCourses = allCourses.filter((c) => {
    if (c.date < startDate || c.date > endDate) return false;

    const startMin = startTimeToMinutes(c.start_time);
    if (startMin === null || startMin < timeLower || startMin > timeUpper) return false;
    if (category && c.category !== category) return false;
    if (spotsOnly && !(c.free_spots && c.free_spots > 0)) return false;
    if (plusOnly && !c.is_plus) return false;
    if (search) {
      const hay = (c.title + " " + c.venue_name + " " + (c.teacher || "")).toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });

  renderCourseList(filteredCourses);
  renderCourseMap(filteredCourses);
  updateCourseStats();
}

function updateCourseStats() {
  const statsEl = document.getElementById("stats");
  const venueCount = new Set(filteredCourses.map((c) => c.venue_id)).size;
  statsEl.textContent = `${filteredCourses.length} courses | ${venueCount} venues`;
}

function formatDateHeader(dateStr) {
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
  });
}

function renderCourseList(courses) {
  const container = document.getElementById("course-list");
  container.innerHTML = "";
  if (courses.length === 0) {
    container.innerHTML = '<div class="loading">No courses match your filters.</div>';
    return;
  }

  let currentDate = "";
  for (const course of courses) {
    if (course.date !== currentDate) {
      currentDate = course.date;
      const header = document.createElement("div");
      header.className = "course-date-header";
      header.textContent = formatDateHeader(course.date);
      container.appendChild(header);
    }

    const item = document.createElement("div");
    item.className = "course-item";
    item.dataset.courseId = course.id;
    item.dataset.venueId = course.venue_id;

    let spotsHtml = "";
    if (course.free_spots != null && course.max_spots != null) {
      const available = course.free_spots > 0;
      const cls = available ? "available" : "full";
      const text = available ? `${course.free_spots} / ${course.max_spots} spots` : "Full";
      spotsHtml = `<span class="course-spots ${cls}">${text}</span>`;
    }

    const plusHtml = course.is_plus ? ' <span class="plus-badge">PLUS</span>' : "";
    const teacherHtml = course.teacher ? ` \u00b7 ${esc(course.teacher)}` : "";

    item.innerHTML = `
      <div class="course-time">${esc(course.start_time)} – ${esc(course.end_time)}</div>
      <div class="course-title">${esc(course.title)}${plusHtml}</div>
      <div class="course-venue">${esc(course.venue_name)}${course.district ? " \u00b7 " + esc(course.district) : ""}</div>
      <div class="course-meta">${esc(course.category)}${teacherHtml} ${spotsHtml}</div>
    `;

    item.addEventListener("click", () => {
      container.querySelectorAll(".course-item.active").forEach((el) => el.classList.remove("active"));
      item.classList.add("active");
      focusCourseVenueOnMap(course);
    });

    container.appendChild(item);
  }
}

function focusCourseVenueOnMap(course) {
  if (!course.lat || !course.lng) return;
  map.setView([course.lat, course.lng], 16);
  const marker = courseMarkers.get(course.venue_id);
  if (marker) marker.openPopup();
}

function renderCourseMap(courses) {
  if (currentView !== "courses") return;
  markerCluster.clearLayers();
  courseMarkers = new Map();

  const venueMap = new Map();
  for (const c of courses) {
    if (!c.lat || !c.lng) continue;
    if (!venueMap.has(c.venue_id)) {
      venueMap.set(c.venue_id, {
        venue_id: c.venue_id,
        venue_name: c.venue_name,
        district: c.district,
        lat: c.lat,
        lng: c.lng,
        courses: [],
      });
    }
    venueMap.get(c.venue_id).courses.push(c);
  }

  for (const venueData of venueMap.values()) {
    const marker = L.circleMarker([venueData.lat, venueData.lng], {
      radius: 7,
      fillColor: "#4a90d9",
      color: "#fff",
      weight: 1.5,
      opacity: 1,
      fillOpacity: 0.85,
    });
    marker.bindPopup(() => buildCoursePopup(venueData));
    courseMarkers.set(venueData.venue_id, marker);
    markerCluster.addLayer(marker);
  }
}

function buildCoursePopup(venueData) {
  const coursesHtml = venueData.courses
    .map((c) => {
      const spots =
        c.free_spots != null
          ? ` \u00b7 <span class="course-spots ${c.free_spots > 0 ? "available" : "full"}">${c.free_spots > 0 ? c.free_spots + " free" : "full"}</span>`
          : "";
      return `
        <div class="popup-course">
          <div><span class="popup-course-time">${esc(c.start_time)}</span> <span class="popup-course-title">${esc(c.title)}</span></div>
          <div class="popup-course-meta">${esc(c.category)}${c.teacher ? " \u00b7 " + esc(c.teacher) : ""}${spots}</div>
        </div>
      `;
    })
    .join("");

  return `
    <div class="popup-name">${esc(venueData.venue_name)}</div>
    <div class="popup-address">${esc(venueData.district || "")}</div>
    <div class="popup-course-list">${coursesHtml}</div>
  `;
}

// ── Filters ──

function cityBounds(city) {
  if (city.lat_min != null && city.lat_max != null) {
    return {
      south: city.lat_min,
      north: city.lat_max,
      west: city.lng_min,
      east: city.lng_max,
    };
  }
  if (city.centroid_lat == null || city.centroid_lng == null) return null;
  return {
    south: city.centroid_lat - CENTROID_BBOX_HALF_DEG,
    north: city.centroid_lat + CENTROID_BBOX_HALF_DEG,
    west: city.centroid_lng - CENTROID_BBOX_HALF_DEG,
    east: city.centroid_lng + CENTROID_BBOX_HALF_DEG,
  };
}

function boundsIntersect(viewport, city) {
  if (!city) return false;
  return !(
    city.east < viewport.getWest() ||
    city.west > viewport.getEast() ||
    city.north < viewport.getSouth() ||
    city.south > viewport.getNorth()
  );
}

function citiesInViewport(viewport) {
  const center = viewport.getCenter();
  const matches = [];
  for (const city of allCities) {
    const cb = cityBounds(city);
    if (!cb) continue;
    if (boundsIntersect(viewport, cb)) {
      const dx = (city.centroid_lat ?? 0) - center.lat;
      const dy = (city.centroid_lng ?? 0) - center.lng;
      matches.push({ id: city.id, distSq: dx * dx + dy * dy });
    }
  }
  matches.sort((a, b) => a.distSq - b.distSq);
  return matches.map((m) => m.id);
}

async function fetchVenuesForCities(cityIds) {
  const query = cityIds.map((id) => `city_ids=${id}`).join("&");
  const resp = await fetch(`${API_BASE}/api/venues?${query}`);
  if (!resp.ok) throw new Error(`/api/venues ${resp.status}`);
  return resp.json();
}

function mergeVenuesResponse(data) {
  // Only set tier_config the first time — it's global.
  if (data.tier_config && Object.keys(tierConfig).length === 0) {
    tierConfig = data.tier_config;
  }
  for (const cityEntry of data.cities || []) {
    loadedVenueCities.add(cityEntry.city_id);
    for (const venue of cityEntry.venues || []) {
      // Venue ids are unique per city; tag with city_id for dedupe.
      venue.city_id = cityEntry.city_id;
      allVenues.push(venue);
    }
  }
  rebuildFilterOptions(allVenues);
  updateSliderLabels();
  updateSliderFill();
  applyFilters();
}

async function onMapViewportChange() {
  saveLastView();
  const zoom = map.getZoom();

  if (zoom < MIN_FETCH_ZOOM) {
    markerCluster.clearLayers();
    showCityPins();
    return;
  }
  hideCityPins();

  const visible = citiesInViewport(map.getBounds());
  const needed = visible.filter((id) => !loadedVenueCities.has(id));

  if (needed.length > 0) {
    try {
      const data = await fetchVenuesForCities(needed);
      mergeVenuesResponse(data);
    } catch (err) {
      console.warn("Venue fetch failed", err);
    }
  } else if (currentView === "venues") {
    applyFilters();
  }

  if (currentView === "courses") refreshCoursesForViewport();
}

function showCityPins() {
  if (!cityPinLayer) return;
  cityPinLayer.clearLayers();
  for (const city of allCities) {
    if (city.centroid_lat == null || city.centroid_lng == null) continue;
    const marker = L.circleMarker([city.centroid_lat, city.centroid_lng], {
      radius: 6,
      fillColor: "#4a90d9",
      color: "#fff",
      weight: 1.5,
      opacity: 1,
      fillOpacity: 0.9,
    });
    marker.bindTooltip(city.name, { permanent: false, direction: "top" });
    marker.on("click", () => {
      map.flyTo([city.centroid_lat, city.centroid_lng], MIN_FETCH_ZOOM);
    });
    cityPinLayer.addLayer(marker);
  }
  if (!map.hasLayer(cityPinLayer)) map.addLayer(cityPinLayer);
}

function hideCityPins() {
  if (cityPinLayer && map.hasLayer(cityPinLayer)) {
    map.removeLayer(cityPinLayer);
  }
}

function refreshCoursesForViewport() {
  if (currentView === "courses") fetchCourses();
}

function bindFilterEvents() {
  document.querySelectorAll("#membership-toggle input").forEach((r) =>
    r.addEventListener("change", () => {
      updateSliderLabels();
      updateSliderFill();
      applyFilters();
    }),
  );
  document.getElementById("slider-min").addEventListener("input", onSliderChange);
  document.getElementById("slider-max").addEventListener("input", onSliderChange);
  document.getElementById("district-filter").addEventListener("change", applyFilters);
  document.getElementById("activity-filter").addEventListener("change", applyFilters);
  document.getElementById("plus-filter").addEventListener("change", applyFilters);
  document.getElementById("coords-filter").addEventListener("change", applyFilters);
  document.getElementById("search-filter").addEventListener("input", applyFilters);
}

function rebuildFilterOptions(venues) {
  const distSelect = document.getElementById("district-filter");
  const actSelect = document.getElementById("activity-filter");
  const prevDist = distSelect.value;
  const prevAct = actSelect.value;

  // Each <select> ships a single hardcoded "All ..." placeholder option in
  // index.html; truncate back to just that, then repopulate from the full
  // accumulated venue set.
  distSelect.length = 1;
  actSelect.length = 1;

  const districts = [...new Set(venues.map((v) => v.district).filter(Boolean))].sort();
  for (const d of districts) {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = d;
    distSelect.appendChild(opt);
  }

  const activities = [...new Set(venues.flatMap((v) => v.activities).filter(Boolean))].sort();
  for (const a of activities) {
    const opt = document.createElement("option");
    opt.value = a;
    opt.textContent = a;
    actSelect.appendChild(opt);
  }

  // Preserve the user's current selection if it still exists post-rebuild.
  if (districts.includes(prevDist)) distSelect.value = prevDist;
  if (activities.includes(prevAct)) actSelect.value = prevAct;
}

// ── Slider ──

function onSliderChange() {
  const minSlider = document.getElementById("slider-min");
  const maxSlider = document.getElementById("slider-max");
  const minVal = parseInt(minSlider.value);
  const maxVal = parseInt(maxSlider.value);

  // Prevent crossing
  if (minVal > maxVal) {
    if (this === minSlider) {
      maxSlider.value = minVal;
    } else {
      minSlider.value = maxVal;
    }
  }

  updateSliderFill();
  updateSliderLabels();
  applyFilters();
}

function updateSliderLabels() {
  const order = getTierOrder();
  const container = document.getElementById("slider-labels");
  const minVal = parseInt(document.getElementById("slider-min").value);
  const maxVal = parseInt(document.getElementById("slider-max").value);

  container.innerHTML = order
    .map((t, i) => {
      const inRange = i >= minVal && i <= maxVal;
      return `<span class="slider-label${inRange ? " in-range" : ""}">${getTierDisplay(t)}</span>`;
    })
    .join("");

  // Description
  const descEl = document.getElementById("slider-description");
  const minName = getTierDisplay(order[minVal]);
  const maxName = getTierDisplay(order[maxVal]);
  if (minVal === 0 && maxVal === order.length - 1) {
    descEl.textContent = "Showing all venues";
  } else if (minVal === maxVal) {
    descEl.textContent = `Only ${minName}-exclusive venues`;
  } else if (minVal === 0) {
    descEl.textContent = `Everything available up to ${maxName}`;
  } else {
    descEl.textContent = `${minName} through ${maxName} (excluding lower tiers)`;
  }
}

function updateSliderFill() {
  const minVal = parseInt(document.getElementById("slider-min").value);
  const maxVal = parseInt(document.getElementById("slider-max").value);
  const fill = document.getElementById("slider-fill");
  const lastIdx = Math.max(1, getTierOrder().length - 1);
  const pctMin = (minVal / lastIdx) * 100;
  const pctMax = (maxVal / lastIdx) * 100;
  fill.style.left = pctMin + "%";
  fill.style.width = (pctMax - pctMin) + "%";
}

function applyFilters() {
  const minVal = parseInt(document.getElementById("slider-min").value);
  const maxVal = parseInt(document.getElementById("slider-max").value);
  const district = document.getElementById("district-filter").value;
  const activity = document.getElementById("activity-filter").value;
  const plusOnly = document.getElementById("plus-filter").checked;
  const coordsOnly = document.getElementById("coords-filter").checked;
  const search = document.getElementById("search-filter").value.toLowerCase();
  const order = getTierOrder();

  filteredVenues = allVenues.filter((v) => {
    const mt = getVenueMinTier(v);
    if (!mt) return false;
    const tierIdx = order.indexOf(mt);
    if (tierIdx < minVal || tierIdx > maxVal) return false;

    if (district && v.district !== district) return false;
    if (activity && !v.activities.includes(activity)) return false;
    if (plusOnly && !v.is_plus) return false;
    if (coordsOnly && !v.has_coordinates) return false;
    if (search && !v.name.toLowerCase().includes(search)) return false;

    return true;
  });

  renderList(filteredVenues);
  renderMap(filteredVenues);
  updateStats();
}

function updateStats() {
  const total = filteredVenues.length;
  const withCoords = filteredVenues.filter((v) => v.has_coordinates).length;
  const statsEl = document.getElementById("stats");
  if (total === allVenues.length) {
    statsEl.textContent = `${total} venues | ${withCoords} on map`;
  } else {
    statsEl.textContent = `Showing ${total} of ${allVenues.length} | ${withCoords} on map`;
  }
}

// ── Rendering ──

function tierBadgeHtml(tierName) {
  const idx = getTierIndex(tierName);
  const display = getTierDisplay(tierName);
  return `<span class="tier-badge tier-${idx >= 0 ? idx : 0}">${esc(display)}</span>`;
}

function renderList(venues) {
  const container = document.getElementById("venue-list");
  container.innerHTML = "";
  const type = getMembershipType();

  for (const venue of venues) {
    const item = document.createElement("div");
    item.className = "venue-item";
    item.dataset.slug = venue.slug;

    const tiers = getVenueTiers(venue);
    let ratingHtml = "";
    if (venue.rating) {
      ratingHtml = `<span class="venue-rating">${"\u2605".repeat(Math.round(venue.rating))} ${venue.rating}</span>`;
    }
    const plusHtml = venue.is_plus ? '<span class="plus-badge">PLUS</span>' : "";

    // Show visit limit for min tier
    let visitHtml = "";
    if (venue.visit_limits) {
      const limits = venue.visit_limits[type] || {};
      const minT = getVenueMinTier(venue);
      if (minT && limits[minT] != null) {
        visitHtml = `<div class="venue-visits">${getTierDisplay(minT)}: ${limits[minT]}x / month</div>`;
      }
    }

    item.innerHTML = `
      <div class="venue-name">${esc(venue.name)}${plusHtml}</div>
      <div class="venue-meta">${esc(venue.district)}${venue.street ? " \u00b7 " + esc(venue.street) : ""} ${ratingHtml}</div>
      <div class="venue-tiers">${tiers.map((t) => tierBadgeHtml(t)).join("")}</div>
      ${visitHtml}
      <div class="venue-activities">${esc(venue.activities.join(" \u00b7 "))}</div>
    `;

    item.addEventListener("click", () => {
      container.querySelectorAll(".venue-item.active").forEach((el) => el.classList.remove("active"));
      item.classList.add("active");
      const marker = venueMarkers.get(venue);
      if (venue.has_coordinates && marker) {
        map.setView([venue.lat, venue.lng], 15);
        marker.openPopup();
      }
    });

    container.appendChild(item);
  }
}

function renderMap(venues) {
  if (currentView !== "venues") return;
  markerCluster.clearLayers();
  const colors = getTierColors();
  for (const venue of venues) {
    if (!venue.has_coordinates) continue;

    const mt = getVenueMinTier(venue);
    const color = colors[mt] || "#999";
    const marker = L.circleMarker([venue.lat, venue.lng], {
      radius: 7,
      fillColor: color,
      color: "#fff",
      weight: 1.5,
      opacity: 1,
      fillOpacity: 0.85,
    });

    marker.bindPopup(() => buildPopup(venue));
    marker.on("click", () => {
      const listItem = document.querySelector(`.venue-item[data-slug="${venue.slug}"]`);
      if (listItem) {
        document.querySelectorAll(".venue-item.active").forEach((el) => el.classList.remove("active"));
        listItem.classList.add("active");
        listItem.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    });

    venueMarkers.set(venue, marker);
    markerCluster.addLayer(marker);
  }
}

function buildPopup(venue) {
  const type = getMembershipType();
  const tiers = getVenueTiers(venue);
  const tiersHtml = tiers.map((t) => tierBadgeHtml(t)).join(" ");

  // Lazy-load visit limits from API once per venue. We track "fetched"
  // separately from visit_limits because a venue may legitimately have no
  // parseable limits (visit_limits === null), and we must not refetch in a loop.
  const vid = venue.address_id;
  if (!venueDetailFetched.has(vid) && !venueDetailInFlight.has(vid)) {
    venueDetailInFlight.add(vid);
    fetch(`${API_BASE}/api/venues/${vid}`)
      .then((r) => r.ok ? r.json() : null)
      .then((detail) => {
        if (detail) {
          venue.visit_limits = detail.visit_limits;
          venue.bookingLimitsText = detail.bookingLimitsText;
        }
        venueDetailFetched.add(vid);
        venueDetailInFlight.delete(vid);
        const marker = venueMarkers.get(venue);
        if (marker && marker.isPopupOpen()) {
          marker.setPopupContent(buildPopup(venue));
        }
      })
      .catch(() => {
        venueDetailFetched.add(vid);
        venueDetailInFlight.delete(vid);
      });
  }

  // Visit limits table
  let visitsHtml = "";
  if (venue.visit_limits) {
    const limits = venue.visit_limits[type] || {};
    const order = getTierOrder();
    const rows = order
      .map((t) => {
        const val = limits[t];
        if (val === undefined) return "";
        const display = val === null
          ? '<span style="color:#ccc">Not included</span>'
          : `${val} / month`;
        return `<tr><td>${esc(getTierDisplay(t))}</td><td>${display}</td></tr>`;
      })
      .filter(Boolean)
      .join("");
    if (rows) {
      const label = type === "corporate" ? "Corporate" : "Private";
      visitsHtml = `<div class="popup-visits"><strong>Visit limits (${label})</strong><table>${rows}</table></div>`;
    }
  } else if (venueDetailInFlight.has(vid)) {
    visitsHtml = '<div class="popup-visits" style="color:#999">Loading visit limits...</div>';
  }

  let addressText = "";
  if (venue.address) {
    addressText = `${venue.address.street}, ${venue.address.postal_code} ${venue.address.city}`;
  } else if (venue.street) {
    addressText = `${venue.district}, ${venue.street}`;
  }

  const plusHtml = venue.is_plus ? ' <span class="plus-badge">PLUS</span>' : "";
  const safeHref = isHttpUrl(venue.url) ? venue.url : null;
  const nameHtml = safeHref
    ? `<a href="${esc(safeHref)}" target="_blank" rel="noopener noreferrer">${esc(venue.name)}</a>`
    : esc(venue.name);

  return `
    <div class="popup-name">${nameHtml}${plusHtml}</div>
    <div class="popup-address">${esc(addressText)}</div>
    <div class="popup-tiers">${tiersHtml}</div>
    ${visitsHtml}
    <div class="popup-activities">${esc(venue.activities.join(" \u00b7 "))}</div>
  `;
}

function isHttpUrl(str) {
  return typeof str === "string" && /^https?:\/\//i.test(str);
}

function esc(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

init();

function timeIndexToMinutes(i) {
  // Concrete clock time for an INNER index (1..32). Do not call with 0 or 33 —
  // those are the open-ended "Any" / "24:00" sentinels handled by the callers.
  return 480 + (i - 1) * 30; // index 1 -> 08:00 (480), index 33 -> 24:00 (1440)
}

function formatTimeIndex(i) {
  if (i <= TIME_MIN_INDEX) return "Any";
  const mins = timeIndexToMinutes(i);
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function initTimeSlider() {
  document.getElementById("time-slider-min").addEventListener("input", onTimeSliderChange);
  document.getElementById("time-slider-max").addEventListener("input", onTimeSliderChange);
  updateTimeSliderFill();
  updateTimeSliderDescription();
}

function onTimeSliderChange() {
  const minSlider = document.getElementById("time-slider-min");
  const maxSlider = document.getElementById("time-slider-max");
  const minVal = parseInt(minSlider.value);
  const maxVal = parseInt(maxSlider.value);

  // Prevent the handles from crossing (same idiom as the tier slider).
  if (minVal > maxVal) {
    if (this === minSlider) {
      maxSlider.value = minVal;
    } else {
      minSlider.value = maxVal;
    }
  }

  updateTimeSliderFill();
  updateTimeSliderDescription();
  applyCourseFilters();
}

function updateTimeSliderFill() {
  const minVal = parseInt(document.getElementById("time-slider-min").value);
  const maxVal = parseInt(document.getElementById("time-slider-max").value);
  const fill = document.getElementById("time-slider-fill");
  const pctMin = (minVal / TIME_MAX_INDEX) * 100;
  const pctMax = (maxVal / TIME_MAX_INDEX) * 100;
  fill.style.left = pctMin + "%";
  fill.style.width = (pctMax - pctMin) + "%";
}

function updateTimeSliderDescription() {
  const minVal = parseInt(document.getElementById("time-slider-min").value);
  const maxVal = parseInt(document.getElementById("time-slider-max").value);
  const descEl = document.getElementById("time-slider-description");
  const openLow = minVal <= TIME_MIN_INDEX;
  const openHigh = maxVal >= TIME_MAX_INDEX;
  if (openLow && openHigh) {
    descEl.textContent = "Showing courses at any time";
  } else if (openLow) {
    descEl.textContent = `Showing courses starting up to ${formatTimeIndex(maxVal)}`;
  } else if (openHigh) {
    descEl.textContent = `Showing courses starting from ${formatTimeIndex(minVal)}`;
  } else if (minVal === maxVal) {
    descEl.textContent = `Showing courses starting at ${formatTimeIndex(minVal)}`;
  } else {
    descEl.textContent =
      `Showing courses starting ${formatTimeIndex(minVal)} – ${formatTimeIndex(maxVal)}`;
  }
}

// Returns {lower, upper} bounds in minutes-of-day; open extremes use ±Infinity.
function timeFilterBounds() {
  const minVal = parseInt(document.getElementById("time-slider-min").value);
  const maxVal = parseInt(document.getElementById("time-slider-max").value);
  return {
    lower: minVal <= TIME_MIN_INDEX ? -Infinity : timeIndexToMinutes(minVal),
    upper: maxVal >= TIME_MAX_INDEX ? Infinity : timeIndexToMinutes(maxVal),
  };
}

// Parse an "HH:MM" (or "HH:MM:SS") start_time string to minutes-of-day.
function startTimeToMinutes(startTime) {
  if (!startTime || startTime.length < 5) return null;
  const h = parseInt(startTime.slice(0, 2), 10);
  const m = parseInt(startTime.slice(3, 5), 10);
  if (isNaN(h) || isNaN(m)) return null;
  return h * 60 + m;
}
