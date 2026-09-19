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

Large files (Ollama weights, Whisper `.bin`, Piper `.onnx`, `whisper-cli`) are **not** in git. You download them and drop them in place.

## Warnings

Jarvis does **not** ship LLMs, Whisper weights, or Piper voices. It only lists **what you already downloaded** so you can pick one. Combinations are untested. Licenses of those files are **not** this repo’s MIT license — read each model card.

### Ollama (required)

Without a running Ollama server, **Start is disabled** and the UI stays locked.

- Install from [ollama.com](https://ollama.com). This repo’s `vendor/ollama/` is **not** what clones get.
- You must run `ollama serve` so the API is on **`127.0.0.1:11434`**. A different host/port will look like “Ollama is not running.”
- Pull at least one **chat/instruct** model before Start, e.g. `ollama pull qwen2.5:7b`. The dropdown is `ollama list`, nothing more.
- **Hardware:** a 7B-class model wants on the order of **8 GB+ RAM** free; 14B needs more; 70B can lock up a laptop. Disk for the pull is several GB. Slow models make the “thinking…” fillers fire often.
- Embedding-only, vision-only, or “reasoning/thinking” models often **will not** behave as a spoken assistant. English vs other languages is the model’s problem, not Jarvis’s.
- Local Ollama has **no token bill**. You still pay in RAM, disk, heat, and time.

### Piper voices (ONNX)

The voice dropdown is **every `*.onnx` file in `models/piper/`**. That is not a quality filter.

- Use voices from [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices) only. Other Hugging Face TTS repos (Coqui, StyleTTS, raw PyTorch, etc.) **will not load**.
- Each voice is **two files with the same stem**: `name.onnx` **and** `name.onnx.json`. Missing JSON → Piper fails at speak-time.
- Files are large (often **50–80 MB** per medium voice). Do not commit them; they are gitignored.
- **Speaker id** (`voice_speaker`) only applies to multi-speaker models (e.g. LibriTTS-R has hundreds of ids). A single-speaker voice should stay at `0`. A wrong id can crash or sound broken.
- Language of the ONNX file should match what you speak. An `en_US` voice reading other languages will sound wrong; that is expected.
- A corrupt or incomplete download will fail when the assistant tries to talk, not when you pick it in the list.

### Whisper

`bin/whisper-cli` must be a **whisper.cpp** binary, and `models/whisper/` must hold a **ggml** checkpoint (`ggml-base.en.bin` is the example). Other `.bin` formats will not work.

`scripts/download-models.sh` only fetches **one** example Whisper file and **one** example Piper voice so the folders exist. Replace them whenever you like.

## Pre-canned speech

Spoken lines that are **not** from the LLM live in `phrases/` as JSON, and you can edit them in the UI under **Admin → Pre-canned speech**.

| File | When it is used |
|---|---|
| `phrases/thinking.json` | Waiting on the model |
| `phrases/missing_wake.json` | Transcript did not contain the wake word |

Each entry is `{"text": "...", "preferred": false}`. Mark **Preferred** in Admin to boost those lines.

- **Flat** (default): every line has equal odds (`time % count`).
- **Preferred weighting:** slider 0–100. **50%** means a preferred line is heard about **2×** as often as a non-preferred one. **100%** uses preferred lines only (falls back to all if none are marked). **25%** is a 1.5× boost.

`{name}` in missing-wake text becomes the wake word (with a breath pause). `...` is a pause.

You can edit Admin while the assistant is running. Leaving Admin with unsaved changes asks to **store** them; they apply only after **Stop** and **Start**. A banner at the top warns when stored settings differ from the running session.

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

Drop extra Piper `.onnx` files in `models/piper/` and extra Whisper `ggml-*.bin` in `models/whisper/`, then refresh the UI. Pull more LLMs with `ollama pull`. Catalogs: [Piper voices](https://huggingface.co/rhasspy/piper-voices), [Ollama library](https://ollama.com/library), [whisper.cpp models](https://huggingface.co/ggerganov/whisper.cpp). See **Warnings** above: listing a file does not mean it is tested.

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
| `phrases/thinking.json` | Thinking lines (edit in Admin) |
| `phrases/missing_wake.json` | Missing-wake lines (`{name}` = wake word) |
| `memory/` | Session JSON (local only) |
| `models/` | Whisper + Piper files (local only) |
| `bin/whisper-cli` | You provide this binary |

## License

MIT. See [LICENSE](LICENSE).
