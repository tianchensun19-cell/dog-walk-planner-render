/* app.js — 狗狗丰容路线规划器前端逻辑 */

// ── 常量 ──────────────────────────────────────────────────────────────
const DISTRICT_CENTERS = {
  "杨浦区":   [31.2592, 121.5327],
  "徐汇区":   [31.1883, 121.4376],
  "静安区":   [31.2276, 121.4484],
  "黄浦区":   [31.2272, 121.4816],
  "长宁区":   [31.2204, 121.4238],
  "普陀区":   [31.2492, 121.3956],
  "虹口区":   [31.2640, 121.5052],
  "浦东新区": [31.2218, 121.5441],
  "宝山区":   [31.4040, 121.4891],
  "闵行区":   [31.1124, 121.3813],
};

const SCORE_DIMS = [
  { key: "greenery", label: "绿化覆盖" },
  { key: "water",    label: "水体邻近" },
  { key: "quiet",    label: "安静度"   },
  { key: "surface",  label: "路面友好" },
];

// ── 状态 ──────────────────────────────────────────────────────────────
const state = {
  map:          null,
  routeLayers:  [],
  originMarker: null,   // 路线结果中的出发点圆点
  pinMarker:    null,   // 用户手动点击的大头针
  userOrigin:   null,   // [lat, lon] 或 null
  activeIdx:    0,
};

// ── 初始化 ────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initMap();
  initSegmentedControls();
  loadBreeds();
  setupBreedToggle();
  setupValidation();
  setupDistrictFly();
  document.getElementById("generate-btn")
          .addEventListener("click", onGenerate);
});

// ── 地图 ──────────────────────────────────────────────────────────────
function initMap() {
  state.map = L.map("map", { zoomControl: true })
               .setView([31.2304, 121.4737], 12);
  L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    {
      attribution:
        '\u00a9 <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
        '\u00a9 <a href="https://carto.com">CARTO</a>',
      subdomains: "abcd",
      maxZoom: 19,
    }
  ).addTo(state.map);

  // 点击地图设置自定义出发点
  state.map.on("click", onMapClick);
}

// ── 地图点击：出发点大头针 ────────────────────────────────────────────
function getPinIcon() {
  return L.divIcon({
    className: "",
    html: '<div style="width:14px;height:14px;background:#E8593C;border:2px solid white;border-radius:50%;box-shadow:0 1px 4px rgba(0,0,0,0.4)"></div>',
    iconAnchor: [7, 7],
  });
}

function onMapClick(e) {
  const { lat, lng } = e.latlng;
  state.userOrigin = [lat, lng];

  if (state.pinMarker) {
    state.pinMarker.setLatLng([lat, lng]);
  } else {
    state.pinMarker = L.marker([lat, lng], {
      icon: getPinIcon(),
      draggable: true,
    }).addTo(state.map).bindTooltip("出发点（可拖动）");

    state.pinMarker.on("dragend", (ev) => {
      const pos = ev.target.getLatLng();
      state.userOrigin = [pos.lat, pos.lng];
      updatePinHint(true);
    });
  }
  updatePinHint(true);
}

function updatePinHint(isSet) {
  const hint = document.getElementById("map-hint");
  if (!hint) return;
  if (isSet) {
    hint.textContent = "\u{1F4CD} 出发点已设置（可重新点击更换）";
    hint.style.background = "rgba(29,158,117,0.85)";
  } else {
    hint.textContent = "点击地图选择出发点（不选则使用区中心）";
    hint.style.background = "rgba(0,0,0,0.45)";
  }
}

// ── 选区后飞到该区 ────────────────────────────────────────────────────
function setupDistrictFly() {
  document.getElementById("district-select").addEventListener("change", (e) => {
    const center = DISTRICT_CENTERS[e.target.value];
    if (center) state.map.flyTo(center, 14, { duration: 0.8 });

    // 换区时清除旧出发点
    if (state.pinMarker) {
      state.map.removeLayer(state.pinMarker);
      state.pinMarker  = null;
      state.userOrigin = null;
      updatePinHint(false);
    }
    validateForm();
  });
}

// ── 分段控件 ──────────────────────────────────────────────────────────
function initSegmentedControls() {
  document.querySelectorAll(".seg-group").forEach((group) => {
    group.querySelectorAll(".seg").forEach((btn) => {
      btn.addEventListener("click", () => {
        group.querySelectorAll(".seg").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
      });
    });
  });
}

function getSegValue(groupId) {
  const active = document.querySelector("#" + groupId + " .seg.active");
  return active ? active.dataset.value : null;
}

