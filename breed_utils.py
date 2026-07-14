"""
breed_utils.py
==============
犬种属性表加载与查询工具，供 Jupyter Notebook 导入使用。

快速上手
--------
    from breed_utils import load_breed_table, lookup_breed, ALIAS_MAP

    df = load_breed_table()          # 加载完整属性表（DataFrame）
    row = lookup_breed("哈士奇")     # 别名/标准名均可，返回 pd.Series
    row = lookup_breed("西伯利亚雪橇犬")

依赖
----
    pip install pandas
"""

from __future__ import annotations
import json
import pandas as pd
from pathlib import Path

# ── 路径约定 ──────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
CSV_PATH  = _HERE / "dog_breed_attributes.csv"
JSON_PATH = _HERE / "breed_alias_map.json"

# ── 列类型定义 ────────────────────────────────────────────────────────────
_FLOAT_COLS = [
    "体重_min_kg", "体重_max_kg",
    "精力等级_0to1", "推荐最大路线长度_km",
    "耐热性_0to1", "坡度容忍_0to1",
]
_BOOL_COLS = ["是否短鼻犬", "是否短腿犬"]
_CAT_COLS  = ["体型分级", "毛发长度", "关节风险默认值"]

# 体型有序分类（用于排序/比较）
_SIZE_ORDER    = pd.CategoricalDtype(["toy", "small", "medium", "large"], ordered=True)
_COAT_ORDER    = pd.CategoricalDtype(["short", "medium", "long"],         ordered=True)
_JOINT_ORDER   = pd.CategoricalDtype(["low", "medium", "high"],           ordered=True)


# ── 公开函数 ──────────────────────────────────────────────────────────────

def load_breed_table(csv_path: str | Path = CSV_PATH) -> pd.DataFrame:
    """
    读取犬种属性 CSV，设定正确的列类型，以 '犬种中文名' 为索引。

    Returns
    -------
    pd.DataFrame
        每行一个品种，列类型已正确转换。
    """
    df = pd.read_csv(csv_path, encoding="utf-8-sig")

    # 数值列
    for col in _FLOAT_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 布尔列（"是"/"否" → True/False）
    for col in _BOOL_COLS:
        df[col] = df[col].map({"是": True, "否": False})

    # 有序分类列
    df["体型分级"]       = pd.Categorical(df["体型分级"],       categories=_SIZE_ORDER.categories,  ordered=True)
    df["毛发长度"]       = pd.Categorical(df["毛发长度"],       categories=_COAT_ORDER.categories,  ordered=True)
    df["关节风险默认值"] = pd.Categorical(df["关节风险默认值"], categories=_JOINT_ORDER.categories, ordered=True)

    # 别名列：拆成 list（空字符串 → 空 list）
    df["别名"] = df["别名"].fillna("").apply(
        lambda s: [x.strip() for x in s.split("、") if x.strip()]
    )

    df = df.set_index("犬种中文名")
    return df


def load_alias_map(json_path: str | Path = JSON_PATH) -> dict[str, str]:
    """
    加载别名映射字典。

    Returns
    -------
    dict
        { "哈士奇": "西伯利亚雪橇犬", "泰迪": "玩具贵宾（泰迪）", ... }
    """
    with open(json_path, encoding="utf-8") as f:
        return json.load(f)


# 模块级缓存（避免重复 IO）
_df_cache: pd.DataFrame | None = None
_alias_cache: dict | None = None


def _get_df() -> pd.DataFrame:
    global _df_cache
    if _df_cache is None:
        _df_cache = load_breed_table()
    return _df_cache


def _get_alias() -> dict:
    global _alias_cache
    if _alias_cache is None:
        _alias_cache = load_alias_map()
    return _alias_cache


# 便捷别名映射（直接从模块访问）
ALIAS_MAP: dict[str, str] = {}   # 延迟加载，首次 lookup 时填充


