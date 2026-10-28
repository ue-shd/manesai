"""計算部。データを受け取り、モデルを組んで解き、結果と状態を返す。

Web やファイルには依存しない（入力も出力も Python のデータ）。
モデルのきまりは docs/model.md を参照。

入力 problem（dict）:
    shops: [{id, name, open, close, stay, price, satisfaction, fullness}]  時刻は 0:00 からの分
    start, end: {id, name}                  出発地・到着地
    legs: [{from, to, mode, walk_min, ride_min, cost}]   地点間の移動（手段ごと）
    fatigue_coef: {手段: 1分あたりの疲労度}    "徒歩" は他の手段の徒歩部分にも使う
    time_start, time_end                    使える時間（分）
    meal_from, meal_to                      食事を始めてよい時間帯（分）
    meals_min, meals_max, meal_interval     食事の回数の範囲と、食事の開始どうしの最小の間隔（分）
    fullness_start, fullness_max            満腹度の出発時の値と上限
    digest_base, digest_extra, digest_threshold
                                            消化（1時間あたり）。疲労度がしきい値以上の区間は extra を足す
    fatigue_start, fatigue_max, fatigue_recovery   疲労度の出発時の値・上限・1回の食事で下がる量
    food_budget, transport_budget           食費・移動費の上限（円）
    objective: "満足度最大" | "重み付き" | "移動費最小"
    weight                                  重み付き: 満足度 − weight × 移動費
    satisfaction_min                        移動費最小: 満足度の合計の下限
    plans, time_limit                       案の数、1案あたりの計算時間（秒）
    must, exclude                           必ず行く店・行かない店の id
"""

import math
import random
import time

from ortools.sat.python import cp_model

# 結果の状態（AGENTS.md の表と同じ文言）
OPTIMAL = "最適"
FEASIBLE = "最適とは限らない解"
INFEASIBLE = "解なし"
NOT_FOUND = "解が見つからない（時間切れ）"
INPUT_ERROR = "入力の誤り"

OBJECTIVES = ("満足度最大", "重み付き", "移動費最小")
METHOD_CPSAT = "CP-SAT"
METHOD_GREEDY = "簡易な方法"

# CP-SAT は整数しか扱えないので、内部ではこの倍率で整数にする（結果は元の値で計算し直す）
FAT = 1000   # 疲労度
FUL = 6000   # 満腹度（6000 = 60分 × 100 なので、1時間あたりの消化が小数2桁まで正確に入る）
SAT = 100    # 満足度
DAY = 1440   # 1日の分


class _InputError(ValueError):
    pass


class _Option:
    __slots__ = ("mode", "minutes", "walk", "ride", "cost", "fat")

    def __init__(self, mode, walk, ride, cost, fat):
        self.mode, self.walk, self.ride, self.cost, self.fat = mode, walk, ride, cost, fat
        self.minutes = walk + ride


class _Prep:
    """検証済みの入力と、計算用の索引。"""


# ---------------------------------------------------------------- 入力の検証

def _num(p, key, low=None, high=None, integer=False):
    v = p.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise _InputError(f"{key} は数値で指定してください")
    if integer and v != int(v):
        raise _InputError(f"{key} は整数で指定してください")
    if low is not None and v < low:
        raise _InputError(f"{key} は {low} 以上にしてください")
    if high is not None and v > high:
        raise _InputError(f"{key} は {high} 以下にしてください")
    return int(v) if integer else v


