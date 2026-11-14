# Contributing

## Set up

```bash
scripts/setup.sh                 # venv, Python packages, checks the GStreamer elements
anycam elements                  # lists missing GStreamer elements (also part of `anycam doctor`)
```

PyGObject and the GStreamer plugins come from your distribution, not from PyPI
(`python3-gi`, `gir1.2-gst-plugins-base-1.0`, `gstreamer1.0-plugins-{base,good,bad,ugly}`,
`gstreamer1.0-libav`, `gstreamer1.0-pipewire`, `v4l-utils`, `ffmpeg`). CI installs the same set on Ubuntu 24.04
(`.github/workflows/ci.yml`).

## Run the tests

```bash
scripts/test.sh                  # the whole suite, Qt offscreen
scripts/test.sh --no-hardware    # hides cameras, loopback, audio server and portal, like the CI runner
ruff check src                   # defect rules only (see pyproject.toml)
```

The tests use synthetic sources (`videotestsrc`, generated clips) and temporary `XDG_*` directories. They must
never open your real camera or your saved project.

## Conventions

- A bug fix comes with a regression test that fails without the fix. Record it in `docs/REGRESSIONS.md`.
- Measured numbers only: if something could not be run on the machine you have, say so in the docs instead
  of stating a result.
- GStreamer work stays off the Qt thread where it can block; Qt objects are touched from the Qt thread only.
- Every file the app writes under the config, state or cache directories is written atomically, and anything
  that can hold a secret (stream URLs, phone secrets) is owner-only.
- Keep `TODO.md` honest: tick items only with a test or a measurement behind them.
- One feature or one fix per pull request, at most about 400 changed lines of code (tests and assets not
  counted). A larger change is split into steps that each land green.
- Name a test file after the feature it covers (`tests/test_recorder.py`), never after an audit, a review or
  a phase. Where a test came from goes in its docstring and in `docs/REGRESSIONS.md`.
- Plans live in `plans/pending/` while open and move to `plans/archive/` with a short execution record.

## Releasing

Bump `version` in `pyproject.toml` and `VERSION` in `src/anycam/config.py`, update `CHANGELOG.md`, commit, then
`git tag vX.Y.Z && git push origin vX.Y.Z` (see the README, "Releases").
