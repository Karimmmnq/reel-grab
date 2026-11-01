"""Reel Grab backend (Flask + yt-dlp + ffmpeg). Deploy with the included Dockerfile."""
import os, re, shutil, subprocess, tempfile
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

def pick_video(formats, cap=None):
    """Choose the highest-quality video format: biggest resolution, then biggest bitrate,
    then biggest file. Watermarked copies are skipped when a clean one exists."""
    vids = [f for f in formats if has(f, "vcodec") or (has(f, "acodec") and f.get("height"))]
    vids = [f for f in vids if f.get("height") or f.get("tbr")]
    clean = [f for f in vids if "watermark" not in
             ((f.get("format_note") or "") + " " + (f.get("format_id") or "")).lower()]
    vids = clean or vids
    if cap:
        vids = [f for f in vids if (f.get("height") or 0) <= cap] or vids
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
            best = pick_video(meta.get("formats", []), cap)
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
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error="Download failed. Try another quality."), 422

    @after_this_request
    def cleanup(resp):
        shutil.rmtree(tmp, ignore_errors=True)
        return resp

    name = re.sub(r"[^\w\- ]+", "", d.get("title") or d["id"])[:60].strip() or d["id"]
    return send_file(path, as_attachment=True, download_name=name + os.path.splitext(path)[1])

@app.route("/api/convert", methods=["POST", "OPTIONS"])
def convert():
    """Re-encode HEVC (or any phone video) to H.264 MP4, max 1080p / 30fps, so every browser can use it."""
    if request.method == "OPTIONS":
        return "", 204
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file."), 400
    tmp = tempfile.mkdtemp(prefix="rgconv_")
    src = os.path.join(tmp, "in" + (os.path.splitext(f.filename or "")[1][:6] or ".mp4"))
    out = os.path.join(tmp, "out.mp4")
    f.save(src)
    def num(key, default):
        try:
            return int(request.form.get(key, default))
        except (TypeError, ValueError):
            return default
    maxh = min(2160, max(240, num("maxh", 1080)))
    fps = num("fps", 30)
    secs = min(180, max(5, num("t", 30)))
    keep_audio = request.form.get("audio", "1") == "1"
    vf = "scale=-2:'min(%d,ih)'" % maxh + (",fps=%d" % fps if fps > 0 else "")
    cmd = ["ffmpeg", "-y", "-i", src, "-t", str(secs), "-vf", vf,
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "128k"] if keep_audio else ["-an"]
    cmd += ["-movflags", "+faststart", out]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=280)
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
