# Contributing

CLAIRE is a **hobby** voice layer for local LLMs (web UI first). There is no support SLA.

- **Bugs and questions:** open a GitHub [issue](https://github.com/psantiago-3151/CLAIRE/issues).
- **Demo recording:** ask in an issue; it is sent on request, not published in the repo.
- **Pull requests:** welcome. Keep the web UI as the default control surface; do not turn the keyboard CLI into a second Admin. Match existing style. Run `python scripts/smoke_test.py` if you can.
- **Models:** do not commit `.onnx`, `.bin`, venv, or `vendor/`. You install Whisper, Piper, and Ollama yourself.

License: [MIT](LICENSE).
