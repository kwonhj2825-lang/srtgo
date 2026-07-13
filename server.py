"""
SRT 예매 도우미 — 백엔드 서버 (FastAPI)
=========================================

프론트엔드(webapp_mockup.html)에서 보낸 예매 조건을 받아,
기존 srtgo 패키지(srt.py)를 이용해 백그라운드에서 계속 빈자리를 조회/예매한다.
자리가 잡히면 텔레그램으로 알림을 보낸다.

실행:
    pip install fastapi uvicorn
    python server.py
그다음 브라우저에서  http://localhost:8000  접속.

같은 집 와이파이의 폰에서 쓰려면:  http://<이_컴퓨터_IP>:8000
"""

import sys
import time
import json
import asyncio
import threading
from pathlib import Path
from datetime import datetime
from random import gammavariate

# Windows 콘솔(cp949)에서 이모지 출력 시 UnicodeEncodeError 방지
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import keyring
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

# ── 기존 srtgo 패키지 로드 ─────────────────────────────
# 폴더구조:  C:\Users\SDS\Downloads\srtgo\srtgo\srtgo\{srt,ktx,srtgo}.py
BASE = Path(__file__).resolve().parent          # ...\Downloads\srtgo
PKG_ROOT = BASE / "srtgo"                        # ...\Downloads\srtgo\srtgo  (패키지 srtgo 의 부모)
sys.path.insert(0, str(PKG_ROOT))

from srtgo.srt import (          # noqa: E402
    SRT, SRTError, SRTNetFunnelError,
    SeatType, Adult, Child, Senior, Disability1To3, Disability4To6,
)
from srtgo.srtgo import (        # noqa: E402
    pay_card, _is_seat_available, get_telegram,
    RESERVE_INTERVAL_SHAPE, RESERVE_INTERVAL_SCALE, RESERVE_INTERVAL_MIN,
)

try:
    from curl_cffi.requests.exceptions import ConnectionError as _ConnErr
except ImportError:                              # pragma: no cover
    from requests.exceptions import ConnectionError as _ConnErr

from json.decoder import JSONDecodeError         # noqa: E402


SEAT_MAP = {
    "GENERAL_FIRST": SeatType.GENERAL_FIRST,
    "GENERAL_ONLY":  SeatType.GENERAL_ONLY,
    "SPECIAL_FIRST": SeatType.SPECIAL_FIRST,
    "SPECIAL_ONLY":  SeatType.SPECIAL_ONLY,
}


# ── 예매 작업 상태 (한 번에 하나의 작업만) ───────────────
class JobState:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.running = False
        self.started_at: float | None = None
        self.attempts = 0
        self.log: list[dict] = []          # [{t, level, msg}]
        self.result: str | None = None     # 예매 성공 메시지
        self.params: dict | None = None

    def add(self, msg, level="info"):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.append({"t": stamp, "level": level, "msg": msg})
        self.log[:] = self.log[-200:]      # 최근 200줄만 유지
        print(f"[{stamp}] {msg}")

    def snapshot(self):
        elapsed = int(time.time() - self.started_at) if self.started_at else 0
        return {
            "running": self.running,
            "attempts": self.attempts,
            "elapsed": elapsed,
            "result": self.result,
            "params": self.params,
            "log": self.log[-60:],
        }


JOB = JobState()


# ── 텔레그램 (스레드 안에서 async 호출) ──────────────────
def tg_send(text: str):
    try:
        tgprintf = get_telegram()
        asyncio.run(tgprintf(text))
    except Exception as e:                        # 알림 실패해도 예매는 계속
        JOB.add(f"텔레그램 전송 실패: {e!r}", "warn")


def build_passengers(p: dict):
    out = []
    if p.get("adult", 0):  out.append(Adult(p["adult"]))
    if p.get("child", 0):  out.append(Child(p["child"]))
    if p.get("senior", 0): out.append(Senior(p["senior"]))
    return out or [Adult(1)]


