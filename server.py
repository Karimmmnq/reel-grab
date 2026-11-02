"""Reel Grab backend (Flask + yt-dlp + ffmpeg). Deploy with the included Dockerfile."""
import json, os, re, shutil, subprocess, tempfile, threading, time, uuid
from flask import Flask, request, jsonify, send_file, after_this_request
import yt_dlp

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 400 * 1024 * 1024
ALLOWED = re.compile(r"^https?://([\w-]+\.)*(tiktok\.com|instagram\.com|instagr\.am)/", re.I)

@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = os.environ.get("ALLOWED_ORIGIN", "*")
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Length"
    return resp

@app.route("/")
def home():
    return jsonify(status="ok")

def valid(url):
    return bool(url and ALLOWED.match(url))

def has(f, key):
    return f.get(key) not in (None, "none")

def vkind(f):
    """Classify a format's video codec: h264, hevc (H.265) or other."""
    c = ((f.get("vcodec") or "") + " " + (f.get("format_id") or "")).lower()
    if "bytevc1" in c or c.startswith(("hvc", "hev", "h265")) or " hvc" in c or " hev" in c or "h265" in c:
        return "hevc"
    if c.startswith(("avc", "h264")) or "h264" in c or " avc" in c:
        return "h264"
    return "other"

def probe_codec(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name", "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return r.stdout.strip().split(",")[0].lower()
    except Exception:
        return ""

def ensure_codec(path, codec, tmp):
    """For 'h264': if the file turned out to be H.265 (or anything else), convert it so it plays everywhere."""
    if codec != "h264" or probe_codec(path) in ("h264", ""):
        return path
    out = os.path.join(tmp, "h264.mp4")
    for audio in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "192k"]):
        try:
            subprocess.run(["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
                            "-pix_fmt", "yuv420p", *audio, "-movflags", "+faststart", out],
                           check=True, capture_output=True, timeout=240)
            return out
        except Exception:
            continue
    return path

def pick_video(formats, cap=None, codec="h264"):
    """Choose the highest-quality video format: biggest resolution, then biggest bitrate,
    then biggest file. Watermarked copies are skipped when a clean one exists."""
    vids = [f for f in formats if has(f, "vcodec") or (has(f, "acodec") and f.get("height"))]
    vids = [f for f in vids if f.get("height") or f.get("tbr")]
    clean = [f for f in vids if "watermark" not in
             ((f.get("format_note") or "") + " " + (f.get("format_id") or "")).lower()]
    vids = clean or vids
    if cap:
        vids = [f for f in vids if (f.get("height") or 0) <= cap] or vids
    if codec in ("h264", "hevc"):
        vids = [f for f in vids if vkind(f) == codec] or vids
    if not vids:
        return None
    return max(vids, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0,
                                    f.get("filesize") or f.get("filesize_approx") or 0))

@app.route("/api/info", methods=["POST", "OPTIONS"])
def info():
    if request.method == "OPTIONS":
        return "", 204
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not valid(url):
        return jsonify(error="Paste a TikTok or Instagram link."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as y:
            d = y.extract_info(url, download=False)
    except Exception:
        return jsonify(error="Couldn't read that link. It may be private or removed."), 422
    heights = sorted({f["height"] for f in d.get("formats", [])
                      if f.get("height") and has(f, "vcodec")}, reverse=True)
    best = pick_video(d.get("formats", []))
    return jsonify(
        title=d.get("title") or "Untitled", author=d.get("uploader") or d.get("channel") or "",
        thumbnail=d.get("thumbnail"), duration=d.get("duration"),
        width=d.get("width"), height=(best or {}).get("height") or d.get("height"),
        fps=d.get("fps"), heights=heights,
    )

@app.route("/api/download", methods=["GET", "OPTIONS"])
def download():
    if request.method == "OPTIONS":
        return "", 204
    a = request.args
    url = a.get("url", "").strip()
    if not valid(url):
        return jsonify(error="Invalid link."), 400
    kind = a.get("type", "video")
    codec = a.get("codec", "h264")
    codec = codec if codec in ("h264", "hevc", "any") else "h264"
    tmp = tempfile.mkdtemp(prefix="reelgrab_")
    opts = {"quiet": True, "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s")}

    if kind == "audio":
        abr = a.get("abr", "320")
        abr = abr if abr in ("128", "192", "256", "320") else "320"
        opts.update(format="bestaudio/best", postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": abr}])
        ext = ".mp3"
    else:
        cap = int(a["height"]) if a.get("height", "").isdigit() else None
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as y:
                meta = y.extract_info(url, download=False)
            best = pick_video(meta.get("formats", []), cap, codec)
        except Exception:
            best = None
        if best:
            fid = best["format_id"]
            print(f"[reelgrab] picked {fid}: {best.get('width')}x{best.get('height')} "
                  f"tbr={best.get('tbr')} vcodec={best.get('vcodec')} note={best.get('format_note')}", flush=True)
            opts["format"] = (f"{fid}/bv*+ba/b" if has(best, "acodec")
                              else f"{fid}+ba/{fid}/bv*+ba/b")
        else:
            opts["format"] = "bv*+ba/b"
        opts["merge_output_format"] = "mp4"       # remux only, no re-encode
        ext = ".mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as y:
            d = y.extract_info(url, download=True)
        path = os.path.join(tmp, d["id"] + ext)
        if not os.path.exists(path):
            path = next(os.path.join(tmp, f) for f in os.listdir(tmp))
        if kind != "audio":
            path = ensure_codec(path, codec, tmp)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error="Download failed. Try another quality."), 422

    @after_this_request
    def cleanup(resp):
        shutil.rmtree(tmp, ignore_errors=True)
        return resp

    name = re.sub(r"[^\w\- ]+", "", d.get("title") or d["id"])[:60].strip() or d["id"]
    return send_file(path, as_attachment=True, download_name=name + os.path.splitext(path)[1])

