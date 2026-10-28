"""API。入力を変換し、計算部（app/solver.py）を呼ぶだけ。"""

import re
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import solver
from .excel import ExcelError, read_dataset
from .travel import build_legs

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
SAMPLE = ROOT.parent / "data" / "sample_osaka.xlsx"

app = FastAPI(title="食べ歩きプランの最適化",
              description="使える時間・食事・満腹度・疲労度・予算の条件から、食べ歩きのプランを最適化する。")


# ---------------------------------------------------------------- データの形

class Shop(BaseModel):
    id: str
    name: str
    area: str = ""
    genre: str = ""
    lat: float
    lon: float
    open: str = Field(description="開店（HH:MM）")
    close: str = Field(description="閉店（HH:MM。24:00 も可）")
    stay: int = Field(description="滞在時間（分）")
    price: int = Field(description="価格（円）")
    satisfaction: float = Field(description="満足度")
    fullness: float = Field(description="食べると上がる満腹度")
    source: str = ""
    checked: str = ""
    memo: str = ""


class Station(BaseModel):
    id: str
    name: str
    lat: float
    lon: float


class Mode(BaseModel):
    name: str = Field(description="手段の名前（徒歩・自転車・バス・電車・タクシー など）")
    route: Literal["直行", "駅経由"]
    speed: float = Field(description="速度（m/分）")
    detour: float = Field(description="迂回係数（直線距離に掛ける）")
    end_walk: int = Field(description="乗り場までと降りてからの徒歩（分）")
    wait: int = Field(description="待ち時間（分）")
    fare_type: Literal["無料", "均一", "距離帯", "初乗り加算", "時間"]
    fare_value: str | int | float = ""
    fatigue: float = Field(description="疲労係数（1分あたり）")
    source: str = ""
    memo: str = ""


class Dataset(BaseModel):
    shops: list[Shop]
    stations: list[Station]
    modes: list[Mode]


class Settings(BaseModel):
    start_station: str = Field("umeda", description="出発駅の id")
    end_station: str = Field("namba", description="到着駅の id")
    time_start: str = Field("10:00", description="使える時間の開始（HH:MM）")
    time_end: str = Field("21:00", description="使える時間の終了（HH:MM）")
    meal_from: str = Field("10:30", description="食事を始めてよい時間帯の開始（HH:MM）")
    meal_to: str = Field("20:00", description="食事を始めてよい時間帯の終了（HH:MM）")
    meals_min: int = Field(3, description="食事の最小回数")
    meals_max: int = Field(5, description="食事の最大回数")
    meal_interval: int = Field(60, description="食事の開始どうしの最小の間隔（分）")
    fullness_start: float = Field(20, description="出発時の満腹度")
    fullness_max: float = Field(100, description="満腹度の上限")
    digest_base: float = Field(10, description="消化（基本）: 1時間あたりに下がる満腹度")
    digest_extra: float = Field(10, description="疲れているときに加わる消化（1時間あたり）")
    digest_threshold: float = Field(50, description="「疲れている」とみなす疲労度")
    fatigue_start: float = Field(0, description="出発時の疲労度")
    fatigue_max: float = Field(100, description="疲労度の上限")
    fatigue_recovery: float = Field(20, description="1回の食事で下がる疲労度")
    food_budget: int = Field(6000, description="食費の上限（円）")
    transport_budget: int = Field(2000, description="移動費の上限（円）")
    objective: Literal["満足度最大", "重み付き", "移動費最小"] = "満足度最大"
    weight: float = Field(0.02, description="重み付き: 満足度 − 重み × 移動費")
    satisfaction_min: float = Field(300, description="移動費最小: 満足度の合計の下限")
    plans: int = Field(3, description="出す案の数（1〜5）")
    time_limit: float = Field(10, description="1案あたりの計算時間（秒）")
    must: list[str] = Field([], description="必ず行く店の id")
    exclude: list[str] = Field([], description="行かない店の id")


class PlanRequest(BaseModel):
    dataset: Dataset
    settings: Settings


