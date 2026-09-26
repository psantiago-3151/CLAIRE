#!/usr/bin/env python3
"""CLAIRE config UI and start/stop control.

Run: python src/web.py
Then open http://127.0.0.1:8742
"""
import asyncio
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import comms  # loads .env via comms

UI_DIR = Path(__file__).resolve().parent / "ui"
HOST = os.environ.get("CLAIRE_UI_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLAIRE_UI_PORT", "8742"))
WIDE_KEYS = {"quit_phrases", "whisper_model"}
HF_PIPER_VOICES = "https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US"
OLLAMA_LIBRARY = "https://ollama.com/library"
_OLLAMA_BASE = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if "://" not in _OLLAMA_BASE:
    _OLLAMA_BASE = "http://" + _OLLAMA_BASE
OLLAMA_TAGS = _OLLAMA_BASE + "/api/tags"

app = FastAPI(title=f"CLAIRE {comms.app_version()}")
templates = Jinja2Templates(directory=str(UI_DIR))
_lock = threading.Lock()
_server = None
_shutting_down = False
_state = {
    "running": False,
    "busy": False,
    "heard": "",
    "reply": "",
    "message": "Standby",
    "alert": None,
    "empty_captures": 0,
    "applied_fingerprint": "",
    "memory_loading": False,
    "memory_progress": 0,
}
EMPTY_CAPTURE_WARN_AFTER = 3
_ollama_cache = {"ok": None, "at": 0.0}
OLLAMA_SERVE_HINT = (
    "Ollama is not running. In a terminal, start it with: ollama serve"
)
MIC_HINT = (
    "No microphone found. Plug one in, then open the hamburger → Admin "
    "and choose Microphone device."
)
MIC_CAPTURE_HINT = (
    "Several recordings in a row picked up no speech. You may not have said "
    "anything — if so, dismiss this. If you were talking, the microphone may "
    "need to be plugged in or assigned under Admin settings → Microphone device."
)


class ConfigUpdate(BaseModel):
    values: dict = {}
    phrases: dict = {}


def _wake_label(word: str) -> str:
    word = (word or "claire").strip()
    return word[:1].upper() + word[1:] if word else "Claire"


def _active_wake() -> str:
    if _state["running"]:
        return comms.AI_NAME
    return comms.file_config()["wake_word"]


def _ollama_up(*, fresh: bool = False) -> bool:
    now = time.monotonic()
    if (
        not fresh
        and _ollama_cache["ok"] is not None
        and now - _ollama_cache["at"] < 1.5
    ):
        return _ollama_cache["ok"]
    try:
        urllib.request.urlopen(OLLAMA_TAGS, timeout=1.0 if fresh else 0.4)
        ok = True
    except (urllib.error.URLError, TimeoutError, OSError):
        ok = False
    _ollama_cache["ok"] = ok
    _ollama_cache["at"] = now
    return ok


def _snapshot(*, fresh_ollama: bool = False) -> dict:
    running = _state["running"]
    recording = bool(running and comms.comms.recording)
    speaking = bool(running and comms.comms.speaking)
    wake = _active_wake()
    return {
        "running": running,
        "recording": recording,
        "speaking": speaking,
        "busy": _state["busy"],
        "memory_loading": bool(_state.get("memory_loading")),
        "memory_progress": int(_state.get("memory_progress") or 0),
        "memory_enabled": bool(comms.MEMORY_ENABLED),
        "memory_turns": comms.memory_turn_count(),
        "memory_load_pct": comms.MEMORY_LOAD_PCT,
        "memory_slider": comms.memory_turn_count() > comms.MEMORY_FULL_LOAD_BELOW,
        "ollama": _ollama_up(fresh=fresh_ollama),
        "ollama_hint": OLLAMA_SERVE_HINT,
        "mic": comms.microphone_status(),
        "wake_word": wake,
        "wake_label": _wake_label(wake),
        "heard": _state["heard"],
        "reply": _state["reply"],
        "message": _state["message"],
        "alert": _state.get("alert"),
        "pending_restart": bool(
            _state["running"]
            and _state.get("applied_fingerprint")
            and comms.disk_fingerprint() != _state.get("applied_fingerprint")
        ),
        "run_key": comms.RUN_KEY,
        "record_key": comms.RECORD_KEY,
        "interrupt_key": comms.INTERRUPT_KEY,
        "quit_key": comms.QUIT_KEY,
        "version": comms.app_version(),
    }


