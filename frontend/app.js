const API_BASE = window.location.port === "8000" ? "" : "http://localhost:8000";

let tierConfig = {};
let map, markerCluster, allVenues, filteredVenues;

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
  let minVal = parseInt(minSlider.value);
  let maxVal = parseInt(maxSlider.value);

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
    let plusHtml = venue.is_plus ? '<span class="plus-badge">PLUS</span>' : "";

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
  const order = getTierOrder();

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
  let tiersHtml = tiers.map((t) => tierBadgeHtml(t)).join(" ");

  // Lazy-load visit limits from API if not yet available
  if (venue.visit_limits === undefined || venue.visit_limits === null) {
    if (!venue._detailLoading) {
      venue._detailLoading = true;
      fetch(`${API_BASE}/api/venues/${venue.address_id}`)
        .then((r) => r.ok ? r.json() : null)
        .then((detail) => {
          if (detail) {
            venue.visit_limits = detail.visit_limits;
            venue.bookingLimitsText = detail.bookingLimitsText;
          }
          venue._detailLoading = false;
          // Re-render popup if still open
          if (venue._marker && venue._marker.isPopupOpen()) {
            venue._marker.setPopupContent(buildPopup(venue));
          }
        })
        .catch(() => { venue._detailLoading = false; });
    }
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

  let plusHtml = venue.is_plus ? ' <span class="plus-badge">PLUS</span>' : "";

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
