"""
route_engine.py
===============
狗狗丰容路线生成引擎。

流程
----
    1. enrich_graph_edges()   为路网每条边打分（安静度/路面/绿化/水体）
    2. apply_hard_constraints() 将违规边代价设为无穷大（坡度/台阶/长度）
    3. build_walk_costs()     按用户偏好权重合成单一通行代价
    4. generate_candidate_routes() 生成多条候选环形路线
    5. score_route()          为每条路线计算多维度分数
    6. rank_routes()          按用户偏好软排序，返回 Top-N

快速上手（Notebook）
--------------------
    import osmnx as ox
    from route_engine import (
        enrich_graph_edges, apply_hard_constraints,
        build_walk_costs, generate_candidate_routes,
        score_route, rank_routes
    )
    from breed_utils import lookup_breed, get_hard_constraints

    # 1. 拉取路网
    G = ox.graph_from_place("杨浦区, 上海市, 中国", network_type="walk")

    # 2. 打分
    G = enrich_graph_edges(G)

    # 3. 硬性约束（来自 breed_utils）
    constraints = get_hard_constraints("哈士奇", age_years=3, has_joint_issue=False)
    G_filtered = apply_hard_constraints(G, constraints)

    # 4. 用户偏好权重
    pref = {"greenery": 1.0, "water": 0.5, "quiet": 1.0, "commercial": 0.0}
    G_filtered = build_walk_costs(G_filtered, pref)

    # 5. 生成候选路线
    origin = (31.2837, 121.5161)   # (lat, lon)
    candidates = generate_candidate_routes(
        G_filtered, origin, target_km=constraints["max_route_km"], n_routes=6
    )

    # 6. 软排序
    ranked = rank_routes(G_filtered, candidates, pref)
    for i, r in enumerate(ranked[:3], 1):
        print(f"路线{i}: {r['total_km']:.1f}km  综合分={r['score']:.3f}")
        print(f"  绿化={r['greenery']:.2f}  水体={r['water']:.2f}  安静={r['quiet']:.2f}")

依赖
----
    pip install osmnx networkx geopandas shapely pandas numpy
    （坡度功能额外需要: pip install elevation rasterio）
"""

from __future__ import annotations

import math
import random
import warnings
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
from shapely.geometry import Point

# ── 常量 ──────────────────────────────────────────────────────────────────

INF = float("inf")

# highway → 安静度（0=嘈杂，1=安静）
HIGHWAY_QUIETNESS: dict[str, float] = {
    "motorway": 0.0, "motorway_link": 0.0,
    "trunk": 0.0,    "trunk_link": 0.0,
    "primary": 0.10, "primary_link": 0.10,
    "secondary": 0.30, "secondary_link": 0.30,
    "tertiary": 0.50,  "tertiary_link": 0.50,
    "unclassified": 0.60,
    "residential": 0.70,
    "living_street": 0.85,
    "service": 0.60,
    "cycleway": 0.90,
    "footway": 1.00, "pedestrian": 1.00,
    "path": 1.00,    "track": 0.90,
    "steps": 0.80,   # 台阶本身安静，但会被硬性约束单独处理
}

# surface → 路面友好度
SURFACE_SCORE: dict[str, float] = {
    "asphalt": 1.0, "paved": 1.0, "concrete": 1.0,
    "paving_stones": 0.70, "wood": 0.70,
    "sett": 0.60, "metal": 0.60, "grass": 0.60, "fine_gravel": 0.60,
    "cobblestone": 0.50, "gravel": 0.50, "unpaved": 0.50,
    "dirt": 0.40, "ground": 0.40, "earth": 0.40, "sand": 0.40,
}

# smoothness → 路面平整度（优先于 surface）
SMOOTHNESS_SCORE: dict[str, float] = {
    "excellent": 1.0, "good": 0.90, "intermediate": 0.60,
    "bad": 0.30, "very_bad": 0.15, "horrible": 0.05, "very_horrible": 0.0,
}

