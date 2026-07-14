"""
main.py — 狗狗丰容路线规划器 FastAPI 后端

本地运行：
    uvicorn main:app --reload
    浏览器打开 http://localhost:8000

云端部署（Render / Railway）：
    Start command: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import sys, warnings
sys.path.insert(0, ".")
warnings.filterwarnings("ignore")

from contextlib import asynccontextmanager
from typing import Optional
import networkx as nx
import geopandas as gpd
import osmnx as ox

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from breed_utils import load_breed_table, get_hard_constraints
from route_engine import (
    enrich_graph_edges,
    apply_hard_constraints,
    build_walk_costs,
    generate_candidate_routes,
    rank_routes,
)

# ── 常量 ─────────────────────────────────────────────────────────────────
DISTRICTS: dict[str, dict] = {
    "杨浦区":   {"center": (31.2592, 121.5327)},
    "徐汇区":   {"center": (31.1883, 121.4376)},
    "静安区":   {"center": (31.2276, 121.4484)},
    "黄浦区":   {"center": (31.2272, 121.4816)},
    "长宁区":   {"center": (31.2204, 121.4238)},
    "普陀区":   {"center": (31.2492, 121.3956)},
    "虹口区":   {"center": (31.2640, 121.5052)},
    "浦东新区": {"center": (31.2218, 121.5441)},
    "宝山区":   {"center": (31.4040, 121.4891)},
    "闵行区":   {"center": (31.1124, 121.3813)},
}

ROUTE_COLORS = ["#E8593C", "#1D9E75", "#378ADD"]
ROUTE_LABELS = ["路线①", "路线②", "路线③"]

# 路网缓存（per district，首次慢，之后常驻内存）
_graph_cache: dict[str, nx.MultiDiGraph] = {}


# ── 非品种犬约束推断（与 app.py 逻辑一致） ───────────────────────────────
def infer_constraints_custom(
    weight_range: str,
    is_brachy: bool,
    is_short_leg: bool,
    coat: str,
    age_years: float,
    has_joint,
) -> dict:
    length_map = {
        "5kg以下": 1.5, "5–15kg": 2.5,
        "15–30kg": 4.0, "30kg以上": 6.0,
    }
    max_km = length_map.get(weight_range, 3.0)
    if is_brachy:
        max_km = min(max_km, 1.5)
    if is_short_leg:
        max_km *= 0.75

    senior_age = 8
    if age_years >= senior_age:
        factor = max(0.5, 1.0 - 0.08 * (age_years - senior_age))
        max_km = round(max_km * factor, 1)
    if age_years < 1:
        max_km = min(max_km, 1.5)

    joint_resolved = (has_joint is True) or (is_short_leg and has_joint is None)
    max_slope, allow_stairs = 6.0, True
    if joint_resolved or is_short_leg:
        max_slope, allow_stairs = 3.0, False
    elif is_brachy:
        max_slope, allow_stairs = 5.0, False
    if joint_resolved:
        max_km = round(max_km * 0.7, 1)

    return {
        "max_route_km":  round(max_km, 1),
        "max_slope_pct": max_slope,
        "allow_stairs":  allow_stairs,
        "heat_sensitive": is_brachy or coat == "长毛",
    }


# Overpass 备用节点（按优先级尝试）
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api",
    "https://overpass.kumi.systems/api",
    "https://maps.mail.ru/osm/tools/overpass/api",
]

# ── 路网加载（带本地缓存） ────────────────────────────────────────────────
def _load_graph(lat: float, lon: float) -> nx.MultiDiGraph:
    # 缓存键：坐标精确到小数点后2位（≈1km精度），同一区域复用缓存
    cache_key = f"{lat:.2f},{lon:.2f}"
    if cache_key in _graph_cache:
        print(f"[cache hit] {cache_key}")
        return _graph_cache[cache_key]

    center = (lat, lon)
    print(f"[load_graph] 开始加载，center={center}")

    ox.settings.log_console  = True          # 输出到 Render 日志
    ox.settings.use_cache    = True
    ox.settings.cache_folder = "./osmnx_cache"

    # ── 路网下载（逐个尝试 Overpass 节点）────────────────────────────
    G = None
    last_err = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            ox.settings.overpass_endpoint = endpoint
            print(f"[load_graph] 尝试节点 {endpoint}")
            # dist=600m：在 Render 免费套餐 30s 超时内完成
            G = ox.graph_from_point(center, dist=600, network_type="walk")
            print(f"[load_graph] 路网下载成功，节点={G.number_of_nodes()}")
            break
        except Exception as e:
            print(f"[load_graph] 节点 {endpoint} 失败：{e}")
            last_err = e

    if G is None:
        raise RuntimeError(f"所有 Overpass 节点均失败，最后错误：{last_err}")

    # ── 绿地/水体（可选，失败时静默降级）──────────────────────────────
    green_tags = {
        "landuse": ["grass", "forest", "meadow", "recreation_ground"],
        "leisure": ["park", "garden", "nature_reserve"],
        "natural": ["wood", "scrub", "grassland"],
    }
    water_tags = {
        "natural":  ["water", "wetland"],
        "waterway": ["river", "canal", "stream"],
        "landuse":  ["reservoir"],
    }

    green_gdf = gpd.GeoDataFrame()
    water_gdf = gpd.GeoDataFrame()
    try:
        green_gdf = ox.features_from_point(center, tags=green_tags, dist=600)
        green_gdf = green_gdf[
            green_gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        ].reset_index(drop=True)
        print(f"[load_graph] 绿地面片：{len(green_gdf)}")
    except Exception as e:
        print(f"[load_graph] 绿地加载失败（降级为0分）：{e}")

    try:
        water_gdf = ox.features_from_point(center, tags=water_tags, dist=600)
        water_gdf = water_gdf[
            water_gdf.geometry.geom_type.isin([
                "Polygon", "MultiPolygon", "LineString", "MultiLineString"
            ])
        ].reset_index(drop=True)
        print(f"[load_graph] 水体面片：{len(water_gdf)}")
    except Exception as e:
        print(f"[load_graph] 水体加载失败（降级为0分）：{e}")

    print("[load_graph] 开始边打分…")
    G = enrich_graph_edges(G, green_gdf=green_gdf, water_gdf=water_gdf)
    print("[load_graph] 打分完成，写入缓存")
    _graph_cache[cache_key] = G
    return G


# ── FastAPI ───────────────────────────────────────────────────────────────
app = FastAPI(title="狗狗丰容路线规划器 API", version="1.0.0")


# ── API: 健康检查 ─────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    """浏览器直接打开 /api/health，返回 ok 说明后端正常"""
    return {
        "status": "ok",
        "cached_districts": list(_graph_cache.keys()),
        "districts_available": list(DISTRICTS.keys()),
    }


# ── API: 品种列表 ─────────────────────────────────────────────────────────
@app.get("/api/breeds")
def get_breeds():
    df = load_breed_table()
    breeds = []
    for name, row in df.iterrows():
        aliases = row["别名"] if isinstance(row["别名"], list) else []
        breeds.append({
            "name":    name,
            "en_name": row["犬种英文名"],
            "size":    str(row["体型分级"]),
            "aliases": aliases,
        })
    return {"breeds": breeds}


# ── API: 路线计算 ─────────────────────────────────────────────────────────
class RouteRequest(BaseModel):
    district:        str
    breed_name:      Optional[str]   = None
    is_custom:       bool            = False
    # 非品种犬形态字段
    weight_range:    Optional[str]   = None
    is_brachy:       bool            = False
    is_short_leg:    bool            = False
    coat:            str             = "中长毛"
    # 年龄与健康
    age_years:       float           = 3.0
    has_joint:       Optional[bool]  = None
    # 偏好
    landscape:       list[str]       = []
    quiet_pref:      str             = "都可以"
    duration_cap_km: float           = 3.5
    # 用户自选出发点（地图点击传入，可选）
    user_lat:        Optional[float] = None
    user_lon:        Optional[float] = None


@app.post("/api/routes")
def compute_routes(req: RouteRequest):
    if req.district not in DISTRICTS:
        raise HTTPException(400, f"未知行政区：{req.district}")

    # 偏好权重
    pref = {
        "greenery":   1.0 if "绿地与公园"   in req.landscape else 0.0,
        "water":      1.0 if "河道与水景"   in req.landscape else 0.0,
        "commercial": 1.0 if "热闹商业街区" in req.landscape else 0.0,
        "quiet":      {"安静小路": 1.0, "都可以": 0.0, "热闹街道": -0.8}.get(req.quiet_pref, 0.0),
        "surface":    0.5,
    }
    if not req.landscape:
        pref["greenery"] = 0.5  # 什么都不选时给绿化一个兜底权重

    # 硬性约束
    if req.is_custom:
        constraints  = infer_constraints_custom(
            req.weight_range or "5–15kg", req.is_brachy,
            req.is_short_leg, req.coat, req.age_years, req.has_joint,
        )
        display_name = f"非品种犬（{req.weight_range or '未知体重'}）"
    else:
        if not req.breed_name:
            raise HTTPException(400, "breed_name 不能为空")
        constraints  = get_hard_constraints(req.breed_name, req.age_years, req.has_joint)
        display_name = req.breed_name

    constraints["max_route_km"] = min(constraints["max_route_km"], req.duration_cap_km)

    # 出发点：优先用用户地图点击坐标，否则用区中心
    if req.user_lat is not None and req.user_lon is not None:
        origin = (req.user_lat, req.user_lon)
    else:
        origin = DISTRICTS[req.district]["center"]

    lat, lon = origin

    try:
        print(f"[routes] 请求：{req.district} / {display_name} / origin={origin}")
        G_enriched = _load_graph(lat, lon)

        # 过滤 + 路线生成
        print("[routes] 开始过滤与路线生成…")
        G_f        = apply_hard_constraints(G_enriched, constraints)
        G_r        = build_walk_costs(G_f, pref)
        candidates = generate_candidate_routes(
            G_r, origin=origin,
            target_km=constraints["max_route_km"],
            n_routes=6, waypoint_sample=20, seed=42,
        )
        ranked = rank_routes(G_r, candidates, pref)
        print(f"[routes] 找到 {len(ranked)} 条路线")

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[routes ERROR]\n{tb}")
        raise HTTPException(500, f"{type(e).__name__}: {e}\n\n{tb}")

    if not ranked:
        raise HTTPException(404, "未找到符合条件的路线，建议放宽约束条件或更换地点")

    # 构建响应
    routes = []
    for i, r in enumerate(ranked[:3]):
        coords = [
            [G_r.nodes[n]["y"], G_r.nodes[n]["x"]]
            for n in r["node_list"] if n in G_r.nodes
        ]
        routes.append({
            "label":       ROUTE_LABELS[i],
            "color":       ROUTE_COLORS[i],
            "coordinates": coords,
            "total_km":    r["total_km"],
            "score":       round(r["score"],    3),
            "greenery":    round(r["greenery"], 2),
            "water":       round(r["water"],    2),
            "quiet":       round(r["quiet"],    2),
            "surface":     round(r["surface"],  2),
        })

    return {
        "routes":       routes,
        "constraints":  {k: v for k, v in constraints.items() if not k.startswith("_")},
        "display_name": display_name,
        "origin":       list(origin),
    }


# ── 静态文件 & 首页 ───────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")


# ── 本地运行入口 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