def _prepare(p) -> _Prep:
    P = _Prep()
    shops = p.get("shops") or []
    if not shops:
        raise _InputError("店が1つもありません")
    ids = [s.get("id") for s in shops]
    if len(set(ids)) != len(ids):
        raise _InputError("店の id が重複しています")
    for s in shops:
        name = s.get("name") or s.get("id")
        for key in ("open", "close", "stay"):
            _num(s, key, 0, DAY, integer=True)
        _num(s, "price", 0, integer=True)
        for key in ("satisfaction", "fullness"):
            _num(s, key, 0)
        if s["open"] >= s["close"]:
            raise _InputError(f"{name}: 開店が閉店より後になっています")
    P.shops = shops
    P.n = n = len(shops)
    P.START, P.END = n, n + 1
    P.index = {s["id"]: i for i, s in enumerate(shops)}
    for key in ("start", "end"):
        if not isinstance(p.get(key), dict) or "id" not in p[key]:
            raise _InputError(f"{key} がありません")
    if p["start"]["id"] in P.index or p["end"]["id"] in P.index:
        raise _InputError("出発地・到着地の id が店の id と重なっています")
    P.names = [s["name"] for s in shops] + [p["start"].get("name", ""), p["end"].get("name", "")]
    P.ids = ids + [p["start"]["id"], p["end"]["id"]]

    P.time_start = _num(p, "time_start", 0, DAY, integer=True)
    P.time_end = _num(p, "time_end", 0, DAY, integer=True)
    if P.time_start >= P.time_end:
        raise _InputError("使える時間の開始が終了より後になっています")
    P.meal_from = _num(p, "meal_from", 0, DAY, integer=True)
    P.meal_to = _num(p, "meal_to", 0, DAY, integer=True)
    if P.meal_from > P.meal_to:
        raise _InputError("食事の時間帯の開始が終了より後になっています")
    P.meals_min = _num(p, "meals_min", 0, integer=True)
    P.meals_max = _num(p, "meals_max", 0, integer=True)
    if P.meals_min > P.meals_max:
        raise _InputError("食事の最小回数が最大回数より大きくなっています")
    P.interval = _num(p, "meal_interval", 0, DAY, integer=True)
    P.f_start = _num(p, "fullness_start", 0)
    P.f_max = _num(p, "fullness_max", 0)
    if P.f_start > P.f_max:
        raise _InputError("出発時の満腹度が上限を超えています")
    P.d_base = _num(p, "digest_base", 0)
    P.d_extra = _num(p, "digest_extra", 0)
    P.d_th = _num(p, "digest_threshold", 0)
    P.g_start = _num(p, "fatigue_start", 0)
    P.g_max = _num(p, "fatigue_max", 0)
    if P.g_start > P.g_max:
        raise _InputError("出発時の疲労度が上限を超えています")
    P.g_rec = _num(p, "fatigue_recovery", 0)
    P.food_budget = _num(p, "food_budget", 0, integer=True)
    P.trans_budget = _num(p, "transport_budget", 0, integer=True)
    P.objective = p.get("objective")
    if P.objective not in OBJECTIVES:
        raise _InputError(f"objective は {' / '.join(OBJECTIVES)} のどれかにしてください")
    P.weight = _num(p, "weight", 0) if P.objective == "重み付き" else 0
    P.sat_min = _num(p, "satisfaction_min", 0) if P.objective == "移動費最小" else 0
    P.n_plans = _num(p, "plans", 1, 5, integer=True)
    P.time_limit = _num(p, "time_limit", 0.1, 3600)

    must, exclude = set(p.get("must") or []), set(p.get("exclude") or [])
    for sid in must | exclude:
        if sid not in P.index:
            raise _InputError(f"必ず行く店・行かない店に、ない店（{sid}）が指定されています")
    if must & exclude:
        raise _InputError("同じ店が「必ず行く」と「行かない」の両方に入っています")
    P.must = {P.index[s] for s in must}
    P.allowed = [i for i, s in enumerate(shops) if s["id"] not in exclude]
    if len(P.must) > P.meals_max:
        raise _InputError("必ず行く店の数が、食事の最大回数より多くなっています")
    P.K = min(P.meals_max, len(P.allowed))

    coef = p.get("fatigue_coef") or {}
    P.modes = list(coef)
    for m, c in coef.items():
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or c < 0:
            raise _InputError(f"手段「{m}」の疲労係数は 0 以上の数値にしてください")
    node_of = {**P.index, p["start"]["id"]: P.START, p["end"]["id"]: P.END}
    P.opts = {}  # (i, j) -> [_Option]
    for leg in p.get("legs") or []:
        if leg.get("from") not in node_of or leg.get("to") not in node_of:
            raise _InputError(f"移動の出発・到着に、ない地点があります（{leg.get('from')} → {leg.get('to')}）")
        m = leg.get("mode")
        if m not in coef:
            raise _InputError(f"手段「{m}」の疲労係数がありません")
        walk = _num(leg, "walk_min", 0, integer=True)
        ride = _num(leg, "ride_min", 0, integer=True)
        cost = _num(leg, "cost", 0, integer=True)
        if walk and "徒歩" not in coef:
            raise _InputError("徒歩の疲労係数がありません")
        fat = walk * coef.get("徒歩", 0) + ride * coef[m]
        i, j = node_of[leg["from"]], node_of[leg["to"]]
        if i == P.END or j == P.START or i == j:
            continue
        P.opts.setdefault((i, j), []).append(_Option(P.modes.index(m), walk, ride, cost, fat))
    return P


