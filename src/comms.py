#!/usr/bin/env python3
"""CLAIRE: mic, STT, LLM, TTS, and the voice loop.

Web UI:  python src/web.py
CLI:     python src/comms.py
"""
import fcntl
import glob
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

import ollama
from rapidfuzz import fuzz
from sshkeyboard import listen_keyboard, stop_listening

IS_MAC = sys.platform == "darwin"
ROOT = Path(__file__).resolve().parent.parent


def app_version() -> str:
    """Release version from pyproject.toml. Bump that file for a new release."""
    path = ROOT / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


# True only while python src/comms.py is the control surface (set in main).
CLI_INTERACTIVE = False
cli_running = False
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _load_conf(path: Path) -> dict:
    data = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return data
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _resolve_path(value: str) -> str:
    value = os.path.expanduser(value)
    path = Path(value)
    if not path.is_absolute():
        return str(ROOT / path)
    return str(path)


def _discover_conf_path() -> Path:
    return ROOT / "claire.conf"


CONF_PATH = _discover_conf_path()
CONF = _load_conf(CONF_PATH)


def _env_lookup(env_name: str) -> str:
    if os.environ.get(env_name, "") != "":
        return os.environ[env_name]
    return ""


def _setting(env_name: str, conf_key: str, default: str, *, path: bool = False) -> str:
    raw = _env_lookup(env_name)
    if raw == "":
        if CONF.get(conf_key, "") != "":
            raw = CONF[conf_key]
        else:
            raw = default
    raw = str(raw).strip()
    if path or raw.startswith("~") or "/" in raw or "\\" in raw:
        return _resolve_path(raw)
    return raw


MIN_WAIT_SECS = 3.0
PHRASES_DIR = ROOT / "phrases"
_FILLER_DEFAULTS = [
    "Thinking...",
    "I'm in Deep thought about our interaction.",
    "Pondering about your message",
]
_MISSING_WAKE_DEFAULTS = [
    "I did not detect the wake word in your request, please ask your request again addressing it to {name}",
]
FILLER_PHRASES = list(_FILLER_DEFAULTS)
MISSING_WAKE_PHRASES = list(_MISSING_WAKE_DEFAULTS)
FILLER_ENTRIES = [{"text": t, "preferred": False} for t in _FILLER_DEFAULTS]
MISSING_WAKE_ENTRIES = [{"text": t, "preferred": False} for t in _MISSING_WAKE_DEFAULTS]
PHRASE_SELECT_MODE = "flat"
WAKE_FUZZ_THRESHOLD = 85.0
PREFERRED_BOOST_PCT = 0.0


def _normalize_entries(raw, fallback_lines: list) -> list:
    entries = []
    if isinstance(raw, dict):
        raw = raw.get("entries") or []
    if not isinstance(raw, list):
        raw = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            preferred = False
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            preferred = bool(item.get("preferred"))
        else:
            continue
        if text:
            entries.append({"text": text, "preferred": preferred})
    if entries:
        return entries
    return [{"text": t, "preferred": False} for t in fallback_lines]


def _load_phrase_file(stem: str, fallback_lines: list) -> list:
    json_path = PHRASES_DIR / f"{stem}.json"
    txt_path = PHRASES_DIR / f"{stem}.txt"
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = []
        return _normalize_entries(data, fallback_lines)
    try:
        text = txt_path.read_text(encoding="utf-8")
    except OSError:
        return [{"text": t, "preferred": False} for t in fallback_lines]
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return _normalize_entries(lines, fallback_lines)


def _load_phrases():
    global FILLER_PHRASES, MISSING_WAKE_PHRASES
    global FILLER_ENTRIES, MISSING_WAKE_ENTRIES
    FILLER_ENTRIES = _load_phrase_file("thinking", _FILLER_DEFAULTS)
    MISSING_WAKE_ENTRIES = _load_phrase_file("missing_wake", _MISSING_WAKE_DEFAULTS)
    FILLER_PHRASES = [e["text"] for e in FILLER_ENTRIES]
    MISSING_WAKE_PHRASES = [e["text"] for e in MISSING_WAKE_ENTRIES]