# highway 类型默认路面估算（完全无标注时兜底）
HW_SURFACE_FALLBACK: dict[str, float] = {
    "footway": 0.80, "pedestrian": 0.80, "residential": 0.75,
    "living_street": 0.75, "service": 0.65,
    "track": 0.40, "path": 0.45,
}


# ── 辅助：提取单值标签 ────────────────────────────────────────────────────

def _first(val: Any) -> Any:
    """OSMnx 有时返回 list，取第一个元素；否则原样返回。"""
    return val[0] if isinstance(val, list) else val


# ── 步骤 1：为路网边打分 ──────────────────────────────────────────────────

def _edge_quietness(data: dict) -> float:
    hw = _first(data.get("highway", "unclassified"))
    score = HIGHWAY_QUIETNESS.get(hw, 0.50)

    sidewalk = _first(data.get("sidewalk", ""))
    if sidewalk in ("both", "left", "right", "separate"):
        score = min(1.0, score + 0.10)

    maxspeed = _first(data.get("maxspeed", None))
    if maxspeed:
        try:
            if int(str(maxspeed).split()[0]) > 50:
                score = max(0.0, score - 0.10)
        except (ValueError, AttributeError):
            pass

    return round(score, 3)


def _edge_surface(data: dict) -> float:
    # 优先级 1：smoothness
    sm = _first(data.get("smoothness", None))
    if sm in SMOOTHNESS_SCORE:
        return SMOOTHNESS_SCORE[sm]

    # 优先级 2：surface 材质
    sf = _first(data.get("surface", None))
    if sf in SURFACE_SCORE:
        return SURFACE_SCORE[sf]

    # 优先级 3：highway 类型兜底
    hw = _first(data.get("highway", "unclassified"))
    return HW_SURFACE_FALLBACK.get(hw, 0.50)


def _edge_slope_pct(data: dict) -> float | None:
    """
    返回路段坡度（%），需要图中已存在 grade 属性（由 ox.add_edge_grades 写入）。
    grade 是 rise/run（无单位），转为百分比取绝对值。
    若无坡度信息则返回 None。
    """
    grade = data.get("grade", None)
    if grade is None:
        return None
    return round(abs(float(grade)) * 100, 2)


def _proximity_score(
    edge_geom,
    gdf: gpd.GeoDataFrame,
    buffer_m: float = 150.0,
) -> float:
    """
    计算路段周边 buffer_m 米范围内绿地/水体的覆盖比例（0-1）。
    gdf 已投影到平面坐标系（单位：米）。
    """
    if gdf is None or gdf.empty:
        return 0.0
    buf = edge_geom.buffer(buffer_m)
    intersection_area = gdf.geometry.intersection(buf).area.sum()
    return min(1.0, round(intersection_area / buf.area, 3))


