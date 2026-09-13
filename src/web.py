#!/usr/bin/env python3
"""Jarvis config UI and start/stop control.

Run: python src/web.py
Then open http://127.0.0.1:8742
"""
import json
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import comms

UI_DIR = Path(__file__).resolve().parent / "ui"
HOST = "127.0.0.1"
PORT = 8742
WIDE_KEYS = {"quit_phrases", "whisper_model"}
HF_PIPER_VOICES = "https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US"
OLLAMA_LIBRARY = "https://ollama.com/library"
OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"

app = FastAPI(title="Jarvis")
templates = Jinja2Templates(directory=str(UI_DIR))
_lock = threading.Lock()
_state = {
    "running": False,
    "busy": False,
    "heard": "",
    "reply": "",
    "message": "Standby",
}
_ollama_cache = {"ok": None, "at": 0.0}
OLLAMA_SERVE_HINT = (
    "Ollama is not running. In a terminal, start it with: "
    "vendor/ollama/ollama serve"
)


class ConfigUpdate(BaseModel):
    values: dict = {}


def _wake_label(word: str) -> str:
    word = (word or "friday").strip()
    return word[:1].upper() + word[1:] if word else "Friday"


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
        "ollama": _ollama_up(fresh=fresh_ollama),
        "ollama_hint": OLLAMA_SERVE_HINT,
        "wake_word": wake,
        "wake_label": _wake_label(wake),
        "heard": _state["heard"],
        "reply": _state["reply"],
        "message": _state["message"],
        "record_key": comms.RECORD_KEY,
        "interrupt_key": comms.INTERRUPT_KEY,
        "quit_key": comms.QUIT_KEY,
    }


def _stop_inner():
    if comms.comms.recording:
        comms.comms.stop_recording()
    comms.comms.interrupt()
    _state["running"] = False
    _state["busy"] = False
    _state["message"] = "Standby"


def _finish_recording():
    try:
        heard = comms.comms.listen()
        if not _ollama_up(fresh=True):
            _state["heard"] = heard or ""
            _state["reply"] = ""
            _state["message"] = OLLAMA_SERVE_HINT
            return
        result = comms.process_utterance(heard)
        _state["heard"] = result.get("heard") or ""
        _state["reply"] = result.get("reply") or ""
        _state["message"] = result.get("message") or result.get("action") or ""
        if result.get("action") == "quit":
            with _lock:
                _stop_inner()
    except Exception as exc:
        _state["message"] = f"{type(exc).__name__}: {exc}"
    finally:
        _state["busy"] = False


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
    }


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("index.html", _page_context(request))


@app.get("/api/config")
def get_config():
    payload = comms.config_payload()
    payload["status"] = _snapshot(fresh_ollama=True)
    return payload


@app.post("/api/config")
def post_config(body: ConfigUpdate):
    try:
        saved = comms.save_config(body.values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    payload = comms.config_payload()
    payload["saved"] = saved
    payload["status"] = _snapshot()
    payload["apply_on_restart"] = _state["running"]
    return payload


@app.get("/api/status")
def get_status():
    return _snapshot()


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
        missing = comms.comms.missing()
        if missing:
            _state["message"] = "Setup problems"
            raise HTTPException(status_code=400, detail=missing)
        if not _ollama_up(fresh=True):
            _state["message"] = OLLAMA_SERVE_HINT
            raise HTTPException(status_code=400, detail=OLLAMA_SERVE_HINT)
        _state["running"] = True
        _state["busy"] = False
        _state["heard"] = ""
        _state["reply"] = ""
        _state["message"] = f"{_wake_label(comms.AI_NAME)} is running"
    return _snapshot()


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
            ollama_ok = _ollama_up(fresh=True)
            _state["busy"] = True
            _state["message"] = "Listening…" if ollama_ok else OLLAMA_SERVE_HINT
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


def _exit_process():
    with _lock:
        _stop_inner()
    os.kill(os.getpid(), signal.SIGTERM)


@app.post("/api/shutdown")
def shutdown_app():
    threading.Timer(0.3, _exit_process).start()
    return {"ok": True, "message": "Shutting down"}


def main():
    import uvicorn

    print(f"Jarvis UI  http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