def save_phrase_file(stem: str, entries: list):
    PHRASES_DIR.mkdir(parents=True, exist_ok=True)
    clean = _normalize_entries(entries, [])
    if not clean:
        raise ValueError(f"{stem} needs at least one phrase")
    path = PHRASES_DIR / f"{stem}.json"
    path.write_text(
        json.dumps({"entries": clean}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def save_phrases(payload: dict, *, apply: bool = True):
    if "thinking" in payload:
        save_phrase_file("thinking", payload["thinking"])
    if "missing_wake" in payload:
        save_phrase_file("missing_wake", payload["missing_wake"])
    if apply:
        _load_phrases()


def phrases_payload() -> dict:
    return {
        "thinking": list(FILLER_ENTRIES),
        "missing_wake": list(MISSING_WAKE_ENTRIES),
        "mode": PHRASE_SELECT_MODE,
        "preferred_boost_pct": PREFERRED_BOOST_PCT,
    }


def disk_fingerprint() -> str:
    thinking = _load_phrase_file("thinking", _FILLER_DEFAULTS)
    wake = _load_phrase_file("missing_wake", _MISSING_WAKE_DEFAULTS)
    blob = {
        "conf": file_config(),
        "thinking": thinking,
        "missing_wake": wake,
    }
    return json.dumps(blob, sort_keys=True, ensure_ascii=False)


def pick_timed_phrase(phrases) -> str:
    if not phrases:
        return ""
    return phrases[int(time.time() * 1000) % len(phrases)]


def pick_phrase_entry(entries: list) -> str:
    texts = [e.get("text") for e in entries if e.get("text")]
    if not texts:
        return ""
    boost = PREFERRED_BOOST_PCT
    if boost <= 0:
        return pick_timed_phrase(texts)
    preferred = [e for e in entries if e.get("text") and e.get("preferred")]
    if boost >= 100:
        pool = preferred or entries
        return pick_timed_phrase([e["text"] for e in pool if e.get("text")])
    weight_pref = 1.0 + (float(boost) / 50.0)
    weighted = []
    for e in entries:
        text = e.get("text")
        if not text:
            continue
        weighted.append((text, weight_pref if e.get("preferred") else 1.0))
    total = sum(w for _, w in weighted)
    if total <= 0:
        return pick_timed_phrase(texts)
    cursor = (time.time() * 1000) % total
    acc = 0.0
    for text, weight in weighted:
        acc += weight
        if cursor < acc:
            return text
    return weighted[-1][0]


BREATH_MARK = "<<<breath>>>"
MIN_BREATH_SECS = 0.05
MAX_BREATH_SECS = 2.0
BREATH_SECS = 0.35


def missing_wake_reply() -> tuple:
    name = AI_NAME.capitalize()
    template = pick_phrase_entry(MISSING_WAKE_ENTRIES)
    display = template.replace("{name}", name)
    spoken = template.replace("{name}", f"{BREATH_MARK}{name}")
    return display, spoken


def _secs(env_name: str, conf_key: str, default: str) -> float:
    try:
        return max(MIN_WAIT_SECS, float(_setting(env_name, conf_key, default)))
    except ValueError:
        return max(MIN_WAIT_SECS, float(default))


def _load_phrase_mode():
    global PHRASE_SELECT_MODE, PREFERRED_BOOST_PCT, WAKE_FUZZ_THRESHOLD
    mode = _setting("CLAIRE_PHRASE_MODE", "phrase_select_mode", "flat").lower()
    PHRASE_SELECT_MODE = mode if mode in ("flat", "preferred") else "flat"
    try:
        PREFERRED_BOOST_PCT = max(
            0.0,
            min(100.0, float(_setting("CLAIRE_PREFERRED_BOOST", "preferred_boost_pct", "0"))),
        )
    except ValueError:
        PREFERRED_BOOST_PCT = 0.0
    try:
        WAKE_FUZZ_THRESHOLD = max(
            50.0,
            min(100.0, float(_setting("CLAIRE_WAKE_FUZZ", "wake_fuzz_threshold", "85"))),
        )
    except ValueError:
        WAKE_FUZZ_THRESHOLD = 85.0


def _load_wait_secs():
    global THINKING_INTERVAL_SECS, BREATH_SECS
    THINKING_INTERVAL_SECS = _secs(
        "CLAIRE_THINKING_INTERVAL_SECS", "thinking_interval_secs", "5"
    )
    try:
        breath = float(_setting("CLAIRE_NAME_BREATH_SECS", "name_breath_secs", "0.35"))
    except ValueError:
        breath = 0.35
    BREATH_SECS = min(MAX_BREATH_SECS, max(MIN_BREATH_SECS, breath))


def _validate_wait_secs(current: dict):
    try:
        value = float(current["thinking_interval_secs"])
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("Thinking interval must be a number") from exc
    if value < MIN_WAIT_SECS:
        raise ValueError(f"Thinking interval must be at least {MIN_WAIT_SECS:g} seconds")
    current["thinking_interval_secs"] = f"{value:g}"
    try:
        breath = float(current["name_breath_secs"])
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("Name pause must be a number") from exc
    if breath < MIN_BREATH_SECS:
        raise ValueError(f"Name pause must be at least {MIN_BREATH_SECS:g} seconds")
    if breath > MAX_BREATH_SECS:
        raise ValueError(f"Name pause must be at most {MAX_BREATH_SECS:g} seconds")
    current["name_breath_secs"] = f"{breath:g}"


LLM_MODEL = _setting("CLAIRE_LLM_MODEL", "llm_model", "qwen2.5:7b")
VOICE_MODEL = _setting(
    "CLAIRE_VOICE_MODEL",
    "voice_model",
    str(ROOT / "models" / "piper" / "en_US-libritts_r-medium.onnx"),
    path=True,
)
VOICE_SPEAKER = int(_setting("CLAIRE_VOICE_SPEAKER", "voice_speaker", "0"))
WHISPER_BIN = _setting(
    "CLAIRE_WHISPER_BIN",
    "whisper_bin",
    str(ROOT / "bin" / "whisper-cli"),
    path=True,
)
WHISPER_MODEL = _setting(
    "CLAIRE_WHISPER_MODEL",
    "whisper_model",
    str(ROOT / "models" / "whisper" / "ggml-base.en.bin"),
    path=True,
)
PIPER_BIN = _setting(
    "CLAIRE_PIPER_BIN",
    "piper_bin",
    str(ROOT / "venv" / "bin" / "piper"),
    path=True,
)
MEMORY_DIR = _setting(
    "CLAIRE_MEMORY_DIR",
    "memory_dir",
    str(ROOT / "memory"),
    path=True,
)
MIC_DEVICE = _setting("CLAIRE_MIC_DEVICE", "mic_device", "")
AI_NAME = _setting("CLAIRE_WAKE_WORD", "wake_word", "claire").lower()
_mic_cache = {"at": 0.0, "status": None}


def list_microphones() -> list:
    devices = []
    seen = set()

    def add(did: str, name: str):
        if not did or did in seen:
            return
        seen.add(did)
        devices.append({"id": did, "name": name})

    try:
        import sounddevice as sd

        for i, info in enumerate(sd.query_devices()):
            if int(info.get("max_input_channels") or 0) > 0:
                label = info.get("name") or f"input {i}"
                add(str(i), f"{label} (#{i})")
    except Exception:
        pass
    if shutil.which("arecord"):
        try:
            listed = subprocess.run(
                ["arecord", "-L"], capture_output=True, text=True, timeout=3
            )
            for line in listed.stdout.splitlines():
                name = line.strip()
                if not name or name.startswith(" ") or name.startswith("*"):
                    continue
                if name in ("null", "hw", "plughw", "sysdefault"):
                    continue
                add(name, name)
        except Exception:
            pass
        try:
            cards = subprocess.run(
                ["arecord", "-l"], capture_output=True, text=True, timeout=3
            )
            for line in cards.stdout.splitlines():
                match = re.search(r"card (\d+).*device (\d+)", line)
                if not match:
                    continue
                upper = line.upper()
                if "HDMI" in upper or "LOOPBACK" in upper:
                    continue
                ident = f"plughw:{match.group(1)},{match.group(2)}"
                add(ident, line.strip())
        except Exception:
            pass
    return devices


def microphone_status(*, fresh: bool = False) -> dict:
    now = time.monotonic()
    if (
        not fresh
        and _mic_cache["status"] is not None
        and now - _mic_cache["at"] < 2.0
    ):
        return _mic_cache["status"]
    devices = list_microphones()
    assigned = (MIC_DEVICE or "").strip()
    ids = {d["id"] for d in devices}
    if assigned and assigned not in ids:
        devices = [{"id": assigned, "name": f"{assigned} (configured)"}] + devices
        ids.add(assigned)
    ok = bool(devices) or bool(assigned)
    hint = (
        ""
        if ok
        else "No microphone found. Plug one in, then open Admin settings and choose it."
    )
    status = {
        "ok": ok,
        "assigned": assigned or "auto",
        "devices": devices,
        "hint": hint,
    }
    _mic_cache["status"] = status
    _mic_cache["at"] = now
    return status
RUN_KEY = _setting("CLAIRE_RUN_KEY", "run_key", "space").lower()
RECORD_KEY = _setting("CLAIRE_RECORD_KEY", "record_key", "tab").lower()
INTERRUPT_KEY = _setting("CLAIRE_INTERRUPT_KEY", "interrupt_key", "f12").lower()
QUIT_KEY = _setting("CLAIRE_QUIT_KEY", "quit_key", "esc").lower()
QUIT_PHRASES = [
    p.strip().lower()
    for p in _setting(
        "CLAIRE_QUIT_PHRASES", "quit_phrases", "exit,goodbye,shut down,shutdown"
    ).split(",")
    if p.strip()
]
NEW_SESSION_PHRASE = _setting(
    "CLAIRE_NEW_SESSION_PHRASE", "new_session_phrase", "scratch that"
).lower()
_load_wait_secs()
_load_phrase_mode()
_load_phrases()
WAKE_PREFIXES = ["hey", "ok", "okay", "please", "yo", "hi", "hello"]

CONFIG_FIELDS = [
    {"key": "llm_model", "env": "CLAIRE_LLM_MODEL", "label": "Ollama model", "group": "models", "ui": "visible", "default": "qwen2.5:7b"},
    {"key": "voice_model", "env": "CLAIRE_VOICE_MODEL", "label": "Piper voice", "group": "models", "ui": "visible", "default": "models/piper/en_US-libritts_r-medium.onnx"},
    {"key": "voice_speaker", "env": "CLAIRE_VOICE_SPEAKER", "label": "Speaker id", "group": "models", "ui": "admin", "default": "0"},
    {"key": "whisper_bin", "env": "CLAIRE_WHISPER_BIN", "label": "Whisper binary", "group": "models", "ui": "admin", "default": "bin/whisper-cli"},
    {"key": "whisper_model", "env": "CLAIRE_WHISPER_MODEL", "label": "Whisper model", "group": "models", "ui": "admin", "default": "models/whisper/ggml-base.en.bin"},
    {"key": "piper_bin", "env": "CLAIRE_PIPER_BIN", "label": "Piper binary", "group": "models", "ui": "admin", "default": "venv/bin/piper"},
    {"key": "memory_dir", "env": "CLAIRE_MEMORY_DIR", "label": "Memory directory", "group": "models", "ui": "admin", "default": "memory"},
    {"key": "mic_device", "env": "CLAIRE_MIC_DEVICE", "label": "Microphone device (Linux arecord; empty = auto)", "group": "models", "ui": "admin", "default": ""},
    {"key": "thinking_interval_secs", "env": "CLAIRE_THINKING_INTERVAL_SECS", "label": "Thinking reminder every (sec)", "group": "models", "ui": "admin", "default": "5"},
    {"key": "name_breath_secs", "env": "CLAIRE_NAME_BREATH_SECS", "label": "Pause before wake word (sec)", "group": "models", "ui": "admin", "default": "0.35"},
    {"key": "phrase_select_mode", "env": "CLAIRE_PHRASE_MODE", "label": "Phrase selection", "group": "models", "ui": "admin", "default": "flat"},
    {"key": "preferred_boost_pct", "env": "CLAIRE_PREFERRED_BOOST", "label": "Preferred mix (%)", "group": "models", "ui": "admin", "default": "0"},
    {"key": "wake_fuzz_threshold", "env": "CLAIRE_WAKE_FUZZ", "label": "Wake-word match (%)", "group": "models", "ui": "admin", "default": "85"},
    {"key": "wake_word", "env": "CLAIRE_WAKE_WORD", "label": "Wake word", "group": "runtime", "ui": "visible", "default": "claire"},
    {"key": "run_key", "env": "CLAIRE_RUN_KEY", "label": "Start/Stop key", "group": "runtime", "ui": "hidden", "default": "space"},
    {"key": "record_key", "env": "CLAIRE_RECORD_KEY", "label": "Record key", "group": "runtime", "ui": "hidden", "default": "tab"},
    {"key": "interrupt_key", "env": "CLAIRE_INTERRUPT_KEY", "label": "Interrupt speech key", "group": "runtime", "ui": "hidden", "default": "f12"},
    {"key": "quit_key", "env": "CLAIRE_QUIT_KEY", "label": "Shut down key (web UI)", "group": "runtime", "ui": "hidden", "default": "esc"},
    {"key": "quit_phrases", "env": "CLAIRE_QUIT_PHRASES", "label": "Spoken quit phrases", "group": "runtime", "ui": "visible", "default": "exit,goodbye,shut down,shutdown"},
    {"key": "new_session_phrase", "env": "CLAIRE_NEW_SESSION_PHRASE", "label": "New session phrase", "group": "runtime", "ui": "visible", "default": "scratch that"},
]
_FIELD_KEYS = {f["key"] for f in CONFIG_FIELDS}

CONF_TEMPLATE = """# CLAIRE settings. Env vars (CLAIRE_*) override these.
# Paths may be relative to the project root.

# Ollama model
llm_model={llm_model}

# Hugging Face Piper voice (rhasspy/piper-voices en_US-libritts_r-medium)
voice_model={voice_model}
voice_speaker={voice_speaker}

whisper_bin={whisper_bin}
whisper_model={whisper_model}
piper_bin={piper_bin}
memory_dir={memory_dir}
# Linux arecord device: default, pulse, plughw:1,0. Empty = auto.
mic_device={mic_device}

# Filler speech while waiting on the model (repeats this often)
thinking_interval_secs={thinking_interval_secs}
# Silence inserted before the wake word in missing-wake replies
name_breath_secs={name_breath_secs}
# flat = equal odds; preferred = boost marked lines (0–100, 50 = 2×, 100 = only preferred)
phrase_select_mode={phrase_select_mode}
preferred_boost_pct={preferred_boost_pct}
# Fuzzy wake-word match 50–100 (85 = default)
wake_fuzz_threshold={wake_fuzz_threshold}

# Wake word (matched anywhere in the transcript)
wake_word={wake_word}

# Keyboard names for python src/comms.py (hidden in the web Admin form)
run_key={run_key}
record_key={record_key}
interrupt_key={interrupt_key}
quit_key={quit_key}

# Spoken runtime commands (comma-separated; new session requires the phrase twice)
quit_phrases={quit_phrases}
new_session_phrase={new_session_phrase}
"""


def reload_settings():
    global CONF, LLM_MODEL, VOICE_MODEL, VOICE_SPEAKER, WHISPER_BIN, WHISPER_MODEL
    global PIPER_BIN, MEMORY_DIR, MIC_DEVICE, AI_NAME, RUN_KEY, RECORD_KEY, INTERRUPT_KEY, QUIT_KEY
    global QUIT_PHRASES, NEW_SESSION_PHRASE, CURRENT_FILE
    global THINKING_INTERVAL_SECS, BREATH_SECS
    global PHRASE_SELECT_MODE, PREFERRED_BOOST_PCT, WAKE_FUZZ_THRESHOLD
    CONF = _load_conf(CONF_PATH)
    LLM_MODEL = _setting("CLAIRE_LLM_MODEL", "llm_model", "qwen2.5:7b")
    VOICE_MODEL = _setting(
        "CLAIRE_VOICE_MODEL",
        "voice_model",
        str(ROOT / "models" / "piper" / "en_US-libritts_r-medium.onnx"),
        path=True,
    )
    VOICE_SPEAKER = int(_setting("CLAIRE_VOICE_SPEAKER", "voice_speaker", "0"))
    WHISPER_BIN = _setting(
        "CLAIRE_WHISPER_BIN",
        "whisper_bin",
        str(ROOT / "bin" / "whisper-cli"),
        path=True,
    )
    WHISPER_MODEL = _setting(
        "CLAIRE_WHISPER_MODEL",
        "whisper_model",
        str(ROOT / "models" / "whisper" / "ggml-base.en.bin"),
        path=True,
    )
    PIPER_BIN = _setting(
        "CLAIRE_PIPER_BIN",
        "piper_bin",
        str(ROOT / "venv" / "bin" / "piper"),
        path=True,
    )
    MEMORY_DIR = _setting(
        "CLAIRE_MEMORY_DIR",
        "memory_dir",
        str(ROOT / "memory"),
        path=True,
    )
    MIC_DEVICE = _setting("CLAIRE_MIC_DEVICE", "mic_device", "")
    _mic_cache["status"] = None
    AI_NAME = _setting("CLAIRE_WAKE_WORD", "wake_word", "claire").lower()
    RUN_KEY = _setting("CLAIRE_RUN_KEY", "run_key", "space").lower()
    RECORD_KEY = _setting("CLAIRE_RECORD_KEY", "record_key", "tab").lower()
    INTERRUPT_KEY = _setting("CLAIRE_INTERRUPT_KEY", "interrupt_key", "f12").lower()
    QUIT_KEY = _setting("CLAIRE_QUIT_KEY", "quit_key", "esc").lower()
    QUIT_PHRASES = [
        p.strip().lower()
        for p in _setting(
            "CLAIRE_QUIT_PHRASES", "quit_phrases", "exit,goodbye,shut down,shutdown"
        ).split(",")
        if p.strip()
    ]
    NEW_SESSION_PHRASE = _setting(
        "CLAIRE_NEW_SESSION_PHRASE", "new_session_phrase", "scratch that"
    ).lower()
    _load_wait_secs()
    _load_phrase_mode()
    _load_phrases()
    CURRENT_FILE = os.path.join(MEMORY_DIR, "current.json")


def file_config() -> dict:
    data = _load_conf(CONF_PATH)
    return {f["key"]: data.get(f["key"], f["default"]) for f in CONFIG_FIELDS}


def config_payload() -> dict:
    file_vals = file_config()
    fields = []
    for field in CONFIG_FIELDS:
        env_value = os.environ.get(field["env"], "")
        fields.append({
            "key": field["key"],
            "env": field["env"],
            "label": field["label"],
            "group": field["group"],
            "ui": field.get("ui", "visible"),
            "value": file_vals[field["key"]],
            "default": field["default"],
            "env_value": env_value,
        })
    wake = _env_lookup("CLAIRE_WAKE_WORD") or file_vals["wake_word"]
    return {"fields": fields, "wake_word": wake.lower()}


def save_config(updates: dict, *, apply: bool = True) -> dict:
    current = file_config()
    for key, value in updates.items():
        if key not in _FIELD_KEYS:
            raise ValueError(f"unknown setting: {key}")
        current[key] = str(value).strip()
    if not current["wake_word"]:
        raise ValueError("wake_word is required")
    try:
        int(current["voice_speaker"])
    except ValueError as exc:
        raise ValueError("voice_speaker must be an integer") from exc
    _validate_wait_secs(current)
    mode = (current.get("phrase_select_mode") or "flat").lower()
    if mode not in ("flat", "preferred"):
        raise ValueError("phrase_select_mode must be flat or preferred")
    current["phrase_select_mode"] = mode
    try:
        boost = float(current.get("preferred_boost_pct") or "0")
    except ValueError as exc:
        raise ValueError("preferred_boost_pct must be a number") from exc
    if boost < 0 or boost > 100:
        raise ValueError("preferred_boost_pct must be between 0 and 100")
    current["preferred_boost_pct"] = f"{boost:g}"
    try:
        fuzz_pct = float(current.get("wake_fuzz_threshold") or "85")
    except ValueError as exc:
        raise ValueError("wake_fuzz_threshold must be a number") from exc
    if fuzz_pct < 50 or fuzz_pct > 100:
        raise ValueError("wake_fuzz_threshold must be between 50 and 100")
    current["wake_fuzz_threshold"] = f"{fuzz_pct:g}"
    for key in ("run_key", "record_key", "interrupt_key", "quit_key"):
        current[key] = current[key].lower()
    current["wake_word"] = current["wake_word"].lower()
    global CONF_PATH, CONF
    CONF_PATH = ROOT / "claire.conf"
    CONF_PATH.write_text(CONF_TEMPLATE.format(**current), encoding="utf-8")
    CONF = _load_conf(CONF_PATH)
    if apply:
        reload_settings()
    return current


def rebind():
    global comms, memory, stop_requested, cli_running
    Path(MEMORY_DIR).mkdir(parents=True, exist_ok=True)
    comms = Comms()
    memory = load_all_history()
    stop_requested = False
    cli_running = False


def _key_label(key: str) -> str:
    if len(key) > 1 and key[0] == "f" and key[1:].isdigit():
        return key.upper()
    return key.capitalize()


# Optional Imagine splash. Wordmark is printed as text; the image should be
# a mark only (no letters — image models garble text).
CLI_BANNER_IMAGE = ROOT / "src" / "ui" / "claire-cli-banner.png"


def _print_iterm_image(path: Path) -> bool:
    data = path.read_bytes()
    if not data or len(data) > 2_000_000:
        return False
    import base64

    b64 = base64.b64encode(data).decode("ascii")
    name = path.name.replace("=", "").replace(":", "")
    sys.stdout.write(
        f"\033]1337;File=name={name};inline=1;width=80;preserveAspectRatio=1:{b64}\a\n"
    )
    sys.stdout.flush()
    return True


def _print_banner_image() -> bool:
    """Show src/ui/claire-cli-banner.png when present. Silent if missing."""
    path = CLI_BANNER_IMAGE
    if not path.is_file():
        return False
    try:
        if os.environ.get("KITTY_WINDOW_ID") and shutil.which("kitty"):
            result = subprocess.run(
                ["kitty", "+kitten", "icat", "--align", "left", str(path)],
                timeout=5,
            )
            return result.returncode == 0
        term = os.environ.get("TERM_PROGRAM", "")
        if term in ("iTerm.app", "WezTerm") or os.environ.get("ITERM_SESSION_ID"):
            return _print_iterm_image(path)
        if shutil.which("chafa"):
            cols = shutil.get_terminal_size((80, 24)).columns
            result = subprocess.run(
                ["chafa", "-s", f"{max(40, cols)}x12", str(path)],
                timeout=5,
            )
            return result.returncode == 0
    except Exception:
        return False
    return False


def _conf_display_value(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return "(empty)"
    try:
        return str(Path(text).expanduser().resolve().relative_to(ROOT))
    except (OSError, ValueError):
        root = str(ROOT)
        if text.startswith(root + os.sep):
            return text[len(root) + 1 :]
        return text


def cli_conf_lines() -> list:
    """Show claire.conf values used at launch (defaults if the file is missing)."""
    exists = CONF_PATH.is_file()
    header = (
        f"claire.conf  {CONF_PATH}"
        if exists
        else f"claire.conf  not found — using defaults"
    )
    vals = file_config()
    fields = [f for f in CONFIG_FIELDS if f.get("ui") != "hidden"]
    label_w = max(len(f["key"]) for f in fields)
    rows = []
    for field in fields:
        raw = vals.get(field["key"], field["default"])
        shown = _conf_display_value(str(raw))
        if field["key"] == "mic_device" and shown in ("(empty)",):
            shown = "auto"
        rows.append(f"{field['key']:<{label_w}}  {shown}")
    return ["", header, ""] + rows


def cli_banner_lines(*, ollama_ok=None):
    """Name, loaded claire.conf, keys, then conversation."""
    if ollama_ok is None:
        ollama_ok = ollama_reachable()
    wake = AI_NAME.capitalize()
    lines = [
        "",
        f"CLAIRE  v{app_version()}",
        f"Say {wake} to talk to {LLM_MODEL}",
    ]
    if not ollama_ok:
        lines.append(OLLAMA_DOWN_ERROR)
    lines.extend(cli_conf_lines())
    lines.extend(cli_key_block(running=False, recording=False))
    lines.extend([
        "",
        "── conversation ──",
        "Standby",
    ])
    return lines


def _print_cli_banner():
    _print_banner_image()
    print("\n".join(cli_banner_lines()), flush=True)


def tts_speak_text(text: str) -> str:
    """Turn markdown emphasis into a pause + firmer clause for Piper.

    `**subject**` must not be read as "asterisk asterisk".
    """
    if not text:
        return text

    def emphasize(inner: str) -> str:
        inner = re.sub(r"[*_`#=~]", "", inner).strip()
        if not inner:
            return " "
        return f" ... {inner}. ..."

    def wrap_emph(match: re.Match) -> str:
        return emphasize(match.group(1))

    spoken = text
    spoken = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+)$", wrap_emph, spoken)
    spoken = re.sub(r"#{2,}(.+?)#{2,}", wrap_emph, spoken, flags=re.DOTALL)
    spoken = re.sub(r"={2,}(.+?)={2,}", wrap_emph, spoken, flags=re.DOTALL)
    spoken = re.sub(r"\*{3,}(.+?)\*{3,}", wrap_emph, spoken, flags=re.DOTALL)
    spoken = re.sub(r"\*\*(.+?)\*\*", wrap_emph, spoken, flags=re.DOTALL)
    spoken = re.sub(r"__(.+?)__", wrap_emph, spoken, flags=re.DOTALL)
    spoken = re.sub(r"~~(.+?)~~", r" \1 ", spoken, flags=re.DOTALL)
    spoken = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r" \1 ", spoken, flags=re.DOTALL)
    spoken = re.sub(r"(?m)^\s*([#\-=*_~])\1{2,}\s*$", " ... ", spoken)
    spoken = re.sub(r"(?m)^\s*[\-*+]\s+", "", spoken)
    spoken = re.sub(r"[#*_`]", " ", spoken)
    spoken = spoken.replace(BREATH_MARK, f" {BREATH_MARK} ")
    spoken = re.sub(r"[ \t]+", " ", spoken)
    spoken = re.sub(r" ?\n ?", "\n", spoken)
    return spoken.strip()


def _write_silence(path: str, seconds: float, channels: int = 1, sampwidth: int = 2, rate: int = 22050):
    frames = int(rate * seconds)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(b"\x00" * frames * channels * sampwidth)


def _concat_wavs(out_path: str, paths: list):
    params = None
    chunks = []
    for path in paths:
        with wave.open(path, "rb") as wf:
            cur = (wf.getnchannels(), wf.getsampwidth(), wf.getframerate())
            if params is None:
                params = cur
            elif cur != params:
                continue
            chunks.append(wf.readframes(wf.getnframes()))
    if not params or not chunks:
        return
    if os.path.exists(out_path):
        os.remove(out_path)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(params[0])
        wf.setsampwidth(params[1])
        wf.setframerate(params[2])
        for chunk in chunks:
            wf.writeframes(chunk)


def _wav_player(path: str):
    if IS_MAC:
        return ["afplay", path]
    for name in ("pw-play", "paplay", "aplay"):
        found = shutil.which(name)
        if found:
            return [found, path]
    return None


_ui_listener = None


def set_ui_listener(fn):
    """Web UI hook: called when Heard/Reply/speaking should refresh."""
    global _ui_listener
    _ui_listener = fn


def notify_ui():
    fn = _ui_listener
    if not fn:
        return
    try:
        fn()
    except Exception:
        pass


def log(*args):
    """Print even if stdout was left non-blocking."""
    msg = " ".join(str(a) for a in args) + "\n"
    data = msg.encode("utf-8", errors="replace")
    fd = sys.stdout.fileno()
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        try:
            os.write(fd, data)
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    except Exception:
        try:
            os.write(fd, data)
        except Exception:
            pass


class Comms:
    """Record, transcribe, think, and speak."""

    def __init__(self):
        self.llm_model = LLM_MODEL
        self.voice_model = VOICE_MODEL
        self.voice_speaker = VOICE_SPEAKER
        self.whisper_bin = WHISPER_BIN
        self.whisper_model = WHISPER_MODEL
        self.piper_bin = PIPER_BIN
        self.tmp_raw = str(ROOT / "temp" / "input.raw")
        self.tmp_wav = str(ROOT / "temp" / "input.wav")
        self.tmp_tts = str(ROOT / "temp" / "response.wav")
        Path(os.path.dirname(self.tmp_wav)).mkdir(parents=True, exist_ok=True)

        self.mic = self._detect_microphone()
        self.recording = False
        self.speaking = False
        self._speech_id = 0
        self.rec_process = None
        self.speak_process = None
        self.rec_stream = None
        self.rec_frames = []
        self.capture_mode = "sd" if IS_MAC else "arecord"

    def missing(self) -> list:
        problems = []
        if not os.path.isfile(self.whisper_bin):
            problems.append(f"whisper-cli not found: {self.whisper_bin}")
        if not os.path.isfile(self.whisper_model):
            problems.append(f"whisper model not found: {self.whisper_model}")
        if not os.path.isfile(self.voice_model):
            problems.append(f"Piper voice not found: {self.voice_model}")
        if not IS_MAC:
            if not shutil.which("arecord"):
                problems.append("arecord not found (install alsa-utils)")
            if not shutil.which("sox"):
                problems.append("sox not found")
            if not any(shutil.which(name) for name in ("pw-play", "paplay", "aplay")):
                problems.append("no WAV player found (pw-play, paplay, or aplay)")
        return problems

    def _detect_microphone(self) -> str:
        if MIC_DEVICE:
            return MIC_DEVICE
        if IS_MAC:
            return "default"
        try:
            listed = subprocess.run(
                ["arecord", "-L"], capture_output=True, text=True, timeout=5
            )
            names = [
                line.strip()
                for line in listed.stdout.splitlines()
                if line.strip() and not line.startswith(" ")
            ]
            for preferred in ("default", "pulse", "pipewire"):
                if preferred in names:
                    return preferred
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["arecord", "-l"], capture_output=True, text=True, check=True, timeout=5
            )
            usb = None
            first = None
            for line in result.stdout.splitlines():
                match = re.search(r"card (\d+).*device (\d+)", line)
                if not match:
                    continue
                card, dev = match.groups()
                name = f"plughw:{card},{dev}"
                upper = line.upper()
                if "HDMI" in upper or "LOOPBACK" in upper:
                    continue
                if first is None:
                    first = name
                if "USB" in upper:
                    usb = name
                    break
            if usb:
                return usb
            if first:
                return first
        except Exception:
            pass
        return "default"

    @staticmethod
    def _write_wav_int16(path: str, audio_bytes: bytes, rate: int = 16000):
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(audio_bytes)

    def start_recording(self):
        if CLI_INTERACTIVE:
            log(
                f"\n🎤 RECORDING... speak now! "
                f"(press {_key_label(RECORD_KEY)} to stop)"
            )
        else:
            log("\n🎤 RECORDING... speak now!")
        for path in (self.tmp_raw, self.tmp_wav):
            if os.path.exists(path):
                os.remove(path)
        self.recording = True

        if self._start_sounddevice():
            return
        if IS_MAC:
            self.recording = False
            log("Could not open the microphone (sounddevice)")
            return

        self.capture_mode = "arecord"
        log(f"arecord device: {self.mic}")
        self.rec_process = subprocess.Popen(
            [
                "arecord",
                "-D", self.mic,
                "-f", "S16_LE",
                "-r", "16000",
                "-c", "1",
                "-t", "raw",
                self.tmp_raw,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _start_sounddevice(self) -> bool:
        try:
            import sounddevice as sd
        except Exception as exc:
            log(f"sounddevice unavailable: {exc}")
            return False
        try:
            self.rec_frames = []

            def _callback(indata, frames, time_info, status):
                self.rec_frames.append(indata.copy())

            kwargs = dict(samplerate=16000, channels=1, dtype="int16", callback=_callback)
            if MIC_DEVICE and not IS_MAC:
                try:
                    kwargs["device"] = int(MIC_DEVICE)
                except ValueError:
                    kwargs["device"] = MIC_DEVICE
            self.rec_stream = sd.InputStream(**kwargs)
            self.rec_stream.start()
            self.capture_mode = "sd"
            return True
        except Exception as exc:
            log(f"sounddevice mic failed: {exc}")
            self.rec_stream = None
            return False

    def stop_recording(self):
        log("🎤 Stopping recording...")
        if self.capture_mode == "sd" or self.rec_stream is not None:
            if self.rec_stream is not None:
                self.rec_stream.stop()
                self.rec_stream.close()
                self.rec_stream = None
            self.recording = False
            if self.rec_frames:
                import numpy as np

                audio = np.concatenate(self.rec_frames, axis=0)
                self._write_wav_int16(self.tmp_wav, audio.tobytes())
            time.sleep(0.2)
            return

        if self.recording and self.rec_process:
            self.rec_process.send_signal(signal.SIGINT)
            try:
                self.rec_process.wait(timeout=5)
            except Exception:
                self.rec_process.terminate()
            self.recording = False
        time.sleep(0.3)

    def _convert_to_wav(self) -> bool:
        if self.capture_mode == "sd":
            if not os.path.exists(self.tmp_wav) or os.path.getsize(self.tmp_wav) < 1000:
                log("No audio captured")
                return False
            return True

        if not os.path.exists(self.tmp_raw) or os.path.getsize(self.tmp_raw) < 1000:
            log("No audio captured")
            return False
        log("Converting raw to WAV with sox...")
        result = subprocess.run(
            [
                "sox",
                "-t", "raw",
                "-r", "16000",
                "-e", "signed",
                "-b", "16",
                "-c", "1",
                self.tmp_raw,
                self.tmp_wav,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log(f"sox error: {result.stderr}")
            return False
        return True

    def transcribe(self) -> str:
        if not self._convert_to_wav():
            return ""

        log("Running Whisper transcription...")
        log_path = str(ROOT / "temp" / "whisper.log")
        result = subprocess.run(
            [self.whisper_bin, "-m", self.whisper_model, "-f", self.tmp_wav, "-t", "4"],
            capture_output=True,
            text=True,
        )
        try:
            with open(log_path, "w", encoding="utf-8") as fp:
                if result.stderr:
                    fp.write(result.stderr)
        except Exception:
            pass

        stdout = (result.stdout or "").strip()
        full_transcript = ""
        for line in stdout.splitlines():
            if " --> " in line:
                parts = line.split("] ", 1)
                if len(parts) > 1:
                    text = parts[1].strip()
                    if text:
                        full_transcript += " " + text

        if not full_transcript:
            for line in stdout.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("[") and "whisper" not in stripped.lower():
                    full_transcript += " " + stripped

        full_transcript = full_transcript.strip()
        log(f'You said: "{full_transcript or "[nothing]"}"')
        return full_transcript

    def listen(self) -> str:
        self.stop_recording()
        return self.transcribe()

    def _piper_wav(self, piper: str, text: str, out_path: str) -> bool:
        if os.path.exists(out_path):
            os.remove(out_path)
        result = subprocess.run(
            [
                piper,
                "--model", self.voice_model,
                "--speaker", str(self.voice_speaker),
                "--sentence-silence", "0.2",
                "--output_file", out_path,
            ],
            input=text,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and os.path.exists(out_path):
            return True
        log(f"Piper error: {result.stderr or result.stdout}")
        return False

    def _synth_spoken(self, piper: str, spoken: str, token: int) -> bool:
        parts = spoken.split(BREATH_MARK)
        if len(parts) == 1:
            return self._piper_wav(piper, spoken, self.tmp_tts)

        tmpdir = str(ROOT / "temp")
        Path(tmpdir).mkdir(parents=True, exist_ok=True)
        wavs = []
        rate = 22050
        for i, part in enumerate(parts):
            if token != self._speech_id:
                return False
            piece = part.strip()
            if piece:
                out = os.path.join(tmpdir, f"tts_part_{i}.wav")
                if not self._piper_wav(piper, piece, out):
                    return False
                wavs.append(out)
                with wave.open(out, "rb") as wf:
                    rate = wf.getframerate()
            if i < len(parts) - 1:
                sil = os.path.join(tmpdir, f"tts_sil_{i}.wav")
                _write_silence(sil, BREATH_SECS, channels=1, sampwidth=2, rate=rate)
                wavs.append(sil)
        if not wavs:
            return False
        _concat_wavs(self.tmp_tts, wavs)
        return os.path.exists(self.tmp_tts)

    def speak(self, text: str, spoken: str = None):
        if not (text or "").strip() and not (spoken or "").strip():
            return
        token = self._speech_id
        raw_spoken = tts_speak_text(spoken if spoken is not None else text)
        self.speaking = True
        notify_ui()

        if os.path.exists(self.tmp_tts):
            os.remove(self.tmp_tts)

        piper = self.piper_bin if os.path.isfile(self.piper_bin) else shutil.which("piper")
        used_piper = False
        try:
            if piper and os.path.isfile(self.voice_model):
                used_piper = self._synth_spoken(piper, raw_spoken, token)

            if token != self._speech_id:
                return

            log(f"AI: {text}")
            if used_piper:
                player = _wav_player(self.tmp_tts)
                if not player:
                    log("No WAV player found (afplay, pw-play, paplay, or aplay)")
                    return
                self.speak_process = subprocess.Popen(player)
            elif IS_MAC:
                slnc_ms = max(50, int(round(BREATH_SECS * 1000)))
                say_text = raw_spoken.replace(BREATH_MARK, f" [[slnc {slnc_ms}]] ")
                self.speak_process = subprocess.Popen(["say", say_text])
            else:
                log("No TTS backend available")
                return
            self.speak_process.wait()
        finally:
            if token == self._speech_id:
                self.speaking = False
                notify_ui()

    def interrupt(self, silent: bool = False):
        self._speech_id += 1
        if self.speak_process:
            if not silent:
                if CLI_INTERACTIVE:
                    log(f"\n🛑 Speech interrupted ({_key_label(INTERRUPT_KEY)})")
                else:
                    log("\n🛑 Speech interrupted")
            try:
                self.speak_process.terminate()
            except Exception:
                pass
        self.speaking = False
        notify_ui()

    def generate(self, prompt: str) -> str:
        return ollama.generate(model=self.llm_model, prompt=prompt)["response"]

    def generate_with_fillers(self, prompt: str) -> str:
        done = threading.Event()
        box = {"response": None, "error": None}

        def _run():
            try:
                box["response"] = self.generate(prompt)
            except Exception as exc:
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True).start()
        filler = {"thread": None}

        def pick_phrase() -> str:
            return pick_phrase_entry(FILLER_ENTRIES)

        def play_filler(text: str):
            if done.is_set():
                return
            speaker = threading.Thread(target=self.speak, args=(text,), daemon=True)
            filler["thread"] = speaker
            speaker.start()
            while speaker.is_alive():
                if done.wait(0.1):
                    self.interrupt(silent=True)
                    speaker.join(timeout=3)
                    return
            speaker.join()

        interval = THINKING_INTERVAL_SECS
        while not done.is_set():
            if done.wait(interval):
                break
            play_filler(pick_phrase())
        self.interrupt(silent=True)
        if filler["thread"] is not None:
            filler["thread"].join(timeout=3)
        if box["error"] is not None:
            raise box["error"]
        return box["response"] or ""


Path(MEMORY_DIR).mkdir(parents=True, exist_ok=True)
CURRENT_FILE = os.path.join(MEMORY_DIR, "current.json")

comms = Comms()
stop_requested = False


def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _shift(now: datetime, n: int, unit: str) -> datetime:
    unit = unit.rstrip("s")
    if unit == "day":
        return now + timedelta(days=n)
    if unit == "week":
        return now + timedelta(weeks=n)
    if unit == "month":
        return _add_months(now, n)
    return _add_months(now, n * 12)


def _next_weekday(today, weekday: int):
    delta = (weekday - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + timedelta(days=delta)


def _last_weekday(today, weekday: int):
    delta = (today.weekday() - weekday) % 7
    if delta == 0:
        delta = 7
    return today - timedelta(days=delta)


def _fmt_date(d) -> str:
    if isinstance(d, datetime):
        return d.strftime("%A, %B %d, %Y, %-I:%M %p %Z")
    return d.strftime("%A, %B %d, %Y")


def _relative_to_today(today: date, target: date) -> str:
    delta = (target - today).days
    if delta == 0:
        when = "today"
    elif delta > 0:
        when = f"{delta} day{'s' if delta != 1 else ''} in the future"
    else:
        when = f"{-delta} day{'s' if delta != -1 else ''} ago"
    return f"{_fmt_date(target)} ({when})"


_WORD_NUMS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "couple": 2, "few": 3,
}
_NUM = r"(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|couple|few|\d+)"
_UNIT = r"(?:day|days|week|weeks|month|months|year|years)"
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_WEEKDAYS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]
_NAMED = {
    "christmas": (12, 25),
    "new year": (1, 1),
    "new year's": (1, 1),
    "halloween": (10, 31),
    "independence day": (7, 4),
    "july 4th": (7, 4),
    "valentine": (2, 14),
}


def _parse_n(token: str) -> int:
    if token.isdigit():
        return int(token)
    return _WORD_NUMS.get(token, 1)


def _parse_calendar_dates(q: str, today: date):
    """Yield (label, date) for explicit calendar dates in the question."""
    found = []

    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", q)
    if iso:
        try:
            found.append((iso.group(0), date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))))
        except ValueError:
            pass

    slash = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", q)
    if slash:
        month, day, year = int(slash.group(1)), int(slash.group(2)), slash.group(3)
        try:
            if year:
                y = int(year)
                if y < 100:
                    y += 2000
                found.append((slash.group(0), date(y, month, day)))
            else:
                this = date(today.year, month, day)
                found.append((slash.group(0), this))
        except ValueError:
            pass

    named = re.search(
        r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(\d{4}))?\b",
        q,
    )
    if named:
        month = _MONTHS[named.group(1)]
        day = int(named.group(2))
        try:
            if named.group(3):
                found.append((named.group(0), date(int(named.group(3)), month, day)))
            else:
                found.append((named.group(0), date(today.year, month, day)))
        except ValueError:
            pass

    return found


