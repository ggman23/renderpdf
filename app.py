# app.py — Flask on Render (v2.7) with PDF extraction + image proxy
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import base64, io
import fitz  # PyMuPDF
import requests
from urllib.parse import urlparse

VERSION = "v2.7"

app = Flask(__name__, static_folder="static", static_url_path="/")
CORS(app)

# ---------- Helpers ----------
def xref_to_dataurl(doc, xref):
    try:
        meta = doc.extract_image(xref)
        if meta and "image" in meta:
            ext = (meta.get("ext") or "png").lower()
            if ext not in ("png", "jpg", "jpeg"):
                ext = "png"
            b64 = base64.b64encode(meta["image"]).decode("ascii")
            return f"data:image/{ext};base64,{b64}"
    except Exception:
        pass
    try:
        pix = fitz.Pixmap(doc, xref)
        if pix.n >= 4 and pix.alpha == 0:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        buf = io.BytesIO()
        pix.save(buf, format="png")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return "data:image/png;base64," + b64
    except Exception:
        return None

def rect_to_dataurl(page, rect, scale=3.0):
    clip = fitz.Rect(rect).intersect(page.rect)
    if clip.is_empty:
        return None
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
    buf = io.BytesIO(pix.tobytes("png"))
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/png;base64," + b64

# ---------- Basic ----------
@app.get("/")
def index():
    try:
        return send_from_directory(app.static_folder, "index.html")
    except Exception:
        return jsonify({"ok": True, "msg": "Backend OK", "version": VERSION})

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "version": VERSION})

# ---------- Simple: extract embedded images ----------
@app.route("/api/extract_images", methods=["GET", "POST", "OPTIONS"])
def extract_images():
    if request.method == "OPTIONS":
        return ("", 204)
    if request.method == "GET":
        return jsonify({"ok": True, "version": VERSION})
    if "pdf" not in request.files:
        return jsonify({"error": "missing 'pdf'"}), 400
    data = request.files["pdf"].read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"cannot open pdf: {e}"}), 400
    pages_out, total = [], 0
    for i in range(len(doc)):
        page = doc[i]
        seen, xrefs = set(), []
        for info in page.get_images(full=True):
            xr = info[0]
            if xr not in seen:
                seen.add(xr); xrefs.append(xr)
        images = []
        for xr in xrefs:
            du = xref_to_dataurl(doc, xr)
            if du:
                images.append(du); total += 1
        pages_out.append({"page": i+1, "images": images})
    return jsonify({"pages": pages_out, "total": total, "version": VERSION})

# ---------- Photo + label crops (client OCR) ----------
@app.post("/api/extract_photo_labels")
def extract_photo_labels():
    if "pdf" not in request.files:
        return jsonify({"error": "missing 'pdf'"}), 400
    data = request.files["pdf"].read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"cannot open pdf: {e}"}), 400
    results = []
    for pi in range(len(doc)):
        page = doc[pi]
        seen, xrefs = set(), []
        for info in page.get_images(full=True):
            xr = info[0]
            if xr not in seen:
                seen.add(xr); xrefs.append(xr)
        items = []
        for xr in xrefs:
            rects = page.get_image_rects(xr)
            if not rects: continue
            r = rects[0]
            photo = xref_to_dataurl(doc, xr)
            if not photo: continue
            w, h = r.width, r.height
            hx = w * 0.10
            gap = max(3.0, h * 0.03)
            band = max(14.0, min(h * 0.20, h * 0.22))
            below = fitz.Rect(r.x0 - hx, r.y1 + gap, r.x1 + hx, r.y1 + gap + band)
            above = fitz.Rect(r.x0 - hx, r.y0 - gap - band, r.x1 + hx, r.y0 - gap)
            label_below = rect_to_dataurl(page, below, scale=3.0)
            label_above = rect_to_dataurl(page, above, scale=3.0)
            items.append({"photo": photo, "bbox": [r.x0, r.y0, r.x1, r.y1], "label_below": label_below, "label_above": label_above})
        results.append({"page": pi+1, "items": items})
    return jsonify({"pages": results, "version": VERSION})

# ---------- NEW: Proxy image (convert cross-origin Pronote URLs -> same-origin bytes) ----------
ALLOWED_HOST_SUFFIX = "index-education.net"

@app.get("/api/proxy_image")
def proxy_image():
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error": "missing url"}), 400
    u = urlparse(url)
    if u.scheme not in ("http","https") or not u.netloc.endswith(ALLOWED_HOST_SUFFIX):
        return jsonify({"error": "forbidden host"}), 403
    try:
        r = requests.get(url, timeout=12, stream=True)
        content = r.content
        ct = r.headers.get("Content-Type", "image/jpeg")
        return Response(content, headers={"Content-Type": ct, "Cache-Control": "no-store"} , status=r.status_code)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
