#!/usr/bin/env python3
"""
LOCAL VOICE AI – FINAL VERSION (Wake word anywhere, forgiving detection)
* Detects wake word anywhere in full transcript
* Raw → sox → clean WAV
* Ollama + Piper TTS
* Tab toggle, F12 interrupt, Esc safe exit
* Reset session with "scratch that, scratch that" (twice, anywhere)
* Full long-term memory – loads ALL history on startup, uses everything
"""
import subprocess
import ollama
import os
import time
import json
import glob
import re
import signal
from pathlib import Path
from datetime import datetime
from sshkeyboard import listen_keyboard, stop_listening

# === CONFIG ===
LLM_MODEL = "llama3.2:3b-instruct-q8_0"
VOICE_MODEL = os.path.expanduser(
    "/mnt/workstation/ai-repos/voice-ai/piper-voices/en_US-libritts_r-medium/en_US-libritts_r-medium.onnx"
)
WHISPER_PATH = os.path.expanduser("/mnt/workstation/ai-repos/voice-ai/whisper.cpp/build/bin")
WHISPER_BIN = os.path.join(WHISPER_PATH, "whisper-cli")
MODEL_PATH = os.path.expanduser("/mnt/workstation/ai-repos/voice-ai/whisper.cpp/models/ggml-base.en.bin")
TMP_RAW = "/tmp/voice_ai_raw.raw"
TMP_WAV = "/mnt/workstation/ai-repos/voice-ai/temp/input.wav"
TMP_TTS = "/mnt/workstation/ai-repos/voice-ai/temp/response.wav"

AI_NAME = "friday"  # ← Your wake word
WAKE_PREFIXES = ["hey", "ok", "okay", "please", "yo", "hi", "hello"]

MEMORY_DIR = os.path.expanduser("/mnt/workstation/ai-repos/voice-ai/.voice_ai_memory")
Path(MEMORY_DIR).mkdir(parents=True, exist_ok=True)
CURRENT_FILE = os.path.join(MEMORY_DIR, "current.json")

# === GLOBALS ===
recording = False
speaking = False
rec_process = None
speak_process = None
stop_requested = False