def enrich_graph_edges(
    G: nx.MultiDiGraph,
    green_gdf: gpd.GeoDataFrame | None = None,
    water_gdf: gpd.GeoDataFrame | None = None,
    commercial_gdf: gpd.GeoDataFrame | None = None,
    crs_projected: str = "EPSG:32651",  # UTM Zone 51N，适合上海
) -> nx.MultiDiGraph:
    """
    为路网每条边添加丰容评分属性：
        quietness_score   安静度   [0, 1]
        surface_score     路面友好度 [0, 1]
        slope_pct         坡度（%），None 表示无 DEM 数据
        greenery_score    绿化覆盖  [0, 1]
        water_score       水体邻近  [0, 1]
        commercial_score  商业密度  [0, 1]
        is_stairs         是否台阶  bool

    Parameters
    ----------
    G               : OSMnx 步行路网
    green_gdf       : 绿地/公园面数据（可选，传入则计算 greenery_score）
    water_gdf       : 水体面数据（可选）
    commercial_gdf  : 商业 POI 数据（可选，传入 Point GeoDataFrame）
    crs_projected   : 投影坐标系（用于缓冲区面积计算）
    """
    # 投影 GeoDataFrame 到平面坐标系
    def _project(gdf):
        if gdf is None or gdf.empty:
            return None
        return gdf.to_crs(crs_projected)

    green_proj      = _project(green_gdf)
    water_proj      = _project(water_gdf)
    commercial_proj = _project(commercial_gdf)

    # 获取边几何（投影后）
    _, edges_gdf = ox.graph_to_gdfs(G)
    edges_proj = edges_gdf.to_crs(crs_projected)

    scored: dict[tuple, dict] = {}
    for (u, v, k), row in edges_proj.iterrows():
        data = G[u][v][k]
        hw   = _first(data.get("highway", "unclassified"))

        q  = _edge_quietness(data)
        sf = _edge_surface(data)
        sl = _edge_slope_pct(data)

        geom = row.geometry
        gr = _proximity_score(geom, green_proj)      if green_proj      is not None else 0.0
        wa = _proximity_score(geom, water_proj)      if water_proj      is not None else 0.0
        co = _proximity_score(geom, commercial_proj, buffer_m=80) if commercial_proj is not None else 0.0

        scored[(u, v, k)] = {
            "quietness_score":   q,
            "surface_score":     sf,
            "slope_pct":         sl,
            "greenery_score":    gr,
            "water_score":       wa,
            "commercial_score":  co,
            "is_stairs":         hw == "steps",
        }

    nx.set_edge_attributes(G, scored)
    return G


# ── 步骤 2：应用硬性约束 ─────────────────────────────────────────────────

def apply_hard_constraints(
    G: nx.MultiDiGraph,
    constraints: dict,
) -> nx.MultiDiGraph:
    """
    将违反硬性约束的边的通行代价设为 INF（不删除节点，保持图连通性）。
    constraints 来自 breed_utils.get_hard_constraints()。

    处理的约束：
        max_slope_pct   最大允许坡度（%）
        allow_stairs    是否允许台阶路段
    （max_route_km 在路线生成阶段控制，不在此处处理）
    """
    G = G.copy()
    max_slope    = constraints.get("max_slope_pct", 15.0)
    allow_stairs = constraints.get("allow_stairs", True)

    updates: dict[tuple, dict] = {}
    for u, v, k, data in G.edges(keys=True, data=True):
        blocked = False

        # 坡度超限（None 表示无 DEM 数据，视为通过）
        slope = data.get("slope_pct", None)
        if slope is not None and slope > max_slope:
            blocked = True

        # 台阶限制
        if not allow_stairs and data.get("is_stairs", False):
            blocked = True

        if blocked:
            updates[(u, v, k)] = {"hard_blocked": True}

    nx.set_edge_attributes(G, updates)
    return G


# ── 步骤 3：合成单一通行代价 ─────────────────────────────────────────────

def build_walk_costs(
    G: nx.MultiDiGraph,
    pref_weights: dict[str, float],
) -> nx.MultiDiGraph:
    """
    将各维度分数按用户偏好权重合成单一通行代价，写入 walk_cost 属性。

    公式：
        enrichment = Σ (weight_i × score_i) / Σ weight_i
        walk_cost  = edge_length_m / (0.01 + enrichment)

    高丰容路段"更便宜"，路径搜索自然偏向它们。
    被硬性约束拦截的边 walk_cost = INF。

    pref_weights 键：greenery | water | quiet | commercial | surface
    """
    G = G.copy()
    total_w = sum(abs(v) for v in pref_weights.values()) or 1.0

    updates: dict[tuple, dict] = {}
    for u, v, k, data in G.edges(keys=True, data=True):
        if data.get("hard_blocked", False):
            updates[(u, v, k)] = {"walk_cost": INF}
            continue

        length_m = data.get("length", 1.0)

        score_map = {
            "greenery":   data.get("greenery_score",   0.0),
            "water":      data.get("water_score",      0.0),
            "quiet":      data.get("quietness_score",  0.5),
            "commercial": data.get("commercial_score", 0.0),
            "surface":    data.get("surface_score",    0.5),
        }

        enrichment = sum(
            pref_weights.get(k_, 0.0) * score_map.get(k_, 0.0)
            for k_ in score_map
        ) / total_w

        # 负权重（如用户选"偏好热闹"→ quiet 权重为负）会降低 enrichment
        enrichment = max(0.0, enrichment)
        walk_cost  = length_m / (0.01 + enrichment)

        updates[(u, v, k)] = {"walk_cost": round(walk_cost, 4)}

    nx.set_edge_attributes(G, updates)
    return G


