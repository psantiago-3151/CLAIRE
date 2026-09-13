# Jarvis (Friday)

Local voice assistant from the Nobara `voice_chat3.py` stack, ported to this Mac.

**mic → Whisper → Ollama → Piper TTS**

Wake word and keys are set in `jarvis.conf` (defaults: **Friday**, **Tab** to record, **F12** to interrupt, **Esc** to quit).

## Run

Start Ollama (once per reboot), then the app **in a real terminal** (record/quit keys need a TTY):

```bash
/Users/p-san/Projects/Jarvis/vendor/ollama/ollama serve
```

Config UI (start/stop + settings):

```bash
cd /Users/p-san/Projects/Jarvis
source venv/bin/activate
python src/web.py
```

Open [http://127.0.0.1:8742](http://127.0.0.1:8742). The page loads defaults from `jarvis.conf`. Pick a downloaded Ollama model and Piper voice, then **Start Friday**. Record and interrupt are buttons on that page. Keyboard keys stay in `jarvis.conf` for the terminal app only. Admin paths are in the top-right menu. Save / Reset appear only after you change a setting.

Keyboard-only loop (no browser):

```bash
cd /Users/p-san/Projects/Jarvis
source venv/bin/activate
python src/comms.py
```

- **Tab** start/stop recording (`record_key`)
- **F12** interrupt speech (`interrupt_key`)
- **Esc** quit when idle (`quit_key`)
- Say the wake word plus your request
- **scratch that, scratch that** starts a new memory session (`new_session_phrase`)
- Spoken **exit** / **goodbye** / **shut down** quits (`quit_phrases`)

macOS will ask for **Microphone** permission the first time.

## Config

Edit `jarvis.conf`. Environment variables override the file.

| Setting | Env | Default |
|---|---|---|
| Hugging Face Piper voice | `JARVIS_VOICE_MODEL` | `models/piper/en_US-libritts_r-medium.onnx` |
| Piper speaker id (0–903) | `JARVIS_VOICE_SPEAKER` | `0` |
| Ollama model | `JARVIS_LLM_MODEL` | `qwen2.5:7b` |
| Whisper binary | `JARVIS_WHISPER_BIN` | `bin/whisper-cli` |
| Whisper model | `JARVIS_WHISPER_MODEL` | `models/whisper/ggml-base.en.bin` |
| Piper binary | `JARVIS_PIPER_BIN` | `venv/bin/piper` |
| Memory directory | `JARVIS_MEMORY_DIR` | `memory/` |
| Wake word | `JARVIS_WAKE_WORD` | `friday` |
| Record start/stop key | `JARVIS_RECORD_KEY` | `tab` |
| Interrupt speech key | `JARVIS_INTERRUPT_KEY` | `f12` |
| Quit key (idle only) | `JARVIS_QUIT_KEY` | `esc` |
| Spoken quit phrases | `JARVIS_QUIT_PHRASES` | `exit,goodbye,shut down` |
| New-session phrase (say twice) | `JARVIS_NEW_SESSION_PHRASE` | `scratch that` |

The voice file is the [Piper `en_US-libritts_r-medium`](https://huggingface.co/rhasspy/piper-voices) template from Hugging Face. Swap `voice_model` to another `.onnx` from that repo to change the voice.

Keys are sshkeyboard names (`tab`, `space`, `f8`, `f12`, `esc`, …). Ctrl+C still quits immediately.

## Layout

| Path | What |
|---|---|
| `jarvis.conf` | Editable settings (also the config UI) |
| `src/web.py` | FastAPI config page on port 8742 |
| `src/ui/index.html` | Config UI |
| `src/comms.py` | Friday app (mic, Whisper, Ollama, Piper, loop) |
| `src/voice_chat3.py` | Wrapper that runs `comms.main()` |
| `src/voice_chat3.linux.py` | Unmodified Nobara copy |
| `venv/` | Python 3.9 env |
| `vendor/ollama/` | Ollama 0.33.3 |
| `bin/whisper-cli` | Whisper.cpp (Metal) |
| `models/whisper/ggml-base.en.bin` | STT model |
| `models/piper/en_US-libritts_r-medium.onnx` | TTS voice |
| `memory/` | Conversation JSON |

Original Linux tools (`arecord`, `sox`, `pw-play`, Piper `--cuda`) are not used on this Mac.

## Credits

- **Design and product:** Philip Santiago (the plumbing).
- **Implementation:** Grok (xAI) (the plumber).