# ---------------------------------------------------------------- 1つの案を元の値でたどる

def _rate(P, tired):
    return P.d_base + (P.d_extra if tired else 0)


def _evaluate(P, seq):
    """seq = [(地点, 手段の番号, 開始時刻)]（最後は到着地と到着時刻）を、元の値で計算した結果にする。"""
    steps, trace, violations = [], [], []
    node, dep, F, G, last_t = P.START, P.time_start, P.f_start, P.g_start, None
    food = trans = sat = 0
    g_peak, visited = G, []
    steps.append({"kind": "出発", "place": P.names[P.START], "id": P.ids[P.START], "time": dep})
    trace.append({"time": dep, "fullness": F, "fatigue": G, "label": "出発"})
    for j, mi, t in seq:
        o = next(o for o in P.opts[(node, j)] if o.mode == mi)
        arr = dep + o.minutes
        G_arr = G + o.fat
        g_peak = max(g_peak, G_arr)
        trans += o.cost
        tired = G_arr >= P.d_th
        rate = _rate(P, tired)
        F_arr = max(0.0, F - rate * o.minutes / 60)
        steps.append({"kind": "移動", "from": P.names[node], "to": P.names[j], "from_id": P.ids[node],
                      "to_id": P.ids[j], "mode": P.modes[mi], "depart": dep, "arrive": arr, "minutes": o.minutes,
                      "walk_min": o.walk, "ride_min": o.ride, "cost": o.cost, "fatigue_added": o.fat,
                      "fatigue_after": G_arr})
        trace.append({"time": arr, "fullness": F_arr, "fatigue": G_arr, "label": f"{P.names[j]} に到着"})
        if G_arr > P.g_max + 1e-9:
            violations.append(f"{P.names[j]} に着いた時点で疲労度が上限を超えます")
        if j == P.END:
            if arr > P.time_end:
                violations.append("終了時刻までに到着地に着けません")
            steps.append({"kind": "到着", "place": P.names[j], "id": P.ids[j], "time": arr})
            end_time = arr
            break
        s = P.shops[j]
        F_before = max(0.0, F - rate * (t - dep) / 60)
        F_after = F_before + s["fullness"]
        G_after = max(0.0, G_arr - P.g_rec)
        if t < arr:
            violations.append(f"{s['name']}: 着く前に食事を始めています")
        if not (s["open"] <= t and t + s["stay"] <= s["close"]):
            violations.append(f"{s['name']}: 営業時間外です")
        if not (P.meal_from <= t <= P.meal_to):
            violations.append(f"{s['name']}: 食事の時間帯の外です")
        if last_t is not None and t - last_t < P.interval:
            violations.append(f"{s['name']}: 前の食事との間隔が足りません")
        if F_after > P.f_max + 1e-9:
            violations.append(f"{s['name']}: 満腹度が上限を超えます")
        steps.append({"kind": "食事", "shop": s["name"], "id": s["id"], "start": t, "end": t + s["stay"],
                      "wait_min": t - arr, "price": s["price"], "satisfaction": s["satisfaction"],
                      "fullness_before": F_before, "fullness_after": F_after,
                      "fatigue_before": G_arr, "fatigue_after": G_after, "tired": tired})
        trace.append({"time": t, "fullness": F_before, "fatigue": G_arr, "label": f"{s['name']} 食事前"})
        trace.append({"time": t, "fullness": F_after, "fatigue": G_after, "label": f"{s['name']} 食事後"})
        trace.append({"time": t + s["stay"], "fullness": F_after, "fatigue": G_after, "label": f"{s['name']} を出る"})
        food += s["price"]
        sat += s["satisfaction"]
        visited.append(j)
        node, dep, F, G, last_t = j, t + s["stay"], F_after, G_after, t

    if food > P.food_budget:
        violations.append("食費が上限を超えます")
    if trans > P.trans_budget:
        violations.append("移動費が上限を超えます")
    if not (P.meals_min <= len(visited) <= P.meals_max):
        violations.append("食事の回数が範囲の外です")
    if not P.must <= set(visited):
        violations.append("必ず行く店に行っていません")
    return {
        "shops": [P.ids[v] for v in visited],
        "totals": {
            "satisfaction": sat, "food_cost": food, "transport_cost": trans, "total_cost": food + trans,
            "meals": len(visited), "fatigue_peak": g_peak, "start_time": P.time_start, "end_time": end_time,
            "weighted_score": sat - P.weight * trans if P.objective == "重み付き" else None,
        },
        "steps": steps,
        "trace": trace,
        "violations": violations,
    }