def _named_occurrence(today: date, month: int, day: int, past: bool) -> date:
    try:
        this = date(today.year, month, day)
    except ValueError:
        return today
    if past:
        return this if this < today else date(today.year - 1, month, day)
    return this if this > today else date(today.year + 1, month, day)


def date_context(question: str) -> str:
    """Ground-truth clock and past/future date math computed in Python."""
    now = datetime.now().astimezone()
    today = now.date()
    q = (question or "").lower()
    want_past = bool(re.search(r"\b(ago|last|previous|was|were|past|before|yesterday)\b", q))
    want_future = bool(re.search(r"\b(in|after|next|until|till|from now|upcoming|will|tomorrow)\b", q))

    lines = [
        f"Current local date and time: {now.strftime('%A, %B %d, %Y, %-I:%M %p %Z')}.",
        f"ISO date: {today.isoformat()}.",
        f"Tomorrow is {_fmt_date(today + timedelta(days=1))}.",
        f"Yesterday was {_fmt_date(today - timedelta(days=1))}.",
        f"The day after tomorrow is {_fmt_date(today + timedelta(days=2))}.",
        f"The day before yesterday was {_fmt_date(today - timedelta(days=2))}.",
    ]

    future = re.search(rf"\b(?:in|after)\s+({_NUM})\s+({_UNIT})\b", q)
    if future:
        n = _parse_n(future.group(1))
        unit = future.group(2).rstrip("s")
        target = _shift(now, n, unit)
        lines.append(f"{n} {unit}{'s' if n != 1 else ''} from now is {_fmt_date(target)}.")

    from_now = re.search(rf"\b({_NUM})\s+({_UNIT})\s+from\s+(?:now|today)\b", q)
    if from_now:
        n = _parse_n(from_now.group(1))
        unit = from_now.group(2).rstrip("s")
        target = _shift(now, n, unit)
        lines.append(f"{n} {unit}{'s' if n != 1 else ''} from now is {_fmt_date(target)}.")

    past = re.search(rf"\b({_NUM})\s+({_UNIT})\s+ago\b", q)
    if past:
        n = _parse_n(past.group(1))
        unit = past.group(2).rstrip("s")
        target = _shift(now, -n, unit)
        lines.append(f"{n} {unit}{'s' if n != 1 else ''} ago was {_fmt_date(target)}.")

    if re.search(r"\bnext week\b", q):
        lines.append(f"Next week, same day, is {_fmt_date(today + timedelta(weeks=1))}.")
    if re.search(r"\blast week\b", q):
        lines.append(f"Last week, same day, was {_fmt_date(today - timedelta(weeks=1))}.")
    if re.search(r"\bnext month\b", q):
        lines.append(f"Next month, same date, is {_fmt_date(_add_months(now, 1))}.")
    if re.search(r"\blast month\b", q):
        lines.append(f"Last month, same date, was {_fmt_date(_add_months(now, -1))}.")
    if re.search(r"\bnext year\b", q):
        lines.append(f"Next year, same date, is {_fmt_date(_add_months(now, 12))}.")
    if re.search(r"\blast year\b", q):
        lines.append(f"Last year, same date, was {_fmt_date(_add_months(now, -12))}.")

    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b(next|this coming)\s+{name}\b", q):
            lines.append(f"Next {name.capitalize()} is {_fmt_date(_next_weekday(today, i))}.")
        if re.search(rf"\blast {name}\b", q):
            lines.append(f"Last {name.capitalize()} was {_fmt_date(_last_weekday(today, i))}.")

    for label, (month, day) in _NAMED.items():
        if label not in q:
            continue
        if want_past and not want_future:
            target = _named_occurrence(today, month, day, past=True)
            lines.append(f"Last {label} was {_relative_to_today(today, target)}.")
        else:
            target = _named_occurrence(today, month, day, past=False)
            lines.append(f"Next {label} is {_relative_to_today(today, target)}.")
            if want_past:
                prev = _named_occurrence(today, month, day, past=True)
                lines.append(f"Last {label} was {_relative_to_today(today, prev)}.")

    for raw, parsed in _parse_calendar_dates(q, today):
        target = parsed
        if parsed.year == today.year and not re.search(r"\b\d{4}\b", raw):
            if want_past and not want_future:
                if parsed >= today:
                    try:
                        target = parsed.replace(year=today.year - 1)
                    except ValueError:
                        target = parsed
            elif want_future and not want_past:
                if parsed <= today:
                    try:
                        target = parsed.replace(year=today.year + 1)
                    except ValueError:
                        target = parsed
        lines.append(f"{raw} resolves to {_relative_to_today(today, target)}.")

    return "\n".join(lines)


