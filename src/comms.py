#!/usr/bin/env python3
"""Friday: mic, STT, LLM, TTS, and the voice loop.

Run: ./comms.py  (from src/, venv active)
"""
import fcntl
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

import ollama
from sshkeyboard import listen_keyboard, stop_listening

IS_MAC = sys.platform == "darwin"
ROOT = Path(__file__).resolve().parent.parent


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


CONF = _load_conf(ROOT / "jarvis.conf")


def _setting(env_name: str, conf_key: str, default: str, *, path: bool = False) -> str:
    if os.environ.get(env_name, "") != "":
        raw = os.environ[env_name]
    elif CONF.get(conf_key, "") != "":
        raw = CONF[conf_key]
    else:
        raw = default
    raw = str(raw).strip()
    if path or raw.startswith("~") or "/" in raw or "\\" in raw:
        return _resolve_path(raw)
    return raw


MIN_WAIT_SECS = 3.0
FILLER_PHRASES = [
    "Thinking...",
    "I'm in Deep thought about our interaction.",
    "Pondering about your message",
    "calculating...",
    "Researching...",
    "I'm pondering...",
    "contemplating on your question!",
    "give me a moment",
    "One Sec.",
    "gathering my thoughts...",
    "Crunching the numbers...",
    "Hold that thought...",
    "hmm.. Interesting",
]
MISSING_WAKE_PHRASES = [
    "I did not detect the wake word in your request, please ask your request again addressing it to {name}",
    "I didn't catch the wake word. Please ask again and say {name}.",
    "Please address that to {name}.",
    "Say {name} so I know you're talking to me.",
    "I need you to include {name} in your request.",
    "Say my name, Say my name, ... {name}",
    "Call me by my title, ... {name}",
    "Direct your question to me, {name}, for a favor.",
    "I ... am ... Spartacus, I mean ... I'm {name}, address your question to me.",
    "Address your questions to me, {name}, please",
    "Please, ask me directly... using my surname is {name}",
    "We can't come to the phone, because you identify me, {name}, in your message.",
    "I only answer when you say {name}.",
    "Please start with {name}, then your question.",
    "I missed the wake word. Try again with {name}.",
    "Address me as {name} so I can help.",
    "Say {name} first, then I'll listen.",
    "To get my attention, include {name} in your request.",
    "I'm here. Call me {name} and ask again.",
    "One more time, please, using {name}.",
    "That's not my name. Try {name}.",
    "I'm not a mind reader. Say {name}.",
    "Close, but no cigar. The magic word is {name}.",
    "You rang? Almost. Lead with {name}.",
    "I answer to {name}, not vibes.",
    "Security check failed. Password is {name}.",
    "Speak friend and enter. Friend is {name}.",
    "I'm {name}, not hey you.",
    "Without {name}, I'm just expensive silence.",
    "New phone, who dis? It's {name}.",
    "That's cute. Now say {name}.",
    "I heard you. I just didn't hear {name}.",
    "Manners, please. My name is {name}.",
    "You've got the question. I've got the name: {name}.",
    "Open sesame is retired. We use {name} now.",
]


def pick_timed_phrase(phrases) -> str:
    if not phrases:
        return ""
    return phrases[int(time.time() * 1000) % len(phrases)]


BREATH_MARK = "<<<breath>>>"
MIN_BREATH_SECS = 0.05
MAX_BREATH_SECS = 2.0
BREATH_SECS = 0.35


def missing_wake_reply() -> tuple:
    name = AI_NAME.capitalize()
    template = pick_timed_phrase(MISSING_WAKE_PHRASES)
    display = template.replace("{name}", name)
    spoken = template.replace("{name}", f"{BREATH_MARK}{name}")
    return display, spoken


def _secs(env_name: str, conf_key: str, default: str) -> float:
    try:
        return max(MIN_WAIT_SECS, float(_setting(env_name, conf_key, default)))
    except ValueError:
        return max(MIN_WAIT_SECS, float(default))


def _load_wait_secs():
    global THINKING_INTERVAL_SECS, BREATH_SECS
    THINKING_INTERVAL_SECS = _secs(
        "JARVIS_THINKING_INTERVAL_SECS", "thinking_interval_secs", "5"
    )
    try:
        breath = float(_setting("JARVIS_NAME_BREATH_SECS", "name_breath_secs", "0.35"))
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