// ── 品种列表（后端动态加载）──────────────────────────────────────────
async function loadBreeds() {
  try {
    const resp = await fetch("/api/breeds");
    const data = await resp.json();
    const sel  = document.getElementById("breed-select");
    const last = sel.lastElementChild; // "不确定" 选项

    data.breeds.forEach((b) => {
      const opt = document.createElement("option");
      opt.value       = b.name;
      opt.textContent = b.name + (b.en_name ? "\uff08" + b.en_name + "\uff09" : "");
      sel.insertBefore(opt, last);
    });
  } catch (e) {
    console.warn("品种列表加载失败", e);
  }
}

// ── 非品种犬字段显示/隐藏 ─────────────────────────────────────────────
function setupBreedToggle() {
  const sel = document.getElementById("breed-select");
  sel.addEventListener("change", () => {
    const isCustom = sel.value === "__custom__";
    document.getElementById("custom-fields").classList.toggle("hidden", !isCustom);
    validateForm();
  });
}

// ── 表单校验 ──────────────────────────────────────────────────────────
function setupValidation() {
  ["district-select", "breed-select"].forEach((id) => {
    document.getElementById(id).addEventListener("change", validateForm);
  });
}

function validateForm() {
  const district = document.getElementById("district-select").value;
  const breed    = document.getElementById("breed-select").value;
  document.getElementById("generate-btn").disabled = !district || !breed;
}

// ── 生成路线 ──────────────────────────────────────────────────────────
async function onGenerate() {
  const district = document.getElementById("district-select").value;
  const breed    = document.getElementById("breed-select").value;
  const isCustom = breed === "__custom__";

  const landscape = Array.from(
    document.querySelectorAll("#landscape-group input:checked")
  ).map((cb) => cb.value);

  const payload = {
    district,
    breed_name:      isCustom ? null : breed,
    is_custom:       isCustom,
    age_years:       parseFloat(getSegValue("age-group")),
    has_joint:       parseJoint(getSegValue("joint-group")),
    landscape,
    quiet_pref:      getSegValue("quiet-group"),
    duration_cap_km: parseFloat(getSegValue("duration-group")),
    user_lat:        state.userOrigin ? state.userOrigin[0] : null,
    user_lon:        state.userOrigin ? state.userOrigin[1] : null,
  };

  if (isCustom) {
    payload.weight_range = getSegValue("weight-group");
    payload.is_brachy    = getSegValue("brachy-group") === "true";
    payload.is_short_leg = getSegValue("short-leg-group") === "true";
    payload.coat         = getSegValue("coat-group");
  }

  showLoading("正在加载路网与绿地数据（首次约 1\u20132 分钟）\u2026");
  hideResults();

  try {
    const resp = await fetch("/api/routes", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: "请求失败" }));
      throw new Error(err.detail || "HTTP " + resp.status);
    }

    const data = await resp.json();
    hideLoading();
    renderRoutes(data);
  } catch (e) {
    hideLoading();
    showError(e.message);
  }
}

function parseJoint(val) {
  if (val === "true")  return true;
  if (val === "false") return false;
  return null;
}

// ── 地图渲染 ──────────────────────────────────────────────────────────
function renderRoutes(data) {
  state.routeLayers.forEach((l) => state.map.removeLayer(l));
  state.routeLayers = [];
  if (state.originMarker) state.map.removeLayer(state.originMarker);

  state.originMarker = L.circleMarker(data.origin, {
    radius: 8, fillColor: "#E8593C",
    color: "white", weight: 2, fillOpacity: 1,
  }).addTo(state.map).bindTooltip("出发点");

  data.routes.forEach((route, i) => {
    const layer = L.polyline(route.coordinates, {
      color:   route.color,
      weight:  i === 0 ? 6 : 4,
      opacity: i === 0 ? 0.95 : 0.55,
    }).addTo(state.map);

    layer.bindTooltip(
      "<strong>" + route.label + "</strong> " + route.total_km + " km " +
      "\u7efc\u5408\u5206 " + route.score + "<br>" +
      "\u7eff\u5316 " + route.greenery + " \u00b7 \u5b89\u9759 " + route.quiet
    );
    layer.on("click", () => activateRoute(i));
    state.routeLayers.push(layer);
  });

  if (state.routeLayers.length) {
    const grp = L.featureGroup(state.routeLayers);
    state.map.fitBounds(grp.getBounds(), { padding: [40, 40] });
  }

  renderPanel(data);
  state.activeIdx = 0;
}