def _stop_inner(quiet: bool = False):
    comms._feed_cancel.set()
    if comms.comms.recording:
        comms.comms.stop_recording()
    comms.comms.interrupt(silent=quiet)
    comms.finish_memory_cycle()
    _state["running"] = False
    _state["busy"] = False
    _state["memory_loading"] = False
    _state["memory_progress"] = 0
    _state["message"] = "Standby"
    _state["alert"] = None
    _state["empty_captures"] = 0


_ui_waiters = []
_ui_waiters_lock = threading.Lock()


def _push_ui():
    with _ui_waiters_lock:
        waiters = list(_ui_waiters)
    for waiter in waiters:
        try:
            waiter.put_nowait(True)
        except queue.Full:
            pass


def _paint_turn(result: dict):
    """Push Heard/Reply to the UI as soon as they exist, before Piper finishes."""
    if result.get("heard") is not None:
        _state["heard"] = result.get("heard") or ""
    if "reply" in result:
        _state["reply"] = result.get("reply") or ""
    if result.get("message"):
        _state["message"] = result["message"]
    elif result.get("action"):
        _state["message"] = result["action"]
    # Unlock Record/Interrupt before Piper; ghosting is only for memory feed.
    if result.get("action") != "thinking":
        _state["busy"] = False
    _push_ui()


def _finish_recording():
    try:
        heard = comms.comms.listen()
        if not (heard or "").strip():
            _state["empty_captures"] = int(_state.get("empty_captures") or 0) + 1
            _state["heard"] = ""
            _state["reply"] = ""
            if _state["empty_captures"] > EMPTY_CAPTURE_WARN_AFTER:
                _state["message"] = MIC_CAPTURE_HINT
                _state["alert"] = {"type": "mic", "message": MIC_CAPTURE_HINT}
            else:
                _state["message"] = "No speech heard"
            return
        _state["empty_captures"] = 0
        _state["heard"] = heard or ""
        _state["message"] = "Heard"
        _push_ui()
        if not _ollama_up(fresh=True):
            with _lock:
                _stop_inner(quiet=True)
                _state["message"] = comms.OLLAMA_DOWN_ERROR
                _state["alert"] = {
                    "type": "setup",
                    "message": comms.OLLAMA_DOWN_ERROR,
                }
            _push_ui()
            return
        _state["alert"] = None
        result = comms.process_utterance(heard, on_result=_paint_turn)
        _paint_turn(result)
        if result.get("action") == "quit":
            with _lock:
                _stop_inner()
        elif result.get("action") == "ollama_down":
            with _lock:
                _stop_inner(quiet=True)
            _state["message"] = result.get("message") or comms.OLLAMA_DOWN_ERROR
            _state["alert"] = {
                "type": "setup",
                "message": _state["message"],
            }
        elif _state["running"] and result.get("action") == "reply":
            _state["message"] = f"{_wake_label(comms.AI_NAME)} is running"
    except Exception as exc:
        err = comms.ollama_error_text(exc)
        _state["message"] = err
        if err == comms.OLLAMA_DOWN_ERROR:
            with _lock:
                _stop_inner(quiet=True)
            _state["message"] = err
            _state["alert"] = {"type": "setup", "message": err}
    finally:
        _state["busy"] = False
        _push_ui()


comms.set_ui_listener(_push_ui)


def _ollama_models() -> list:
    try:
        with urllib.request.urlopen(OLLAMA_TAGS, timeout=2) as resp:
            data = json.loads(resp.read().decode())
        names = [m["name"] for m in data.get("models", []) if m.get("name")]
        _ollama_cache["ok"] = True
        _ollama_cache["at"] = time.monotonic()
        return names
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, ValueError):
        _ollama_cache["ok"] = False
        _ollama_cache["at"] = time.monotonic()
        return []


def _piper_voices() -> list:
    voices = []
    piper_dir = comms.ROOT / "models" / "piper"
    if piper_dir.is_dir():
        for path in sorted(piper_dir.glob("*.onnx")):
            voices.append({
                "path": path.relative_to(comms.ROOT).as_posix(),
                "name": path.stem,
            })
    return voices


def _with_current(options: list, current: str) -> list:
    if current and current not in options:
        return [current] + options
    return options


