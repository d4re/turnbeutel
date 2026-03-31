// Mapping from private tier names (in scraped data) to corporate display names
const PRIVATE_TO_CORPORATE = {
  Essential: "S",
  Classic: "M Pro",
  Premium: "L Pro",
  Max: "XL Pro",
};

const TIER_COLORS = {
  Essential: "#27ae60",
  Classic: "#2980b9",
  Premium: "#e67e22",
  Max: "#c0392b",
};

const TIER_ORDER_PRIVATE = ["Essential", "Classic", "Premium", "Max"];

function corpName(privateTier) {
  return PRIVATE_TO_CORPORATE[privateTier] || privateTier;
}

let map, markerCluster, allVenues, filteredVenues;

async function init() {
  document.getElementById("venue-list").innerHTML =
    '<div class="loading">Loading venue data...</div>';

  // Init map centered on Berlin
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

  // Load data
  try {
    const resp = await fetch("../data/venues_final.json");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    allVenues = data.venues;
    populateFilters(allVenues);
    applyFilters();
    updateStats(data);
  } catch (err) {
    document.getElementById("venue-list").innerHTML =
      `<div class="loading">Error loading data: ${err.message}<br>Run the scrapers first.</div>`;
  }
}

function populateFilters(venues) {
  // Districts
  const districts = [
    ...new Set(venues.map((v) => v.district).filter(Boolean)),
  ].sort();
  const distSelect = document.getElementById("district-filter");
  for (const d of districts) {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = d;
    distSelect.appendChild(opt);
  }

  // Activities
  const activities = [
    ...new Set(venues.flatMap((v) => v.activities).filter(Boolean)),
  ].sort();
  const actSelect = document.getElementById("activity-filter");
  for (const a of activities) {
    const opt = document.createElement("option");
    opt.value = a;
    opt.textContent = a;
    actSelect.appendChild(opt);
  }

  // Bind filter events
  document
    .querySelectorAll('#tier-filter input[name="tier"]')
    .forEach((r) => r.addEventListener("change", applyFilters));
  distSelect.addEventListener("change", applyFilters);
  actSelect.addEventListener("change", applyFilters);
  document
    .getElementById("plus-filter")
    .addEventListener("change", applyFilters);
  document
    .getElementById("coords-filter")
    .addEventListener("change", applyFilters);
  document
    .getElementById("search-filter")
    .addEventListener("input", applyFilters);
}

function applyFilters() {
  const tierValue = document.querySelector(
    '#tier-filter input[name="tier"]:checked'
  ).value;
  const district = document.getElementById("district-filter").value;
  const activity = document.getElementById("activity-filter").value;
  const plusOnly = document.getElementById("plus-filter").checked;
  const coordsOnly = document.getElementById("coords-filter").checked;
  const search = document.getElementById("search-filter").value.toLowerCase();

  filteredVenues = allVenues.filter((v) => {
    // Tier filter (data uses private names internally)
    if (tierValue === "upgrade") {
      if (v.tiers.includes("Classic") || !v.tiers.includes("Premium"))
        return false;
    } else if (tierValue === "classic") {
      if (!v.tiers.includes("Classic")) return false;
    } else if (tierValue === "max-only") {
      if (v.tiers.length !== 1 || v.tiers[0] !== "Max") return false;
    }

    if (district && v.district !== district) return false;
    if (activity && !v.activities.includes(activity)) return false;
    if (plusOnly && !v.is_plus) return false;
    if (coordsOnly && !v.has_coordinates) return false;
    if (search && !v.name.toLowerCase().includes(search)) return false;

    return true;
  });

  renderList(filteredVenues);
  renderMap(filteredVenues);
  updateFilterStats(filteredVenues);
}

function updateStats(data) {
  const upgrade = allVenues.filter(
    (v) => v.tiers.includes("Premium") && !v.tiers.includes("Classic")
  );
  document.getElementById("stats").textContent =
    `${data.total_venues} venues | ${data.venues_with_coords} on map | ${upgrade.length} L Pro exclusive`;
}

