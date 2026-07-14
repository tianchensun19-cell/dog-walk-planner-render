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


# ── 路网加载（带本地缓存） ────────────────────────────────────────────────
def _load_graph(district: str) -> nx.MultiDiGraph:
    if district in _graph_cache:
        return _graph_cache[district]

    ox.settings.log_console = False
    ox.settings.overpass_endpoint = "https://overpass.kumi.systems/api"
    ox.settings.use_cache   = True
    ox.settings.cache_folder = "./osmnx_cache"

    center = DISTRICTS[district]["center"]  # (lat, lon)

    # 以区中心为圆心拉取 1500m 路网（内存友好，适合云端部署）
    G = ox.graph_from_point(center, dist=1500, network_type="walk")

    # 拉取绿地 / 水体（失败时静默降级，不影响主流程）
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
    try:
        green_gdf = ox.features_from_point(center, tags=green_tags, dist=1500)
        green_gdf = green_gdf[
            green_gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        ].reset_index(drop=True)
    except Exception:
        green_gdf = gpd.GeoDataFrame()

    try:
        water_gdf = ox.features_from_point(center, tags=water_tags, dist=1500)
        water_gdf = water_gdf[
            water_gdf.geometry.geom_type.isin([
                "Polygon", "MultiPolygon", "LineString", "MultiLineString"
            ])
        ].reset_index(drop=True)
    except Exception:
        water_gdf = gpd.GeoDataFrame()

    G = enrich_graph_edges(G, green_gdf=green_gdf, water_gdf=water_gdf)
    _graph_cache[district] = G
    return G


# ── FastAPI ───────────────────────────────────────────────────────────────
app = FastAPI(title="狗狗丰容路线规划器 API", version="1.0.0")


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
    breed_name:      Optional[str]  = None
    is_custom:       bool           = False
    # 非品种犬形态字段
    weight_range:    Optional[str]  = None
    is_brachy:       bool           = False
    is_short_leg:    bool           = False
    coat:            str            = "中长毛"
    # 年龄与健康
    age_years:       float          = 3.0
    has_joint:       Optional[bool] = None
    # 偏好
    landscape:       list[str]      = []
    quiet_pref:      str            = "都可以"
    duration_cap_km: float          = 3.5


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

    # 加载路网
    try:
        G_enriched = _load_graph(req.district)
    except Exception as e:
        raise HTTPException(503, f"路网加载失败：{e}")

    origin = DISTRICTS[req.district]["center"]

    # 过滤 + 路线生成
    G_f        = apply_hard_constraints(G_enriched, constraints)
    G_r        = build_walk_costs(G_f, pref)
    candidates = generate_candidate_routes(
        G_r, origin=origin,
        target_km=constraints["max_route_km"],
        n_routes=6, waypoint_sample=20, seed=42,
    )
    ranked = rank_routes(G_r, candidates, pref)

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