# ── 실제 예매 루프 (백그라운드 스레드) ───────────────────
def reserve_worker(params: dict):
    JOB.running = True
    JOB.started_at = time.time()
    JOB.attempts = 0
    JOB.result = None
    JOB.log.clear()
    JOB.params = params

    dep, arr = params["dep"], params["arr"]
    date = params["date"]                         # YYYYMMDD
    t_from = params["time_from"]                  # HHMMSS
    t_to = params["time_to"]                       # HHMMSS
    seat_type = SEAT_MAP[params["seat_type"]]
    passengers = build_passengers(params.get("passengers", {}))
    auto_pay = bool(params.get("auto_pay", False))

    uid = keyring.get_password("SRT", "id")
    pw = keyring.get_password("SRT", "pass")
    if not uid or not pw:
        JOB.add("SRT 로그인 정보가 없습니다. 계정을 먼저 저장하세요.", "error")
        JOB.running = False
        return

    JOB.add(f"{dep} → {arr} / {date} / {t_from[:2]}:{t_from[2:4]}~{t_to[:2]}:{t_to[2:4]} 표 사냥 시작 🎯")

    try:
        rail = SRT(uid, pw, verbose=False)
        JOB.add("SRT 로그인 성공 ✅", "ok")
    except Exception as e:
        JOB.add(f"로그인 실패: {e!r}", "error")
        JOB.running = False
        return

    while JOB.running:
        JOB.attempts += 1
        try:
            trains = rail.search_train(
                dep=dep, arr=arr, date=date, time=t_from, available_only=False
            )
            trains = [t for t in trains if t_from <= t.dep_time <= t_to]

            for train in trains:
                if _is_seat_available(train, seat_type, "SRT"):
                    JOB.add(f"자리 발견! → {train}", "ok")
                    reservation = rail.reserve(
                        train, passengers=passengers, option=seat_type
                    )
                    msg = f"🎉 표 잡았어요!\n{reservation}"
                    if getattr(reservation, "tickets", None):
                        msg += "\n" + "\n".join(map(str, reservation.tickets))

                    if auto_pay and not reservation.is_waiting:
                        if pay_card(rail, reservation):
                            msg += "\n💳 결제 완료"

                    JOB.add(msg.replace("\n", " / "), "ok")
                    JOB.result = msg
                    tg_send(msg)
                    JOB.running = False
                    return

        except SRTNetFunnelError:
            rail.clear()
        except SRTError as e:
            m = e.msg
            if "정상적인 경로로 접근" in m:
                rail.clear()
            elif "로그인 후 사용하십시오" in m:
                JOB.add("세션 만료, 재로그인...", "warn")
                rail = SRT(uid, pw, verbose=False)
            elif not any(x in m for x in ("잔여석없음", "사용자가 많아", "조회 결과가 없습니다",
                                          "예약대기 접수가 마감", "예약대기자한도수초과")):
                JOB.add(f"SRTError: {m!r} — 중단", "error")
                JOB.running = False
                tg_send(f"[SRT] 오류로 중단됨: {m}")
                return
        except JSONDecodeError:
            rail = SRT(uid, pw, verbose=False)
        except _ConnErr:
            JOB.add("연결 끊김, 재로그인...", "warn")
            time.sleep(2)
            try:
                rail = SRT(uid, pw, verbose=False)
            except Exception:
                pass
        except Exception as e:
            JOB.add(f"예외({type(e).__name__}): {e!r} — 중단", "error")
            JOB.running = False
            return

        time.sleep(
            gammavariate(RESERVE_INTERVAL_SHAPE, RESERVE_INTERVAL_SCALE)
            + RESERVE_INTERVAL_MIN
        )

    JOB.add("사냥을 종료했습니다.", "info")


# ── FastAPI ──────────────────────────────────────────
app = FastAPI(title="SRT 예매 도우미")


class Passengers(BaseModel):
    adult: int = 1
    child: int = 0
    senior: int = 0


