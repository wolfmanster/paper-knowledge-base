"""Tests for `models.py` — device selection and encoder loading."""

from __future__ import annotations

import sys
import types
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import models as models_module


def test_query_prefers_cuda_when_available(monkeypatch):
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: True),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False)
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert models_module._preferred_model_device() == "cuda"


def test_query_falls_back_to_cpu_without_gpu(monkeypatch):
    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        backends=types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: False)
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    assert models_module._preferred_model_device() == "cpu"


def test_query_model_loaders_receive_preferred_device(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    class FakeSentenceTransformer:
        def __init__(self, model_name, *, device):
            calls.append(("bi", str(model_name), device))

    class FakeCrossEncoder:
        def __init__(self, model_name, *, device):
            calls.append(("cross", str(model_name), device))

    monkeypatch.setattr(models_module, "_preferred_model_device", lambda: "cuda")
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(
            SentenceTransformer=FakeSentenceTransformer,
            CrossEncoder=FakeCrossEncoder,
        ),
    )

    models_module.load_bi_encoder()
    models_module.load_cross_encoder()

    assert calls[0][0::2] == ("bi", "cuda")
    assert calls[1] == (
        "cross",
        "cross-encoder/ms-marco-MiniLM-L-6-v2",
        "cuda",
    )