def load_all_history() -> list:
    history = []
    for path in sorted(glob.glob(os.path.join(MEMORY_DIR, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                if isinstance(data, list):
                    history.extend(data)
        except Exception:
            pass
    return history


def rename_current_and_start_new():
    if os.path.exists(CURRENT_FILE):
        os.rename(CURRENT_FILE, os.path.join(MEMORY_DIR, f"{timestamp()}_session.json"))
    with open(CURRENT_FILE, "w", encoding="utf-8") as fp:
        json.dump([], fp)


def save_turn(turn: dict):
    try:
        with open(CURRENT_FILE, "r+", encoding="utf-8") as fp:
            data = json.load(fp)
            data.append(turn)
            fp.seek(0)
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.truncate()
    except Exception:
        with open(CURRENT_FILE, "w", encoding="utf-8") as fp:
            json.dump([turn], fp, ensure_ascii=False, indent=2)


memory = load_all_history()


def _normalize_wake_text(text: str) -> str:
    text = (text or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = text.replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def is_addressed(user_text: str) -> tuple:
    if not user_text:
        return False, ""
    wake = _normalize_wake_text(AI_NAME)
    norm = _normalize_wake_text(user_text)
    if not wake:
        return False, user_text

    tokens = norm.split()
    wake_n = max(1, len(wake.split()))
    best = 0
    start = None
    width = wake_n
    if wake_n == 1:
        for i, tok in enumerate(tokens):
            score = fuzz.ratio(wake, tok)
            if score > best:
                best, start, width = score, i, 1
    else:
        for i in range(0, max(0, len(tokens) - wake_n + 1)):
            window = " ".join(tokens[i : i + wake_n])
            score = fuzz.ratio(wake, window)
            if score > best:
                best, start, width = score, i, wake_n
    partial = fuzz.partial_ratio(wake, norm) if norm else 0
    if partial > best:
        best = partial
        if start is None and tokens:
            start, width = 0, min(wake_n, len(tokens))
    if best < WAKE_FUZZ_THRESHOLD:
        return False, user_text

    if start is not None and tokens:
        remain = tokens[:start] + tokens[start + width :]
        clean = " ".join(remain)
    else:
        clean = norm
    for prefix in WAKE_PREFIXES:
        pref = _normalize_wake_text(prefix)
        if pref:
            clean = re.sub(rf"\b{re.escape(pref)}\b\s*", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if clean:
        clean = clean[0].upper() + clean[1:]
    else:
        clean = "Yes?"
    return True, clean


def is_double_scratch_that(user_text: str) -> bool:
    if not user_text or not NEW_SESSION_PHRASE:
        return False
    matches = list(
        re.finditer(rf"\b{re.escape(NEW_SESSION_PHRASE)}\b", user_text.lower())
    )
    return len(matches) >= 2 and matches[-2].start() < matches[-1].start()


OLLAMA_DOWN_ERROR = "Error: Ollama is not running. Start it with: ollama serve"


def ollama_reachable() -> bool:
    try:
        ollama.list()
        return True
    except Exception:
        return False


def ollama_error_text(exc=None) -> str:
    if exc is None:
        return OLLAMA_DOWN_ERROR
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    if (
        "connect" in msg
        or "refused" in msg
        or "11434" in msg
        or "connection" in name
    ):
        return OLLAMA_DOWN_ERROR
    return f"Error: {type(exc).__name__}: {exc}"


def ui_host_port():
    host = os.environ.get("CLAIRE_UI_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("CLAIRE_UI_PORT", "8742"))
    except ValueError:
        port = 8742
    return host, port


def ui_port_in_use(host=None, port=None) -> bool:
    """True when something is already accepting connections on the UI port."""
    if host is None or port is None:
        host, port = ui_host_port()
    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        sock.connect((probe, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def startup_warnings() -> list:
    """Configured pieces that are missing. Warnings only — do not block Start."""
    items = []
    items.extend(comms.missing())
    mic = microphone_status(fresh=True)
    if not mic.get("ok"):
        items.append(mic.get("hint") or "No microphone found.")
    return items


def format_startup_warnings(items) -> list:
    if not items:
        return []
    lines = [
        "Warning: for an orderly start-up the following component(s) are necessary:",
        "",
    ]
    for item in items:
        lines.append(f"  - {item}")
    lines.append("")
    lines.append("See README for Ollama, whisper.cpp, and Piper download links.")
    return lines


def print_startup_warnings(items=None):
    if items is None:
        items = startup_warnings()
    lines = format_startup_warnings(items)
    if not lines:
        return
    print("\n".join(lines), flush=True)


def _key_token(key: str) -> str:
    return key.upper()


def cli_key_hint(*, running=None, recording=None) -> str:
    """Keys that work in the current CLI state."""
    if running is None:
        running = cli_running
    if recording is None:
        recording = bool(getattr(comms, "recording", False))

    def item(key, action):
        return f"{_key_token(key)} [{action}]"

    if not running:
        parts = [item(RUN_KEY, "start"), item(QUIT_KEY, "shut down")]
    elif recording:
        parts = [
            item(RECORD_KEY, "stop recording"),
            item(INTERRUPT_KEY, "interrupt"),
            item(RUN_KEY, "stop"),
            item(QUIT_KEY, "shut down"),
        ]
    else:
        parts = [
            item(RECORD_KEY, "record"),
            item(INTERRUPT_KEY, "interrupt"),
            item(RUN_KEY, "stop"),
            item(QUIT_KEY, "shut down"),
        ]
    return " ".join(parts)


def cli_key_rule(keys_line: str) -> str:
    """Dash rule the same width as the keys line, label centered."""
    label = " key bindings "
    width = max(len(keys_line), len(label) + 2)
    inner = width - len(label)
    left = inner // 2
    right = inner - left
    rule = ("-" * left) + label + ("-" * right)
    if len(rule) < width:
        rule += "-" * (width - len(rule))
    return rule


def cli_key_block(*, running=None, recording=None) -> list:
    keys = cli_key_hint(running=running, recording=recording)
    return [
        "",
        cli_key_rule(keys),
        keys,
    ]


def _print_cli_keys():
    if not CLI_INTERACTIVE:
        return
    keys = cli_key_hint()
    rule = cli_key_rule(keys)
    log("\n" + rule)
    log(keys)


def cli_action_for_key(key: str, *, running: bool) -> str:
    """Map a sshkeyboard name to a CLI action."""
    if key == QUIT_KEY:
        return "shutdown"
    if key == RUN_KEY:
        return "stop" if running else "start"
    if key == RECORD_KEY:
        return "record" if running else "standby"
    if key == INTERRUPT_KEY:
        return "interrupt" if running else "ignore"
    return "ignore"


def _cli_stop_loop(*, quiet: bool = False, hint: bool = True):
    global cli_running
    if comms.recording:
        comms.stop_recording()
    comms.interrupt(silent=quiet)
    cli_running = False
    log("Voice loop stopped.")
    log("Standby")
    if hint:
        _print_cli_keys()


def _cli_start_loop() -> bool:
    global cli_running
    reload_settings()
    rebind()
    if not ollama_reachable():
        log(OLLAMA_DOWN_ERROR)
        try:
            stop_listening()
        except Exception:
            pass
        sys.exit(1)
    warnings = startup_warnings()
    if warnings:
        for line in format_startup_warnings(warnings):
            log(line)
    cli_running = True
    log(f"{AI_NAME.capitalize()} is running")
    _print_cli_keys()
    return True


def _cli_shutdown():
    global stop_requested
    log("Shutting down...")
    _cli_stop_loop(quiet=True, hint=False)
    stop_requested = True
    try:
        stop_listening()
    except Exception:
        pass
    log("CLAIRE stopped.")


def _cli_leave():
    """Ctrl+C: same as Shut down."""
    _cli_shutdown()


def on_key_press(key):
    try:
        _on_key_press(key)
    except Exception as exc:
        log(f"handler error: {type(exc).__name__}: {exc}")


# Quit matching is independent of the wake-word slider. A low wake
# threshold (e.g. 50%) must not treat "count one" as "shut down".
QUIT_FUZZ_THRESHOLD = 90.0


def _quit_requested(user_text: str) -> bool:
    norm = _normalize_wake_text(user_text)
    if not norm:
        return False
    compact = norm.replace(" ", "")
    for phrase in QUIT_PHRASES:
        target = _normalize_wake_text(phrase)
        if not target:
            continue
        if re.search(rf"\b{re.escape(target)}\b", norm):
            return True
        compact_target = target.replace(" ", "")
        if compact_target and compact_target in compact:
            return True
        parts = target.split()
        tokens = norm.split()
        width = max(1, len(parts))
        if width == 1:
            for tok in tokens:
                if fuzz.ratio(target, tok) >= QUIT_FUZZ_THRESHOLD:
                    return True
        else:
            for i in range(0, max(0, len(tokens) - width + 1)):
                window = " ".join(tokens[i : i + width])
                if fuzz.ratio(target, window) >= QUIT_FUZZ_THRESHOLD:
                    return True
    return False


def process_utterance(user_text: str, on_result=None) -> dict:
    """Handle one transcript. `on_result` is called with the UI payload
    before Piper starts so Heard/Reply can paint while speech plays.
    """
    global memory, cli_running

    def emit(result: dict) -> dict:
        if on_result:
            try:
                on_result(result)
            except Exception:
                pass
        return result

    if not user_text:
        return emit({"ok": False, "action": "empty", "heard": "", "message": "No audio captured"})

    if _quit_requested(user_text):
        log("Quit phrase heard — stopping")
        result = emit({"ok": True, "action": "quit", "heard": user_text, "reply": "Goodbye!"})
        comms.speak("Goodbye!")
        cli_running = False
        if CLI_INTERACTIVE:
            log("Voice loop stopped.")
            log("Standby")
        return result

    addressed, clean = is_addressed(user_text)
    if not addressed:
        msg, spoken = missing_wake_reply()
        log(msg)
        result = emit({
            "ok": False,
            "action": "ignored",
            "heard": user_text,
            "message": msg,
            "reply": msg,
        })
        comms.speak(msg, spoken=spoken)
        return result

    if is_double_scratch_that(user_text):
        rename_current_and_start_new()
        memory.clear()
        result = emit({
            "ok": True,
            "action": "new_session",
            "heard": user_text,
            "reply": "New session started.",
        })
        comms.speak("New session started.")
        return result

    name = AI_NAME.capitalize()
    prompt = (
        f"You are {name}, a helpful, friendly, and highly intelligent AI assistant. "
        "You have perfect recall of our entire conversation history, no matter how long it is. "
        "Always stay in character and continue the conversation naturally.\n"
        "Dates in conversation history are from when those turns happened. "
        "They are not today's date. After the history you will get a system clock; "
        "that clock is the only ground truth for 'today', 'now', the day of the week, "
        "and the current time. Do not guess dates or use placeholders.\n\n"
        "Conversation history:\n"
    )
    for turn in memory:
        prompt += f"User: {turn['user']}\n{name}: {turn['ai']}\n"
    prompt += (
        "\n--- End of recovered memory ---\n"
        "System clock (authoritative; use this for any question about today or now):\n"
        f"{date_context(clean)}\n\n"
        f"User: {clean}\n{name}:"
    )

    emit({"ok": True, "action": "thinking", "heard": user_text, "message": "Thinking…"})
    try:
        response = comms.generate_with_fillers(prompt)
    except Exception as exc:
        err = ollama_error_text(exc)
        log(err)
        result = emit({
            "ok": False,
            "action": "ollama_down",
            "heard": user_text,
            "message": err,
        })
        if CLI_INTERACTIVE:
            try:
                stop_listening()
            except Exception:
                pass
            sys.exit(1)
        return result
    turn = {"user": user_text, "ai": response}
    memory.append(turn)
    save_turn(turn)
    result = emit({
        "ok": True,
        "action": "reply",
        "heard": user_text,
        "reply": response,
        "message": "Speaking",
        "clean": clean,
    })
    comms.speak(response)
    return result


def _on_key_press(key):
    action = cli_action_for_key(key, running=cli_running)
    if action == "start":
        _cli_start_loop()
    elif action == "stop":
        _cli_stop_loop()
    elif action == "shutdown":
        _cli_shutdown()
    elif action == "standby":
        log("Standby")
    elif action == "record":
        if comms.recording:
            process_utterance(comms.listen())
        else:
            comms.start_recording()
    elif action == "interrupt":
        comms.interrupt()


def main():
    global CLI_INTERACTIVE
    CLI_INTERACTIVE = True
    host, port = ui_host_port()
    if ui_port_in_use(host, port):
        print(
            f"Warning: port {port} is already in use on {host}. Not starting.",
            flush=True,
        )
        print(f"If the web UI is running, open http://{host}:{port}", flush=True)
        sys.exit(1)
    _print_cli_banner()
    if not ollama_reachable():
        sys.exit(1)
    print_startup_warnings()
    try:
        listen_keyboard(on_press=on_key_press)
    except KeyboardInterrupt:
        _cli_leave()


if __name__ == "__main__":
    main()

