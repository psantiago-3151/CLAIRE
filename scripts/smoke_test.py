#!/usr/bin/env python3
"""Offline smoke test: imports, config, wake-word, phrases, HTTP UI. No mic, no Ollama generate."""
import re
import socket
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

    check(comms.CLI_INTERACTIVE is False, "CLI off when imported by the web UI")
    check(comms.cli_running is False, "CLI starts in Standby")
    check(comms.RUN_KEY == "space", f"run key {comms.RUN_KEY}")
    check(
        comms.cli_action_for_key("space", running=False) == "start",
        "space starts when Standby",
    )
    check(
        comms.cli_action_for_key("space", running=True) == "stop",
        "space stops when running",
    )
    check(
        comms.cli_action_for_key("esc", running=True) == "shutdown",
        "esc is Shut down while running",
    )
    check(
        comms.cli_action_for_key("esc", running=False) == "shutdown",
        "esc is Shut down in Standby",
    )
    check(
        comms.cli_action_for_key("f5", running=False) == "ignore",
        "no reload key",
    )
    check(
        comms.cli_action_for_key("tab", running=False) == "standby",
        "tab does not record in Standby",
    )
    check(
        comms.cli_action_for_key("tab", running=True) == "record",
        "tab records when running",
    )
    check(
        comms.cli_action_for_key("f12", running=True) == "interrupt",
        "f12 interrupts when running",
    )
    import inspect
    quit_src = inspect.getsource(comms.process_utterance)
    check("stop_requested" not in quit_src, "spoken quit does not exit the CLI process")

    banner = comms.cli_banner_lines(ollama_ok=True)
    text = "\n".join(banner)
    check(banner[0] == "", "banner starts with whitespace")
    check(banner[1].startswith("CLAIRE"), f"banner wordmark {banner[1]!r}")
    check(f"v{ver}" in text, "banner version")
    check("Say Claire to talk" in text, "banner is converse-first")
    check("claire.conf" in text, "banner shows claire.conf")
    check("llm_model" in text, "banner lists llm_model")
    check("wake_word" in text, "banner lists wake_word")
    check("Ollama" not in text or "not running" not in text, "no config table when ollama is up")
    check("── conversation ──" in text, "conversation header")
    check(text.strip().endswith("Standby"), "banner ends in Standby")
    check(" key bindings " in text, "CLI banner uses key bindings rule")
    check("ESC [shut down]" in text, "CLI banner includes shut down")
    check("Reload" not in text, "CLI banner has no Reload")
    block = comms.cli_key_block(running=False, recording=False)
    check(block[0] == "", f"whitespace before keys {block!r}")
    check(len(block) == 3, f"key menu is blank + 2 lines {block!r}")
    check(block[1].startswith("-") and "key bindings" in block[1], f"wide rule {block[1]!r}")
    check(len(block[1]) == len(block[2]), f"rule width {len(block[1])} matches keys {len(block[2])}")
    run_block = comms.cli_key_block(running=True, recording=False)
    check(
        len(run_block[1]) == len(run_block[2]),
        f"running rule {len(run_block[1])} matches keys {len(run_block[2])}",
    )
    standby_keys = comms.cli_key_hint(running=False, recording=False)
    check(
        standby_keys == "SPACE [start] ESC [shut down]",
        f"standby keys {standby_keys}",
    )
    check("[record]" not in standby_keys, "Record hidden in Standby")
    run_keys = comms.cli_key_hint(running=True, recording=False)
    check(
        run_keys == "TAB [record] F12 [interrupt] SPACE [stop] ESC [shut down]",
        f"running keys {run_keys}",
    )
    rec_keys = comms.cli_key_hint(running=True, recording=True)
    check("TAB [stop recording]" in rec_keys, f"recording keys {rec_keys}")
    check(comms._print_banner_image() is False, "no banner image yet")
    down = "\n".join(comms.cli_banner_lines(ollama_ok=False))
    check("Error: Ollama is not running" in down, "ollama-down is an error on the banner")
    check("ollama serve" in down, "hint when Ollama is down")
    check(
        comms.ollama_error_text() == comms.OLLAMA_DOWN_ERROR,
        "ollama error text",
    )

    warn = comms.format_startup_warnings(["whisper-cli not found: bin/whisper-cli"])
    wtext = "\n".join(warn)
    check("orderly start-up" in wtext, "startup warning header")
    check("whisper-cli" in wtext, "startup warning lists missing piece")
    check(comms.format_startup_warnings([]) == [], "no warnings when complete")
    host, port = comms.ui_host_port()
    check(port == 8742, f"default UI port {port}")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    probe.bind(("127.0.0.1", 0))
    probe.listen(1)
    busy = probe.getsockname()[1]
    check(comms.ui_port_in_use("127.0.0.1", busy), "detects an open UI port")
    probe.close()
    check(not comms.ui_port_in_use("127.0.0.1", busy), "free port is not in use")

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
    check(body.get("run_key") == comms.RUN_KEY, "status includes run_key")

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