LLM_MODEL = _setting("JARVIS_LLM_MODEL", "llm_model", "qwen2.5:7b")
VOICE_MODEL = _setting(
    "JARVIS_VOICE_MODEL",
    "voice_model",
    str(ROOT / "models" / "piper" / "en_US-libritts_r-medium.onnx"),
    path=True,
)
VOICE_SPEAKER = int(_setting("JARVIS_VOICE_SPEAKER", "voice_speaker", "0"))
WHISPER_BIN = _setting(
    "JARVIS_WHISPER_BIN",
    "whisper_bin",
    str(ROOT / "bin" / "whisper-cli"),
    path=True,
)
WHISPER_MODEL = _setting(
    "JARVIS_WHISPER_MODEL",
    "whisper_model",
    str(ROOT / "models" / "whisper" / "ggml-base.en.bin"),
    path=True,
)
PIPER_BIN = _setting(
    "JARVIS_PIPER_BIN",
    "piper_bin",
    str(ROOT / "venv" / "bin" / "piper"),
    path=True,
)
MEMORY_DIR = _setting(
    "JARVIS_MEMORY_DIR",
    "memory_dir",
    str(ROOT / "memory"),
    path=True,
)
MIC_DEVICE = _setting("JARVIS_MIC_DEVICE", "mic_device", "")
AI_NAME = _setting("JARVIS_WAKE_WORD", "wake_word", "friday").lower()
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
RECORD_KEY = _setting("JARVIS_RECORD_KEY", "record_key", "tab").lower()
INTERRUPT_KEY = _setting("JARVIS_INTERRUPT_KEY", "interrupt_key", "f12").lower()
QUIT_KEY = _setting("JARVIS_QUIT_KEY", "quit_key", "esc").lower()
QUIT_PHRASES = [
    p.strip().lower()
    for p in _setting(
        "JARVIS_QUIT_PHRASES", "quit_phrases", "exit,goodbye,shut down"
    ).split(",")
    if p.strip()
]
NEW_SESSION_PHRASE = _setting(
    "JARVIS_NEW_SESSION_PHRASE", "new_session_phrase", "scratch that"
).lower()
_load_wait_secs()
WAKE_PREFIXES = ["hey", "ok", "okay", "please", "yo", "hi", "hello"]

CONF_PATH = ROOT / "jarvis.conf"
CONFIG_FIELDS = [
    {"key": "llm_model", "env": "JARVIS_LLM_MODEL", "label": "Ollama model", "group": "models", "ui": "visible", "default": "qwen2.5:7b"},
    {"key": "voice_model", "env": "JARVIS_VOICE_MODEL", "label": "Piper voice", "group": "models", "ui": "visible", "default": "models/piper/en_US-libritts_r-medium.onnx"},
    {"key": "voice_speaker", "env": "JARVIS_VOICE_SPEAKER", "label": "Speaker id", "group": "models", "ui": "admin", "default": "0"},
    {"key": "whisper_bin", "env": "JARVIS_WHISPER_BIN", "label": "Whisper binary", "group": "models", "ui": "admin", "default": "bin/whisper-cli"},
    {"key": "whisper_model", "env": "JARVIS_WHISPER_MODEL", "label": "Whisper model", "group": "models", "ui": "admin", "default": "models/whisper/ggml-base.en.bin"},
    {"key": "piper_bin", "env": "JARVIS_PIPER_BIN", "label": "Piper binary", "group": "models", "ui": "admin", "default": "venv/bin/piper"},
    {"key": "memory_dir", "env": "JARVIS_MEMORY_DIR", "label": "Memory directory", "group": "models", "ui": "admin", "default": "memory"},
    {"key": "mic_device", "env": "JARVIS_MIC_DEVICE", "label": "Microphone device (Linux arecord; empty = auto)", "group": "models", "ui": "admin", "default": ""},
    {"key": "thinking_interval_secs", "env": "JARVIS_THINKING_INTERVAL_SECS", "label": "Thinking reminder every (sec)", "group": "models", "ui": "admin", "default": "5"},
    {"key": "name_breath_secs", "env": "JARVIS_NAME_BREATH_SECS", "label": "Pause before wake word (sec)", "group": "models", "ui": "admin", "default": "0.35"},
    {"key": "wake_word", "env": "JARVIS_WAKE_WORD", "label": "Wake word", "group": "runtime", "ui": "visible", "default": "friday"},
    {"key": "record_key", "env": "JARVIS_RECORD_KEY", "label": "Record key", "group": "runtime", "ui": "hidden", "default": "tab"},
    {"key": "interrupt_key", "env": "JARVIS_INTERRUPT_KEY", "label": "Interrupt speech key", "group": "runtime", "ui": "hidden", "default": "f12"},
    {"key": "quit_key", "env": "JARVIS_QUIT_KEY", "label": "Quit key", "group": "runtime", "ui": "hidden", "default": "esc"},
    {"key": "quit_phrases", "env": "JARVIS_QUIT_PHRASES", "label": "Spoken quit phrases", "group": "runtime", "ui": "visible", "default": "exit,goodbye,shut down"},
    {"key": "new_session_phrase", "env": "JARVIS_NEW_SESSION_PHRASE", "label": "New session phrase", "group": "runtime", "ui": "visible", "default": "scratch that"},
]
_FIELD_KEYS = {f["key"] for f in CONFIG_FIELDS}