# ---------------------------------------------------------------- 簡単な手順（時間内に CP-SAT が案を出せないとき用）

def _try_meal(P, st, s, o):
    """今の状態 st から、手段 o で店 s に行って食べた後の状態。できなければ None。"""
    shop = P.shops[s]
    arr = st["dep"] + o.minutes
    G_arr = st["G"] + o.fat
    if G_arr > P.g_max or st["trans"] + o.cost > P.trans_budget or st["food"] + shop["price"] > P.food_budget:
        return None
    t = max(arr, shop["open"], P.meal_from)
    if st["last_t"] is not None:
        t = max(t, st["last_t"] + P.interval)
    rate = _rate(P, G_arr >= P.d_th)
    room = P.f_max - shop["fullness"]
    if room < 0:
        return None
    if st["F"] - rate * (t - st["dep"]) / 60 > room:  # お腹が空くまで待つ
        if rate <= 0:
            return None
        t = max(t, st["dep"] + math.ceil((st["F"] - room) * 60 / rate))
    if t > P.meal_to or t + shop["stay"] > shop["close"]:
        return None
    F_before = max(0.0, st["F"] - rate * (t - st["dep"]) / 60)
    return {"node": s, "dep": t + shop["stay"], "F": F_before + shop["fullness"], "G": max(0.0, G_arr - P.g_rec),
            "last_t": t, "food": st["food"] + shop["price"], "trans": st["trans"] + o.cost,
            "sat": st["sat"] + shop["satisfaction"], "seq": st["seq"] + [(s, o.mode, t)],
            "visited": st["visited"] | {s}}


def _finish(P, st):
    """到着地への、条件を満たすいちばん安い（同じなら速い）移動。なければ None。"""
    best = None
    for o in P.opts.get((st["node"], P.END), []):
        if st["dep"] + o.minutes <= P.time_end and st["G"] + o.fat <= P.g_max and st["trans"] + o.cost <= P.trans_budget:
            if best is None or (o.cost, o.minutes) < (best.cost, best.minutes):
                best = o
    return best


def _greedy(P, banned=frozenset(), style=0, rng=None):
    """店を1つずつ選ぶ簡単な手順。style で選び方の重みを変え、rng があれば少し揺らす。"""
    st = {"node": P.START, "dep": P.time_start, "F": P.f_start, "G": P.g_start, "last_t": None,
          "food": 0, "trans": 0, "sat": 0, "seq": [], "visited": frozenset()}
    while len(st["seq"]) < P.K:
        done_min = len(st["seq"]) >= P.meals_min and P.must <= st["visited"]
        if P.objective == "移動費最小" and done_min and st["sat"] >= P.sat_min:
            break
        best = None
        for s in P.allowed:
            if s in st["visited"] or s in banned:
                continue
            for o in P.opts.get((st["node"], s), []):
                nxt = _try_meal(P, st, s, o)
                if nxt is None or _finish(P, nxt) is None:
                    continue
                gain = P.shops[s]["satisfaction"] - (P.weight * o.cost if P.objective == "重み付き" else 0)
                score = gain
                if style == 1:    # お腹にたまりにくい店を優先
                    score = gain / (10 + P.shops[s]["fullness"])
                elif style == 2:  # 待ち時間が短い店を優先
                    score = gain - 0.2 * (nxt["last_t"] - st["dep"])
                if rng is not None:
                    score *= 1 + rng.uniform(-0.3, 0.3)
                key = (s in P.must, score, -o.cost, -o.minutes)
                if best is None or key > best[0]:
                    best = (key, nxt)
        if best is None:
            break
        if P.objective == "重み付き" and done_min and \
                P.shops[best[1]["node"]]["satisfaction"] - P.weight * (best[1]["trans"] - st["trans"]) <= 0:
            break
        st = best[1]
    o = _finish(P, st)
    if o is None or len(st["seq"]) < P.meals_min or not P.must <= st["visited"]:
        return None
    if P.objective == "移動費最小" and st["sat"] < P.sat_min:
        return None
    return st["seq"] + [(P.END, o.mode, st["dep"] + o.minutes)]


