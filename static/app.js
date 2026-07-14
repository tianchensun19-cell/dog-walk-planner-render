/* app.js — 狗狗丰容路线规划器前端逻辑 */

const SCORE_DIMS = [
  { key: "greenery", label: "绿化覆盖" },
  { key: "water",    label: "水体邻近" },
  { key: "quiet",    label: "安静度"   },
  { key: "surface",  label: "路面友好" },
];

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

// ── 状态 ──────────────────────────────────────────────────────────────
const state = {
  map:          null,
  routeLayers:  [],
  originMarker: null,
  pinMarker:    null,  // 用户手动点击的出发点大头针
  userOrigin:   null,  // [lat, lon] 或 null
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
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
        '© <a href="https://carto.com">CARTO</a>',
      subdomains: "abcd",
      maxZoom: 19,
    }
  ).addTo(state.map);
  state.map.on("click", onMapClick);
}

// ── 地图点击：设置自定义出发点 ────────────────────────────────────────
const PIN_ICON = L.divIcon({
  className: "",
  html: `<div style="width:14px;height:14px;background:#E8593C;border:2px solid white;border-radius:50%;box-shadow:0 1px 4px rgba(0,0,0,0.4)"></div>`,
  iconAnchor: [7, 7],
});

function onMapClick(e) {
  const { lat, lng } = e.latlng;
  state.userOrigin = [lat, lng];
  if (state.pinMarker) {
    state.pinMarker.setLatLng([lat, lng]);
  } else {
    state.pinMarker = L.marker([lat, lng], { icon: PIN_ICON, draggable: true })
      .addTo(state.map)
      .bindTooltip("出发点（可拖动）");
    state.pinMarker.on("dragend", (ev) => {
      const pos = ev.target.getLatLng();
      state.userOrigin = [pos.lat, pos.lng];
      updatePinHint();
    });
  }
  updatePinHint();
}

function updatePinHint() {
  const hint = document.getElementById("map-hint");
  if (!hint) return;
  if (state.userOrigin) {
    hint.textContent = "📍 出发点已设置（可重新点击更换）";
    hint.style.background = "rgba(29,158,117,0.85)";
  }
}

// ── 选区后飞到该区 ────────────────────────────────────────────────────
function setupDistrictFly() {
  document.getElementById("district-select").addEventListener("change", (e) => {
    const center = DISTRICT_CENTERS[e.target.value];
    if (center) state.map.flyTo(center, 14, { duration: 0.8 });
    if (state.pinMarker) {
      state.map.removeLayer(state.pinMarker);
      state.pinMarker  = null;
      state.userOrigin = null;
      const hint = document.getElementById("map-hint");
      if (hint) {
        hint.textContent = "点击地图选择出发点（不选则使用区中心）";
        hint.style.background = "rgba(0,0,0,0.45)";
      }
    }
    validateForm();
  });
}

 ──────────────────────────────────────────────────────────
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
  const active = document.querySelector(`#${groupId} .seg.active`);
  return active ? active.dataset.value : null;
}