function updateFilterStats(venues) {
  const withCoords = venues.filter((v) => v.has_coordinates).length;
  const total = venues.length;
  const statsEl = document.getElementById("stats");
  const baseStats = `${allVenues.length} total venues`;
  if (total === allVenues.length) {
    statsEl.textContent = `${baseStats} | ${withCoords} on map`;
  } else {
    statsEl.textContent = `Showing ${total} of ${allVenues.length} | ${withCoords} on map`;
  }
}

function tierBadgeHtml(privateTier) {
  const display = corpName(privateTier);
  return `<span class="tier-badge tier-${privateTier.toLowerCase()}">${display}</span>`;
}

function renderList(venues) {
  const container = document.getElementById("venue-list");
  container.innerHTML = "";

  for (const venue of venues) {
    const item = document.createElement("div");
    item.className = "venue-item";
    item.dataset.slug = venue.slug;

    let ratingHtml = "";
    if (venue.rating) {
      ratingHtml = `<span class="venue-rating">${"★".repeat(Math.round(venue.rating))} ${venue.rating}</span>`;
    }

    let plusHtml = venue.is_plus ? '<span class="plus-badge">PLUS</span>' : "";

    item.innerHTML = `
      <div class="venue-name">${esc(venue.name)}${plusHtml}</div>
      <div class="venue-meta">${esc(venue.district)}${venue.street ? " · " + esc(venue.street) : ""} ${ratingHtml}</div>
      <div class="venue-tiers">${venue.tiers.map((t) => tierBadgeHtml(t)).join("")}</div>
      <div class="venue-activities">${esc(venue.activities.join(" · "))}</div>
    `;

    item.addEventListener("click", () => {
      container
        .querySelectorAll(".venue-item.active")
        .forEach((el) => el.classList.remove("active"));
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

  for (const venue of venues) {
    venue._marker = null;
    if (!venue.has_coordinates) continue;

    const color = TIER_COLORS[venue.min_tier] || "#999";
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
      const listItem = document.querySelector(
        `.venue-item[data-slug="${venue.slug}"]`
      );
      if (listItem) {
        document
          .querySelectorAll(".venue-item.active")
          .forEach((el) => el.classList.remove("active"));
        listItem.classList.add("active");
        listItem.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    });

    venue._marker = marker;
    markerCluster.addLayer(marker);
  }
}

function buildPopup(venue) {
  let tiersHtml = venue.tiers.map((t) => tierBadgeHtml(t)).join(" ");

  // Build visit limits table — prefer corporate data if available
  let visitsHtml = "";
  if (venue.visit_limits) {
    const corpLimits = venue.visit_limits.corporate;
    const privLimits = venue.visit_limits.private || venue.visit_limits;

    if (corpLimits && Object.keys(corpLimits).length > 0) {
      // Show corporate visit limits (M, L, XL)
      const corpOrder = ["S", "M", "L", "XL"];
      const corpDisplay = { S: "S", M: "M Pro", L: "L Pro", XL: "XL Pro" };
      const rows = corpOrder
        .map((t) => {
          const val = corpLimits[t];
          if (val === undefined) return "";
          const display =
            val === null
              ? '<span style="color:#ccc">Not included</span>'
              : `${val} / month`;
          return `<tr><td>${corpDisplay[t]}</td><td>${display}</td></tr>`;
        })
        .filter(Boolean)
        .join("");
      if (rows) {
        visitsHtml = `<div class="popup-visits"><strong>Visit limits (Corporate)</strong><table>${rows}</table></div>`;
      }
    } else {
      // Fallback to private limits with corporate names
      const rows = TIER_ORDER_PRIVATE.map((t) => {
        const val = privLimits[t];
        if (val === undefined) return "";
        const display =
          val === null
            ? '<span style="color:#ccc">Not included</span>'
            : `${val} / month`;
        return `<tr><td>${corpName(t)}</td><td>${display}</td></tr>`;
      })
        .filter(Boolean)
        .join("");
      if (rows) {
        visitsHtml = `<div class="popup-visits"><strong>Visit limits</strong><table>${rows}</table></div>`;
      }
    }
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
    <div class="popup-activities">${esc(venue.activities.join(" · "))}</div>
  `;
}

function esc(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

init();