def lookup_breed(name: str) -> pd.Series | None:
    """
    按品种名或别名查询属性，返回对应行（pd.Series）。
    找不到时返回 None 并打印提示。

    Parameters
    ----------
    name : str
        标准中文名或任意别名，例如 "哈士奇"、"泰迪"、"西伯利亚雪橇犬"。

    Examples
    --------
    >>> row = lookup_breed("哈士奇")
    >>> row["推荐最大路线长度_km"]
    7.0
    """
    global ALIAS_MAP
    if not ALIAS_MAP:
        ALIAS_MAP.update(_get_alias())

    canonical = ALIAS_MAP.get(name)
    if canonical is None:
        print(f"[breed_utils] 未找到品种：'{name}'，请检查拼写或使用自定义问卷分支。")
        return None

    df = _get_df()
    if canonical not in df.index:
        print(f"[breed_utils] 别名 '{name}' 映射到 '{canonical}'，但属性表中无此条目。")
        return None

    return df.loc[canonical]


def get_hard_constraints(
    breed_name: str,
    age_years: float,
    has_joint_issue: bool | None = None,
) -> dict:
    """
    根据品种、年龄、关节状况，计算路线硬性过滤参数。

    Parameters
    ----------
    breed_name    : 标准中文名或别名
    age_years     : 狗狗年龄（岁）
    has_joint_issue : True=有关节问题, False=没有, None=用户未填（用默认值兜底）

    Returns
    -------
    dict，包含：
        max_route_km   : 路线长度上限（km）
        max_slope_pct  : 最大坡度（%，超过此值的路段被过滤）
        allow_stairs   : 是否允许台阶路段
        heat_sensitive : 是否需要高温时段提醒
    """
    row = lookup_breed(breed_name)
    if row is None:
        return {}

    # 1. 基础值来自属性表
    max_km    = float(row["推荐最大路线长度_km"])
    slope_tol = float(row["坡度容忍_0to1"])       # 0-1 → 下面转成坡度%
    is_brachy = bool(row["是否短鼻犬"])
    is_short  = bool(row["是否短腿犬"])
    joint_def = str(row["关节风险默认值"])         # low/medium/high
    heat_tol  = float(row["耐热性_0to1"])

    # 2. 年龄折减：老年犬路线压缩
    size = str(row["体型分级"])
    senior_age = 7 if size in ("large", "medium") else 9   # 大型犬更早进入老年
    if age_years >= senior_age:
        age_factor = max(0.5, 1.0 - 0.08 * (age_years - senior_age))
        max_km = round(max_km * age_factor, 1)
    # 幼犬（< 1岁）也需要限制，骨骼发育期
    if age_years < 1:
        max_km = min(max_km, 1.5)

    # 3. 关节问题：优先级 has_joint_issue > 年龄推断 > 属性表默认值
    joint_issue_resolved: bool
    if has_joint_issue is True:
        joint_issue_resolved = True
    elif has_joint_issue is False:
        joint_issue_resolved = False
    else:
        # 用户未填：用属性表默认值 + 年龄兜底
        joint_issue_resolved = (joint_def == "high") or (age_years >= senior_age + 2)

    # 4. 坡度容忍 → 最大坡度%（线性映射：tol=1.0 → 15%，tol=0 → 0%）
    max_slope = round(slope_tol * 15, 1)
    allow_stairs = slope_tol >= 0.6

    if joint_issue_resolved:
        max_slope  = min(max_slope, 3.0)   # 关节问题：最大3%坡度
        allow_stairs = False
        max_km = round(max_km * 0.7, 1)   # 再压缩路线长度

    if is_short:
        max_slope  = min(max_slope, 4.0)
        allow_stairs = False

    # 5. 短鼻犬路线上限
    if is_brachy:
        max_km = min(max_km, 1.5)

    return {
        "max_route_km":   max_km,
        "max_slope_pct":  max_slope,
        "allow_stairs":   allow_stairs,
        "heat_sensitive": heat_tol < 0.4,
        # 调试用：记录推断过程
        "_joint_resolved": joint_issue_resolved,
        "_age_senior":     age_years >= senior_age,
    }
