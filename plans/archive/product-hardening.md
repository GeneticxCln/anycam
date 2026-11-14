# Product hardening plan (lessons from the omabroadcast comparison)

Written 2026-11-14 from a side-by-side read of AnyCam 1.4.0 (`master` at `1fed2e6`) and
[28allday/omabroadcast](https://github.com/28allday/omabroadcast) 0.1.0 (`827031a`).
Status: **done 2026-11-14** (approved with all recommended answers). See "Execution record" at the end.

AnyCam is ahead on engineering discipline: 1,251 tests, two CI jobs, defect lint, atomic
owner-only saves, log redaction and a regression log. omabroadcast has no tests, no CI and a
single squashed commit, so none of its process is copied. What it does better is **what happens
to the user when something goes wrong**: secrets in the keyring, crash detection with safe mode,
repair, out-of-process encoder probing, a persistent log and a remote-control command line.
This plan brings those ideas over without adding new feature breadth. Ideas only: no
omabroadcast code is copied, because it is C++/Qt Quick and AnyCam is Python/GStreamer.

## Findings this plan answers

| # | Finding | Evidence | Phase |
| --- | --- | --- | --- |
| F1 | The stream key is stored in plain text in the project file and its `.bak` | `rtmp_url` and `rtmp_extra_urls` in `src/anycam/model.py:486-496`; written by `Project.save` (`model.py:635`), which keeps the file at 0600 but still holds the key | 1 |
| F2 | No crash detection, safe mode or recovery choice after an unclean exit | the flock in `src/anycam/ui/instance.py` stops a second copy but records nothing; `run_gui` ends in `os._exit` (`gui_many.py:1159`, `gui.py:80`), so nothing runs on exit unless it is called explicitly | 2 |
| F3 | No persistent log: diagnostics disappear with the process, which is exactly when they are needed | `log_event` prints to stderr (`ui/engine_control.py:346`); the Diagnostics tab is in memory only (`gui_many.py:457`); `faulthandler` is only bound to SIGUSR1 (`ui/instance.py:126`) | 2 |
| F4 | A project that does not load falls back to defaults without offering the last good backup | `_load_saved_project` copies the file to `.corrupt` and returns `default_project()` (`ui/project_io.py:27-53`) | 2 |
| F5 | Encoder probing runs inside the app, and NVENC is never test-encoded | `_va_encodes` runs a 5-frame encode in-process with a 5 s wait (`engine/encoders.py:98`); `_probe_nvenc` only runs `nvidia-smi` (`encoders.py:81`). A driver crash takes the app down; a broken NVENC is found only when recording starts | 3 |
| F6 | Too much breadth too fast: Phases 0-9 landed on 11-12 Nov, and almost every merge was followed by a fix commit | Phase 2 (1/n) +8,181 lines in 42 files; Phase 1 +4,649/−3,983; Phase 2 +1,891 | 0 |
| F7 | Test files are named after audits and phases, not behaviour | 18 files, 12,353 lines: `test_audit_fixes_2026_11_07.py`, `_07b`, `_11_11`, `test_audit_regressions.py`, `test_review_fixes_2026_11_08.py`, `test_effect_fixes_2026_11_08.py`, `test_housekeeping_2026_11_12.py`, `test_engine_fixes.py`, `test_gui_state_fixes.py`, `test_cleanups.py`, `test_phase2_foundations.py` … `test_phase9_packaging.py` | 4 |
| F8 | Internal plan documents outnumber user documentation; `PLAN.md` is marked superseded but still at the root | `PLAN.md` line 1; `TODO.md:208` already asks for it to be replaced; there is no task-oriented user guide | 4 |
| F9 | Four packaging recipes, three of them never built | README "Known limitations → Packaging"; `release.yml` builds only the sdist and wheel | 4 |
| F10 | The single-instance socket ignores what it is sent, so the app cannot be driven from a terminal or a compositor key binding | `serve_instance_requests` raises the window on any connection (`ui/instance.py:103`); no `setSocketOptions` call, so it is not explicitly owner-only | 5 |
| F11 | No "check the recording I just made" step and no setup by purpose | `firstrun.py` checks the machine but does not ask what the user is making | 6 |
| F12 | Flat module layout: about 50 modules directly in `src/anycam/` | `ls src/anycam` | not in this plan (see "Not yet specified") |

Already covered, no work needed: crash-surviving recordings (fragmented MP4 with
`fragment_duration=500`, `engine/outputs/record_tap.py:232` and `recorder.py:127`, does what
omabroadcast's MKV default does), the single-instance lock, atomic owner-only saves, a real
VA-API test encode, and secret redaction in errors (`config.redact_text`).

## Phases

Order: guardrails first (cheap, and they shape every later PR), then the three hardening phases
in risk order, then hygiene, then the two small product additions. Phases 1, 2 and 3 touch
different files and can run in parallel. Phases 5 and 6 wait until 1-3 have landed.

### Phase 0: Guardrails
Verified when: `CONTRIBUTING.md` holds the PR-size, freeze and test-naming rules, and `TODO.md`
states the freeze.
Files to touch: `CONTRIBUTING.md`, `TODO.md`

#### Step 0.1: Write the PR-size, feature-freeze and test-naming rules (Size: XS)
- Acceptance criteria:
  - One feature or one fix per PR, at most about 400 changed lines of non-test, non-asset code (decision D10).
  - No new user-facing feature until Phases 1-3 are merged; Phases 5-6 are the only features in this plan.
  - New tests go in `tests/test_<feature>.py`; where a test came from (an audit, a review) goes in its docstring and `docs/REGRESSIONS.md`, never in the file name.
- Verification command: `git diff --stat master -- CONTRIBUTING.md TODO.md` (review only)
- Files likely touched: `CONTRIBUTING.md`, `TODO.md`
- Test seams: none (process documentation)
- Blocked by: none

### Phase 1: Secrets out of project files
Verified when: after a migration run, no stream key, SRT passphrase or camera password appears in
`~/.config/anycam/*.json*`, while streaming and network cameras still work from the keyring.
Files to touch: `src/anycam/credentials.py` (new), `src/anycam/model.py`, `src/anycam/streaming.py`,
`src/anycam/config.py`, `src/anycam/engine/outputs/stream.py`, `src/anycam/sources.py`,
`src/anycam/ui/output_panel.py`, `src/anycam/ui/project_io.py`, `src/anycam/firstrun.py`,
`.github/workflows/ci.yml`, `tests/test_credentials.py` (new), `tests/test_load_and_secrets.py`

#### Step 1.1: Credentials store over libsecret (Size: S)
- Acceptance criteria:
  - `credentials.py` exposes `available()`, `store(id, label, secret)`, `lookup(id)`, `remove(id)` over libsecret through `gi` (`Secret` 1), using one AnyCam schema; plus an in-memory store for tests.
  - A keyring that is locked, missing or does not answer returns a typed "unavailable" result, never an exception reaching the UI.
  - `anycam check` (the first-run checklist) reports whether a keyring answers.
- Verification command: `scripts/test.sh tests/test_credentials.py`
- Files likely touched: `src/anycam/credentials.py`, `src/anycam/firstrun.py`, `tests/test_credentials.py`
- Test seams: the module API with the in-memory store; the libsecret store in the CI job from step 1.4
- Blocked by: none
- Note: the module is not named `secrets`, so it cannot be confused with the standard library.

#### Step 1.2: Secret references in saved URLs (Size: M)
- Acceptance criteria:
  - The project saves a secret as a reference inside its URL (`${secret:<id>}`) and keeps the value in the keyring (decision D2). This covers `rtmp_url`, `rtmp_extra_urls`, SRT `passphrase`/`streamid` and the `user:password@` of network camera layers (decision D4).
  - References are resolved only when an output or source is built. URL validation (`streaming.validate_stream_url`) runs on the resolved URL, and every error message still goes through `config.redact_text`.
  - The stream-key field writes to the keyring and shows "Saved in your keyring". A reference with no stored value shows "Stream key missing: enter it" instead of failing at stream start.
- Verification command: `scripts/test.sh tests/test_credentials.py tests/test_load_and_secrets.py tests/test_phase5_outputs.py`
- Files likely touched: `model.py`, `streaming.py`, `config.py`, `engine/outputs/stream.py`, `sources.py`, `ui/output_panel.py`, `ui/project_io.py`
- Test seams: `Project.to_dict`/`from_dict` round trip with the in-memory store; the existing local `ffmpeg` RTMP and SRT receiver tests with a key resolved from the store
- Blocked by: 1.1

#### Step 1.3: Migrate existing projects and scrub the copies (Size: S)
- Acceptance criteria:
  - Loading a project with an inline secret moves it to the keyring and rewrites the project, its `.bak` and any `.corrupt` copy with references.
  - With no keyring, the secret stays inline in the 0600 file (today's behaviour) and Diagnostics says so once per launch (decision D3).
  - A migrated project still loads on a machine without the keyring entry and asks for the key (covers "Save project as" and sharing).
- Verification command: `scripts/test.sh tests/test_credentials.py tests/test_load_and_secrets.py`; then by hand: `grep -lE 'live_|passphrase=' ~/.config/anycam/*.json*` prints nothing
- Files likely touched: `model.py`, `ui/project_io.py`, `credentials.py`
- Test seams: load a fixture project with an inline key from a temporary `XDG_CONFIG_HOME`, then assert on the rewritten files
- Blocked by: 1.2

#### Step 1.4: libsecret in CI and a real-keyring job (Size: S)
- Acceptance criteria:
  - Both CI jobs install `gir1.2-secret-1`.
  - One job runs `tests/test_credentials.py` against a real Secret Service: `dbus-run-session` with `gnome-keyring-daemon --unlock` and a throwaway password.
  - Without a session bus the libsecret tests skip with a reason, like the hardware-gated tests.
- Verification command: CI green on the PR (`gh pr checks`)
- Files likely touched: `.github/workflows/ci.yml`, `tests/test_credentials.py`, `tests/conftest.py`
- Test seams: the CI job itself
- Blocked by: 1.1

### Phase 2: Crash safety
Verified when: killing the app with `kill -9` and with `kill -SEGV` mid-preview leads the next
launch to say so, point at the log and offer safe mode; safe mode starts with nothing risky
running and leaves the project file unchanged.
Files to touch: `src/anycam/applog.py` (new), `src/anycam/crashguard.py` (new),
`src/anycam/ui/instance.py`, `src/anycam/ui/engine_control.py`, `src/anycam/gui_many.py`,
`src/anycam/gui.py`, `src/anycam/__main__.py`, `src/anycam/ui/project_io.py`, `src/anycam/config.py`,
`tests/test_applog.py`, `tests/test_crash_guard.py`, `tests/test_safe_mode.py` (new)

#### Step 2.1: Persistent, redacted application log (Size: S)
- Acceptance criteria:
  - Every `[diag]`, `[guard]`, `[phone]` and `[preview]` line also goes to `state_dir()/anycam.log`, redacted with `config.redact_text`, with timestamps, rotated at 512 KiB with one old file kept.
  - `faulthandler.enable` writes to the same log, so a native crash in GStreamer or a driver leaves every thread's Python stack.
  - The Diagnostics tab gets "Open log folder" and "Copy log".
- Verification command: `scripts/test.sh tests/test_applog.py`; by hand: `kill -SEGV <pid>`, then `tail ~/.local/state/anycam/anycam.log` shows "Fatal Python error"
- Files likely touched: `applog.py`, `ui/engine_control.py`, `ui/instance.py`, `gui_many.py`, `gui.py`
- Test seams: `applog` with a temporary `XDG_STATE_HOME`; a child process that segfaults on purpose
- Blocked by: none

#### Step 2.2: Unclean-exit marker with a stage note (Size: S)
- Acceptance criteria:
  - After taking the instance lock, the app writes `state_dir()/running.json` (pid, boot id, version, start time, current stage). Because the lock guarantees one owner, a marker found by the new owner always means the last run did not exit cleanly.
  - Risky stages update the stage field: GPU preview start, canvas build, effect model load, vcam hold.
  - `shutdown()` removes the marker explicitly before `os._exit`, since `os._exit` runs no exit handlers. A hang that the user kills counts as unclean, which is intended.
- Verification command: `scripts/test.sh tests/test_crash_guard.py`
- Files likely touched: `crashguard.py`, `gui_many.py`, `gui.py`, `ui/engine_control.py`
- Test seams: `crashguard` with a temporary state directory; one offscreen GUI test that launches, quits and finds no marker
- Blocked by: 2.1 (the crash message links to the log)

#### Step 2.3: Safe mode (Size: M)
- Acceptance criteria:
  - `anycam gui --safe-mode`, and a dialog after an unclean exit ("The last run stopped during: GPU preview start. Start in safe mode?"), start with the CPU canvas and image preview (the existing `ANYCAM_CANVAS=cpu` and `ANYCAM_PREVIEW=image` paths), no vcam hold, no preview autostart, no tray or portal shortcuts, no phone link and no AI effects.
  - Safe mode never writes its overrides into the project: autosave stores what the user changes, never the safe-mode switches.
  - A banner says safe mode is on and how to leave it (restart normally).
- Verification command: `scripts/test.sh tests/test_safe_mode.py tests/test_gui_many.py`
- Files likely touched: `__main__.py`, `gui_many.py`, `gui.py`, `ui/engine_control.py`, `ui/project_io.py`
- Test seams: offscreen `ManyCamWindow` started with the safe flag: assert nothing is held or started, then save and diff the project file
- Blocked by: 2.2

#### Step 2.4: Offer the last good backup when the project will not load (Size: S)
- Acceptance criteria:
  - When the project does not load and `.bak` does, the app offers "Restore the last good copy", instead of silently starting from defaults.
  - The unreadable file is moved into `config_dir()/set-aside/<date-time>/`, never deleted. This replaces today's `.corrupt` sibling.
  - `anycam repair` checks the project, `hotkeys.json` and `phones.json` from the command line, and sets aside the files that do not read.
- Verification command: `scripts/test.sh tests/test_load_and_secrets.py tests/test_crash_guard.py`
- Files likely touched: `ui/project_io.py`, `model.py`, `__main__.py`, `crashguard.py` (or a small `repair.py`)
- Test seams: fixture projects (truncated, not an object) in a temporary `XDG_CONFIG_HOME`
- Blocked by: none

### Phase 3: Encoder probing out of process
Verified when: a probe child that segfaults or hangs leaves the app running, records an x264
fallback with a reason, and NVENC is offered only after a real test encode.
Files to touch: `src/anycam/engine/encoders.py`, `src/anycam/pipeline.py`, `src/anycam/__main__.py`,
`src/anycam/ui/output_panel.py`, `tests/test_encoders.py` (new)

#### Step 3.1: `anycam encoders --json`, a child process that test-encodes (Size: S)
- Acceptance criteria:
  - The subcommand runs a 5-frame test encode for every candidate (`nvh264enc`/`nvh265enc`/`nvav1enc`, `vah264enc`/`vah264lpenc` and the others in `VA_FACTORIES`, `x264enc`) and prints one JSON object with a reason for each failure.
  - NVENC is judged by a real encode, not by `nvidia-smi`.
  - The caller runs it with a total timeout (10 s, decision D7); a crash or timeout in the child marks the encoders it had not finished as "probe crashed".
- Verification command: `scripts/test.sh tests/test_encoders.py`
- Files likely touched: `engine/encoders.py`, `__main__.py`, `tests/test_encoders.py`
- Test seams: the subcommand run as a subprocess with `videotestsrc`; a fake child that kills itself with SIGSEGV
- Blocked by: none

#### Step 3.2: Cached probe results drive the encoder choice (Size: M)
- Acceptance criteria:
  - Results are cached in `cache_dir()/encoders.json`, keyed by AnyCam version, GStreamer version, kernel release and the GPU driver version (`/sys/module/nvidia/version`; Mesa through the `libva` driver files). They are re-probed off the UI thread only when the key changes.
  - `encoder_plan` reads the cache. `_va_encodes` and `_probe_nvenc` are removed from the app process (expand → migrate `pipeline.py` and the tests → contract).
  - With no cache yet, the first recording uses x264 and says why, rather than blocking on a probe.
- Verification command: `scripts/test.sh tests/test_encoders.py tests/test_pipeline.py tests/test_recorder.py`
- Files likely touched: `engine/encoders.py`, `pipeline.py`, tests that import `_probe_nvenc` (`test_audit_fixes_2026_11_11.py::test_the_nvenc_probe_is_remembered_for_a_minute`)
- Test seams: `encoder_plan` with a cache fixture; the existing recorder tests
- Blocked by: 3.1

#### Step 3.3: Offer only the encoders that work (Size: S)
- Acceptance criteria:
  - The encoder list in Settings → Output shows only encoders that passed the probe. Below it, one line names each missing one and why ("NVENC: driver 580 refused the session").
  - A "Test again" button re-runs the probe.
- Verification command: `scripts/test.sh tests/test_encoders.py tests/test_gui_many.py`
- Files likely touched: `ui/output_panel.py`
- Test seams: offscreen output panel with a cache fixture
- Blocked by: 3.2

### Phase 4: Test and documentation hygiene
Verified when: no test file name carries a date, an audit or a phase; the collected test names
are the same set before and after; the root holds only README, CHANGELOG, CONTRIBUTING, SECURITY,
NOTICE, LICENSE, PARITY and TODO; a user guide exists.
Files to touch: `tests/*`, `PLAN.md`, `docs/UPGRADE_PLAN.md`, `docs/MANUAL.md` (new), `README.md`, `TODO.md`, `packaging/*`

#### Steps 4.1a-d: Split the dated test files into feature files (Size: M each)
- Batches, one PR each:
  - 4.1a `test_audit_fixes_2026_11_07.py`, `_07b`, `_11_11`, `test_audit_regressions.py` (2,476 lines)
  - 4.1b `test_review_fixes_2026_11_08.py`, `test_effect_fixes_2026_11_08.py`, `test_housekeeping_2026_11_12.py`, `test_engine_fixes.py`, `test_gui_state_fixes.py`, `test_cleanups.py` (2,310 lines)
  - 4.1c `test_phase2_foundations.py` … `test_phase5_outputs.py` (2,166 lines)
  - 4.1d `test_phase6_worker.py` … `test_phase9_packaging.py` (1,121 lines)
- Acceptance criteria:
  - Each test function moves unchanged into `tests/test_<feature>.py` (existing feature files first: `test_recorder.py`, `test_phone.py`, `test_effects.py`, `test_model.py` and so on); helpers shared by several files move to `tests/helpers/`.
  - The set of collected test function names is identical before and after each batch.
  - Pass/skip counts match on `scripts/test.sh --no-hardware`.
- Verification command: `PYTHONPATH=src python3 -m pytest tests --collect-only -q -o addopts="" | sed 's/^[^:]*:://' | sort > /tmp/names.txt`, run before and after, then `diff` the two lists; then `scripts/test.sh --no-hardware`
- Test seams: the suite itself
- Blocked by: 3.2 for 4.1a (it moves the NVENC probe test that 3.2 rewrites); none for 4.1b-d

#### Step 4.2: Move the plans out of the root (Size: XS)
- Acceptance criteria:
  - `PLAN.md` → `plans/archive/2026-10-30-remediation.md`; `docs/UPGRADE_PLAN.md` → `plans/pending/native-linux-upgrade.md` (it still has open items); links updated (decision D8).
  - `TODO.md:208` ticked.
- Verification command: `grep -rn 'PLAN.md\|UPGRADE_PLAN' --include='*.md' --include='*.py' .` prints only the new paths
- Files likely touched: `PLAN.md`, `docs/UPGRADE_PLAN.md`, `TODO.md`, `README.md`, `CONTRIBUTING.md`
- Test seams: none
- Blocked by: none

#### Step 4.3: A task-oriented user guide (Size: M)
- Acceptance criteria:
  - `docs/MANUAL.md` covers: first recording; using the virtual camera in a browser or Discord; phone camera; streaming and where keys are kept; keyboard shortcuts; "if something goes wrong" (log, safe mode, restoring a backup, encoder list).
  - The README links it and keeps its own Quick start to one screen.
  - Every command in it was run on this machine; the ones that could not be run are marked, following the "measured numbers only" rule.
- Verification command: by hand: follow each MANUAL section on the reference machine
- Files likely touched: `docs/MANUAL.md`, `README.md`
- Test seams: none
- Blocked by: 1.3, 2.4, 3.3

#### Step 4.4: Packaging targets (Size: S)
- Acceptance criteria (with the recommended answer to decision D9):
  - Supported: `.deb` (already built in CI) and `PKGBUILD`, which gets a CI job in an `archlinux` container that runs `makepkg` and installs the result.
  - Flatpak and AppImage move to `packaging/unsupported/` with a README saying they have never been built.
  - The README's packaging limitation is updated to match.
- Verification command: CI green, including the new `makepkg` job
- Files likely touched: `packaging/*`, `.github/workflows/ci.yml`, `README.md`, `tests/test_phase9_packaging.py` (or its successor after 4.1d)
- Test seams: the CI job
- Blocked by: none

### Phase 5: Control the running app from outside
Verified when: `anycam ctl record` starts and stops a recording in the running window from a
terminal, `anycam ctl list` prints every action, and a different user cannot connect.
Files to touch: `src/anycam/ui/instance.py`, `src/anycam/ui/desktop.py`, `src/anycam/ui/phone.py`,
`src/anycam/__main__.py`, `tests/test_ctl.py` (new), `README.md`, `docs/MANUAL.md`

#### Step 5.1: An owner-only command socket with one action table (Size: M)
- Acceptance criteria:
  - The instance socket lives under `$XDG_RUNTIME_DIR`, uses `QLocalServer.UserAccessOption`, and speaks one JSON object per line: `{"action": "record"}` → `{"ok": true, "state": {...}}`. A bare `show` still raises the window.
  - Actions are the hotkey actions (`_hotkey_actions` in `ui/desktop.py`) plus `state` and `scene <name>`, dispatched on the UI thread. The phone remote's `phone_control` uses the same table, so the two cannot drift.
  - Unknown actions and malformed lines return an error object, never an exception.
- Verification command: `scripts/test.sh tests/test_ctl.py tests/test_phase8_remote.py`
- Files likely touched: `ui/instance.py`, `ui/desktop.py`, `ui/phone.py`
- Test seams: offscreen window plus a raw `AF_UNIX` client in the test
- Blocked by: 1.3, 2.3, 3.3 (the freeze from step 0.1)

#### Step 5.2: `anycam ctl` (Size: S)
- Acceptance criteria:
  - `anycam ctl <action> [argument]` and `anycam ctl list` use a plain `socket.AF_UNIX` client with no Qt import, so a compositor key binding gets an answer quickly.
  - Exit codes: 0 done, 1 refused, 2 AnyCam not running. The JSON answer goes to stdout.
  - README and MANUAL show a KDE and a Hyprland key-binding example.
- Verification command: `scripts/test.sh tests/test_ctl.py`; by hand: `anycam ctl record` twice while the window is open
- Files likely touched: `__main__.py`, `README.md`, `docs/MANUAL.md`
- Test seams: the CLI as a subprocess against the offscreen window
- Blocked by: 5.1

### Phase 6: Small product additions
Verified when: after a recording stops, the window shows whether the file closed cleanly and
can open it; the purpose-based setup is decided by D11.
Files to touch: `src/anycam/ui/output_panel.py` or `src/anycam/gui_many.py`, `src/anycam/engine/verify.py`,
`src/anycam/firstrun.py`, tests

#### Step 6.1: Check the last recording (Size: S)
- Acceptance criteria:
  - After Stop, a notice shows the `engine/verify.py` result (duration, frames, audio present, closed cleanly) with "Play" (`xdg-open`) and "Show in folder".
  - A recording that failed verification says what is wrong instead of a plain "saved".
- Verification command: `scripts/test.sh tests/test_recorder.py tests/test_gui_many.py`
- Files likely touched: `gui_many.py`, `engine/verify.py`
- Test seams: offscreen window with a short `videotestsrc` recording
- Blocked by: 1.3, 2.3, 3.3

#### Step 6.2: "What are you making?" setup (Size: M) — deferred unless D11 says otherwise
- Acceptance criteria:
  - The first-run check, and Settings → "Set up for…", offer video calls, streaming, recording and recording for editing. Each picks a canvas, fps, encoder (from the Phase 3 cache), bitrate and vcam on/off, and shows the choice before applying it.
  - Nothing is changed until the user presses Apply.
- Verification command: `scripts/test.sh tests/test_firstrun.py`
- Files likely touched: `firstrun.py`, `ui/general_panel.py`, `tests/test_firstrun.py` (new)
- Test seams: preset table unit tests; offscreen dialog
- Blocked by: 3.2

## Decision log

| # | Decision | Chosen option (recommended, awaiting approval) | Why |
| --- | --- | --- | --- |
| D1 | Keyring library | libsecret through `gi` (`Secret` 1 typelib) | No new PyPI dependency; same `gi` stack as GStreamer; present on this machine (libsecret 0.21.8) and on Ubuntu as `gir1.2-secret-1`. Rejected: `secretstorage`/`keyring` from PyPI (extra dependencies, and `keyring` picks backends implicitly) |
| D2 | How a saved project refers to a secret | `${secret:<id>}` inside the URL string | One mechanism for RTMP, extra destinations, SRT query strings and camera userinfo; no change to the shape of the project file; resolved only when an output is built. Rejected: separate key fields per destination (schema change for every URL-bearing field) |
| D3 | No keyring available | Keep the secret inline in the 0600 file and say so in Diagnostics | Same as today, so nothing breaks on minimal desktops. Rejected: refusing to save a key (breaks streaming on those machines) |
| D4 | Which secrets move | Stream keys, extra destinations, SRT passphrase/streamid, network camera passwords | All are in the same file with the same exposure |
| D5 | How an unclean exit is detected | A marker file plus the existing flock; no pid checks | The lock already proves a single owner, so a leftover marker can only mean an unclean exit |
| D6 | Safe-mode persistence | Never persisted | A crash must not leave the project changed |
| D7 | Encoder probe | A child process, 10 s total timeout, results cached by driver fingerprint | A driver crash or hang cannot reach the app; probing at most once per driver change |
| D8 | Where plans live | `plans/pending/` and `plans/archive/` | Clears the root; this file is the first one there |
| D9 | Packaging | `.deb` and `PKGBUILD` supported and built in CI; Flatpak and AppImage moved to `packaging/unsupported/` | Only built recipes are claims; the reference machine is CachyOS (Arch) |
| D10 | PR size | At most about 400 changed lines of non-test, non-asset code; one feature or fix per PR | Phase-sized commits (+8,181 lines) cannot be reviewed or bisected |
| D11 | Phase 6 scope | 6.1 now; 6.2 deferred to its own plan | 6.1 is small and closes a trust gap; 6.2 is new breadth |

None of these needs a `.ai/DECISIONS.md` entry yet. D2 is the closest (a file-format choice), but it
is reversible, because a reference can be resolved back into an inline value.

## Assumptions (correct any of these before approval)

1. Work happens on short branches off `master` in GeneticxCln/anycam, one PR per step, as before.
2. After migration (step 1.3), an older AnyCam cannot resolve `${secret:…}`, so a downgrade needs the stream key typed again. That is acceptable.
3. Python ≥ 3.11, the distribution's PySide6 and the two existing CI jobs stay as they are.
4. Real NVENC and VA-API crashes cannot be reproduced in CI; they are simulated with a child that kills itself. Real hardware is checked by hand on the reference machine (RTX 4060 Ti).
5. The version becomes 1.5.0 once Phases 1-3 are merged.

## Not yet specified

- Phase 2: should native GStreamer warnings (written to fd 2, not through Python) also be captured in the log by redirecting fd 2 to a pipe? Research this at step 2.1's pickup: it changes what a terminal launch shows.
- Phase 4: fog. Which feature file each moved test lands in is decided per batch while reading the tests.
- F12, module layout: a separate plan after this one. Precise question: group the effect modules (`effects`, `effect_thumbs`, `effect_worker`, `masks`, `frames`, `particles`, `procedural`, `packs`, `segment`, `face`, `inference`, `models`) into `anycam/effects/` with re-export shims (expand → migrate → contract), or leave the layout alone?
- Performance: fog. The open gate in `TODO.md:118` (≤100% CPU for 1080p60 with 3 layers + recording) is measured with `scripts/bench_canvas.py` before anything is planned. No rewrite is planned on a guess.

## Always / Never

- Always: one regression test per fix, recorded in `docs/REGRESSIONS.md`; atomic, owner-only writes for anything that can hold a secret; `config.redact_text` on every message that can quote a URL; `ruff check src` and both CI jobs green before merge; `TODO.md` and `CHANGELOG.md` updated in the same PR.
- Always: user files are set aside, never deleted.
- Never: rewrite in C++/Qt Quick/Vulkan, or copy omabroadcast code. Its ideas are used, not its source.
- Never: new user-facing features outside Phases 5-6 while this plan is open.
- Never: a secret in the log, the crash marker, the encoder cache or a test fixture committed to git.
- Never: safe mode writing its switches into the project.
- Never: commit `.idea/` or `.shiki/`; add files by explicit path.

## Progress

| Phase | Status |
| --- | --- |
| 0 Guardrails | Done |
| 1 Secrets out of project files | Done (CI real-keyring job not yet seen green) |
| 2 Crash safety | Done |
| 3 Encoder probing out of process | Done |
| 4 Test and documentation hygiene | Done, with the 4.1 deviation below |
| 5 Control from outside | Done |
| 6 Small product additions | 6.1 done; 6.2 deferred (D11) |

## Execution record

What shipped: every step of Phases 0-5 and step 6.1. Verification: the full suite on a snapshot of the
final tree (`pytest tests --no-hardware`) and `ruff check src`; new tests in `tests/test_credentials.py`,
`test_applog.py`, `test_crash_guard.py`, `test_encoders.py`, `test_ctl.py`, `test_recording_summary.py`,
`test_repo_hygiene.py`. The real encoder probe ran on the reference machine: NVENC H.264/H.265/AV1 and x264
passed, VA-API not installed. The real KWallet round trip ran once. `makepkg` built the PKGBUILD locally.

Deviations from the plan:

- **D2, where references resolve.** References are resolved when a project file is read and made when it is
  written (`Project.load`, `Project.save`, `save_scene_preset`), not when an output is built. The engine and
  UI keep seeing real URLs. Cost: a locked keyring may prompt for unlock at project open.
- **Secret ids** are random UUIDs remembered per run (`credentials._known_ids`), not derived from the secret.
  An entry whose stream key was later changed stays in the keyring (no pruning).
- **5.1, socket location.** The instance socket kept its existing name in the temp directory, now with
  `UserAccessOption`; it did not move to `$XDG_RUNTIME_DIR`, which would have changed single-instance
  semantics. `ctl.socket_path()` mirrors Qt's path rule.
- **2.4**, restoring the backup is offered in a dialog on a real desktop and automatic on Qt's offscreen
  platform. The unreadable file is moved to `set-aside/` instead of a `.corrupt` sibling (one existing test
  was updated for that).
- **3.2**, with no probe answer yet, the first recording uses x264 and the plan note says the check is not
  finished; the probe starts when the GUI starts. The old "NVENC probe is remembered for a minute" test was
  removed with the in-process probe.
- **4.1, no split.** The dated test files were renamed to descriptive names, not split into one file per
  feature: each file has its own fixtures and helpers (`isolated`, `make_project`, `app` differ between
  files), so merging them into feature files would have shadowed fixtures. All 1,294 collected test names are
  unchanged. `tests/test_repo_hygiene.py` stops dated names coming back. A real split stays open.
- **4.4**, the PKGBUILD CI job and the real-keyring CI job are `continue-on-error` until they have run green
  on GitHub.
- **Version** is not bumped; the changes sit under "Unreleased" in the CHANGELOG (assumption 5 awaits a release).
- **Open question answered** (Phase 2 fog): native text written straight to file descriptor 2 is not captured
  in the log; it still reaches the terminal. Stated in `applog.py`.
