"""
Reel Grab backend (deploy on Render / Railway / Fly.io / any Docker host).
Setup:   pip install flask yt-dlp
         (install ffmpeg too: needed only to join separate video+audio streams without re-encoding)
Run:     start.bat (Windows) / ./start.sh (Mac, Linux)  ->  http://localhost:5000
"""
import os, re, shutil, tempfile
from flask import Flask, request, jsonify, send_file, send_from_directory, after_this_request
import yt_dlp

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))

@app.route("/")
def home():
    return jsonify(status="ok")

ALLOWED = re.compile(r"^https?://([\w-]+\.)*(tiktok\.com|instagram\.com|instagr\.am)/", re.I)

@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = os.environ.get("ALLOWED_ORIGIN", "*")
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Length"
    return resp

def check(url):
    return bool(url and ALLOWED.match(url))

# Best video + best audio, copied as-is (stream copy, never re-encoded).
FMT = "bestvideo*+bestaudio/best"

@app.route("/api/info", methods=["POST", "OPTIONS"])
def info():
    if request.method == "OPTIONS":
        return "", 204
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not check(url):
        return jsonify(error="Paste a TikTok or Instagram link."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "format": FMT}) as y:
            d = y.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error="Couldn't read that link. It may be private or removed."), 422
    return jsonify(
        title=d.get("title") or "Untitled",
        author=d.get("uploader") or d.get("channel") or "",
        thumbnail=d.get("thumbnail"),
        duration=d.get("duration"),
        width=d.get("width"), height=d.get("height"),
        fps=d.get("fps"), ext=d.get("ext"),
    )

@app.route("/api/download", methods=["GET", "OPTIONS"])
def download():
    if request.method == "OPTIONS":
        return "", 204
    url = request.args.get("url", "").strip()
    if not check(url):
        return jsonify(error="Invalid link."), 400
    tmp = tempfile.mkdtemp(prefix="reelgrab_")
    opts = {
        "quiet": True, "format": FMT,
        "merge_output_format": "mp4",          # remux only, no re-encode
        "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            d = y.extract_info(url, download=True)
            path = y.prepare_filename(d)
            if not os.path.exists(path):
                path = os.path.splitext(path)[0] + ".mp4"
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return jsonify(error="Download failed."), 422

    @after_this_request
    def cleanup(resp):
        shutil.rmtree(tmp, ignore_errors=True)
        return resp

    name = re.sub(r"[^\w\- ]+", "", d.get("title") or d.get("id"))[:60].strip() or d["id"]
    return send_file(path, as_attachment=True, download_name=f"{name}{os.path.splitext(path)[1]}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