def _shop_set(seq):
    return frozenset(j for j, _, _ in seq[:-1])


def _goodness(P, seq):
    """目的に沿った良さ（大きいほど良い）。"""
    sat = sum(P.shops[j]["satisfaction"] for j, _, _ in seq[:-1])
    node, trans = P.START, 0
    for j, mi, _ in seq:
        trans += next(o.cost for o in P.opts[(node, j)] if o.mode == mi)
        node = j
    if P.objective == "満足度最大":
        return (sat, -trans)
    if P.objective == "重み付き":
        return (sat - P.weight * trans, 0)
    return (-trans, sat)


def _greedy_diverse(P, prev_sets, tries=60):
    """これまでの案と店の組み合わせが違う、簡単な手順の案のうち、いちばん良いもの。"""
    rng = random.Random(0)
    bans = [frozenset()] + [frozenset({s}) for prev in prev_sets for s in sorted(prev)]
    best = None
    for n in range(tries):
        style = n % 3
        seq = _greedy(P, bans[n % len(bans)], style, rng if n >= 3 else None)
        if seq is None or _shop_set(seq) in prev_sets:
            continue
        if best is None or _goodness(P, seq) > _goodness(P, best):
            best = seq
    return best


# ---------------------------------------------------------------- CP-SAT のモデル