class StartReq(BaseModel):
    dep: str
    arr: str
    date: str                  # YYYYMMDD
    time_from: str             # HHMMSS
    time_to: str               # HHMMSS
    seat_type: str             # GENERAL_FIRST 등
    passengers: Passengers = Passengers()
    auto_pay: bool = False


class ConfigReq(BaseModel):
    srt_id: str | None = None
    srt_pass: str | None = None
    tg_token: str | None = None
    tg_chat_id: str | None = None
    card_number: str | None = None
    card_password: str | None = None
    card_birthday: str | None = None
    card_expire: str | None = None


@app.on_event("startup")
async def _startup_notify():
    """서버가 (재)시작될 때 텔레그램으로 알림 — 재부팅/복구를 알 수 있게."""
    try:
        tgprintf = get_telegram()
        await tgprintf("✅ SRT 표 사냥꾼 서버가 켜졌어요. (사냥 대기 🎯)")
    except Exception:
        pass


@app.get("/download/guide")
def download_guide():
    """설치 안내서(.md) 다운로드 — 다른 사람에게 공유용."""
    path = BASE / "설치가이드.md"
    if not path.exists():
        return JSONResponse({"ok": False, "error": "안내서 파일이 없습니다."}, status_code=404)
    return FileResponse(path, media_type="text/markdown; charset=utf-8",
                        filename="SRT_표사냥꾼_설치가이드.md")


@app.get("/", response_class=HTMLResponse)
def index():
    html = (BASE / "webapp_mockup.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.post("/api/config")
def save_config(cfg: ConfigReq):
    saved = []
    if cfg.srt_id and cfg.srt_pass:
        keyring.set_password("SRT", "id", cfg.srt_id)
        keyring.set_password("SRT", "pass", cfg.srt_pass)
        keyring.set_password("SRT", "ok", "1")
        saved.append("SRT 계정")
    if cfg.tg_token and cfg.tg_chat_id:
        keyring.set_password("telegram", "token", cfg.tg_token)
        keyring.set_password("telegram", "chat_id", cfg.tg_chat_id)
        keyring.set_password("telegram", "ok", "1")
        saved.append("텔레그램")
    if cfg.card_number:
        keyring.set_password("card", "number", cfg.card_number)
        keyring.set_password("card", "password", cfg.card_password or "")
        keyring.set_password("card", "birthday", cfg.card_birthday or "")
        keyring.set_password("card", "expire", cfg.card_expire or "")
        keyring.set_password("card", "ok", "1")
        saved.append("카드")
    return {"ok": True, "saved": saved}


@app.get("/api/config/status")
def config_status():
    return {
        "srt": bool(keyring.get_password("SRT", "id")),
        "telegram": bool(keyring.get_password("telegram", "token")),
        "card": bool(keyring.get_password("card", "ok")),
    }


@app.post("/api/start")
def start(req: StartReq):
    with JOB.lock:
        if JOB.running:
            return JSONResponse({"ok": False, "error": "이미 표 사냥이 진행 중입니다."}, status_code=409)
        params = {
            "dep": req.dep, "arr": req.arr, "date": req.date,
            "time_from": req.time_from, "time_to": req.time_to,
            "seat_type": req.seat_type, "auto_pay": req.auto_pay,
            "passengers": req.passengers.model_dump(),
        }
        JOB.thread = threading.Thread(target=reserve_worker, args=(params,), daemon=True)
        JOB.thread.start()
    return {"ok": True}


@app.post("/api/stop")
def stop():
    JOB.running = False
    return {"ok": True}


@app.get("/api/status")
def status():
    return JOB.snapshot()


if __name__ == "__main__":
    print("=" * 50)
    print(" SRT 예매 도우미 서버 시작")
    print(" 이 컴퓨터에서:  http://localhost:8000")
    print(" 같은 와이파이 폰에서:  http://<이_PC_IP>:8000")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000)
