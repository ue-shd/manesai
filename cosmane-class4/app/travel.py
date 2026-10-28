"""緯度経度から、地点間の移動時間・移動費を近似で計算する。API 側の変換処理。

移動時間は分単位の整数に切り上げる（近似のデータとして作るため）。
"""

import math

EARTH_R = 6_371_000  # m


def distance(a, b) -> float:
    """2点間の直線距離（m）。"""
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def fare(mode, path_m: float, ride_min: int) -> int:
    """料金方式ごとの運賃（円）。"""
    kind, value = mode["fare_type"], str(mode.get("fare_value", "")).strip()
    if kind == "無料":
        return 0
    if kind == "均一":
        return int(float(value))
    pairs = [p.split(":") for p in value.split(",")]
    if kind == "距離帯":  # "3000:190,7000:240,:390" → 3000m 以下 190円 …
        for limit, yen in pairs:
            if limit == "" or path_m <= float(limit):
                return int(float(yen))
        raise ValueError(f"{mode['name']}: 距離帯の最後は「:料金」（上限なし）にしてください")
    if kind == "初乗り加算":  # "1200:600,231:100" → 1.2km まで 600円、以降 231m ごとに 100円
        (base_m, base_yen), (step_m, step_yen) = pairs
        extra = max(0.0, path_m - float(base_m))
        return int(float(base_yen)) + math.ceil(extra / float(step_m)) * int(float(step_yen))
    if kind == "時間":  # "30:165" → 30分ごとに 165円
        ((unit, yen),) = pairs
        return max(1, math.ceil(ride_min / float(unit))) * int(float(yen))
    raise ValueError(f"{mode['name']}: 料金方式「{kind}」は使えません（無料・均一・距離帯・初乗り加算・時間）")


def _walk_mode(modes):
    for m in modes:
        if m["name"] == "徒歩":
            return m
    return {"speed": 80, "detour": 1.3}


def _nearest(point, stations):
    return min(stations, key=lambda s: distance(point, s))


def leg_options(a, b, modes, stations) -> list[dict]:
    """a から b へ行く手段ごとの {mode, walk_min, ride_min, cost}。使えない手段は含めない。"""
    walk = _walk_mode(modes)
    d = distance(a, b)
    out = []
    for m in modes:
        speed, detour = float(m["speed"]), float(m["detour"])
        wait, end_walk = int(m["wait"]), int(m["end_walk"])
        if m["route"] == "直行":
            path = d * detour
            moving = math.ceil(path / speed)
            if m["name"] == "徒歩":
                out.append({"mode": m["name"], "walk_min": moving, "ride_min": 0, "cost": fare(m, path, moving)})
            else:
                out.append({"mode": m["name"], "walk_min": end_walk, "ride_min": moving + wait,
                            "cost": fare(m, path, moving)})
        elif m["route"] == "駅経由":
            if not stations:
                continue
            sa, sb = _nearest(a, stations), _nearest(b, stations)
            if sa["id"] == sb["id"]:
                continue  # 同じ駅なら電車は使わない
            to_station = distance(a, sa) * float(walk["detour"]) / float(walk["speed"])
            from_station = distance(sb, b) * float(walk["detour"]) / float(walk["speed"])
            path = distance(sa, sb) * detour
            moving = math.ceil(path / speed)
            out.append({"mode": m["name"], "walk_min": math.ceil(to_station) + math.ceil(from_station),
                        "ride_min": moving + wait, "cost": fare(m, path, moving), "via": [sa["id"], sb["id"]]})
        else:
            raise ValueError(f"{m['name']}: 経路「{m['route']}」は使えません（直行・駅経由）")
    return out


def build_legs(shops, stations, modes, start_id, end_id) -> list[dict]:
    """出発駅・店・到着駅の間の、必要な組み合わせすべての移動。"""
    st = {s["id"]: s for s in stations}
    start, end = st[start_id], st[end_id]
    pairs = [(start, s) for s in shops] + [(s, end) for s in shops] + [(start, end)]
    pairs += [(s, t) for s in shops for t in shops if s["id"] != t["id"]]
    legs = []
    for a, b in pairs:
        for o in leg_options(a, b, modes, stations):
            legs.append({"from": a["id"], "to": b["id"], **o})
    return legs
