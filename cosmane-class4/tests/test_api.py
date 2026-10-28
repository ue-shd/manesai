from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.travel import fare

client = TestClient(app)
SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_osaka.xlsx"


def test_pages():
    assert client.get("/").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/sample.xlsx").status_code == 200


def test_sample_data():
    data = client.get("/api/sample").json()
    assert len(data["shops"]) == 30
    assert len(data["stations"]) == 10
    assert [m["name"] for m in data["modes"]] == ["徒歩", "自転車", "バス", "電車", "タクシー"]
    assert all(s["source"] for s in data["shops"])


def test_upload_excel():
    res = client.post("/api/upload", files={"file": ("s.xlsx", SAMPLE.read_bytes())})
    assert res.status_code == 200 and len(res.json()["shops"]) == 30
    res = client.post("/api/upload", files={"file": ("x.xlsx", b"not excel")})
    assert res.status_code == 422 and res.json()["status"] == "入力の誤り"


def test_fares():
    taxi = {"name": "タクシー", "fare_type": "初乗り加算", "fare_value": "1200:600,231:100"}
    assert fare(taxi, 1000, 3) == 600
    assert fare(taxi, 1431, 4) == 700
    assert fare(taxi, 1432, 4) == 800
    metro = {"name": "電車", "fare_type": "距離帯", "fare_value": "3000:190,7000:240,:390"}
    assert [fare(metro, d, 5) for d in (2000, 5000, 20000)] == [190, 240, 390]
    bike = {"name": "自転車", "fare_type": "時間", "fare_value": "30:165"}
    assert [fare(bike, 0, m) for m in (10, 30, 31)] == [165, 165, 330]


def test_plan_with_sample():
    data = client.get("/api/sample").json()
    settings = client.get("/api/settings/default").json()
    settings.update(plans=2, time_limit=3)
    res = client.post("/api/plan", json={"dataset": data, "settings": settings})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in ("最適", "最適とは限らない解")
    for p in body["plans"]:
        assert p["violations"] == []
        assert p["totals"]["food_cost"] <= settings["food_budget"]
        assert p["totals"]["transport_cost"] <= settings["transport_budget"]
        assert 3 <= p["totals"]["meals"] <= 5


def test_plan_input_errors():
    data = client.get("/api/sample").json()
    settings = client.get("/api/settings/default").json()
    for bad in ({"time_start": "25:00"}, {"start_station": "nowhere"}, {"meals_min": "たくさん"}):
        res = client.post("/api/plan", json={"dataset": data, "settings": {**settings, **bad}})
        assert res.status_code == 422 and res.json()["status"] == "入力の誤り", bad