# ----------------------------------------------------------------------
# MICROPHONE
# ----------------------------------------------------------------------
def detect_microphone() -> str:
    try:
        result = subprocess.run(["arecord", "-l"], capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            if "USB" in line.upper():
                match = re.search(r"card (\d+).*device (\d+)", line)
                if match:
                    card, dev = match.groups()
                    return f"plughw:{card},{dev}"
    except:
        pass
    return "plughw:0,0"

MIC = detect_microphone()

# ----------------------------------------------------------------------
# MEMORY
# ----------------------------------------------------------------------
def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def load_all_history() -> list:
    history = []
    for f in sorted(glob.glob(os.path.join(MEMORY_DIR, "*.json"))):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                if isinstance(data, list):
                    history.extend(data)
        except:
            pass
    return history

def rename_current_and_start_new():
    if os.path.exists(CURRENT_FILE):
        new_name = os.path.join(MEMORY_DIR, f"{timestamp()}_session.json")
        os.rename(CURRENT_FILE, new_name)
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
    except:
        with open(CURRENT_FILE, "w", encoding="utf-8") as fp:
            json.dump([turn], fp, ensure_ascii=False, indent=2)

memory = load_all_history()
print(f"Loaded full conversation history: {len(memory)} turns total")

# ----------------------------------------------------------------------
# WAKE WORD – FORGIVING, ANYWHERE IN FULL TRANSCRIPT
# ----------------------------------------------------------------------
def is_addressed(user_text: str) -> tuple[bool, str]:
    if not user_text:
        return False, ""

    lower_text = user_text.lower()

    if AI_NAME.lower() in lower_text:
        # Remove wake word and prefixes
        clean = re.sub(rf'\b{AI_NAME}\b[.,!?]*\s*', ' ', user_text, flags=re.IGNORECASE)
        for prefix in WAKE_PREFIXES:
            clean = re.sub(rf'\b{prefix}\b[.,!?]*\s+', ' ', clean, flags=re.IGNORECASE)
        clean = clean.strip()
        if clean:
            clean = clean[0].upper() + clean[1:]
        else:
            clean = "Yes?"
        return True, clean

    return False, user_text

# ----------------------------------------------------------------------
# RESET SESSION – "scratch that, scratch that" (twice, anywhere)
# ----------------------------------------------------------------------
def is_double_scratch_that(user_text: str) -> bool:
    if not user_text:
        return False
    lower_text = user_text.lower()
    # Find all occurrences of "scratch that"
    matches = list(re.finditer(r'\bscratch that\b', lower_text))
    if len(matches) >= 2:
        # Use the last two occurrences to handle cases with more than two
        last_two = matches[-2:]
        if last_two[0].start() < last_two[1].start():
            return True
    return False

# ----------------------------------------------------------------------
# RECORDING – RAW + SOX
# ----------------------------------------------------------------------
def start_recording():
    global recording, rec_process
    print("\n🎤 RECORDING... speak now! (press Tab to stop)")
    for f in [TMP_RAW, TMP_WAV]:
        if os.path.exists(f):
            os.remove(f)
    recording = True
    cmd = [
        "arecord",
        "-D", MIC,
        "-f", "S16_LE",
        "-r", "16000",
        "-c", "1",
        "-t", "raw",
        TMP_RAW
    ]
    rec_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def stop_recording():
    global recording, rec_process
    print("🎤 Stopping recording...")
    if recording and rec_process:
        rec_process.send_signal(signal.SIGINT)
        try:
            rec_process.wait(timeout=5)
        except:
            rec_process.terminate()
        recording = False
    time.sleep(0.3)

def convert_to_wav():
    if not os.path.exists(TMP_RAW) or os.path.getsize(TMP_RAW) < 1000:
        print("No audio captured")
        return False
    print("Converting raw to WAV with sox...")
    cmd = [
        "sox",
        "-t", "raw",
        "-r", "16000",
        "-e", "signed",
        "-b", "16",
        "-c", "1",
        TMP_RAW,
        TMP_WAV
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"sox error: {result.stderr}")
        return False
    return True

# ----------------------------------------------------------------------
# TRANSCRIBE – FULL TRANSCRIPT FROM ALL SEGMENTS
# ----------------------------------------------------------------------
def transcribe() -> str:
    if not convert_to_wav():
        return ""

    print("Running Whisper transcription...")
    cmd = [WHISPER_BIN, "-m", MODEL_PATH, "-f", TMP_WAV, "-t", "4"]
    result = subprocess.run(cmd, capture_output=True, text=True)

    output = result.stdout.strip()
    print("Whisper output:")
    print(output or "[no output]")

    full_transcript = ""
    for line in output.splitlines():
        if " --> " in line:
            parts = line.split("] ", 1)
            if len(parts) > 1:
                text = parts[1].strip()
                if text:
                    full_transcript += " " + text

    full_transcript = full_transcript.strip()
    print(f"You said: \"{full_transcript or '[nothing]'}\"")
    return full_transcript

# ----------------------------------------------------------------------
# SPEAK
# ----------------------------------------------------------------------
def speak(text: str):
    global speaking, speak_process
    if not text.strip():
        return
    print(f"AI: {text}")
    safe_text = text.replace("'", r"'\''")
    cmd = f"echo '{safe_text}' | piper --model {VOICE_MODEL} --output_file {TMP_TTS} --cuda"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Piper error: {result.stderr}")
        return
    speaking = True
    speak_process = subprocess.Popen(["pw-play", TMP_TTS])
    speak_process.wait()
    speaking = False

# ----------------------------------------------------------------------
# KEYBOARD
# ----------------------------------------------------------------------
def on_key_press(key):
    global speaking, speak_process, stop_requested
    if key == "tab":
        if recording:
            stop_recording()
            user_text = transcribe()
            if not user_text:
                return

            if any(w in user_text.lower() for w in ["exit", "goodbye", "shut down"]):
                speak("Goodbye!")
                stop_requested = True
                stop_listening()
                return

            addressed, clean = is_addressed(user_text)
            if not addressed:
                print(f"(Ignored – say '{AI_NAME.capitalize()}' to activate)")
                return

            speak("Yes?")

            # Check for double "scratch that" to start new session
            if is_double_scratch_that(user_text):
                rename_current_and_start_new()
                memory.clear()
                speak("New session started.")
                return

            # Use the FULL history — no limits, best possible recall
            system_prompt = "You are Friday, a helpful, friendly, and highly intelligent AI assistant. "
            system_prompt += "You have perfect recall of our entire conversation history, no matter how long it is. "
            system_prompt += "Always stay in character and continue the conversation naturally."

            prompt = system_prompt + "\n\nConversation history:\n"
            for turn in memory:
                prompt += f"User: {turn['user']}\n"
                prompt += f"Friday: {turn['ai']}\n"

            prompt += f"User: {clean}\nFriday:"

            response = ollama.generate(model=LLM_MODEL, prompt=prompt)["response"]

            turn = {"user": user_text, "ai": response}
            memory.append(turn)
            save_turn(turn)

            speak(response)
        else:
            start_recording()

    elif key == "f12":
        if speaking and speak_process:
            print("\n🛑 Speech interrupted (F12)")
            speak_process.terminate()
            speaking = False

    elif key == "esc":
        if not speaking and not recording:
            print("\nGoodbye!")
            stop_requested = True
            stop_listening()

# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
def main():
    print("\n" + "="*60)
    print("LOCAL VOICE AI – FINAL WORKING")
    print("="*60)
    print(f"   Wake word: {AI_NAME.capitalize()} (detected anywhere)")
    print("   Say 'scratch that, scratch that' (twice) to start a new session")
    print("   Tab: start/stop recording")
    print("   F12: interrupt speech")
    print("   Esc: exit (only when idle)\n")

    print("👂 Waiting for you to press TAB...")

    try:
        listen_keyboard(on_press=on_key_press)
    except KeyboardInterrupt:
        print("\nGoodbye!")

if __name__ == "__main__":
    main()