def _build(P, prev_sets):
    m = cp_model.CpModel()
    n, K, START, END = P.n, P.K, P.START, P.END
    NONE = len(P.modes)  # 到着地にとどまる（移動しない）
    v = {}

    # 位置 k（1..K）に入る地点。食事しない位置は到着地（END）にし、END の後はずっと END
    node = [m.NewConstant(START)]
    x = [None]
    is_end = [None]
    for k in range(1, K + 1):
        nk = m.NewIntVarFromDomain(cp_model.Domain.FromValues(P.allowed + [END]), f"node{k}")
        xk = {s: m.NewBoolVar(f"x{k}_{s}") for s in P.allowed}
        ek = m.NewBoolVar(f"end{k}")
        m.AddExactlyOne(list(xk.values()) + [ek])
        m.Add(nk == sum(s * b for s, b in xk.items()) + END * ek)
        node.append(nk)
        x.append(xk)
        is_end.append(ek)
    node.append(m.NewConstant(END))
    for k in range(2, K + 1):
        m.AddImplication(is_end[k - 1], is_end[k])

    visit = {s: sum(x[k][s] for k in range(1, K + 1)) for s in P.allowed}
    for s in P.allowed:
        m.Add(visit[s] <= 1)
    for s in P.must:
        m.Add(visit[s] == 1)
    meals = sum(1 - is_end[k] for k in range(1, K + 1))
    m.Add(meals >= P.meals_min)

    def lin(k, attr):
        return sum(int(P.shops[s][attr]) * x[k][s] for s in P.allowed)

    # 移動（位置 k-1 → k）。手段と所要時間・費用・疲労度を表で結びつける
    def tuples(froms):
        out = []
        for i in froms:
            for j in P.allowed + [END]:
                for o in P.opts.get((i, j), []):
                    out.append((i, j, o.mode, o.minutes, o.cost, math.ceil(o.fat * FAT - 1e-9)))
        return out

    t_first = tuples([START])
    t_mid = tuples(P.allowed) + [(END, END, NONE, 0, 0, 0)]
    max_min = max([t[3] for t in t_first + t_mid] + [0])
    max_cost = max([t[4] for t in t_first + t_mid] + [0])
    max_fat = max([t[5] for t in t_first + t_mid] + [0])
    mode, lmin, lcost, lfat = [None], [None], [None], [None]
    for k in range(1, K + 2):
        mk = m.NewIntVar(0, NONE, f"mode{k}")
        tk = m.NewIntVar(0, max_min, f"lmin{k}")
        ck = m.NewIntVar(0, max_cost, f"lcost{k}")
        fk = m.NewIntVar(0, max_fat, f"lfat{k}")
        m.AddAllowedAssignments([node[k - 1], node[k], mk, tk, ck, fk], t_first if k == 1 else t_mid)
        mode.append(mk)
        lmin.append(tk)
        lcost.append(ck)
        lfat.append(fk)

    # 時刻
    t = [m.NewConstant(P.time_start)]
    dep = [m.NewConstant(P.time_start)]
    for k in range(1, K + 1):
        tk = m.NewIntVar(0, DAY, f"t{k}")
        dk = m.NewIntVar(0, 2 * DAY, f"dep{k}")
        m.Add(dk == tk + lin(k, "stay"))
        arr = dep[k - 1] + lmin[k]
        m.Add(tk >= arr)
        m.Add(tk == arr).OnlyEnforceIf(is_end[k])
        m.Add(tk >= lin(k, "open"))
        m.Add(dk <= lin(k, "close") + 2 * DAY * is_end[k])
        m.Add(tk >= P.meal_from).OnlyEnforceIf(is_end[k].Not())
        m.Add(tk <= P.meal_to).OnlyEnforceIf(is_end[k].Not())
        if k >= 2:
            m.Add(tk - t[k - 1] >= P.interval).OnlyEnforceIf(is_end[k].Not())
        t.append(tk)
        dep.append(dk)
    m.Add(dep[K] + lmin[K + 1] <= P.time_end)

    # 疲労度：移動で増え、食事で下がる。着いた時点で上限以下
    g_max = math.floor(P.g_max * FAT + 1e-9)
    g_rec = math.floor(P.g_rec * FAT + 1e-9)
    g_th = math.ceil(P.d_th * FAT - 1e-9)
    G_after = [m.NewConstant(math.ceil(P.g_start * FAT - 1e-9))]
    tired = [None]
    for k in range(1, K + 2):
        ga = m.NewIntVar(0, g_max, f"garr{k}")
        m.Add(ga == G_after[k - 1] + lfat[k])
        if k <= K:
            tk_ = m.NewBoolVar(f"tired{k}")
            m.Add(ga >= g_th).OnlyEnforceIf(tk_)
            m.Add(ga < g_th).OnlyEnforceIf(tk_.Not())
            gn = m.NewIntVar(0, g_max, f"gafter{k}")
            m.AddMaxEquality(gn, [0, ga - g_rec * (1 - is_end[k])])
            tired.append(tk_)
            G_after.append(gn)

    # 満腹度：時間とともに下がり（疲れている区間は速く）、食べた直後に上限以下
    f_max = math.floor(P.f_max * FUL + 1e-9)
    base = math.floor(P.d_base * FUL / 60 + 1e-9)    # 1分あたり
    extra = math.floor(P.d_extra * FUL / 60 + 1e-9)
    F_after = m.NewConstant(math.ceil(P.f_start * FUL - 1e-9))
    for k in range(1, K + 1):
        e = m.NewIntVar(0, 2 * DAY, f"elapsed{k}")
        m.Add(e == t[k] - dep[k - 1])
        z = m.NewIntVar(0, 2 * DAY, f"tired_min{k}")
        m.Add(z == e).OnlyEnforceIf(tired[k])
        m.Add(z == 0).OnlyEnforceIf(tired[k].Not())
        # 満腹度は低いほど条件を満たしやすいので、max(0, …) は「以上」の2本で書ける（解は変わらない）
        fb = m.NewIntVar(0, f_max, f"fbefore{k}")
        m.Add(fb >= F_after - base * e - extra * z)
        fa = m.NewIntVar(0, f_max, f"fafter{k}")
        m.Add(fa == fb + sum(math.ceil(P.shops[s]["fullness"] * FUL - 1e-9) * x[k][s] for s in P.allowed))
        F_after = fa
        # 冗長な制約（解は変わらず、探索が速くなる）: いちばん速く消化したとしても、
        # 出発時 + それまでに食べた量 − 消化（食事中を除く経過時間）が上限を超えないこと
        eaten = sum(math.ceil(P.shops[s]["fullness"] * FUL - 1e-9) * x[i][s] for i in range(1, k + 1) for s in P.allowed)
        stays = sum(lin(i, "stay") for i in range(1, k))
        m.Add(math.ceil(P.f_start * FUL - 1e-9) + eaten - (base + extra) * (t[k] - P.time_start - stays) <= f_max) \
            .OnlyEnforceIf(is_end[k].Not())

    # 予算
    m.Add(sum(int(P.shops[s]["price"]) * visit[s] for s in P.allowed) <= P.food_budget)
    trans = sum(lcost[k] for k in range(1, K + 2))
    m.Add(trans <= P.trans_budget)

    # これまでの案と、店の組み合わせが少なくとも1店違う
    for S in prev_sets:
        m.Add(sum(1 - visit[s] for s in S if s in visit) + sum(visit[s] for s in P.allowed if s not in S) >= 1)

    sat = sum(round(P.shops[s]["satisfaction"] * SAT) * visit[s] for s in P.allowed)
    v.update(node=node, x=x, mode=mode, t=t, lmin=lmin, lcost=lcost, lfat=lfat, dep=dep, is_end=is_end,
             sat=sat, trans=trans)
    return m, v