JOBS = os.path.join(tempfile.gettempdir(), "rgjobs")
os.makedirs(JOBS, exist_ok=True)

def parse_opts(form):
    def num(key, default):
        try:
            return int(form.get(key, default))
        except (TypeError, ValueError):
            return default
    return {"maxh": min(2160, max(240, num("maxh", 1080))), "fps": num("fps", 30),
            "secs": min(180, max(5, num("t", 30))), "audio": form.get("audio", "1") == "1",
            "hq": form.get("q", "std") == "high"}

def make_cmd(src, out, o, extra=()):
    vf = "scale=-2:'min(%d,ih)'" % o["maxh"] + (",fps=%d" % o["fps"] if o["fps"] > 0 else "")
    cmd = ["ffmpeg", "-y", *extra, "-i", src, "-t", str(o["secs"]), "-vf", vf,
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "15" if o.get("hq") else "21", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "128k"] if o["audio"] else ["-an"]
    return cmd + ["-movflags", "+faststart", out]

def write_status(jid, **kw):
    p = os.path.join(JOBS, jid, "status.json")
    with open(p + ".tmp", "w") as fh:
        json.dump(kw, fh)
    os.replace(p + ".tmp", p)

def sweep():
    now = time.time()
    for d in os.listdir(JOBS):
        p = os.path.join(JOBS, d)
        try:
            if now - os.path.getmtime(p) > 1800:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass

def run_job(jid, o):
    d = os.path.join(JOBS, jid)
    src, out = os.path.join(d, "in" + o["ext"]), os.path.join(d, "out.mp4")
    try:
        total = float(o["secs"])
        try:
            pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                 "default=nw=1:nk=1", src], capture_output=True, text=True, timeout=60)
            total = max(1.0, min(float(pr.stdout.strip()), total))
        except Exception:
            pass
        write_status(jid, state="running", pct=0)
        proc = subprocess.Popen(make_cmd(src, out, o, ("-progress", "pipe:1", "-nostats")),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        timer = threading.Timer(900, proc.kill)
        timer.start()
        last = 0
        for line in proc.stdout:
            if line.startswith("out_time_ms="):
                try:
                    pct = min(99, int(int(line.split("=")[1]) / 1e6 / total * 100))
                except ValueError:
                    continue
                if pct != last:
                    last = pct
                    write_status(jid, state="running", pct=pct)
        proc.wait()
        timer.cancel()
        ok = proc.returncode == 0 and os.path.exists(out)
        write_status(jid, state="done" if ok else "error", pct=100 if ok else 0)
    except Exception:
        write_status(jid, state="error", pct=0)

@app.route("/api/convert/start", methods=["POST", "OPTIONS"])
def convert_start():
    """Start a background conversion and return a job id so the page can show real progress."""
    if request.method == "OPTIONS":
        return "", 204
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file."), 400
    sweep()
    jid = uuid.uuid4().hex[:16]
    os.makedirs(os.path.join(JOBS, jid))
    o = parse_opts(request.form)
    o["ext"] = os.path.splitext(f.filename or "")[1][:6] or ".mp4"
    f.save(os.path.join(JOBS, jid, "in" + o["ext"]))
    write_status(jid, state="queued", pct=0)
    threading.Thread(target=run_job, args=(jid, o), daemon=True).start()
    return jsonify(id=jid)

@app.route("/api/convert/status/<jid>")
def convert_status(jid):
    if not re.fullmatch(r"[0-9a-f]{16}", jid):
        return jsonify(error="bad id"), 400
    try:
        with open(os.path.join(JOBS, jid, "status.json")) as fh:
            return jsonify(json.load(fh))
    except Exception:
        return jsonify(state="error", pct=0), 404

@app.route("/api/convert/result/<jid>")
def convert_result(jid):
    out = os.path.join(JOBS, jid, "out.mp4")
    if not re.fullmatch(r"[0-9a-f]{16}", jid) or not os.path.exists(out):
        return jsonify(error="Not found."), 404

    @after_this_request
    def cleanup(resp):
        shutil.rmtree(os.path.join(JOBS, jid), ignore_errors=True)
        return resp

    return send_file(out, mimetype="video/mp4", as_attachment=True, download_name="converted.mp4")

@app.route("/api/convert", methods=["POST", "OPTIONS"])
def convert():
    """Single-request converter (kept for older pages)."""
    if request.method == "OPTIONS":
        return "", 204
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file."), 400
    tmp = tempfile.mkdtemp(prefix="rgconv_")
    src = os.path.join(tmp, "in" + (os.path.splitext(f.filename or "")[1][:6] or ".mp4"))
    out = os.path.join(tmp, "out.mp4")
    f.save(src)
    try:
        subprocess.run(make_cmd(src, out, parse_opts(request.form)), check=True, capture_output=True, timeout=280)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error="Conversion failed."), 422

    @after_this_request
    def cleanup(resp):
        shutil.rmtree(tmp, ignore_errors=True)
        return resp

    return send_file(out, mimetype="video/mp4", as_attachment=True, download_name="converted.mp4")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