CONF_TEMPLATE = """# Jarvis settings. Env vars (JARVIS_*) override these.
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

# Wake word (matched anywhere in the transcript)
wake_word={wake_word}

# sshkeyboard names: tab, space, f8, f12, esc, ...
record_key={record_key}
interrupt_key={interrupt_key}
quit_key={quit_key}

# Spoken runtime commands (comma-separated; new session requires the phrase twice)
quit_phrases={quit_phrases}
new_session_phrase={new_session_phrase}
"""


def reload_settings():
    global CONF, LLM_MODEL, VOICE_MODEL, VOICE_SPEAKER, WHISPER_BIN, WHISPER_MODEL
    global PIPER_BIN, MEMORY_DIR, MIC_DEVICE, AI_NAME, RECORD_KEY, INTERRUPT_KEY, QUIT_KEY
    global QUIT_PHRASES, NEW_SESSION_PHRASE, CURRENT_FILE
    global THINKING_INTERVAL_SECS, BREATH_SECS
    CONF = _load_conf(CONF_PATH)
    LLM_MODEL = _setting("JARVIS_LLM_MODEL", "llm_model", "qwen2.5:7b")
    VOICE_MODEL = _setting(
        "JARVIS_VOICE_MODEL",
        "voice_model",
        str(ROOT / "models" / "piper" / "en_US-libritts_r-medium.onnx"),
        path=True,
    )
    VOICE_SPEAKER = int(_setting("JARVIS_VOICE_SPEAKER", "voice_speaker", "0"))
    WHISPER_BIN = _setting(
        "JARVIS_WHISPER_BIN",
        "whisper_bin",
        str(ROOT / "bin" / "whisper-cli"),
        path=True,
    )
    WHISPER_MODEL = _setting(
        "JARVIS_WHISPER_MODEL",
        "whisper_model",
        str(ROOT / "models" / "whisper" / "ggml-base.en.bin"),
        path=True,
    )
    PIPER_BIN = _setting(
        "JARVIS_PIPER_BIN",
        "piper_bin",
        str(ROOT / "venv" / "bin" / "piper"),
        path=True,
    )
    MEMORY_DIR = _setting(
        "JARVIS_MEMORY_DIR",
        "memory_dir",
        str(ROOT / "memory"),
        path=True,
    )
    MIC_DEVICE = _setting("JARVIS_MIC_DEVICE", "mic_device", "")
    _mic_cache["status"] = None
    AI_NAME = _setting("JARVIS_WAKE_WORD", "wake_word", "friday").lower()
    RECORD_KEY = _setting("JARVIS_RECORD_KEY", "record_key", "tab").lower()
    INTERRUPT_KEY = _setting("JARVIS_INTERRUPT_KEY", "interrupt_key", "f12").lower()
    QUIT_KEY = _setting("JARVIS_QUIT_KEY", "quit_key", "esc").lower()
    QUIT_PHRASES = [
        p.strip().lower()
        for p in _setting(
            "JARVIS_QUIT_PHRASES", "quit_phrases", "exit,goodbye,shut down"
        ).split(",")
        if p.strip()
    ]
    NEW_SESSION_PHRASE = _setting(
        "JARVIS_NEW_SESSION_PHRASE", "new_session_phrase", "scratch that"
    ).lower()
    _load_wait_secs()
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
    wake = os.environ.get("JARVIS_WAKE_WORD") or file_vals["wake_word"]
    return {"fields": fields, "wake_word": wake.lower()}


def save_config(updates: dict) -> dict:
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
    for key in ("record_key", "interrupt_key", "quit_key"):
        current[key] = current[key].lower()
    current["wake_word"] = current["wake_word"].lower()
    CONF_PATH.write_text(CONF_TEMPLATE.format(**current), encoding="utf-8")
    reload_settings()
    return current


def rebind():
    global comms, memory, stop_requested
    Path(MEMORY_DIR).mkdir(parents=True, exist_ok=True)
    comms = Comms()
    memory = load_all_history()
    stop_requested = False


def _key_label(key: str) -> str:
    if len(key) > 1 and key[0] == "f" and key[1:].isdigit():
        return key.upper()
    return key.capitalize()


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