# ── 步骤 4：生成候选环形路线 ─────────────────────────────────────────────

def _nearest_node(G: nx.MultiDiGraph, lat: float, lon: float) -> int:
    """
    找到图中距 (lat, lon) 最近的节点，不依赖 scikit-learn。
    对 600m 半径的小路网（几百个节点）速度完全足够。
    使用度数空间欧氏距离（误差在小范围内可忽略）。
    """
    best_node, best_dist = None, float("inf")
    for node, data in G.nodes(data=True):
        d = (data["y"] - lat) ** 2 + (data["x"] - lon) ** 2
        if d < best_dist:
            best_dist, best_node = d, node
    return best_node


def _route_length_m(G: nx.MultiDiGraph, node_list: list[int]) -> float:
    total = 0.0
    for a, b in zip(node_list[:-1], node_list[1:]):
        # 取两节点间最短边长度
        edge_data = G.get_edge_data(a, b)
        if edge_data:
            lengths = [d.get("length", 0) for d in edge_data.values()]
            total += min(lengths)
    return total


def generate_candidate_routes(
    G: nx.MultiDiGraph,
    origin: tuple[float, float],
    target_km: float,
    n_routes: int = 6,
    waypoint_sample: int = 15,
    seed: int | None = 42,
) -> list[list[int]]:
    """
    生成候选环形路线（节点列表形式）。

    策略：
        1. 找到距起点约 target_km/2 距离的节点集合作为候选转折点
        2. 随机抽取若干转折点
        3. 对每个转折点：shortest_path(origin→wp) + shortest_path(wp→origin)
        4. 过滤：长度在 [0.7×target_km, 1.3×target_km] 范围内的留下
        5. 返回前 n_routes 条（按实际长度与目标长度差值升序）

    Parameters
    ----------
    origin       : (lat, lon) 起点坐标
    target_km    : 目标路线长度（来自 get_hard_constraints 的 max_route_km）
    n_routes     : 返回的候选路线数量
    waypoint_sample : 随机抽取的转折点数量（越大候选越多，越慢）
    seed         : 随机种子（None 表示不固定）
    """
    if seed is not None:
        random.seed(seed)

    target_m = target_km * 1000
    lat, lon = origin

    # 找最近起点节点（手动实现，不依赖 scikit-learn）
    origin_node = _nearest_node(G, lat, lon)

    # 在图上找距离约 target_m/2 的候选转折点
    # 用 ego_graph 截取半径，再过滤太近/太远的节点
    half_m = target_m / 2
    try:
        sub = nx.ego_graph(G, origin_node, radius=half_m * 1.4, distance="length")
    except Exception:
        sub = G

    candidates = [
        n for n in sub.nodes
        if n != origin_node and
        nx.has_path(G, origin_node, n) and
        nx.has_path(G, n, origin_node)
    ]

    if not candidates:
        warnings.warn("未找到可达转折点，请检查路网连通性或放宽约束条件。")
        return []

    waypoints = random.sample(candidates, min(waypoint_sample, len(candidates)))

    routes: list[tuple[float, list[int]]] = []
    for wp in waypoints:
        try:
            path_out  = nx.shortest_path(G, origin_node, wp,         weight="walk_cost")
            path_back = nx.shortest_path(G, wp,          origin_node, weight="walk_cost")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue

        full_route = path_out + path_back[1:]
        length_m   = _route_length_m(G, full_route)

        # 长度过滤：0.7x ~ 1.3x 目标长度
        if 0.7 * target_m <= length_m <= 1.3 * target_m:
            diff = abs(length_m - target_m)
            routes.append((diff, full_route))

    # 按与目标长度差值升序，去重（起点相同、转折点不同路线视为不同）
    routes.sort(key=lambda x: x[0])
    return [r for _, r in routes[:n_routes]]