def _hint(m, v, P, seq):
    """seq を解の候補として渡す（地点・手段・時刻と、移動の値）。"""
    m.ClearHints()
    NONE = len(P.modes)
    seq = list(seq)
    seq += [(P.END, NONE, seq[-1][2])] * (P.K + 1 - len(seq))
    prev, dep = P.START, P.time_start
    for k, (j, mi, tt) in enumerate(seq, start=1):
        if k <= P.K:
            m.AddHint(v["node"][k], j)
            m.AddHint(v["is_end"][k], j == P.END)
            for s, b in v["x"][k].items():
                m.AddHint(b, s == j)
        m.AddHint(v["mode"][k], mi)
        if mi == NONE:
            minutes = cost = fat = 0
        else:
            o = next(o for o in P.opts[(prev, j)] if o.mode == mi)
            minutes, cost, fat = o.minutes, o.cost, math.ceil(o.fat * FAT - 1e-9)
        m.AddHint(v["lmin"][k], minutes)
        m.AddHint(v["lcost"][k], cost)
        m.AddHint(v["lfat"][k], fat)
        if k <= P.K:
            m.AddHint(v["t"][k], tt)
            stay = 0 if j == P.END else P.shops[j]["stay"]
            m.AddHint(v["dep"][k], tt + stay)
            dep = tt + stay
        prev = j


def _extract(solver, v, P):
    seq = []
    for k in range(1, P.K + 2):
        j = P.END if k == P.K + 1 else solver.Value(v["node"][k])
        mi = solver.Value(v["mode"][k])
        if j == P.END:
            arr = solver.Value(v["dep"][k - 1]) + solver.Value(v["lmin"][k])
            seq.append((P.END, mi, arr))
            break
        seq.append((j, mi, solver.Value(v["t"][k])))
    return seq


def _run(m, seconds):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.05, seconds)
    solver.parameters.num_workers = 8
    solver.parameters.cp_model_probing_level = 0  # 前処理を軽くして、早く解を出す
    return solver, solver.Solve(m)