class Plan(BaseModel):
    rank: int
    status: str
    method: str = Field(description="CP-SAT / 簡易な方法")
    shops: list[str]
    totals: dict
    steps: list[dict] = Field(description="行程（時刻は 0:00 からの分）")
    trace: list[dict] = Field(description="満腹度・疲労度の変化")
    violations: list[str]


class PlanResult(BaseModel):
    status: str = Field(description="最適 / 最適とは限らない解 / 解なし / 解が見つからない（時間切れ） / 入力の誤り")
    message: str = ""
    plans: list[Plan] | None = None


# ---------------------------------------------------------------- 変換

def minutes(hhmm: str, label: str) -> int:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(hhmm).strip())
    if not m or int(m[2]) >= 60 or int(m[1]) * 60 + int(m[2]) > 1440:
        raise ValueError(f"{label} の時刻「{hhmm}」は HH:MM（00:00〜24:00）で書いてください")
    return int(m[1]) * 60 + int(m[2])


def to_problem(data: Dataset, s: Settings) -> dict:
    """画面・Excel の形を、計算部の入力に変換する。"""
    st = {x.id: x for x in data.stations}
    for key in ("start_station", "end_station"):
        if getattr(s, key) not in st:
            raise ValueError(f"駅「{getattr(s, key)}」がデータにありません")
    shops = [x.model_dump() for x in data.shops]
    modes = [x.model_dump() for x in data.modes]
    legs = build_legs(shops, [x.model_dump() for x in data.stations], modes, s.start_station, s.end_station)
    return {
        "shops": [{"id": x["id"], "name": x["name"], "open": minutes(x["open"], x["name"]),
                   "close": minutes(x["close"], x["name"]), "stay": x["stay"], "price": x["price"],
                   "satisfaction": x["satisfaction"], "fullness": x["fullness"]} for x in shops],
        "start": {"id": s.start_station, "name": st[s.start_station].name},
        "end": {"id": s.end_station, "name": st[s.end_station].name},
        "legs": legs,
        "fatigue_coef": {x["name"]: x["fatigue"] for x in modes},
        **{k: minutes(getattr(s, k), k) for k in ("time_start", "time_end", "meal_from", "meal_to")},
        **s.model_dump(exclude={"start_station", "end_station", "time_start", "time_end", "meal_from", "meal_to"}),
    }


def input_error(message: str):
    return JSONResponse(PlanResult(status=solver.INPUT_ERROR, message=message).model_dump(), status_code=422)


@app.exception_handler(RequestValidationError)
def validation_error(request: Request, exc: RequestValidationError):
    """型が合わない入力も、結果と同じ形（状態「入力の誤り」）で返す。"""
    return input_error("; ".join(f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in exc.errors()))


# ---------------------------------------------------------------- API

@app.get("/api/sample", response_model=Dataset, summary="サンプルデータ（大阪キタ・ミナミ）")
def sample():
    return read_dataset(SAMPLE.read_bytes())


@app.get("/api/sample.xlsx", summary="サンプルデータの Excel（アップロードする Excel の見本）")
def sample_xlsx():
    return FileResponse(SAMPLE, filename="sample_osaka.xlsx")


@app.post("/api/upload", response_model=Dataset, summary="Excel を読み込んでデータにする",
          responses={422: {"model": PlanResult, "description": "入力の誤り"}})
async def upload(file: UploadFile):
    try:
        return Dataset(**read_dataset(await file.read()))
    except (ExcelError, ValueError) as e:
        return input_error(str(e))


@app.get("/api/settings/default", response_model=Settings, summary="設定の初期値")
def default_settings():
    return Settings()


@app.post("/api/plan", response_model=PlanResult, summary="プランを作る",
          responses={422: {"model": PlanResult, "description": "入力の誤り"}})
def make_plan(req: PlanRequest):
    try:
        problem = to_problem(req.dataset, req.settings)
    except ValueError as e:
        return input_error(str(e))
    result = solver.plan(problem)
    if result["status"] == solver.INPUT_ERROR:
        return JSONResponse(result, status_code=422)
    return result


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")
