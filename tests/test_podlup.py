import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
import podlup


def make_segments(n=30, dur=4.0):
    return [{"id": i, "start": i * dur, "end": i * dur + dur - 0.2, "text": f"جملة {i}"} for i in range(n)]


# ── التحقق من رد GPT ──
def test_validate_drops_invalid_and_keeps_valid():
    segs = make_segments()
    raw = [
        {"start_id": 2, "end_id": 12, "title": "ok", "caption": "c", "score": 9, "reason": "r"},
        {"start_id": 5, "end_id": 3},            # start > end
        {"start_id": 999, "end_id": 1000},       # غير موجود
        {"start_id": "x", "end_id": 4},          # ليس رقماً
        {"start_id": True, "end_id": 4},         # bool
        {"start_id": 1.5, "end_id": 4},          # كسر
        "junk", None, {"end_id": 4},
    ]
    out = podlup.validate_clips(raw, segs, count=5)
    assert len(out) == 1 and out[0]["title"] == "ok"
    assert (out[0]["start_id"], out[0]["end_id"]) == (2, 12)


def test_validate_non_list_returns_empty():
    assert podlup.validate_clips({"clips": 1}, make_segments()) == []
    assert podlup.validate_clips(None, make_segments()) == []


def test_validate_accepts_numeric_strings_and_missing_text():
    out = podlup.validate_clips([{"start_id": "1", "end_id": "9"}], make_segments())
    assert out and out[0]["title"] == "" and out[0]["score"] == 0


def test_validate_respects_count():
    raw = [{"start_id": i * 8, "end_id": i * 8 + 7} for i in range(4)]
    assert len(podlup.validate_clips(raw, make_segments(40), count=2)) == 2


# ── تمديد المقطع إلى 25 ثانية ──
def test_extend_reaches_min_duration():
    segs = make_segments()
    by = {s["id"]: s for s in segs}
    sid, eid = podlup.extend_clip(3, 4, by, sorted(by), 25)
    assert by[eid]["end"] - by[sid]["start"] >= 25
    assert sid == 3 and eid > 4  # يمدّ للأمام أولاً


def test_extend_falls_back_to_previous_at_end_of_episode():
    segs = make_segments(10)
    by = {s["id"]: s for s in segs}
    sid, eid = podlup.extend_clip(8, 9, by, sorted(by), 25)
    assert eid == 9 and sid < 8
    assert by[eid]["end"] - by[sid]["start"] >= 25


def test_extend_stops_when_episode_too_short():
    segs = make_segments(3)
    by = {s["id"]: s for s in segs}
    assert podlup.extend_clip(0, 1, by, sorted(by), 25) == (0, 2)


def test_parse_json_content_variants():
    assert podlup.parse_json_content('{"a":1}') == {"a": 1}
    assert podlup.parse_json_content('```json\n{"a":1}\n```') == {"a": 1}
    assert podlup.parse_json_content('هذا الرد: {"a":1} شكراً') == {"a": 1}
    with pytest.raises(Exception):
        podlup.parse_json_content("not json")


# ── تنظيف الجلسات ──
def test_cleanup_removes_only_old_podlup_dirs(tmp_path):
    old, fresh, other = tmp_path / "podlup_old1", tmp_path / "podlup_new1", tmp_path / "other"
    for d in (old, fresh, other):
        d.mkdir()
        (d / "f.txt").write_text("x")
    past = time.time() - 7 * 3600
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    removed = podlup.cleanup_sessions(str(tmp_path), ttl_sec=6 * 3600)
    assert removed == ["podlup_old1"]
    assert not old.exists() and fresh.exists() and other.exists()


# ── الحدود والصحة ──
@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    return TestClient(podlup.app)


def test_health_reports_keys(client, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert client.get("/health").json()["keys"] is False
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "y")
    assert client.get("/health").json()["keys"] is True


def test_oversized_upload_rejected_before_processing(client, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "y")
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    called = []
    monkeypatch.setattr(podlup, "extract_audio", lambda *a, **k: called.append(1))
    r = client.post("/analyze", files={"file": ("a.mp4", b"0" * (2 * 1024 * 1024))})
    assert r.status_code == 413 and "الحد" in r.json()["detail"]
    assert not called


def test_over_duration_rejected_before_extract(client, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "y")
    monkeypatch.setenv("MAX_DURATION_MIN", "1")
    monkeypatch.setattr(podlup, "probe_duration", lambda p: 3600.0)
    called = []
    monkeypatch.setattr(podlup, "extract_audio", lambda *a, **k: called.append(1))
    r = client.post("/analyze", files={"file": ("a.mp4", b"0" * 100)})
    assert r.status_code == 413 and not called


def test_missing_keys_refuse_analyze(client, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    r = client.post("/analyze", files={"file": ("a.mp4", b"0")})
    assert r.status_code == 503


def test_download_rejects_bad_session_and_caption(client):
    assert client.get("/download/..%2Fetc/0").status_code in (400, 404)
    assert client.get("/download/podlup_zzzz/0?caption=evil").status_code == 400
    r = client.get("/download/podlup_zzzz/0")
    assert r.status_code == 404 and "ارفع الملف من جديد" in r.json()["detail"]


def test_clips_count_is_clamped(client, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "y")
    monkeypatch.setattr(podlup, "probe_duration", lambda p: 60.0)
    monkeypatch.setattr(podlup, "extract_audio", lambda *a, **k: "audio.m4a")
    monkeypatch.setattr(podlup, "transcribe", lambda *a, **k: {"segments": make_segments(200)})
    seen = {}
    def fake_analyze(segs, count):
        seen["count"] = count
        return [{"start_id": i * 8, "end_id": i * 8 + 7} for i in range(20)]
    monkeypatch.setattr(podlup, "analyze", fake_analyze)
    r = client.post("/analyze", files={"file": ("a.mp4", b"0")}, data={"clips_count": "99"})
    assert r.status_code == 200 and seen["count"] == 10 and len(r.json()["clips"]) == 10
    podlup.shutil.rmtree(os.path.join(podlup.tempfile.gettempdir(), r.json()["session_id"]), ignore_errors=True)
    r = client.post("/analyze", files={"file": ("a.mp4", b"0")}, data={"clips_count": "-5"})
    assert seen["count"] == 1
    podlup.shutil.rmtree(os.path.join(podlup.tempfile.gettempdir(), r.json()["session_id"]), ignore_errors=True)
