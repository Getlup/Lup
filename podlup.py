"""
Podlup v3.1 — ملف واحد: السيرفر + الواجهة معاً
التشغيل:  python3 podlup.py
الفتح:    http://localhost:8000

المفاتيح تُقرأ من متغيرات البيئة فقط (انظر .env.example):
  GROQ_API_KEY, OPENROUTER_API_KEY
"""

import os, sys, json, subprocess, tempfile, shutil, re, socket, time, threading, logging, uuid
from contextlib import asynccontextmanager
from pathlib import Path

# ── فحص المكتبات ──
try:
    from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
    from fastapi.middleware.cors import CORSMiddleware
except ImportError:
    print("❌ نقص مكتبات — شغّل هذا الأمر أولاً:")
    print("   pip3 install fastapi uvicorn python-multipart")
    sys.exit(1)

try:
    from PIL import Image, ImageDraw, ImageFont
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:
    print("❌ نقص مكتبات الكابشن — شغّل هذا الأمر أولاً:")
    print("   pip3 install pillow arabic-reshaper python-bidi")
    sys.exit(1)

import urllib.request
import urllib.error

VERSION = "3.1"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("podlup")

FONT_CANDIDATES_BOLD = [
    # Linux (Railway/Render container) — يثبَّت عبر Dockerfile
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    # macOS (تشغيل محلي)
    "/System/Library/Fonts/Supplemental/GeezaPro-Bold.ttf",
    "/System/Library/Fonts/SFArabicRounded.ttf",
    "/System/Library/Fonts/SFArabic.ttf",
    "/System/Library/Fonts/GeezaPro.ttc",
    "/System/Library/Fonts/Supplemental/GeezaPro.ttc",
]
FONT_CANDIDATES = [
    # Linux (Railway/Render container)
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    # macOS (تشغيل محلي)
    "/System/Library/Fonts/SFArabic.ttf",
    "/System/Library/Fonts/GeezaPro.ttc",
    "/System/Library/Fonts/Supplemental/GeezaPro.ttc",
]

CAPTION_MODES = ("none", "normal", "focus")
SESSION_RE = re.compile(r"^podlup_[A-Za-z0-9_]{4,40}$")
USER_AGENT = "podlup/" + VERSION


# ══════════════ الإعدادات (من متغيرات البيئة فقط) ══════════════
def _env_num(name: str, default, cast=int):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


def groq_key() -> str:
    return os.environ.get("GROQ_API_KEY", "").strip()


def openrouter_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


def keys_ok() -> bool:
    return bool(groq_key() and openrouter_key())


def max_upload_bytes() -> int:
    return _env_num("MAX_UPLOAD_MB", 2048) * 1024 * 1024


def max_duration_sec() -> int:
    return _env_num("MAX_DURATION_MIN", 150) * 60


def session_ttl_sec() -> float:
    return _env_num("SESSION_TTL_HOURS", 6, float) * 3600


def allowed_origins() -> list:
    raw = os.environ.get("ALLOWED_ORIGINS", "https://podlup.com,https://app.podlup.com")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if "PORT" not in os.environ:  # تشغيل محلي
        local = f"http://localhost:{os.environ.get('PORT', 8000)}"
        if local not in origins:
            origins.append(local)
    return origins


class UserError(Exception):
    """خطأ رسالته آمنة وتُعرض للمستخدم كما هي."""
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ══════════════ تنظيف الجلسات المؤقتة ══════════════
def cleanup_sessions(base_dir: str = None, ttl_sec: float = None, now: float = None) -> list:
    """يحذف مجلدات podlup_* الأقدم من المدة المسموحة. يرجع أسماء المحذوف."""
    base = Path(base_dir or tempfile.gettempdir())
    ttl = session_ttl_sec() if ttl_sec is None else ttl_sec
    now = time.time() if now is None else now
    removed = []
    for d in base.glob("podlup_*"):
        try:
            if d.is_dir() and not d.is_symlink() and now - d.stat().st_mtime > ttl:
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d.name)
        except OSError:
            continue
    if removed:
        log.info("تنظيف: حُذفت %d جلسة منتهية", len(removed))
    return removed


_stop_cleaner = threading.Event()


def _cleaner_loop():
    while True:
        try:
            cleanup_sessions()
        except Exception:
            log.exception("فشل تنظيف الجلسات")
        if _stop_cleaner.wait(1800):
            return


@asynccontextmanager
async def lifespan(_app):
    if not keys_ok():
        log.error("GROQ_API_KEY و/أو OPENROUTER_API_KEY غير مضبوطة — التحليل لن يعمل (/health يرجع keys:false)")
    _stop_cleaner.clear()
    t = threading.Thread(target=_cleaner_loop, daemon=True, name="podlup-cleaner")
    t.start()
    yield
    _stop_cleaner.set()


app = FastAPI(title=f"Podlup v{VERSION}", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins(), allow_methods=["GET", "POST"], allow_headers=["*"])


@app.middleware("http")
async def reject_oversized(request: Request, call_next):
    """رفض مبكر عبر Content-Length قبل قراءة الجسم كاملاً."""
    if request.method == "POST" and request.url.path == "/analyze":
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > max_upload_bytes() + 1024 * 1024:
            return JSONResponse({"detail": _too_big_msg()}, status_code=413)
    return await call_next(request)


def _too_big_msg() -> str:
    return f"حجم الملف يتجاوز الحد المسموح ({max_upload_bytes() // (1024 * 1024)}MB). ضغّط الملف أو ارفع جزءاً منه."


def load_font(size: int, bold: bool = True):
    candidates = FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    # fallback إن لم يوجد أي خط بولد
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def run_cmd(cmd, cwd=None, timeout=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise UserError("انتهت مهلة المعالجة. جرّب ملفاً أقصر.", 504)


# ══════════════ AUDIO ══════════════
def probe_duration(path: str) -> float:
    r = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path], timeout=60)
    try:
        d = float(r.stdout.strip())
    except ValueError:
        log.error("ffprobe فشل: %s", r.stderr[-300:])
        raise UserError("تعذّر قراءة الملف. تأكد أنه ملف صوت أو فيديو سليم.")
    if d <= 0:
        raise UserError("الملف فارغ أو مدته صفر.")
    return d


