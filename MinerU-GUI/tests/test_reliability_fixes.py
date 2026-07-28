"""Regression tests for API output isolation and GUI worker cleanup."""

from __future__ import annotations

import queue
import threading
from pathlib import Path

import mineru_api
from gui import worker
from gui.widgets.params_panel import ParamsPanel


class Variable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def __init__(self):
        self.state = None

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]


def test_backend_device_controls_follow_backend_requirements():
    panel = type("Panel", (), {})()
    panel.backend_var = Variable("pipeline")
    panel.device_var = Variable("cpu")
    panel.vlm_describe_var = Variable(True)
    panel.device_combo = Widget()
    panel.vlm_describe_cb = Widget()

    ParamsPanel._on_backend_change(panel)
    assert panel.device_combo.state == "normal"
    assert panel.device_var.get() == "cpu"
    assert panel.vlm_describe_cb.state == "disabled"

    panel.backend_var.set("hybrid-auto-engine")
    ParamsPanel._on_backend_change(panel)
    assert panel.device_combo.state == "disabled"
    assert panel.device_var.get() == "gpu"
    assert panel.vlm_describe_cb.state == "normal"


def test_batch_convert_isolates_custom_output_directories(tmp_path, monkeypatch):
    output_dirs: list[Path] = []

    def fake_run_core(**kwargs):
        output_dir = Path(kwargs["output_dir_str"])
        output_dirs.append(output_dir)
        return True, output_dir / "result.md", output_dir, None

    monkeypatch.setattr(mineru_api, "run_core", fake_run_core)

    mineru_api.batch_convert(
        [tmp_path / "first" / "paper.pdf", tmp_path / "second" / "paper.pdf"],
        output_dir=tmp_path / "output",
    )

    assert output_dirs == [tmp_path / "output" / "paper", tmp_path / "output" / "paper-2"]


def test_worker_sets_done_event_when_conversion_raises(monkeypatch):
    log_queue: queue.Queue = queue.Queue()
    done_event = threading.Event()
    worker.init_worker(log_queue, done_event)
    monkeypatch.setattr(
        worker,
        "run_core",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("post-process failed")),
    )

    worker._run_batch_conversion(
        ["paper.pdf"],
        "pipeline",
        "en",
        "auto",
        0,
        "cpu",
    )

    assert done_event.is_set()
    assert "post-process failed" in "".join(list(log_queue.queue))
