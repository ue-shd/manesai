import pytest

from app import solver


def shop(id, sat, full=30, price=1000, open=600, close=1260, stay=30):
    return {"id": id, "name": id, "open": open, "close": close, "stay": stay, "price": price,
            "satisfaction": sat, "fullness": full}


def make(shops, minutes=10, **kw):
    """どの2地点の間も、徒歩 minutes 分（無料）とタクシー 2 分（500円）で移動できる問題。"""
    points = [s["id"] for s in shops] + ["S", "E"]
    legs = []
    for a in points:
        for b in points:
            if a != b:
                legs.append({"from": a, "to": b, "mode": "徒歩", "walk_min": minutes, "ride_min": 0, "cost": 0})
                legs.append({"from": a, "to": b, "mode": "タクシー", "walk_min": 0, "ride_min": 2, "cost": 500})
    p = {
        "shops": shops, "start": {"id": "S", "name": "出発駅"}, "end": {"id": "E", "name": "到着駅"},
        "legs": legs, "fatigue_coef": {"徒歩": 1.0, "タクシー": 0.1},
        "time_start": 600, "time_end": 1260, "meal_from": 600, "meal_to": 1200,
        "meals_min": 1, "meals_max": 3, "meal_interval": 60,
        "fullness_start": 0, "fullness_max": 100, "digest_base": 10, "digest_extra": 10, "digest_threshold": 50,
        "fatigue_start": 0, "fatigue_max": 100, "fatigue_recovery": 20,
        "food_budget": 10000, "transport_budget": 10000,
        "objective": "満足度最大", "weight": 0, "satisfaction_min": 0, "plans": 1, "time_limit": 5,
        "must": [], "exclude": [],
    }
    p.update(kw)
    return p


def test_picks_best_shops_and_walks_when_satisfaction_ties():
    r = solver.plan(make([shop("a", 90), shop("b", 80), shop("c", 70), shop("d", 10)]))
    assert r["status"] == solver.OPTIMAL
    plan = r["plans"][0]
    assert set(plan["shops"]) == {"a", "b", "c"}
    assert plan["totals"]["satisfaction"] == 240
    assert plan["totals"]["transport_cost"] == 0  # 満足度が同じなら移動費が安い方（徒歩）
    assert plan["violations"] == []


def test_fullness_limit_reduces_meals():
    # 満腹度 60 の店を 1 時間おきに食べると、消化 10/時間 では 2 回目で上限を超える
    r = solver.plan(make([shop("a", 90, full=60), shop("b", 80, full=60)], time_end=700, meal_to=700))
    assert r["status"] == solver.OPTIMAL
    assert r["plans"][0]["totals"]["meals"] == 1


def test_food_and_transport_budget():
    r = solver.plan(make([shop("a", 90, price=3000), shop("b", 80, price=500), shop("c", 70, price=500)],
                         food_budget=1500))
    assert set(r["plans"][0]["shops"]) == {"b", "c"}


def test_fatigue_limit_forces_taxi():
    # 徒歩 60 分（疲労 60）を2回続けると上限 100 を超えるので、どこかでタクシーを使う
    r = solver.plan(make([shop("a", 90)], minutes=60, fatigue_recovery=0, time_end=1300))
    plan = r["plans"][0]
    assert plan["violations"] == []
    assert plan["totals"]["fatigue_peak"] <= 100
    assert plan["totals"]["transport_cost"] == 500


def test_tired_digests_faster():
    # 着いた時点の疲労度がしきい値以上なら、その区間は消化が速い（基本 10 + 追加 10）
    r = solver.plan(make([shop("a", 90, full=50)], minutes=60, fullness_start=40, meals_max=1,
                         digest_threshold=50, transport_budget=0))
    meal = next(s for s in r["plans"][0]["steps"] if s["kind"] == "食事")
    assert meal["tired"] is True
    assert meal["fullness_before"] == pytest.approx(40 - 20 * (meal["start"] - 600) / 60)


def test_must_and_exclude():
    shops = [shop("a", 90), shop("b", 80), shop("c", 70), shop("d", 10)]
    plan = solver.plan(make(shops, must=["d"], exclude=["a"]))["plans"][0]
    assert "d" in plan["shops"] and "a" not in plan["shops"]


def test_opening_hours_and_waiting():
    plan = solver.plan(make([shop("a", 90, open=720, close=800)], meals_max=1))["plans"][0]
    meal = next(s for s in plan["steps"] if s["kind"] == "食事")
    assert meal["start"] >= 720 and meal["wait_min"] > 0


def test_three_plans_differ_and_are_sorted():
    r = solver.plan(make([shop(c, 90 - i * 5) for i, c in enumerate("abcde")], plans=3))
    sets = [frozenset(p["shops"]) for p in r["plans"]]
    assert len(set(sets)) == 3
    sats = [p["totals"]["satisfaction"] for p in r["plans"]]
    assert sats == sorted(sats, reverse=True)
    assert [p["rank"] for p in r["plans"]] == [1, 2, 3]


def test_weighted_and_cost_objectives():
    shops = [shop("a", 90), shop("b", 80)]
    r = solver.plan(make(shops, objective="移動費最小", satisfaction_min=170, meals_min=0))
    assert r["plans"][0]["totals"]["satisfaction"] >= 170 and r["plans"][0]["totals"]["transport_cost"] == 0
    r = solver.plan(make(shops, objective="重み付き", weight=0.5))
    assert r["plans"][0]["totals"]["weighted_score"] == 170


def test_infeasible_returns_reason_without_plans():
    r = solver.plan(make([shop("a", 90)], meals_min=2, meals_max=2))
    assert r["status"] == solver.INFEASIBLE
    assert r["plans"] is None
    assert "見直す候補" in r["message"]


def test_fallback_to_simple_method_when_cpsat_finds_nothing(monkeypatch):
    monkeypatch.setattr(solver, "_cpsat", lambda P, prev, hint: (solver.NOT_FOUND, None))
    r = solver.plan(make([shop("a", 90), shop("b", 80)]))
    assert r["status"] == solver.FEASIBLE
    assert r["plans"][0]["method"] == solver.METHOD_GREEDY
    assert r["plans"][0]["violations"] == []


def test_not_found_when_simple_method_also_fails(monkeypatch):
    monkeypatch.setattr(solver, "_cpsat", lambda P, prev, hint: (solver.NOT_FOUND, None))
    monkeypatch.setattr(solver, "_greedy_diverse", lambda P, prev: None)
    r = solver.plan(make([shop("a", 90)]))
    assert r["status"] == solver.NOT_FOUND and r["plans"] is None


@pytest.mark.parametrize("change", [
    {"shops": []},
    {"time_start": 900, "time_end": 800},
    {"meals_min": 3, "meals_max": 1},
    {"objective": "なにか"},
    {"must": ["zzz"]},
    {"must": ["a"], "exclude": ["a"]},
    {"fullness_start": 200},
    {"food_budget": float("nan")},
])
def test_input_errors(change):
    p = make([shop("a", 90)])
    p.update(change)
    r = solver.plan(p)
    assert r["status"] == solver.INPUT_ERROR and r["plans"] is None