# ── 步骤 5：为路线计算多维度分数 ─────────────────────────────────────────

def score_route(
    G: nx.MultiDiGraph,
    node_list: list[int],
) -> dict[str, float]:
    """
    计算一条路线的多维度平均分（各边长度加权平均）。

    Returns
    -------
    dict with keys:
        total_km, greenery, water, quiet, commercial, surface
    """
    total_len = 0.0
    weighted  = {k: 0.0 for k in ("greenery", "water", "quiet", "commercial", "surface")}

    for a, b in zip(node_list[:-1], node_list[1:]):
        edge_data = G.get_edge_data(a, b)
        if not edge_data:
            continue
        # 取第一条平行边
        data = next(iter(edge_data.values()))
        length_m = data.get("length", 1.0)
        total_len += length_m

        weighted["greenery"]   += data.get("greenery_score",   0.0) * length_m
        weighted["water"]      += data.get("water_score",      0.0) * length_m
        weighted["quiet"]      += data.get("quietness_score",  0.5) * length_m
        weighted["commercial"] += data.get("commercial_score", 0.0) * length_m
        weighted["surface"]    += data.get("surface_score",    0.5) * length_m

    if total_len == 0:
        return {"total_km": 0.0, **{k: 0.0 for k in weighted}}

    result: dict[str, float] = {"total_km": round(total_len / 1000, 2)}
    for k, v in weighted.items():
        result[k] = round(v / total_len, 3)
    return result


# ── 步骤 6：软排序 ──────────────────────────────────────────────────────

def rank_routes(
    G: nx.MultiDiGraph,
    candidate_routes: list[list[int]],
    pref_weights: dict[str, float],
) -> list[dict]:
    """
    对候选路线按用户偏好加权排序，返回含分数的字典列表（降序）。

    Returns
    -------
    list of dict，每条路线包含：
        node_list   节点 ID 列表
        total_km    路线总长度
        score       综合偏好分（0-1）
        greenery / water / quiet / commercial / surface  各维度均值
    """
    total_w = sum(abs(v) for v in pref_weights.values()) or 1.0

    results = []
    for nodes in candidate_routes:
        s = score_route(G, nodes)
        composite = sum(
            pref_weights.get(dim, 0.0) * s.get(dim, 0.0)
            for dim in ("greenery", "water", "quiet", "commercial", "surface")
        ) / total_w
        composite = max(0.0, composite)
        results.append({
            "node_list": nodes,
            "score":     round(composite, 4),
            **s,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ── 辅助：快速获取绿地/水体 GeoDataFrame（OSMnx）─────────────────────────

def fetch_green_water(place: str) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    从 OSM 获取指定地点的绿地和水体面数据。
    返回 (green_gdf, water_gdf)，WGS84 坐标系。

    用法：
        green_gdf, water_gdf = fetch_green_water("杨浦区, 上海市, 中国")
        G = enrich_graph_edges(G, green_gdf=green_gdf, water_gdf=water_gdf)
    """
    green_tags = {
        "landuse": ["grass", "forest", "meadow", "recreation_ground", "village_green"],
        "leisure": ["park", "garden", "nature_reserve", "pitch"],
        "natural": ["wood", "scrub", "grassland"],
    }
    water_tags = {
        "natural": ["water", "wetland"],
        "waterway": ["river", "canal", "stream"],
        "landuse": ["reservoir"],
    }

    green_gdf = ox.features_from_place(place, tags=green_tags)
    green_gdf = green_gdf[green_gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]

    water_gdf = ox.features_from_place(place, tags=water_tags)
    water_gdf = water_gdf[water_gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon",
                                                               "LineString", "MultiLineString"])]

    return green_gdf.reset_index(drop=True), water_gdf.reset_index(drop=True)