def _page_context(request: Request) -> dict:
    payload = comms.config_payload()
    fields = {f["key"]: f for f in payload["fields"]}
    llm_models = _with_current(_ollama_models(), fields["llm_model"]["value"])
    status = _snapshot()
    piper_voices = _piper_voices()
    piper_paths = [v["path"] for v in piper_voices]
    current_voice = fields["voice_model"]["value"]
    if current_voice and current_voice not in piper_paths:
        piper_voices = [{"path": current_voice, "name": Path(current_voice).stem}] + piper_voices
    return {
        "request": request,
        "runtime_fields": [
            f for f in payload["fields"]
            if f["group"] == "runtime" and f["ui"] == "visible"
        ],
        "admin_fields": [f for f in payload["fields"] if f["ui"] == "admin"],
        "llm_field": fields["llm_model"],
        "voice_field": fields["voice_model"],
        "llm_models": llm_models,
        "piper_voices": piper_voices,
        "hf_piper_url": HF_PIPER_VOICES,
        "ollama_library_url": OLLAMA_LIBRARY,
        "wide_keys": WIDE_KEYS,
        "status": status,
        "status_json": json.dumps(status),
        "wake_label": status["wake_label"],
        "mic_devices": (status.get("mic") or {}).get("devices") or [],
        "mic_field": fields.get("mic_device") or {
            "key": "mic_device",
            "label": "Microphone device",
            "value": "",
            "default": "",
            "env_value": "",
        },
        "memory_enabled_field": fields.get("memory_enabled") or {
            "key": "memory_enabled",
            "label": "Use conversation memory",
            "value": "1",
            "default": "1",
            "env_value": "",
            "env": "CLAIRE_MEMORY_ENABLED",
        },
        "memory_load_pct_field": fields.get("memory_load_pct") or {
            "key": "memory_load_pct",
            "label": "Memory to load (%)",
            "value": "100",
            "default": "100",
            "env_value": "",
            "env": "CLAIRE_MEMORY_LOAD_PCT",
        },
        "phrases": comms.phrases_payload(),
        "phrases_json": json.dumps(comms.phrases_payload()),
        "app_version": comms.app_version(),
    }


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _page_context(request))


@app.get("/api/config")
def get_config():
    payload = comms.config_payload()
    payload["status"] = _snapshot(fresh_ollama=True)
    payload["phrases"] = comms.phrases_payload()
    return payload


@app.post("/api/config")
def post_config(body: ConfigUpdate):
    apply = not _state["running"]
    try:
        saved = comms.save_config(body.values, apply=apply)
        if body.phrases:
            comms.save_phrases(body.phrases, apply=apply)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = comms.config_payload()
    payload["saved"] = saved
    payload["status"] = _snapshot()
    payload["mic_devices"] = comms.list_microphones()
    payload["phrases"] = {
        "thinking": comms._load_phrase_file("thinking", comms._FILLER_DEFAULTS),
        "missing_wake": comms._load_phrase_file("missing_wake", comms._MISSING_WAKE_DEFAULTS),
        "mode": saved.get("phrase_select_mode", "flat"),
        "preferred_boost_pct": float(saved.get("preferred_boost_pct") or 0),
    }
    payload["apply_on_restart"] = _state["running"]
    return payload


@app.get("/api/status")
def get_status():
    return _snapshot()


def _sse_pack(data: dict) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode()


@app.get("/api/events")
async def ui_events():
    async def gen():
        waiter: queue.Queue = queue.Queue(maxsize=4)
        with _ui_waiters_lock:
            _ui_waiters.append(waiter)
        try:
            yield _sse_pack(_snapshot())
            while not _shutting_down:
                try:
                    await asyncio.to_thread(waiter.get, True, 1)
                except queue.Empty:
                    if _shutting_down:
                        break
                    yield b": keepalive\n\n"
                    continue
                if _shutting_down:
                    break
                while True:
                    try:
                        waiter.get_nowait()
                    except queue.Empty:
                        break
                yield _sse_pack(_snapshot())
        finally:
            with _ui_waiters_lock:
                if waiter in _ui_waiters:
                    _ui_waiters.remove(waiter)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/start")
def start_assistant(body: Optional[ConfigUpdate] = None):
    with _lock:
        if _state["running"]:
            return _snapshot()
        if body and body.values:
            try:
                comms.save_config(body.values)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            comms.reload_settings()
        comms.rebind()
        if not _ollama_up(fresh=True):
            _state["running"] = False
            _state["message"] = comms.OLLAMA_DOWN_ERROR
            _state["alert"] = {
                "type": "setup",
                "message": comms.OLLAMA_DOWN_ERROR,
            }
            return _snapshot()
        warnings = comms.startup_warnings()
        _state["running"] = True
        _state["heard"] = ""
        _state["reply"] = ""
        if warnings:
            _state["alert"] = {
                "type": "setup",
                "message": "\n".join(comms.format_startup_warnings(warnings)),
            }
        else:
            _state["alert"] = None
        _state["applied_fingerprint"] = comms.disk_fingerprint()
        comms._feed_cancel.clear()
        _state["busy"] = False
        _state["memory_loading"] = False
        _state["memory_progress"] = 0
        _state["message"] = f"{_wake_label(comms.AI_NAME)} is running"
    return _snapshot()


