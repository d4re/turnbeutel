const API_BASE = window.location.port === "8000" ? "" : "http://localhost:8000";

let tierConfig = {};
let map, markerCluster, allVenues, filteredVenues;

// Courses view state
let currentView = "venues";
let allCourses = [];
let filteredCourses = [];
let coursesLoadToken = 0;
let courseMarkers = new Map();

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

// ── Init ──

async function init() {
  document.getElementById("venue-list").innerHTML =
    '<div class="loading">Loading venue data...</div>';

  map = L.map("map").setView([52.52, 13.405], 11);
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

  try {
    let resp;
    try {
      resp = await fetch(`${API_BASE}/api/venues`);
      if (!resp.ok) throw new Error(`API ${resp.status}`);
    } catch {
      // Fallback to static JSON if backend is unavailable
      resp = await fetch("../data/venues_final.json");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    }
    const data = await resp.json();
    allVenues = data.venues;
    tierConfig = data.tier_config;
    populateFilters(allVenues);
    updateSliderLabels();
    updateSliderFill();
    applyFilters();
  } catch (err) {
    document.getElementById("venue-list").innerHTML =
      `<div class="loading">Error loading data: ${err.message}<br>Start the backend server or check the static data file.</div>`;
  }

  // Courses view is independent from the venues fetch — always wire it up.
  initCoursesView();
}

// ── Courses view ──

function initCoursesView() {
  // Default date = today
  const today = new Date().toISOString().slice(0, 10);
  const startEl = document.getElementById("course-date-start");
  const endEl = document.getElementById("course-date-end");
  startEl.value = today;
  endEl.value = today;

  // Bind view tabs
  document.querySelectorAll(".view-tab").forEach((btn) =>
    btn.addEventListener("click", () => switchView(btn.dataset.view))
  );

  // Bind filters that require re-fetching
  startEl.addEventListener("change", fetchCourses);
  endEl.addEventListener("change", fetchCourses);

  // Bind client-side filter events
  document.getElementById("category-filter").addEventListener("change", applyCourseFilters);
  document.getElementById("course-spots-filter").addEventListener("change", applyCourseFilters);
  document.getElementById("course-plus-filter").addEventListener("change", applyCourseFilters);
  document.getElementById("course-search-filter").addEventListener("input", applyCourseFilters);
  document.querySelectorAll("#time-toggle input").forEach((cb) =>
    cb.addEventListener("change", applyCourseFilters)
  );
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
  } else {
    if (allCourses.length === 0) {
      fetchCourses();
    } else {
      applyCourseFilters();
    }
  }
}

function daysBetween(start, end) {
  const s = new Date(start);
  const e = new Date(end);
  return Math.round((e - s) / 86400000);
}