def log(*args):
    """Print even when sshkeyboard leaves stdout non-blocking."""
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
        log("\n🎤 RECORDING... speak now! (press Tab to stop)")
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

        if os.path.exists(self.tmp_tts):
            os.remove(self.tmp_tts)

        piper = self.piper_bin if os.path.isfile(self.piper_bin) else shutil.which("piper")
        used_piper = False
        if piper and os.path.isfile(self.voice_model):
            used_piper = self._synth_spoken(piper, raw_spoken, token)

        if token != self._speech_id:
            return

        log(f"AI: {text}")
        self.speaking = True
        if used_piper:
            player = _wav_player(self.tmp_tts)
            if not player:
                log("No WAV player found (afplay, pw-play, paplay, or aplay)")
                self.speaking = False
                return
            self.speak_process = subprocess.Popen(player)
        elif IS_MAC:
            slnc_ms = max(50, int(round(BREATH_SECS * 1000)))
            say_text = raw_spoken.replace(BREATH_MARK, f" [[slnc {slnc_ms}]] ")
            self.speak_process = subprocess.Popen(["say", say_text])
        else:
            log("No TTS backend available")
            self.speaking = False
            return
        self.speak_process.wait()
        if token == self._speech_id:
            self.speaking = False

    def interrupt(self, silent: bool = False):
        self._speech_id += 1
        if self.speaking and self.speak_process:
            if not silent:
                log(f"\n🛑 Speech interrupted ({_key_label(INTERRUPT_KEY)})")
            self.speak_process.terminate()
            self.speaking = False

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
            return pick_timed_phrase(FILLER_PHRASES)

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


def is_addressed(user_text: str) -> tuple:
    if not user_text:
        return False, ""
    lower_text = user_text.lower()
    if AI_NAME.lower() not in lower_text:
        return False, user_text
    clean = re.sub(rf"\b{AI_NAME}\b[.,!?]*\s*", " ", user_text, flags=re.IGNORECASE)
    for prefix in WAKE_PREFIXES:
        clean = re.sub(rf"\b{prefix}\b[.,!?]*\s+", " ", clean, flags=re.IGNORECASE)
    clean = clean.strip()
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


def on_key_press(key):
    global stop_requested
    try:
        _on_key_press(key)
    except Exception as exc:
        log(f"handler error: {type(exc).__name__}: {exc}")


def process_utterance(user_text: str) -> dict:
    global stop_requested, memory
    if not user_text:
        return {"ok": False, "action": "empty", "heard": "", "message": "No audio captured"}

    if any(w in user_text.lower() for w in QUIT_PHRASES):
        comms.speak("Goodbye!")
        stop_requested = True
        return {"ok": True, "action": "quit", "heard": user_text, "reply": "Goodbye!"}

    addressed, clean = is_addressed(user_text)
    if not addressed:
        msg, spoken = missing_wake_reply()
        log(msg)
        comms.speak(msg, spoken=spoken)
        return {
            "ok": False,
            "action": "ignored",
            "heard": user_text,
            "message": msg,
            "reply": msg,
        }

    if is_double_scratch_that(user_text):
        rename_current_and_start_new()
        memory.clear()
        comms.speak("New session started.")
        return {
            "ok": True,
            "action": "new_session",
            "heard": user_text,
            "reply": "New session started.",
        }

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

    response = comms.generate_with_fillers(prompt)
    turn = {"user": user_text, "ai": response}
    memory.append(turn)
    save_turn(turn)
    comms.speak(response)
    return {"ok": True, "action": "reply", "heard": user_text, "reply": response, "clean": clean}


def _on_key_press(key):
    global stop_requested
    if key == RECORD_KEY:
        if comms.recording:
            result = process_utterance(comms.listen())
            if result.get("action") == "quit":
                stop_listening()
        else:
            comms.start_recording()

    elif key == INTERRUPT_KEY:
        comms.interrupt()

    elif key == QUIT_KEY:
        if not comms.speaking and not comms.recording:
            log("\nGoodbye!")
            stop_requested = True
            stop_listening()


def main():
    print(f"Loaded full conversation history: {len(memory)} turns total")
    print("\n" + "=" * 60)
    print("LOCAL VOICE AI – FINAL WORKING")
    print("=" * 60)
    print(f"   Project: {ROOT}")
    print(f"   LLM: {comms.llm_model}")
    print(f"   Voice: {Path(comms.voice_model).name} speaker {comms.voice_speaker}")
    print(f"   Wake word: {AI_NAME.capitalize()} (detected anywhere)")
    print(
        f"   Say '{NEW_SESSION_PHRASE}, {NEW_SESSION_PHRASE}' (twice) to start a new session"
    )
    print(f"   {_key_label(RECORD_KEY)}: start/stop recording")
    print(f"   {_key_label(INTERRUPT_KEY)}: interrupt speech")
    print(f"   {_key_label(QUIT_KEY)}: exit (only when idle)\n")

    missing = comms.missing()
    if missing:
        print("Setup problems:")
        for item in missing:
            print(f"  - {item}")
        sys.exit(1)

    print(f"👂 Waiting for you to press {RECORD_KEY.upper()}...")
    try:
        listen_keyboard(on_press=on_key_press)
    except KeyboardInterrupt:
        print("\nGoodbye!")


if __name__ == "__main__":
    main()