def extract_audio(input_name: str, work_dir: str) -> str:
    """يستخرج صوتاً مضغوطاً من الفيديو ليقبله Groq مهما كان حجم الفيديو"""
    audio_name = "audio.m4a"
    r = run_cmd(["ffmpeg", "-y", "-i", input_name, "-vn", "-ac", "1",
                 "-c:a", "aac", "-b:a", "48k", audio_name], cwd=work_dir, timeout=3600)
    if r.returncode != 0:
        log.error("فشل استخراج الصوت: %s", r.stderr[-500:])
        raise UserError("تعذّر استخراج الصوت من الملف. تأكد أنه ملف سليم.")
    return audio_name


# ══════════════ HTTP مساعدات ══════════════
def _multipart(fields: dict, file_field: str, filename: str, file_bytes: bytes):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                  f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _error_text(e: "urllib.error.HTTPError") -> str:
    try:
        body = json.loads(e.read())
        err = body.get("error", body)
        return err.get("message", str(err)) if isinstance(err, dict) else str(err)
    except Exception:
        return f"HTTP {e.code}"


# ══════════════ TRANSCRIBE ══════════════
def transcribe(audio_name: str, work_dir: str) -> dict:
    key = groq_key()
    if not key:
        raise UserError("الخدمة غير مهيّأة (مفتاح التفريغ مفقود).", 503)
    with open(os.path.join(work_dir, audio_name), "rb") as f:
        audio = f.read()
    body, ctype = _multipart(
        {"model": "whisper-large-v3", "language": "ar", "response_format": "verbose_json"},
        "file", audio_name, audio)
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": ctype, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_env_num("GROQ_TIMEOUT_SEC", 600)) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.error("Groq HTTP %s: %s", e.code, _error_text(e))
        raise UserError("فشل التفريغ الصوتي. حاول لاحقاً، وإن تكرر الخطأ فالملف قد يكون كبيراً جداً.", 502)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.error("Groq اتصال: %s", e)
        raise UserError("تعذّر الاتصال بخدمة التفريغ أو انتهت المهلة. حاول مرة أخرى.", 504)
    except json.JSONDecodeError:
        raise UserError("رد غير متوقع من خدمة التفريغ.", 502)
    if "error" in data:
        log.error("Groq خطأ: %s", data["error"])
        raise UserError("خطأ من خدمة التفريغ الصوتي.", 502)
    return data


