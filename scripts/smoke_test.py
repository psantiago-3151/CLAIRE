#!/usr/bin/env python3
"""Offline smoke test: imports, config, wake-word, phrases, HTTP UI. No mic, no Ollama generate."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    import comms
    import web
    from fastapi.testclient import TestClient

    errors = []

    def check(ok, msg):
        if ok:
            print("OK ", msg)
        else:
            print("FAIL", msg)
            errors.append(msg)

    ver = comms.app_version()
    check(bool(re.fullmatch(r"\d+\.\d+\.\d+", ver)), f"app version {ver}")
    check("claire" in comms.AI_NAME or comms.AI_NAME, f"wake word loaded: {comms.AI_NAME}")
    check(comms.FILLER_ENTRIES, "thinking phrases loaded")
    check(comms.MISSING_WAKE_ENTRIES, "missing-wake phrases loaded")
    check(comms.WAKE_FUZZ_THRESHOLD >= 50, f"fuzz threshold {comms.WAKE_FUZZ_THRESHOLD}")

    comms.AI_NAME = "claire"
    hit, clean = comms.is_addressed("Claire what day is it")
    check(hit and "day" in clean.lower(), f"exact wake: {hit} {clean!r}")
    hit, _ = comms.is_addressed("claire what day is it")
    check(hit, "fuzzy/normalized wake")
    miss, _ = comms.is_addressed("what day is it")
    check(not miss, "no false wake")

    comms.QUIT_PHRASES = ["exit", "goodbye", "shut down"]
    check(comms._quit_requested("Claire goodbye"), "quit goodbye")
    check(comms._quit_requested("good bye"), "quit good bye")
    check(comms._quit_requested("shutdown"), "quit shutdown")
    check(comms._quit_requested("Claire, shut down."), "quit shut down")
    check(not comms._quit_requested("check one two three"), "no false quit")
    comms.WAKE_FUZZ_THRESHOLD = 50.0
    count_q = "Can you count? One to ten backwards in the opposite direction? Thank you."
    check(not comms._quit_requested(count_q), "count question is not quit at 50% wake fuzz")
    check(comms._quit_requested("Claire goodbye"), "quit still matches goodbye")
    check(comms._quit_requested("good bye"), "quit still matches good bye")
    check(comms._quit_requested("shutdown"), "quit still matches shutdown")

    spoken = comms.tts_speak_text("### Hello **world**")
    check("hash" not in spoken.lower() and "*" not in spoken, f"tts markup: {spoken!r}")

    phrase = comms.pick_phrase_entry(comms.FILLER_ENTRIES)
    check(bool(phrase), f"pick filler: {phrase!r}")

    fp = comms.disk_fingerprint()
    check(len(fp) > 10, "disk fingerprint")

    problems = comms.Comms().missing()
    check(isinstance(problems, list), f"missing() -> {problems}")

    client = TestClient(web.app)
    home = client.get("/")
    check(home.status_code == 200, f"GET / {home.status_code}")
    check(b"CLAIRE" in home.content, "HTML title/brand CLAIRE")
    check(b"Conversational Local Audio" in home.content, "HTML acronym")
    check(ver.encode() in home.content, f"HTML shows v{ver}")

    st = client.get("/api/status")
    check(st.status_code == 200, f"GET /api/status {st.status_code}")
    body = st.json()
    check("ollama" in body and "mic" in body, f"status keys {sorted(body)}")
    check(body.get("running") is False, "not running")
    check(body.get("version") == ver, f"status version {body.get('version')}")

    cfg = client.get("/api/config")
    check(cfg.status_code == 200, f"GET /api/config {cfg.status_code}")
    fields = [f["key"] for f in cfg.json().get("fields", [])]
    check("wake_word" in fields and "wake_fuzz_threshold" in fields, "config fields")
    check("thinking" in (cfg.json().get("phrases") or {}), "phrases in config")

    if errors:
        print(f"\n{len(errors)} failure(s)")
        return 1
    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
