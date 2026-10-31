"""The soak gates must see leaks and stalls, not buffer-pool steps."""
from anycam.soak import _memory_assessment, _resource_snapshot, _stall_assessment


def series(seconds=600, base=202.6, steps=(), rate=0.0, start_growth=0.0):
    """Per-second RSS samples with optional steps and a linear creep."""
    out = []
    for t in range(1, seconds + 1):
        value = base + start_growth * min(t, 30)
        value += sum(delta for at, delta in steps if t >= at)
        value += rate * (t / 60.0)
        out.append({"t": float(t), "rss_mib": round(value, 1)})
    return out


def test_pool_step_up_is_not_a_leak():
    """The measured 2026-10-31 shape: three 8 MiB frames, then flat."""
    samples = series(steps=((124, 8.0), (239, 7.9), (272, 7.9)))
    result = _memory_assessment(samples, warmup=120.0)
    assert result["slope_mib_per_min"] > 1.0        # the old gate failed here
    assert len(result["steps"]) == 3
    assert result["steps"][0]["delta_mib"] == 8.0
    assert result["settled_slope_mib_per_min"] < 0.1
    assert result["tail_growth_mib"] == 0.0
    assert result["ok"] is True


def test_linear_creep_still_fails():
    """A real leak of one 1080p frame per minute must not pass."""
    samples = series(rate=8.0)
    result = _memory_assessment(samples, warmup=60.0)
    assert result["settled_slope_mib_per_min"] > 7.0
    assert result["ok"] is False


def test_leak_that_starts_late_fails_on_the_tail():
    late = [{"t": float(t), "rss_mib": round(200.0 + max(0, t - 300) * 0.02, 1)}
            for t in range(1, 601)]
    result = _memory_assessment(late, warmup=60.0)
    assert result["settled_slope_mib_per_min"] > 0.5
    assert result["tail_growth_mib"] > 1.0
    assert result["ok"] is False


def test_too_few_samples_does_not_pass():
    result = _memory_assessment(series(seconds=4), warmup=0.0)
    assert result["ok"] is False
    assert result["steps"] == []


def test_stalled_frame_counter_fails_the_run():
    frames = 0
    samples = []
    for t in range(1, 31):
        if t > 10:
            frames = 30 * (t - 10)
        samples.append({"t": float(t), "frames": frames})
    result = _stall_assessment(samples, warmup=5.0, stall_seconds=2.0)
    assert result["ok"] is False
    assert result["stalls"][0]["from_s"] == 5.0
    assert result["longest_s"] >= 5.0


def test_healthy_frame_counter_passes():
    samples = [{"t": float(t), "frames": t * 30} for t in range(1, 31)]
    result = _stall_assessment(samples, warmup=5.0)
    assert result["ok"] is True
    assert result["stalls"] == []


def test_stall_at_the_end_is_recorded():
    samples = [{"t": float(t), "frames": 30 * t if t <= 20 else 600}
               for t in range(1, 31)]
    result = _stall_assessment(samples, warmup=5.0)
    assert result["ok"] is False
    assert result["stalls"][-1]["from_s"] == 20.0
    assert result["longest_s"] == 10.0


def test_too_few_stall_samples_does_not_pass():
    assert _stall_assessment([{"t": 1.0, "frames": 0}], warmup=0.0)["ok"] is False


def test_resource_snapshot_reports_filesystem_and_fds():
    snapshot = _resource_snapshot("/tmp/anycam-soak-test.mp4")
    assert {"fds", "tmp_used_mib", "tmp_free_mib", "tmp_fstype",
            "record_free_mib"} <= set(snapshot)