// ── 品种列表（从后端动态加载）────────────────────────────────────────
async function loadBreeds() {
  try {
    const resp = await fetch("/api/breeds");
    const data = await resp.json();
    const sel  = document.getElementById("breed-select");
    const last = sel.lastElementChild; // "不确定" 选项

    data.breeds.forEach((b) => {
      const opt = document.createElement("option");
      opt.value       = b.name;
      opt.textContent = b.name + (b.en_name ? `（${b.en_name}）` : "");
      sel.insertBefore(opt, last);
    });
  } catch (e) {
    console.warn("品种列表加载失败，将使用空列表", e);
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

// ── 表单校验（启用/禁用按钮）────────────────────────────────────────
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

  // 收集景观多选
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
    // 用户地图点击的出发点（null 则后端使用区中心）
    user_lat:        state.userOrigin ? state.userOrigin[0] : null,
    user_lon:        state.userOrigin ? state.userOrigin[1] : null,
  };

  if (isCustom) {
    payload.weight_range = getSegValue("weight-group");
    payload.is_brachy    = getSegValue("brachy-group") === "true";
    payload.is_short_leg = getSegValue("short-leg-group") === "true";
    payload.coat         = getSegValue("coat-group");
  }

  showLoading("正在加载路网与绿地数据（首次约 1–2 分钟）…");
  hideResults();

  try {
    const resp = await fetch("/api/routes", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: "请求失败" }));
      throw new Error(err.detail || `HTTP ${resp.status}`);
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
  // 清除旧图层
  state.routeLayers.forEach((l) => state.map.removeLayer(l));
  state.routeLayers = [];
  if (state.originMarker) state.map.removeLayer(state.originMarker);

  // 出发点标记
  state.originMarker = L.circleMarker(data.origin, {
    radius: 8, fillColor: "#E8593C",
    color: "white", weight: 2, fillOpacity: 1,
  }).addTo(state.map).bindTooltip("出发点");

  // 路线
  data.routes.forEach((route, i) => {
    const layer = L.polyline(route.coordinates, {
      color: route.color,
      weight:  i === 0 ? 6 : 4,
      opacity: i === 0 ? 0.95 : 0.55,
    }).addTo(state.map);

    layer.bindTooltip(
      `<strong>${route.label}</strong> ${route.total_km} km ` +
      `综合分 ${route.score}<br>` +
      `绿化 ${route.greenery} · 安静 ${route.quiet}`
    );
    layer.on("click", () => activateRoute(i));
    state.routeLayers.push(layer);
  });

  // 自动缩放到路线范围
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
  // 警告
  const alerts = document.getElementById("alerts-area");
  alerts.innerHTML = "";
  if (data.constraints.heat_sensitive) {
    alerts.innerHTML +=
      `<div class="alert alert-warn">` +
      `⚠️ 该犬种热敏感，建议早晨 7 点前或傍晚 6 点后遛狗，避开正午高温` +
      `</div>`;
  }

  // 路线卡片
  const cards = document.getElementById("route-cards");
  cards.innerHTML = data.routes
    .map(
      (r, i) =>
        `<div class="route-card ${i === 0 ? "active" : ""}"
              style="border-color:${r.color}"
              onclick="activateRoute(${i})">
           <div class="card-label" style="color:${r.color}">${r.label}</div>
           <div class="card-km"    style="color:${r.color}">${r.total_km} <small>km</small></div>
           <div class="card-score">综合分 ${r.score}</div>
         </div>`
    )
    .join("");

  // 评分表表头
  document.getElementById("score-head").innerHTML =
    `<th>维度</th>` +
    data.routes.map((r) => `<th style="color:${r.color}">${r.label}</th>`).join("");

  // 评分表行
  document.getElementById("score-body").innerHTML = SCORE_DIMS.map(
    (dim) =>
      `<tr>
         <td>${dim.label}</td>
         ${data.routes
           .map(
             (r) =>
               `<td>
                  <div class="bar-wrap">
                    <div class="bar-bg">
                      <div class="bar-fill"
                           style="width:${r[dim.key] * 100}%;background:${r.color}">
                      </div>
                    </div>
                    <span>${r[dim.key]}</span>
                  </div>
                </td>`
           )
           .join("")}
       </tr>`
  ).join("");

  // 约束条
  const c = data.constraints;
  document.getElementById("constraints-strip").innerHTML = [
    `<div class="c-chip">最大路线 <strong>${c.max_route_km} km</strong></div>`,
    `<div class="c-chip">最大坡度 <strong>${c.max_slope_pct}%</strong></div>`,
    `<div class="c-chip">台阶 <strong>${c.allow_stairs ? "允许" : "回避"}</strong></div>`,
    `<div class="c-chip">热敏感 <strong>${c.heat_sensitive ? "⚠️ 是" : "否"}</strong></div>`,
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
  welcome.querySelector(".welcome-box").innerHTML = `
    <div style="font-size:40px;margin-bottom:12px">⚠️</div>
    <p style="font-weight:500;color:#A32D2D;margin-bottom:6px">${msg}</p>
    <p style="font-size:12px;color:#73726c">请检查网络连接，或调整筛选条件后重试</p>
  `;
}
