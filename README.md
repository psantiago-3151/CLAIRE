# Jarvis

Local voice assistant for **macOS** and **Linux**: **mic → Whisper → Ollama → Piper TTS**.

A web UI on [http://127.0.0.1:8742](http://127.0.0.1:8742) starts and stops the assistant, picks models and voices, and edits `jarvis.conf`. The assistant is a named wake-word loop with session memory, thinking fillers, and on-device speech.

Windows is not supported yet.

## Credits

- **Design and product:** Philip Santiago (the plumbing).
- **Implementation:** Grok (xAI) (the plumber).

## Requirements

- Python 3.9+
- [Ollama](https://ollama.com) (`ollama serve` on `127.0.0.1:11434`)
- Microphone
- **macOS:** Mic permission for Terminal/Python
- **Linux:** `alsa-utils`, `sox`, and `pw-play` / `paplay` / `aplay`

Large files (Ollama weights, Whisper `.bin`, Piper `.onnx`, `whisper-cli`) are **not** in git. Download them locally.

## Setup

```bash
git clone https://github.com/YOUR_USER/Jarvis.git
cd Jarvis
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp jarvis.conf.example jarvis.conf
chmod +x scripts/download-models.sh
./scripts/download-models.sh
```

Put a `whisper-cli` binary in `bin/` ([whisper.cpp](https://github.com/ggml-org/whisper.cpp) — build for your OS). Piper’s CLI is `venv/bin/piper` from `piper-tts`.

Pull an LLM (once Ollama is installed):

```bash
ollama pull qwen2.5:7b
```

### Linux packages

```bash
# Fedora / Nobara
sudo dnf install alsa-utils sox pipewire-utils

# Debian / Ubuntu
sudo apt install alsa-utils sox pipewire-utils pulseaudio-utils
```

Add your user to the `audio` group if capture is silent, then log out and back in.

## Run

Terminal 1 — Ollama:

```bash
ollama serve
```

Terminal 2 — UI (from the repo, venv on):

```bash
source venv/bin/activate
python src/web.py
```

Open [http://127.0.0.1:8742](http://127.0.0.1:8742). If Ollama is down, the UI locks except **Shut down**. Pick a model and Piper voice, then **Start**. **Record** / **Stop recording** take a clip. **Interrupt** stops speech.

Keyboard-only (real TTY):

```bash
python src/comms.py
```

- **Tab** start/stop recording  
- **F12** interrupt  
- **Esc** quit when idle  
- Say the **wake word** plus your request  
- **scratch that, scratch that** starts a new memory session  
- Spoken **exit** / **goodbye** / **shut down** quits  

## Config

Copy `jarvis.conf.example` → `jarvis.conf` (gitignored). The UI writes that file. Env vars `JARVIS_*` override it.

| Setting | Env | Default |
|---|---|---|
| Ollama model | `JARVIS_LLM_MODEL` | `qwen2.5:7b` |
| Piper voice | `JARVIS_VOICE_MODEL` | `models/piper/en_US-libritts_r-medium.onnx` |
| Piper speaker id | `JARVIS_VOICE_SPEAKER` | `0` |
| Whisper binary | `JARVIS_WHISPER_BIN` | `bin/whisper-cli` |
| Whisper model | `JARVIS_WHISPER_MODEL` | `models/whisper/ggml-base.en.bin` |
| Piper binary | `JARVIS_PIPER_BIN` | `venv/bin/piper` |
| Memory directory | `JARVIS_MEMORY_DIR` | `memory/` |
| Microphone (Linux) | `JARVIS_MIC_DEVICE` | auto (`default` / USB) |
| Wake word | `JARVIS_WAKE_WORD` | `friday` |
| Record key | `JARVIS_RECORD_KEY` | `tab` |
| Interrupt key | `JARVIS_INTERRUPT_KEY` | `f12` |
| Quit key | `JARVIS_QUIT_KEY` | `esc` |
| Spoken quit phrases | `JARVIS_QUIT_PHRASES` | `exit,goodbye,shut down` |
| New-session phrase | `JARVIS_NEW_SESSION_PHRASE` | `scratch that` |

More Piper voices: [Hugging Face piper-voices (en_US)](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US). More Ollama models: [ollama.com/library](https://ollama.com/library).

If Record is silent, set **Microphone device** in Admin (`default`, `pulse`, `plughw:1,0`). Check `arecord -l` / `arecord -L`. After several consecutive empty recordings, the UI warns that the mic may need assignment — or that nobody spoke.

## Layout

| Path | What |
|---|---|
| `src/web.py` | FastAPI UI (`http://127.0.0.1:8742`) |
| `src/ui/index.html` | Config page |
| `src/comms.py` | Mic, Whisper, Ollama, Piper, loop |
| `src/voice_chat3.py` | Wrapper for `comms.main()` |
| `jarvis.conf.example` | Settings template |
| `scripts/download-models.sh` | Whisper + default Piper voice |
| `memory/` | Session JSON (local only) |
| `models/` | Whisper + Piper files (local only) |
| `bin/whisper-cli` | You provide this binary |

## License

MIT. See [LICENSE](LICENSE).