def _feed_memory_then_ready():
    def progress(pct, message):
        _state["memory_progress"] = int(pct)
        _state["message"] = message
        _push_ui()

    try:
        comms.feed_memory_for_start(progress, cancel=comms._feed_cancel)
    except Exception as exc:
        _state["message"] = f"Memory load warning: {exc}"
    finally:
        with _lock:
            _state["busy"] = False
            _state["memory_loading"] = False
            _state["memory_progress"] = 100
            if _state["running"] and not comms._feed_cancel.is_set():
                if not _state.get("alert"):
                    _state["message"] = f"{_wake_label(comms.AI_NAME)} is running"
        _push_ui()


@app.post("/api/stop")
def stop_assistant():
    with _lock:
        _stop_inner()
    return _snapshot()


@app.post("/api/record")
def toggle_record():
    with _lock:
        if not _state["running"]:
            raise HTTPException(status_code=409, detail="not running")
        if _state["busy"]:
            raise HTTPException(status_code=409, detail="busy")
        if comms.comms.recording:
            comms.comms.stop_recording()
            _state["busy"] = True
            _state["heard"] = "Captured."
            _state["reply"] = ""
            _state["message"] = "Transcribing…"
            threading.Thread(target=_finish_recording, daemon=True).start()
            snap = _snapshot()
            snap["action"] = "processing"
            return snap
        comms.comms.start_recording()
        _state["message"] = "Recording"
        snap = _snapshot()
        snap["action"] = "recording"
        return snap


@app.post("/api/interrupt")
def interrupt_speech():
    if not _state["running"]:
        raise HTTPException(status_code=409, detail="not running")
    comms.comms.interrupt()
    _state["message"] = "Interrupted"
    return _snapshot()


@app.post("/api/ack-alert")
def ack_alert():
    _state["alert"] = None
    _state["empty_captures"] = 0
    return _snapshot()


def _quiet_shutdown_logs():
    class _DropCancelled(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if _shutting_down:
                return False
            text = record.getMessage()
            if "CancelledError" in text:
                return False
            exc = record.exc_info
            if exc and exc[0] is not None and issubclass(exc[0], asyncio.CancelledError):
                return False
            return True

    drop = _DropCancelled()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi", "asyncio"):
        log = logging.getLogger(name)
        log.addFilter(drop)
        if _shutting_down:
            log.setLevel(logging.CRITICAL)


def _hard_exit_soon(delay: float = 0.6):
    def _go():
        os._exit(0)
    timer = threading.Timer(delay, _go)
    timer.daemon = True
    timer.start()


def _request_exit():
    """Exit this process. Menu Shut down only — spoken quit phrases use Stop."""
    global _shutting_down
    if _shutting_down:
        comms.log("Shut down already in progress.")
        return
    _shutting_down = True
    comms.log("Shutting down gracefully...")
    _quiet_shutdown_logs()
    _push_ui()
    _hard_exit_soon(0.6)
    server = _server
    if server is not None:
        server.should_exit = True
    try:
        with _lock:
            _stop_inner(quiet=True)
        comms.log("Voice loop stopped.")
    except Exception as exc:
        comms.log(f"Voice loop stop error: {exc}")
    comms.log("CLAIRE stopped.")


def _exit_process():
    _request_exit()


@app.post("/api/shutdown")
def shutdown_app():
    _request_exit()
    return {"ok": True, "message": "Shutting down gracefully"}


def main():
    global _server
    import uvicorn

    if comms.ui_port_in_use(HOST, PORT):
        print(
            f"Warning: port {PORT} is already in use on {HOST}. Not starting.",
            flush=True,
        )
        print(f"Open the running UI at http://{HOST}:{PORT}", flush=True)
        raise SystemExit(1)
    print(f"CLAIRE {comms.app_version()}  http://{HOST}:{PORT}", flush=True)
    comms.print_startup_warnings()
    config = uvicorn.Config(
        app, host=HOST, port=PORT, log_level="warning", access_log=False
    )
    _server = uvicorn.Server(config)
    _server.run()
    if _shutting_down:
        comms.log("Shutdown complete.")


if __name__ == "__main__":
    main()