function activateRoute(idx) {
  state.activeIdx = idx;
  state.routeLayers.forEach((layer, i) => {
    layer.setStyle({
      weight:  i === idx ? 6 : 4,
      opacity: i === idx ? 0.95 : 0.45,
    });
    if (i === idx) layer.bringToFront();
  });
  document.querySelectorAll(".route-card").forEach((card, i) => {
    card.classList.toggle("active", i === idx);
  });
}

// ── 结果面板 ──────────────────────────────────────────────────────────
function renderPanel(data) {
  const alerts = document.getElementById("alerts-area");
  alerts.innerHTML = "";
  if (data.constraints.heat_sensitive) {
    alerts.innerHTML =
      '<div class="alert alert-warn">' +
      "\u26a0\ufe0f \u8be5\u72ac\u79cd\u70ed\u654f\u611f\uff0c\u5efa\u8bae\u65e9\u6668 7 \u70b9\u524d\u6216\u5088\u665a 6 \u70b9\u540e\u9535\u72d7\uff0c\u907f\u5f00\u6b63\u5348\u9ad8\u6e29" +
      "</div>";
  }

  const cards = document.getElementById("route-cards");
  cards.innerHTML = data.routes.map((r, i) =>
    '<div class="route-card ' + (i === 0 ? "active" : "") + '" ' +
    'style="border-color:' + r.color + '" onclick="activateRoute(' + i + ')">' +
    '<div class="card-label" style="color:' + r.color + '">' + r.label + "</div>" +
    '<div class="card-km" style="color:' + r.color + '">' + r.total_km + " <small>km</small></div>" +
    '<div class="card-score">\u7efc\u5408\u5206 ' + r.score + "</div>" +
    "</div>"
  ).join("");

  document.getElementById("score-head").innerHTML =
    "<th>\u7ef4\u5ea6</th>" +
    data.routes.map((r) => '<th style="color:' + r.color + '">' + r.label + "</th>").join("");

  document.getElementById("score-body").innerHTML = SCORE_DIMS.map((dim) =>
    "<tr><td>" + dim.label + "</td>" +
    data.routes.map((r) =>
      "<td>" +
      '<div class="bar-wrap">' +
      '<div class="bar-bg"><div class="bar-fill" style="width:' +
      (r[dim.key] * 100) + "%;background:" + r.color + '"></div></div>' +
      "<span>" + r[dim.key] + "</span>" +
      "</div></td>"
    ).join("") +
    "</tr>"
  ).join("");

  const c = data.constraints;
  document.getElementById("constraints-strip").innerHTML = [
    '<div class="c-chip">\u6700\u5927\u8def\u7ebf <strong>' + c.max_route_km + " km</strong></div>",
    '<div class="c-chip">\u6700\u5927\u5761\u5ea6 <strong>' + c.max_slope_pct + "%</strong></div>",
    '<div class="c-chip">\u53f0\u9636 <strong>' + (c.allow_stairs ? "\u5141\u8bb8" : "\u56de\u907f") + "</strong></div>",
    '<div class="c-chip">\u70ed\u654f\u611f <strong>' + (c.heat_sensitive ? "\u26a0\ufe0f \u662f" : "\u5426") + "</strong></div>",
  ].join("");

  document.getElementById("results").classList.remove("hidden");
  document.getElementById("welcome").classList.add("hidden");
}

// ── Loading / Error / Reset ────────────────────────────────────────────
function showLoading(text) {
  document.getElementById("loading-text").textContent = text;
  document.getElementById("loading").classList.remove("hidden");
}
function hideLoading() {
  document.getElementById("loading").classList.add("hidden");
}
function hideResults() {
  document.getElementById("results").classList.add("hidden");
  document.getElementById("welcome").classList.add("hidden");
}
function showError(msg) {
  hideLoading();
  const welcome = document.getElementById("welcome");
  welcome.classList.remove("hidden");
  welcome.querySelector(".welcome-box").innerHTML =
    '<div style="font-size:40px;margin-bottom:12px">\u26a0\ufe0f</div>' +
    '<p style="font-weight:500;color:#A32D2D;margin-bottom:6px">' + msg + "</p>" +
    '<p style="font-size:12px;color:#73726c">\u8bf7\u68c0\u67e5\u7f51\u7edc\u8fde\u63a5\uff0c\u6216\u8c03\u6574\u7b5b\u9009\u6761\u4ef6\u540e\u91cd\u8bd5</p>';
}