# ══════════════ OpenRouter ══════════════
def parse_json_content(content: str):
    """يحلل رد GPT حتى لو لُفّ بأسوار ``` أو نص زائد."""
    text = (content or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def openrouter_json(messages: list, timeout: int = None):
    """طلب JSON من OpenRouter مع محاولة إعادة واحدة. يرجع الكائن المحلَّل."""
    key = openrouter_key()
    if not key:
        raise UserError("الخدمة غير مهيّأة (مفتاح التحليل مفقود).", 503)
    timeout = timeout or _env_num("OPENROUTER_TIMEOUT_SEC", 120)
    payload = json.dumps({
        "model": "openai/gpt-4o",
        "messages": messages,
        "response_format": {"type": "json_object"},
    }).encode()
    last = None
    for attempt in (1, 2):
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions", data=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                result = json.loads(r.read())
            return parse_json_content(result["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {_error_text(e)}"
            if e.code in (400, 401, 402, 403):  # لا فائدة من الإعادة
                break
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError,
                KeyError, IndexError, TypeError, ValueError) as e:
            last = repr(e)
        log.warning("OpenRouter محاولة %d فشلت: %s", attempt, last)
    log.error("OpenRouter فشل: %s", last)
    raise UserError("تعذّر تحليل النص حالياً. حاول مرة أخرى.", 502)


# ══════════════ ANALYZE ══════════════
def analyze(segments: list, count: int = 3) -> list:
    transcript_text = "\n".join(f"[{s['id']}] {s['text'].strip()}" for s in segments)
    system_prompt = (
        "أنت محرر محتوى محترف متخصص في اختيار أقوى لحظات البودكاست العربي لمنصات التيك توك والسناب شات وإنستغرام ريلز. "
        "النص مقسّم لجمل مرقّمة. معايير اللحظة القوية: جملة صادمة أو متناقضة، قصة شخصية حقيقية، رأي جريء، "
        "لحظة اعتراف وصدق، جملة قابلة للاقتباس، معلومة مفاجئة. "
        f"اختر أقوى {count} لحظات (8-20 جملة متتالية على الأقل لكل مقطع، لا تقل عن 25 ثانية). "
        "مهم جداً: end_id يجب أن يكون آخر جملة في الفكرة وليس أول جملة في الفكرة التالية — إذا شككت اختر الجملة السابقة. "
        'أجب بـ JSON فقط: {"clips":[{"start_id":0,"end_id":0,"title":"","caption":"","score":0,"reason":""}]}'
    )
    obj = openrouter_json([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": transcript_text},
    ])
    clips = obj.get("clips") if isinstance(obj, dict) else None
    if not isinstance(clips, list):
        raise UserError("رد التحليل غير مكتمل. حاول مرة أخرى.", 502)
    return clips


def _as_int(v):
    """يقبل الأعداد الصحيحة ونصوصها فقط (لا bool ولا كسور)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def extend_clip(sid: int, eid: int, seg_by_id: dict, all_ids: list, min_dur: float = 25):
    """يمدّ المقطع بجمل لاحقة (ثم سابقة) حتى يبلغ min_dur ثانية."""
    dur = seg_by_id[eid]["end"] - seg_by_id[sid]["start"]
    while dur < min_dur:
        nxt = next((i for i in all_ids if i > eid), None)
        prv = next((i for i in reversed(all_ids) if i < sid), None)
        if nxt is not None:
            eid = nxt
        elif prv is not None:
            sid = prv
        else:
            break
        dur = seg_by_id[eid]["end"] - seg_by_id[sid]["start"]
    return sid, eid


def _seg_public(s: dict) -> dict:
    return {"id": s["id"], "text": s["text"].strip(), "start": s["start"], "end": s["end"]}


def validate_clips(raw_clips, segments: list, count: int = 3, min_dur: float = 25) -> list:
    """يتحقق من مقاطع GPT ويتجاهل غير الصالح منها بدل إفشال الطلب كله."""
    if not isinstance(raw_clips, list):
        return []
    seg_by_id = {s["id"]: s for s in segments}
    all_ids = sorted(seg_by_id.keys())
    clips = []
    for c in raw_clips:
        if not isinstance(c, dict):
            continue
        sid, eid = _as_int(c.get("start_id")), _as_int(c.get("end_id"))
        if sid is None or eid is None or sid > eid:
            continue
        if sid not in seg_by_id or eid not in seg_by_id:
            continue
        sid, eid = extend_clip(sid, eid, seg_by_id, all_ids, min_dur)
        ss, es = seg_by_id[sid], seg_by_id[eid]
        try:
            score = int(round(float(c.get("score", 0))))
        except (TypeError, ValueError):
            score = 0
        after_ids = [i for i in all_ids if i > eid][:8]
        clips.append({
            "title": str(c.get("title") or "")[:200],
            "caption": str(c.get("caption") or "")[:1000],
            "score": score,
            "reason": str(c.get("reason") or "")[:500],
            "start": round(ss["start"], 2),
            "end": round(es["end"], 2),
            "duration": round(es["end"] - ss["start"]),
            "start_id": sid,
            "end_id": eid,
            "clip_segments": [_seg_public(s) for s in segments if sid <= s["id"] <= eid],
            "after_segments": [_seg_public(seg_by_id[i]) for i in after_ids],
        })
        if len(clips) >= count:
            break
    return clips


# ══════════════ KEYWORDS (دفعة واحدة) ══════════════
def batch_keywords(texts: list) -> dict:
    """طلب واحد يستخرج الكلمات المفتاحية لكل الجمل دفعة واحدة"""
    if not texts or not openrouter_key():
        return {}
    numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(texts))
    try:
        raw = openrouter_json([{
            "role": "user",
            "content": (
                "لكل جملة مرقّمة أدناه، استخرج أهم 1-3 كلمات مفتاحية (الأكثر تأثيراً وجذباً للانتباه). "
                'أجب بـ JSON فقط بدون أي نص آخر بهذا الشكل: {"0":["كلمة"],"1":["كلمة","كلمة"]}\n\n'
                + numbered)
        }], timeout=60)
        return {int(k): v for k, v in raw.items() if isinstance(v, list) and str(k).isdigit()}
    except Exception as e:
        log.warning("keywords fallback: %s", e)
        return {}


# ══════════════ CAPTION RENDERING (Python + PIL) ══════════════
def make_caption_png(text: str, keywords: list, mode: str, out_path: str):
    """يرسم الكابشن العربي كصورة شفافة — لا يعتمد على libass إطلاقاً"""
    W = 1080
    margin = 60
    size = 64
    font = load_font(size, bold=True)

    def disp(w):
        return get_display(arabic_reshaper.reshape(w))

    words = text.split()
    if not words:
        return None

    space_w = font.getlength(disp("ا ا")) - font.getlength(disp("اا"))
    if space_w <= 0:
        space_w = size * 0.3

    items = []
    for w in words:
        d = disp(w)
        clean = re.sub(r"[^\w]", "", w, flags=re.UNICODE)
        is_key = (mode == "focus") and any(k and (k in clean or clean in k) for k in (keywords or []))
        items.append((d, font.getlength(d), is_key))

    maxw = W - 2 * margin
    lines, cur, curw = [], [], 0
    for it in items:
        add = it[1] + (space_w if cur else 0)
        if cur and curw + add > maxw:
            lines.append(cur)
            cur, curw = [it], it[1]
        else:
            cur.append(it)
            curw += add
    if cur:
        lines.append(cur)

    lh = size + 20
    H = lh * len(lines) + 24
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)

    y = 8
    for line in lines:
        line_w = sum(i[1] for i in line) + space_w * (len(line) - 1)
        x = (W + line_w) / 2  # نبدأ من يمين السطر (توسيط)
        for d, wlen, is_key in line:
            x -= wlen
            if is_key:
                dr.text((x, y), d, font=font, fill=(255, 90, 42, 255),
                        stroke_width=6, stroke_fill=(0, 0, 0, 255))
            else:
                dr.text((x, y), d, font=font, fill=(255, 255, 255, 255),
                        stroke_width=4, stroke_fill=(0, 0, 0, 255))
            x -= space_w
        y += lh

    img.save(out_path)
    return (W, H)


# ══════════════ CUT CLIP ══════════════
def cut_clip(input_name: str, start: float, end: float, out_name: str,
             caption_mode: str, segments: list, work_dir: str):
    duration = round(end - start, 2)
    fade_in, fade_out = 0.4, 0.2
    fade_out_start = round(max(0, duration - fade_out), 2)
    t1 = out_name + "_t1.mp4"
    t2 = out_name + "_t2.mp4"
    pngs = []

    try:
        # ── الخطوة ١: قص + عمودي 9:16 ──
        r1 = run_cmd(["ffmpeg", "-y", "-i", input_name,
                      "-ss", str(start), "-t", str(duration),
                      "-c:v", "libx264", "-c:a", "aac",
                      "-vf", "scale=1080:-2:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
                      t1], cwd=work_dir, timeout=900)
        if r1.returncode != 0:
            raise RuntimeError(f"فشل القص: {r1.stderr[-300:]}")

        caption_input = t1

        # ── الخطوة ٢: الكابشن (مرسوم بـ Python — يعمل مع أي نسخة FFmpeg) ──
        if caption_mode in ("normal", "focus"):
            clip_segs = [s for s in segments if s["end"] > start and s["start"] < end and s["text"].strip()]

            kw_map = {}
            if caption_mode == "focus":
                kw_map = batch_keywords([s["text"].strip() for s in clip_segs])

            overlays = []
            for idx, seg in enumerate(clip_segs):
                png = f"{out_name}_cap{idx}.png"
                pngs.append(png)
                res = make_caption_png(seg["text"].strip(), kw_map.get(idx, []), caption_mode,
                                       os.path.join(work_dir, png))
                if res:
                    s_rel = max(0, round(seg["start"] - start, 2))
                    e_rel = min(duration, round(seg["end"] - start, 2))
                    overlays.append((png, s_rel, e_rel, res[1]))

            if overlays:
                cmd = ["ffmpeg", "-y", "-i", t1]
                fparts = []
                prev = "[0:v]"
                for k, (png, s_rel, e_rel, h) in enumerate(overlays, 1):
                    cmd += ["-loop", "1", "-i", png]
                    out_lbl = f"[v{k}]"
                    y_pos = 1920 - h - 170
                    fparts.append(
                        f"{prev}[{k}:v]overlay=(W-w)/2:{y_pos}:enable='between(t,{s_rel},{e_rel})':shortest=1{out_lbl}"
                    )
                    prev = out_lbl
                cmd += ["-filter_complex", ";".join(fparts),
                        "-map", prev, "-map", "0:a?",
                        "-c:v", "libx264", "-c:a", "copy", t2]
                r2 = run_cmd(cmd, cwd=work_dir, timeout=900)
                if r2.returncode != 0:
                    raise RuntimeError(f"فشل الكابشن: {r2.stderr[-400:]}")
                caption_input = t2

        # ── الخطوة ٣: fade ──
        r3 = run_cmd(["ffmpeg", "-y", "-i", caption_input,
                      "-vf", f"fade=t=in:st=0:d={fade_in},fade=t=out:st={fade_out_start}:d={fade_out}",
                      "-af", f"afade=t=in:st=0:d={fade_in},afade=t=out:st={fade_out_start}:d={fade_out}",
                      "-c:v", "libx264", "-c:a", "aac", out_name], cwd=work_dir, timeout=900)
        if r3.returncode != 0:
            raise RuntimeError(f"فشل الـ fade: {r3.stderr[-300:]}")
    finally:
        for f in [t1, t2] + pngs:
            p = os.path.join(work_dir, f)
            if os.path.exists(p):
                os.remove(p)


# ══════════════ الجلسات ══════════════
def session_dir(session_id: str) -> str:
    if not SESSION_RE.match(session_id or ""):
        raise HTTPException(status_code=400, detail="معرّف غير صالح")
    return os.path.join(tempfile.gettempdir(), session_id)


SESSION_EXPIRED_MSG = ("انتهت الجلسة. تُحذف الملفات تلقائياً بعد فترة من الوقت، "
                       "فارفع الملف من جديد وأعد التحليل.")


def save_upload(file: UploadFile, dest: str, limit: int) -> int:
    """ينسخ الملف على دفعات ويُلغي فور تجاوز الحد."""
    total = 0
    with open(dest, "wb") as out:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UserError(_too_big_msg(), 413)
            out.write(chunk)
    return total


# ══════════════ ENDPOINTS (دوال عادية → تعمل في threadpool ولا تجمّد السيرفر) ══════════════
@app.post("/analyze")
def analyze_endpoint(
    file: UploadFile = File(...),
    clips_count: int = Form(3),
):
    if not keys_ok():
        raise HTTPException(status_code=503, detail="الخدمة غير مهيّأة حالياً. تواصل مع الدعم.")
    clips_count = max(1, min(10, clips_count))
    work_dir = tempfile.mkdtemp(prefix="podlup_")
    try:
        ext = Path(file.filename or "").suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
            ext = ".mp4"
        input_name = f"input{ext}"
        input_path = os.path.join(work_dir, input_name)
        save_upload(file, input_path, max_upload_bytes())

        duration = probe_duration(input_path)
        if duration > max_duration_sec():
            raise UserError(f"مدة الملف ({int(duration // 60)} دقيقة) تتجاوز الحد المسموح "
                            f"({max_duration_sec() // 60} دقيقة).", 413)

        audio_name = extract_audio(input_name, work_dir)
        transcript = transcribe(audio_name, work_dir)
        segments = transcript.get("segments", [])
        if not segments:
            raise UserError("لم يُستخرج أي نص من الملف. تأكد أن الصوت واضح.", 422)

        raw_clips = analyze(segments, clips_count)
        clips = validate_clips(raw_clips, segments, clips_count)
        if not clips:
            raise UserError("لم نتمكن من اختيار مقاطع صالحة من هذه الحلقة. حاول مرة أخرى.", 502)

        meta = {"input_name": input_name, "clips": clips, "segments": segments}
        with open(os.path.join(work_dir, "meta.json"), "w") as f:
            json.dump(meta, f, ensure_ascii=False)

        return JSONResponse({
            "session_id": os.path.basename(work_dir),
            "clips": clips,
            "segments_count": len(segments),
        })
    except UserError as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=e.status, detail=str(e))
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        log.exception("فشل /analyze")
        raise HTTPException(status_code=500, detail="حدث خطأ غير متوقع أثناء التحليل. حاول مرة أخرى.")


@app.get("/download/{session_id}/{clip_index}")
def download_endpoint(session_id: str, clip_index: int,
                      caption: str = "none", end_id: int = -1):
    if caption not in CAPTION_MODES:
        raise HTTPException(status_code=400, detail="وضع الكابشن غير صالح")
    work_dir = session_dir(session_id)
    meta_path = os.path.join(work_dir, "meta.json")
    if not os.path.exists(meta_path):
        raise HTTPException(status_code=404, detail=SESSION_EXPIRED_MSG)

    with open(meta_path) as f:
        meta = json.load(f)
    os.utime(work_dir, None)  # تمديد عمر الجلسة بعد الاستخدام

    clips = meta["clips"]
    segments = meta["segments"]
    if not (0 <= clip_index < len(clips)):
        raise HTTPException(status_code=400, detail="رقم المقطع غير صحيح")

    clip = clips[clip_index]
    end_time = clip["end"]
    if end_id >= 0:
        seg_by_id = {s["id"]: s for s in segments}
        if end_id in seg_by_id:
            end_time = round(seg_by_id[end_id]["end"], 2)

    out_name = f"clip_{clip_index}_{caption}_{end_id}.mp4"
    out_path = os.path.join(work_dir, out_name)

    if not os.path.exists(out_path):
        tmp_name = f"{uuid.uuid4().hex[:8]}_{out_name}"
        try:
            cut_clip(meta["input_name"], clip["start"], end_time, tmp_name,
                     caption, segments, work_dir)
            os.replace(os.path.join(work_dir, tmp_name), out_path)
        except UserError as e:
            raise HTTPException(status_code=e.status, detail=str(e))
        except Exception:
            log.exception("فشل رندر المقطع")
            tmp = os.path.join(work_dir, tmp_name)
            if os.path.exists(tmp):
                os.remove(tmp)
            raise HTTPException(status_code=500, detail="تعذّر تجهيز المقطع. حاول مرة أخرى.")

    safe_title = "".join(ch for ch in clip["title"] if ch.isalnum() or ch in " _-")[:40].strip()
    return FileResponse(out_path, media_type="video/mp4",
                        filename=f"podlup_{clip_index+1}_{safe_title}.mp4")


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION,
            "font": any(os.path.exists(p) for p in FONT_CANDIDATES),
            "keys": keys_ok()}


# ══════════════ الواجهة (مدمجة — لا ملف HTML منفصل) ══════════════
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Podlup — استخرج أقوى لحظاتك</title>
<link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800;900&family=Inter:wght@500;700;800&display=swap" rel="stylesheet">
<style>
:root{--ivory:#F5F0E8;--ivory-dark:#EDE6D8;--forest:#38433F;--forest-mid:#5E716A;--forest-light:#9EA9A5;--orange:#FF5A2A;--orange-pale:#FFF1EA;--border:rgba(56,67,63,.12);--border-strong:rgba(56,67,63,.25);--white:#FFF;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--ivory);color:var(--forest);font-family:'Tajawal',sans-serif;min-height:100vh;}
nav{position:fixed;top:0;left:0;right:0;z-index:100;height:64px;padding:0 clamp(20px,5vw,60px);display:flex;align-items:center;justify-content:space-between;background:var(--ivory);border-bottom:1px solid var(--border);}
.nav-logo{display:flex;align-items:center;gap:10px;text-decoration:none;}
.nav-wordmark{font-size:20px;font-weight:900;letter-spacing:-.5px;font-family:'Inter',sans-serif;}
.pod{color:var(--forest);}.lup{color:var(--orange);}
.nav-badge{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--orange);background:var(--orange-pale);border-radius:100px;padding:3px 10px;border:1px solid rgba(255,90,42,.2);font-family:'Inter',sans-serif;}
.page{padding:100px clamp(20px,5vw,60px) 80px;max-width:740px;margin:0 auto;}
.eyebrow{display:inline-flex;align-items:center;gap:10px;font-size:11px;font-weight:700;letter-spacing:3px;text-transform:uppercase;color:var(--orange);margin-bottom:20px;}
.eyebrow::before{content:'';width:24px;height:2px;background:var(--orange);}
.page-title{font-size:clamp(36px,7vw,68px);font-weight:900;line-height:.95;letter-spacing:-2px;margin-bottom:14px;}
.page-title em{font-style:normal;color:var(--orange);}
.page-sub{font-size:16px;color:var(--forest-mid);line-height:1.65;margin-bottom:40px;max-width:520px;}
.card{background:var(--white);border:1px solid var(--border);border-radius:20px;padding:28px 32px;margin-bottom:14px;}
.card-label{font-size:11px;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;color:var(--forest-light);margin-bottom:18px;display:flex;align-items:center;gap:8px;}
.card-label::after{content:'';flex:1;height:1px;background:var(--border);}
.field-label{font-size:12px;font-weight:700;color:var(--forest-mid);margin-bottom:7px;display:block;}
.field-input{width:100%;background:var(--ivory);border:1.5px solid var(--border-strong);border-radius:12px;padding:12px 16px;color:var(--forest);font-family:'Tajawal',sans-serif;font-size:15px;outline:none;transition:border-color .2s;direction:ltr;text-align:left;}
.field-input:focus{border-color:var(--orange);}
.upload-zone{border:2px dashed var(--border-strong);border-radius:14px;padding:28px 20px;text-align:center;cursor:pointer;transition:all .25s;position:relative;background:var(--ivory);}
.upload-zone:hover,.upload-zone.drag{border-color:var(--orange);background:var(--orange-pale);}
.upload-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%;}
.upload-name{margin-top:8px;font-family:'Inter',sans-serif;font-size:12px;color:var(--orange);font-weight:600;}
.settings-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px;}
.field-select{width:100%;background:var(--ivory);border:1.5px solid var(--border-strong);border-radius:12px;padding:11px 16px;color:var(--forest);font-family:'Tajawal',sans-serif;font-size:14px;outline:none;cursor:pointer;appearance:none;}
.btn-primary{width:100%;background:var(--forest);color:var(--ivory);border:none;border-radius:100px;padding:15px 32px;font-size:16px;font-weight:800;cursor:pointer;font-family:'Tajawal',sans-serif;transition:all .25s;}
.btn-primary:hover:not(:disabled){background:var(--orange);transform:translateY(-2px);box-shadow:0 12px 28px rgba(255,90,42,.3);}
.btn-primary:disabled{opacity:.4;cursor:not-allowed;}
.progress-card{display:none;}.progress-card.visible{display:block;}
.p-step{display:flex;align-items:center;gap:14px;padding:13px 0;border-bottom:1px solid var(--border);font-size:14px;}
.p-step:last-child{border-bottom:none;}
.p-num{width:30px;height:30px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;font-family:'Inter',sans-serif;background:var(--ivory-dark);color:var(--forest-light);border:1.5px solid var(--border-strong);transition:all .3s;}
.p-num.active{background:var(--orange-pale);color:var(--orange);border-color:var(--orange);animation:blink 1.5s infinite;}
.p-num.done{background:var(--forest);color:var(--ivory);border-color:var(--forest);}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.5}}
.p-txt{font-weight:500;}
.p-txt.muted{color:var(--forest-light);font-weight:400;}
.error-box{background:rgba(255,90,42,.06);border:1px solid rgba(255,90,42,.25);border-radius:12px;padding:13px 18px;font-size:14px;color:var(--orange);font-weight:600;display:none;margin-bottom:14px;line-height:1.7;}
.error-box.visible{display:block;}
.results-sec{display:none;}.results-sec.visible{display:block;}
.results-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;}
.results-title{font-size:11px;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;color:var(--forest-light);}
.btn-restart{font-size:12px;color:var(--forest-mid);cursor:pointer;background:none;border:1px solid var(--border-strong);border-radius:100px;padding:6px 14px;font-family:'Tajawal',sans-serif;}
.btn-restart:hover{background:var(--forest);color:var(--ivory);}
.clip-card{background:var(--white);border:1px solid var(--border);border-radius:18px;padding:22px 26px;margin-bottom:16px;}
.clip-meta{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.clip-time{font-family:'Inter',sans-serif;font-size:12px;font-weight:600;color:var(--forest-mid);background:var(--ivory);padding:4px 12px;border-radius:100px;border:1px solid var(--border-strong);}
.clip-score{font-family:'Inter',sans-serif;font-size:12px;font-weight:700;color:var(--orange);background:var(--orange-pale);padding:4px 12px;border-radius:100px;border:1px solid rgba(255,90,42,.2);}
.clip-title{font-size:17px;font-weight:800;margin-bottom:6px;line-height:1.4;}
.clip-caption{font-size:13px;color:var(--forest-mid);line-height:1.75;margin-bottom:10px;}
.clip-reason{font-size:12px;color:var(--forest-mid);background:var(--ivory);border-radius:10px;padding:9px 13px;border-right:3px solid var(--orange);margin-bottom:14px;line-height:1.6;}
.editor-toggle{font-size:12px;font-weight:700;color:var(--forest-mid);cursor:pointer;background:none;border:1px solid var(--border-strong);border-radius:100px;padding:7px 16px;font-family:'Tajawal',sans-serif;margin-bottom:12px;}
.editor-toggle:hover{background:var(--ivory-dark);}
.text-editor{display:none;background:var(--ivory);border-radius:14px;padding:14px;margin-bottom:14px;max-height:400px;overflow-y:auto;}
.text-editor.open{display:block;}
.editor-hint{font-size:11px;color:var(--forest-light);margin-bottom:10px;line-height:1.6;}
.seg-line{padding:9px 13px;border-radius:9px;cursor:pointer;transition:all .2s;line-height:1.65;font-size:13px;margin-bottom:5px;border:1px solid var(--border);background:var(--white);color:var(--forest);display:flex;align-items:flex-start;gap:8px;}
.seg-line:hover{border-color:var(--orange);background:var(--orange-pale);}
.seg-line.after-end{opacity:0.4;}
.seg-line.after-end:hover{opacity:1;}
.seg-line.is-end{border-color:var(--orange)!important;background:var(--orange-pale)!important;font-weight:600;}
.seg-num{font-family:'Inter',sans-serif;font-size:10px;color:var(--forest-light);flex-shrink:0;padding-top:2px;min-width:28px;}
.editor-note{font-size:11px;color:var(--orange);margin-top:8px;font-weight:600;}
.caption-opts{display:flex;gap:8px;margin-bottom:14px;}
.cap-opt{flex:1;background:var(--ivory);border:1.5px solid var(--border-strong);border-radius:12px;padding:11px 14px;cursor:pointer;text-align:center;transition:all .2s;}
.cap-opt:hover{border-color:var(--forest);}
.cap-opt.selected{border-color:var(--orange);background:var(--orange-pale);}
.cap-opt-title{font-size:13px;font-weight:700;display:block;margin-bottom:2px;}
.cap-opt-sub{font-size:11px;color:var(--forest-light);}
.btn-dl{width:100%;background:var(--forest);color:var(--ivory);border:none;border-radius:100px;padding:14px 24px;font-size:15px;font-weight:800;cursor:pointer;font-family:'Tajawal',sans-serif;transition:all .25s;margin-bottom:10px;}
.btn-dl:hover:not(:disabled){background:var(--orange);}
.btn-dl:disabled{opacity:.5;cursor:wait;}
.btn-copy{background:var(--ivory);border:1.5px solid var(--border-strong);border-radius:100px;padding:8px 16px;font-size:13px;font-weight:700;cursor:pointer;font-family:'Tajawal',sans-serif;color:var(--forest);}
.btn-copy:hover{background:var(--forest);color:var(--ivory);}
.footer-note{text-align:center;margin-top:40px;font-size:12px;color:var(--forest-light);line-height:1.8;}
@media(max-width:560px){.settings-row{grid-template-columns:1fr;}.caption-opts{flex-direction:column;}}
</style>
</head>
<body>
<nav>
  <a class="nav-logo" href="/">
    <span class="nav-wordmark"><span class="pod">Pod</span><span class="lup">lup</span></span>
    <span class="nav-badge" id="verBadge">v…</span>
  </a>
</nav>

<div class="page">
  <div class="eyebrow">الأداة</div>
  <h1 class="page-title">استخرج <em>أقوى</em><br>لحظاتك</h1>
  <p class="page-sub">ارفع حلقتك — وسنختار لك أقوى المقاطع جاهزة للتحميل والنشر. حلقات طويلة؟ لا مشكلة، نستخرج الصوت تلقائياً.</p>

  <div class="error-box" id="errorBox"></div>

  <div class="card" id="inputCard">
    <div class="card-label">الملف</div>
    <div class="upload-zone" id="uploadZone">
      <input type="file" id="fileInput" accept="audio/*,video/*">
      <span style="font-size:28px;display:block;margin-bottom:8px;">🎙️</span>
      <div style="font-size:14px;color:var(--forest-mid);line-height:1.7;">
        <strong style="color:var(--forest);">اسحب الملف هنا</strong> أو اضغط للاختيار<br>
        <span style="font-size:12px;color:var(--forest-light);">MP4، MP3، M4A — حلقات كاملة مدعومة</span>
      </div>
      <div class="upload-name" id="uploadName"></div>
    </div>
    <div class="card-label" style="margin-top:4px;">الإعدادات</div>
    <div class="settings-row">
      <div>
        <label class="field-label">عدد المقاطع</label>
        <select id="clipsCount" class="field-select">
          <option value="3">3 مقاطع</option>
          <option value="5">5 مقاطع</option>
          <option value="7">7 مقاطع</option>
        </select>
      </div>
      <div>
        <label class="field-label">المدة التقريبية</label>
        <select id="clipDur" class="field-select">
          <option value="30">30 – 60 ثانية</option>
          <option value="60" selected>60 – 90 ثانية</option>
          <option value="90">90 – 120 ثانية</option>
        </select>
      </div>
    </div>
    <button class="btn-primary" id="analyzeBtn" onclick="startAnalysis()">⚡ استخرج المقاطع</button>
  </div>

  <div class="card progress-card" id="progressCard">
    <div class="card-label">الحالة</div>
    <div class="p-step"><div class="p-num" id="s1">1</div><span id="s1t" class="p-txt muted">استخراج الصوت وتفريغه بـ Whisper</span></div>
    <div class="p-step"><div class="p-num" id="s2">2</div><span id="s2t" class="p-txt muted">تحليل النص واختيار أقوى اللحظات</span></div>
    <div class="p-step"><div class="p-num" id="s3">3</div><span id="s3t" class="p-txt muted">جاهز للتحميل</span></div>
  </div>

  <div class="results-sec" id="resultsSec">
    <div class="results-hdr">
      <div class="results-title">المقاطع المقترحة</div>
      <button class="btn-restart" onclick="restart()">← تحليل جديد</button>
    </div>
    <div id="clipsContainer"></div>
  </div>

  <div class="footer-note">podlup.com</div>
</div>

<script>
const API='';
let sessionId=null, selectedEndId={}, captionMode={};

// version badge
fetch('/health').then(r=>r.json()).then(d=>{document.getElementById('verBadge').textContent='v'+d.version;});

const uz=document.getElementById('uploadZone'),fi=document.getElementById('fileInput'),un=document.getElementById('uploadName');
['dragover','dragenter'].forEach(e=>uz.addEventListener(e,ev=>{ev.preventDefault();uz.classList.add('drag');}));
['dragleave','drop'].forEach(e=>uz.addEventListener(e,ev=>{ev.preventDefault();uz.classList.remove('drag');}));
uz.addEventListener('drop',e=>{if(e.dataTransfer.files[0]){fi.files=e.dataTransfer.files;un.textContent='✓ '+e.dataTransfer.files[0].name;}});
fi.addEventListener('change',()=>{if(fi.files[0])un.textContent='✓ '+fi.files[0].name;});

function escapeHtml(v){return String(v==null?'':v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function fmt(s){return Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0');}
function setStep(n,state,label){
  const num=document.getElementById('s'+n),txt=document.getElementById('s'+n+'t');
  num.className='p-num'+(state?' '+state:'');
  num.textContent=state==='done'?'✓':n;
  txt.className='p-txt'+(state?'':' muted');
  if(label)txt.textContent=label;
}
function showError(msg){
  const b=document.getElementById('errorBox');b.textContent=msg;b.classList.add('visible');
  document.getElementById('analyzeBtn').disabled=false;
  document.getElementById('progressCard').classList.remove('visible');
  document.getElementById('inputCard').style.display='block';
  [1,2,3].forEach(n=>setStep(n,'',null));
}
function restart(){
  document.getElementById('resultsSec').classList.remove('visible');
  document.getElementById('inputCard').style.display='block';
  document.getElementById('errorBox').classList.remove('visible');
  sessionId=null;selectedEndId={};captionMode={};
}
function selectCaption(i,mode){
  captionMode[i]=mode;
  ['none','normal','focus'].forEach(m=>{
    const el=document.getElementById('cap-'+i+'-'+m);
    if(el)el.classList.toggle('selected',m===mode);
  });
}
function toggleEditor(i){
  const ed=document.getElementById('editor-'+i);
  const btn=document.getElementById('editor-btn-'+i);
  const open=ed.classList.toggle('open');
  btn.textContent=open?'▲ إخفاء النص':'✏️ تعديل نقطة التوقف';
}
function selectEndSeg(i,segId,endTime,el){
  selectedEndId[i]=segId;
  const editor=document.getElementById('editor-'+i);
  editor.querySelectorAll('.seg-line').forEach(line=>{
    const lid=parseInt(line.dataset.segId);
    line.classList.remove('is-end','after-end');
    if(lid===segId)line.classList.add('is-end');
    else if(lid>segId)line.classList.add('after-end');
  });
  const clipStart=parseFloat(el.dataset.clipStart);
  const timeEl=document.getElementById('clip-time-'+i);
  if(timeEl)timeEl.textContent=fmt(clipStart)+' ← '+fmt(endTime)+' · '+Math.round(endTime-clipStart)+'ث';
  document.getElementById('editor-note-'+i).textContent='✓ تم تحديد نقطة التوقف';
}
async function downloadClip(i,btn){
  btn.disabled=true;btn.textContent='⏳ جاري القص والتجهيز...';
  const endId=selectedEndId[i]??-1;
  const cap=captionMode[i]??'none';
  try{
    const res=await fetch(API+'/download/'+sessionId+'/'+i+'?caption='+cap+'&end_id='+endId);
    if(!res.ok){const e=await res.json().catch(()=>({detail:'خطأ'}));throw new Error(e.detail);}
    const blob=await res.blob();
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url;a.download='podlup_clip_'+(i+1)+'.mp4';a.click();
    URL.revokeObjectURL(url);
    btn.textContent='✓ تم التحميل';btn.disabled=false;
  }catch(e){
    btn.disabled=false;btn.textContent='⬇ تحميل المقطع';
    alert('خطأ: '+e.message);
  }
}
function copyCaption(text,btn){
  navigator.clipboard.writeText(text).then(()=>{
    const o=btn.textContent;btn.textContent='✓ تم النسخ';
    setTimeout(()=>{btn.textContent=o;},2000);
  });
}
function renderClips(clips){
  const container=document.getElementById('clipsContainer');
  container.innerHTML='';
  clips.forEach((c,i)=>{
    captionMode[i]='none';selectedEndId[i]=-1;
    const allSegs=[...(c.clip_segments||[]),...(c.after_segments||[])];
    const defEnd=Number(c.end_id);
    let segsHtml='';
    if(allSegs.length){
      segsHtml=allSegs.map(s=>{
        const isEnd=s.id===defEnd, isAfter=s.id>defEnd;
        return '<div class="seg-line'+(isEnd?' is-end':'')+(isAfter?' after-end':'')+'" data-seg-id="'+Number(s.id)+'" data-clip-start="'+Number(c.start)+'" onclick="selectEndSeg('+i+','+Number(s.id)+','+Number(s.end)+',this)"><span class="seg-num">['+Number(s.id)+']</span><span>'+escapeHtml(s.text)+'</span></div>';
      }).join('');
    }
    const editorHtml=allSegs.length?
      '<button class="editor-toggle" id="editor-btn-'+i+'" onclick="toggleEditor('+i+')">✏️ تعديل نقطة التوقف</button>'+
      '<div class="text-editor" id="editor-'+i+'">'+
        '<div class="editor-hint">اضغط على أي جملة لجعلها نقطة التوقف ✂️ — الجمل الباهتة خارج المقطع الحالي</div>'+
        segsHtml+
        '<div class="editor-note" id="editor-note-'+i+'"></div>'+
      '</div>':'';
    const div=document.createElement('div');
    div.className='clip-card';
    div.innerHTML=
      '<div class="clip-meta">'+
        '<span class="clip-time" id="clip-time-'+i+'">'+fmt(Number(c.start))+' ← '+fmt(Number(c.end))+' · '+Number(c.duration)+'ث</span>'+
        '<span class="clip-score">🔥 '+Number(c.score)+'</span>'+
      '</div>'+
      '<div class="clip-title">'+escapeHtml(c.title)+'</div>'+
      '<div class="clip-caption">'+escapeHtml(c.caption)+'</div>'+
      (c.reason?'<div class="clip-reason">'+escapeHtml(c.reason)+'</div>':'')+
      editorHtml+
      '<div class="card-label" style="margin-top:4px;">الكابشن</div>'+
      '<div class="caption-opts">'+
        '<div class="cap-opt selected" id="cap-'+i+'-none" onclick="selectCaption('+i+',\'none\')"><span class="cap-opt-title">بدون</span><span class="cap-opt-sub">فيديو نظيف</span></div>'+
        '<div class="cap-opt" id="cap-'+i+'-normal" onclick="selectCaption('+i+',\'normal\')"><span class="cap-opt-title">Caption</span><span class="cap-opt-sub">نص أسفل الفيديو</span></div>'+
        '<div class="cap-opt" id="cap-'+i+'-focus" onclick="selectCaption('+i+',\'focus\')"><span class="cap-opt-title">Caption Focus</span><span class="cap-opt-sub">كلمات مميزة</span></div>'+
      '</div>'+
      '<button class="btn-dl" id="dl-btn-'+i+'" onclick="downloadClip('+i+',this)">⬇ تحميل المقطع</button>'+
      '<button class="btn-copy" onclick="copyCaption(this.dataset.cap,this)" data-cap="'+escapeHtml(c.caption)+'">💬 نسخ الكابشن</button>';
    container.appendChild(div);
  });
}
async function startAnalysis(){
  const file=fi.files[0];
  const count=document.getElementById('clipsCount').value;
  document.getElementById('errorBox').classList.remove('visible');
  if(!file)return showError('الرجاء رفع ملف صوتي أو فيديو.');

  document.getElementById('inputCard').style.display='none';
  document.getElementById('progressCard').classList.add('visible');
  document.getElementById('analyzeBtn').disabled=true;
  setStep(1,'active','استخراج الصوت وتفريغه بـ Whisper...');
  setStep(2,'','تحليل النص واختيار أقوى اللحظات');
  setStep(3,'','جاهز للتحميل');
  try{
    const form=new FormData();
    form.append('file',file,file.name);
    form.append('clips_count',count);
    const res=await fetch(API+'/analyze',{method:'POST',body:form});
    if(!res.ok){const e=await res.json().catch(()=>({detail:'خطأ في التحليل'}));throw new Error(e.detail);}
    const data=await res.json();
    sessionId=data.session_id;
    setStep(1,'done','تم التفريغ ✓ — '+data.segments_count+' جملة');
    setStep(2,'done','تم الاختيار ✓ — '+data.clips.length+' مقاطع');
    setStep(3,'done','المقاطع جاهزة ✓');
    renderClips(data.clips);
    document.getElementById('progressCard').classList.remove('visible');
    document.getElementById('resultsSec').classList.add('visible');
  }catch(e){showError(e.message||'خطأ غير متوقع');}
  document.getElementById('analyzeBtn').disabled=false;
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


# ══════════════ MAIN ══════════════
if __name__ == "__main__":
    # Railway/Render يحددان رقم المنفذ عبر متغير البيئة PORT
    # محلياً (على جهازك) هذا المتغير غير موجود، فنستخدم 8000 كافتراضي
    PORT = int(os.environ.get("PORT", 8000))
    IS_CLOUD = "PORT" in os.environ

    if not IS_CLOUD:
        # فحص المنفذ قبل التشغيل — محلياً فقط
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", PORT))
            s.close()
        except OSError:
            s.close()
            print(f"❌ المنفذ {PORT} مشغول بسيرفر قديم. أوقفه أولاً بهذا الأمر:")
            print(f"   kill $(lsof -ti:{PORT})")
            print("ثم أعد تشغيل هذا الملف.")
            sys.exit(1)

    import uvicorn
    print("=" * 46)
    print(f"🎬 Podlup v{VERSION} — كل شيء في ملف واحد")
    print("=" * 46)
    print(f"افتح المتصفح على:  http://localhost:{PORT}" if not IS_CLOUD else "🚀 يعمل على الاستضافة السحابية")
    print("(الواجهة مدمجة — لا حاجة لأي ملف HTML)")
    print("=" * 46)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