async function fetchCourses() {
  const startDate = document.getElementById("course-date-start").value;
  let endDate = document.getElementById("course-date-end").value;
  if (!startDate) return;
  if (!endDate || endDate < startDate) {
    endDate = startDate;
    document.getElementById("course-date-end").value = startDate;
  }

  const days = Math.min(13, Math.max(1, daysBetween(startDate, endDate) + 1));

  const listEl = document.getElementById("course-list");
  listEl.innerHTML = '<div class="loading">Loading courses...</div>';

  const token = ++coursesLoadToken;
  try {
    const resp = await fetch(
      `${API_BASE}/api/courses?start_date=${startDate}&days=${days}`
    );
    const data = await resp.json().catch(() => ({}));
    if (token !== coursesLoadToken) return; // stale response
    if (!resp.ok) {
      throw new Error(data.detail || data.error || `API ${resp.status}`);
    }
    allCourses = data.courses || [];
    if (data.errors && data.errors.length > 0) {
      console.warn("Some days failed to load:", data.errors);
    }
    populateCategoryFilter(allCourses);
    applyCourseFilters();
  } catch (err) {
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

function getSelectedTimeSlots() {
  return [...document.querySelectorAll("#time-toggle input:checked")].map((cb) => cb.value);
}

function courseTimeSlot(startTime) {
  const hour = parseInt(startTime.split(":")[0], 10);
  if (isNaN(hour)) return null;
  if (hour < 12) return "morning";
  if (hour < 17) return "afternoon";
  return "evening";
}

function applyCourseFilters() {
  const timeSlots = getSelectedTimeSlots();
  const category = document.getElementById("category-filter").value;
  const spotsOnly = document.getElementById("course-spots-filter").checked;
  const plusOnly = document.getElementById("course-plus-filter").checked;
  const search = document.getElementById("course-search-filter").value.toLowerCase();

  filteredCourses = allCourses.filter((c) => {
    const slot = courseTimeSlot(c.start_time);
    if (!slot || !timeSlots.includes(slot)) return false;
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

function populateFilters(venues) {
  const districts = [...new Set(venues.map((v) => v.district).filter(Boolean))].sort();
  const distSelect = document.getElementById("district-filter");
  for (const d of districts) {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = d;
    distSelect.appendChild(opt);
  }

  const activities = [...new Set(venues.flatMap((v) => v.activities).filter(Boolean))].sort();
  const actSelect = document.getElementById("activity-filter");
  for (const a of activities) {
    const opt = document.createElement("option");
    opt.value = a;
    opt.textContent = a;
    actSelect.appendChild(opt);
  }

  // Bind events
  document.querySelectorAll('#membership-toggle input').forEach((r) =>
    r.addEventListener("change", () => {
      updateSliderLabels();
      updateSliderFill();
      applyFilters();
    })
  );
  document.getElementById("slider-min").addEventListener("input", onSliderChange);
  document.getElementById("slider-max").addEventListener("input", onSliderChange);
  distSelect.addEventListener("change", applyFilters);
  actSelect.addEventListener("change", applyFilters);
  document.getElementById("plus-filter").addEventListener("change", applyFilters);
  document.getElementById("coords-filter").addEventListener("change", applyFilters);
  document.getElementById("search-filter").addEventListener("input", applyFilters);
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
  const pctMin = (minVal / 3) * 100;
  const pctMax = (maxVal / 3) * 100;
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
      if (venue.has_coordinates && venue._marker) {
        map.setView([venue.lat, venue.lng], 15);
        venue._marker.openPopup();
      }
    });

    container.appendChild(item);
  }
}

function renderMap(venues) {
  markerCluster.clearLayers();
  const colors = getTierColors();
  for (const venue of venues) {
    venue._marker = null;
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

    venue._marker = marker;
    markerCluster.addLayer(marker);
  }
}

function buildPopup(venue) {
  const type = getMembershipType();
  const tiers = getVenueTiers(venue);
  const tiersHtml = tiers.map((t) => tierBadgeHtml(t)).join(" ");

  // Lazy-load visit limits from API once per venue. We track _detailLoaded
  // separately from visit_limits because a venue may legitimately have no
  // parseable limits (visit_limits === null), and we must not refetch in a loop.
  if (!venue._detailLoaded && !venue._detailLoading) {
    venue._detailLoading = true;
    fetch(`${API_BASE}/api/venues/${venue.address_id}`)
      .then((r) => r.ok ? r.json() : null)
      .then((detail) => {
        if (detail) {
          venue.visit_limits = detail.visit_limits;
          venue.bookingLimitsText = detail.bookingLimitsText;
        }
        venue._detailLoaded = true;
        venue._detailLoading = false;
        // Re-render popup if still open
        if (venue._marker && venue._marker.isPopupOpen()) {
          venue._marker.setPopupContent(buildPopup(venue));
        }
      })
      .catch(() => {
        venue._detailLoaded = true;
        venue._detailLoading = false;
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
  } else if (venue._detailLoading) {
    visitsHtml = '<div class="popup-visits" style="color:#999">Loading visit limits...</div>';
  }

  let addressText = "";
  if (venue.address) {
    addressText = `${venue.address.street}, ${venue.address.postal_code} ${venue.address.city}`;
  } else if (venue.street) {
    addressText = `${venue.district}, ${venue.street}`;
  }

  const plusHtml = venue.is_plus ? ' <span class="plus-badge">PLUS</span>' : "";

  return `
    <div class="popup-name"><a href="${esc(venue.url)}" target="_blank">${esc(venue.name)}</a>${plusHtml}</div>
    <div class="popup-address">${esc(addressText)}</div>
    <div class="popup-tiers">${tiersHtml}</div>
    ${visitsHtml}
    <div class="popup-activities">${esc(venue.activities.join(" \u00b7 "))}</div>
  `;
}

function esc(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

init();