def _cpsat(P, prev_sets, hint_seq):
    """CP-SAT で1案を解く。(状態, seq) を返す（seq は案がなければ None）。"""
    m, v = _build(P, prev_sets)
    if hint_seq is not None:
        _hint(m, v, P, hint_seq)
    t0 = time.monotonic()
    ok = (cp_model.OPTIMAL, cp_model.FEASIBLE)

    if P.objective == "満足度最大":
        m.Maximize(v["sat"])
        solver, st = _run(m, P.time_limit * 0.7)  # 残りの時間で移動費を下げる
        if st == cp_model.INFEASIBLE:
            return INFEASIBLE, None
        if st not in ok:
            return NOT_FOUND, None
        first = _extract(solver, v, P)
        proven = st == cp_model.OPTIMAL
        # 満足度を保ったまま、移動費を最小にする
        m.Add(v["sat"] >= round(solver.ObjectiveValue()))
        m.Minimize(v["trans"])
        _hint(m, v, P, first)
        solver2, st2 = _run(m, P.time_limit - (time.monotonic() - t0))
        if st2 in ok:
            return (OPTIMAL if proven and st2 == cp_model.OPTIMAL else FEASIBLE), _extract(solver2, v, P)
        return FEASIBLE, first

    if P.objective == "重み付き":
        m.Maximize(v["sat"] - round(P.weight * SAT) * v["trans"])
    else:
        m.Add(v["sat"] >= math.ceil(P.sat_min * SAT - 1e-9))
        m.Minimize(v["trans"])
    solver, st = _run(m, P.time_limit)
    if st == cp_model.OPTIMAL:
        return OPTIMAL, _extract(solver, v, P)
    if st == cp_model.FEASIBLE:
        return FEASIBLE, _extract(solver, v, P)
    if st == cp_model.INFEASIBLE:
        return INFEASIBLE, None
    return NOT_FOUND, None


# ---------------------------------------------------------------- 全体

def _infeasible_reason(P):
    hints = []
    open_in_window = [s for s in P.allowed
                      if P.shops[s]["open"] <= P.meal_to and
                      max(P.shops[s]["open"], P.meal_from) + P.shops[s]["stay"] <= P.shops[s]["close"]]
    if len(open_in_window) < P.meals_min:
        hints.append(f"食事の時間帯に営業している店が {len(open_in_window)} 店しかなく、最小回数 {P.meals_min} 回に届きません")
    for s in P.must:
        if s not in open_in_window:
            hints.append(f"必ず行く店「{P.shops[s]['name']}」は、食事の時間帯に営業していません")
    direct = P.opts.get((P.START, P.END), [])
    if P.meals_min == 0 and direct and min(o.minutes for o in direct) > P.time_end - P.time_start:
        hints.append("使える時間内に到着地まで移動できません")
    hints.append("見直す候補: 食事の最小回数・時間帯・最小の間隔、食費・移動費の上限、疲労度の上限、満腹度の上限、必ず行く店")
    return "条件をすべて満たすプランはありません。" + " / ".join(hints)


def plan(problem):
    """上位の案を返す。{status, message, plans}"""
    try:
        P = _prepare(problem)
    except _InputError as e:
        return {"status": INPUT_ERROR, "message": str(e), "plans": None}

    plans, prev_sets, note = [], [], ""
    for rank in range(1, P.n_plans + 1):
        hint = _greedy_diverse(P, prev_sets)
        status, seq = _cpsat(P, prev_sets, hint)
        method = METHOD_CPSAT
        if seq is None and hint is not None:  # 時間内に CP-SAT が案を出せなかった
            status, seq, method = FEASIBLE, hint, METHOD_GREEDY
        if seq is None:
            if rank == 1:
                msg = _infeasible_reason(P) if status == INFEASIBLE else \
                    "制限時間内に条件を満たす案が見つかりませんでした。計算時間を延ばすか、条件をゆるめてください"
                return {"status": status, "message": msg, "plans": None}
            note = (f"{rank}番目以降の案はありません（それまでの案と店の組み合わせが違う案が、条件を満たしません）"
                    if status == INFEASIBLE else f"{rank}番目以降の案は、制限時間内に見つかりませんでした")
            break
        result = _evaluate(P, seq)
        result.update(status=status, method=method, _goodness=_goodness(P, seq))
        plans.append(result)
        prev_sets.append(_shop_set(seq))
    # 時間切れで途中の解を返したときは、後の案のほうが良いことがあるので、良い順に並べ直す
    plans.sort(key=lambda r: r.pop("_goodness"), reverse=True)
    for rank, r in enumerate(plans, start=1):
        r["rank"] = rank
    return {"status": plans[0]["status"], "message": note, "plans": plans}
