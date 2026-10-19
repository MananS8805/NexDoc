"""Tests for the web app's engine layer (webapp/engine.py) and the adapter
changes that support it: the streaming event protocol, the content-hash
cache, the sample whitelist, and early diagram geometry.

None of these need the OCR engine: spreadsheet streaming and diagram
geometry detection are pure OpenCV/openpyxl, so this file stays fast and
runs even where torch/EasyOCR can't load.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

FIX = ROOT / "tests" / "fixtures"
BUDGET = FIX / "budget_test.xlsx"
ACTIVITY = FIX / "activity_diagram_clean.png"


def _events(filename: str, content: bytes) -> list[dict]:
    from webapp import engine

    return [json.loads(line) for line in engine.stream_analysis(filename, content)]


def _fresh_cache():
    from webapp import engine

    engine.CACHE._items.clear()
    engine.CACHE.hits = engine.CACHE.misses = 0
    return engine


# ----------------------------------------------------------------- adapters: on_region hook


def test_on_region_callback_sees_every_region_in_order_and_changes_nothing():
    from adapters.spreadsheet.adapter import build_artifact

    seen = []
    with_cb = build_artifact(BUDGET, on_region=lambda r: seen.append(r.id))
    without = build_artifact(BUDGET)
    assert seen == [r["id"] for r in without["regions"]], "callback must fire once per region, in artifact order"
    assert with_cb["regions"] == without["regions"] and with_cb["checks"] == without["checks"]


def test_router_forwards_on_region():
    from adapters.router import build_artifact_for

    seen = []
    kind, artifact = build_artifact_for(BUDGET, on_region=lambda r: seen.append(r.id))
    assert kind == "spreadsheet" and len(seen) == len(artifact["regions"]) == 54


# ----------------------------------------------------------------- streaming protocol


def test_stream_event_order_and_payloads_for_a_spreadsheet():
    engine = _fresh_cache()
    events = _events("budget_test.xlsx", BUDGET.read_bytes())
    names = [e["event"] for e in events]

    assert names[0] == "start" and names[-1] == "end"
    assert names[1] == "sniffed" and events[1]["kind"] == "spreadsheet"
    assert names[2] == "preview" and events[2]["images"] == [], "a spreadsheet has no bitmap preview"
    region_events = [e for e in events if e["event"] == "region"]
    assert len(region_events) == 54
    assert [e["index"] for e in region_events] == list(range(54))
    # every region precedes `done`, and `done` precedes `xray`
    assert names.index("done") > max(i for i, n in enumerate(names) if n == "region")
    assert names.index("xray") > names.index("done")

    done = next(e for e in events if e["event"] == "done")
    assert len(done["doc"]["regions"]) == 54
    assert done["timings"]["cached"] is False
    for key in ("sniffMs", "previewMs", "extractMs", "mapMs", "totalMs"):
        assert key in done["timings"]
    # streamed regions carry the same data as the final doc
    assert [e["region"]["id"] for e in region_events] == [r["id"] for r in done["doc"]["regions"]]


def test_xray_for_spreadsheet_reports_real_blank_formula_cells():
    _fresh_cache()
    events = _events("budget_test.xlsx", BUDGET.read_bytes())
    xray = next(e for e in events if e["event"] == "xray")["xray"]
    assert xray is not None
    stat = {s["label"]: s["nexdoc"] for s in xray["stats"]}
    assert stat["Typed regions"] == 54 and stat["Validation checks"] == 2 and stat["Relationships"] > 0
    joined = " ".join(xray["lost"])
    # budget_test.xlsx's formulas have no cached values, so the flat CSV blanks every one of them
    assert "formula cells come out blank" in joined
    assert "cross-sheet" in joined and "broken reference" in joined


def test_second_identical_upload_is_served_from_cache():
    engine = _fresh_cache()
    first = _events("budget_test.xlsx", BUDGET.read_bytes())
    second = _events("budget_test.xlsx", BUDGET.read_bytes())
    d1 = next(e for e in first if e["event"] == "done")
    d2 = next(e for e in second if e["event"] == "done")
    assert d1["timings"]["cached"] is False and d2["timings"]["cached"] is True
    assert [r["id"] for r in d1["doc"]["regions"]] == [r["id"] for r in d2["doc"]["regions"]]
    assert engine.CACHE.stats()["hits"] == 1
    assert any(e["event"] == "xray" for e in second), "the cached replay also replays the X-ray"


def test_stream_reports_empty_and_unsupported_files_as_clean_errors():
    _fresh_cache()
    empty = _events("empty.xlsx", b"")
    err = next(e for e in empty if e["event"] == "error")
    assert err["status"] == 400 and "empty" in err["message"].lower()
    assert empty[-1]["event"] == "end"

    junk = _events("notes.txt", b"just some plain text, not a supported artifact")
    err = next(e for e in junk if e["event"] == "error")
    assert err["status"] == 400 and "Supported" in err["message"]
    assert not any(e["event"] == "done" for e in junk)


# ----------------------------------------------------------------- cache + samples


def test_result_cache_is_a_bounded_lru():
    from webapp.engine import ResultCache

    c = ResultCache(maxsize=2)
    c.put("a", {"v": 1}); c.put("b", {"v": 2})
    assert c.get("a") == {"v": 1}          # touch a -> b is now least recently used
    c.put("c", {"v": 3})
    assert c.get("b") is None and c.get("a") and c.get("c")
    assert c.stats()["entries"] == 2


def test_sample_lookup_is_a_whitelist_not_a_path_builder():
    from webapp import engine

    ids = {s["id"] for s in engine.list_samples()}
    assert {"budget", "invoice", "flow", "uml"} <= ids
    assert engine.sample_path("budget") == BUDGET
    for evil in ("../README.md", "..\\README.md", "budget_test.xlsx", "", "../../etc/passwd"):
        assert engine.sample_path(evil) is None


def test_http_endpoints_wire_the_engine_without_starting_ocr_warmup():
    from fastapi.testclient import TestClient

    from webapp.main import app

    client = TestClient(app)  # not used as a context manager: lifespan (OCR warm-up) deliberately not started
    samples = client.get("/api/samples").json()["samples"]
    assert any(s["id"] == "budget" for s in samples)
    assert client.get("/api/samples/budget").status_code == 200
    assert client.get("/api/samples/nope").status_code == 404
    assert "ocr" in client.get("/api/engine").json()
    assert client.get("/static/js/app.js").headers["cache-control"] == "no-cache"

    _fresh_cache()
    resp = client.post("/api/analyze/stream", files={"file": ("budget_test.xlsx", BUDGET.read_bytes())})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(l) for l in resp.text.splitlines() if l.strip()]
    assert lines[0]["event"] == "start" and any(e["event"] == "done" for e in lines)


def test_oversized_upload_is_rejected_with_413(monkeypatch):
    from fastapi.testclient import TestClient

    from webapp import engine
    from webapp.main import app

    monkeypatch.setattr(engine, "MAX_UPLOAD_BYTES", 10)
    resp = TestClient(app).post("/api/analyze/stream", files={"file": ("big.xlsx", b"x" * 100)})
    assert resp.status_code == 413 and resp.json()["ok"] is False and "MB" in resp.json()["error"]


def test_concurrent_first_use_of_the_ocr_reader_loads_the_model_once(monkeypatch):
    """Regression: the server warms the OCR model in a background thread while a
    request thread can call get_reader() simultaneously. Unguarded, both built
    their own multi-hundred-MB reader (a scan that started mid-warm-up took 41s
    instead of ~15s). Uses a fake `easyocr` so no real model is loaded.
    """
    import threading
    import time
    import types

    from adapters.common import ocr

    built = []

    class FakeReader:
        def __init__(self, langs, **kw):
            built.append(tuple(langs))
            time.sleep(0.3)  # long enough that an unguarded second caller would also start building

    monkeypatch.setitem(sys.modules, "easyocr", types.SimpleNamespace(Reader=FakeReader))
    langs = ["zz-test"]
    ocr._READERS.pop(tuple(langs), None)
    ocr._STATUS.pop(tuple(langs), None)
    try:
        results = []
        threads = [threading.Thread(target=lambda: results.append(ocr.get_reader(langs))) for _ in range(4)]
        for th in threads: th.start()
        for th in threads: th.join()
        assert built == [("zz-test",)], f"reader was constructed {len(built)} times"
        assert len({id(r) for r in results}) == 1, "all threads must receive the same reader"
    finally:
        ocr._READERS.pop(tuple(langs), None)
        ocr._STATUS.pop(tuple(langs), None)


# ----------------------------------------------------------------- deployment: health, password gate, CPU budget


def test_healthz_is_open_and_reports_ocr_state_without_loading_it():
    from fastapi.testclient import TestClient

    from webapp.main import app

    body = TestClient(app).get("/healthz").json()
    assert body["ok"] is True and body["ocr"] in {"idle", "warming", "ready", "unavailable"}


def test_password_gate_is_off_by_default_and_enforced_when_configured(monkeypatch):
    import base64

    from fastapi.testclient import TestClient

    from webapp.main import app

    client = TestClient(app)
    monkeypatch.delenv("NEXDOC_PASSWORD", raising=False)
    assert client.get("/api/samples").status_code == 200, "local use stays open"

    monkeypatch.setenv("NEXDOC_PASSWORD", "s3cret")
    denied = client.get("/api/samples")
    assert denied.status_code == 401 and "Basic" in denied.headers["www-authenticate"]
    assert client.get("/").status_code == 401
    assert client.get("/healthz").status_code == 200, "platform health checks must not need credentials"

    def basic(user, pw):
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

    assert client.get("/api/samples", headers=basic("anyone", "wrong")).status_code == 401
    assert client.get("/api/samples", headers=basic("anyone", "s3cret")).status_code == 200
    assert client.get("/api/samples", headers={"Authorization": "Basic !!notbase64!!"}).status_code == 401
    # the upload endpoint is protected too — that's the expensive/abusable one
    assert client.post("/api/analyze/stream", files={"file": ("x.xlsx", b"x")}).status_code == 401


def test_cpu_limit_detection_only_reports_a_real_constraint(monkeypatch):
    from webapp import engine

    monkeypatch.setenv("NEXDOC_TORCH_THREADS", "3")
    assert engine.detect_cpu_limit() == 3, "an explicit override always wins"
    monkeypatch.setenv("NEXDOC_TORCH_THREADS", "not-a-number")
    # no override: with no affinity/cgroup constraint it must return None (leave PyTorch's default alone)
    monkeypatch.setattr(engine.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(engine.os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3}, raising=False)
    monkeypatch.setattr(engine.Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError()))
    assert engine.detect_cpu_limit() is None
    # an affinity mask narrower than the host is a constraint
    monkeypatch.setattr(engine.os, "sched_getaffinity", lambda _pid: {0, 1}, raising=False)
    assert engine.detect_cpu_limit() == 2

    # cgroup v2 quota of 2.5 CPUs rounds up to 3 usable threads
    def fake_read(self, *a, **k):
        if str(self).replace("\\", "/").endswith("cgroup/cpu.max"):
            return "250000 100000"
        raise OSError()

    monkeypatch.setattr(engine.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(engine.os, "sched_getaffinity", lambda _pid: set(range(16)), raising=False)
    monkeypatch.setattr(engine.Path, "read_text", fake_read)
    assert engine.detect_cpu_limit() == 3


# ----------------------------------------------------------------- diagram: geometry


def test_all_diagram_boxes_cover_nodes_edges_and_swimlanes_before_any_ocr():
    from build_prototype import compute_all_diagram_boxes

    boxes = compute_all_diagram_boxes(ACTIVITY)
    assert {f"node-{i}" for i in range(1, 9)} <= set(boxes)
    assert {f"edge-{i}" for i in range(1, 9)} <= set(boxes)
    assert "swimlane-1" in boxes, "the swimlane divider gets a real position, not an ordinal fallback"
    for b in boxes.values():
        assert 0 <= b["left"] <= 100 and 0 <= b["top"] <= 100 and b["width"] > 0 and b["height"] > 0


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
