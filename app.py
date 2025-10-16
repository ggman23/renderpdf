# app.py — Render (Flask + CORS + PyMuPDF) v2.6
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import base64, io, re
import fitz  # PyMuPDF

VERSION = "v2.6"

app = Flask(__name__, static_folder="static", static_url_path="/")
CORS(app)

# ---------------- Utils ----------------
def xref_to_dataurl(doc, xref):
    """Extract an embedded image by xref to a PNG/JPG data URL (robust fallback)."""
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
    """Render a rectangle clip of a page to PNG data URL at given scale."""
    clip = fitz.Rect(rect).intersect(page.rect)
    if clip.is_empty:
        return None
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
    buf = io.BytesIO(pix.tobytes("png"))
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/png;base64," + b64

def overlap_ratio(a0, a1, b0, b1):
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    width = max(a1 - a0, 1e-6)
    return inter / width

# ---------------- Basic routes ----------------
@app.get("/")
def index():
    try:
        return send_from_directory(app.static_folder, "index.html")
    except Exception:
        return jsonify({"ok": True, "msg": "Backend Render en ligne.", "version": VERSION})

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "version": VERSION})

# ---------------- Simple image extraction ----------------
@app.route("/api/extract_images", methods=["GET", "POST", "OPTIONS")
def extract_images():
    if request.method == "OPTIONS":
        return ("", 204)
    if request.method == "GET":
        return jsonify({"ok": True, "version": VERSION})

    if "pdf" not in request.files:
        return jsonify({"error": "missing file field 'pdf'"}), 400

    data = request.files["pdf"].read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"cannot open pdf: {e}"}), 400

    pages_out, total = [], 0
    for page_index in range(len(doc)):
        page = doc[page_index]
        seen, xrefs = set(), []
        for info in page.get_images(full=True):
            xr = info[0]
            if xr not in seen:
                seen.add(xr)
                xrefs.append(xr)

        images = []
        for xr in xrefs:
            dataurl = xref_to_dataurl(doc, xr)
            if dataurl:
                images.append(dataurl)
                total += 1

        pages_out.append({
            "page": page_index + 1,
            "images": images,
            "debug": {"xrefs": len(xrefs), "images_extracted": len(images)}
        })

    return jsonify({"pages": pages_out, "debug": {"pages": len(doc), "total_images": total}, "version": VERSION})

# ---------------- Vector-text pairing (may be empty on scanned PDFs) ----------------
@app.route("/api/extract_pairs", methods=["GET", "POST", "OPTIONS")
def extract_pairs():
    if request.method == "OPTIONS":
        return ("", 204)
    if request.method == "GET":
        return jsonify({"ok": True, "version": VERSION})

    if "pdf" not in request.files:
        return jsonify({"error": "missing file field 'pdf'"}), 400

    data = request.files["pdf"].read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"cannot open pdf: {e}"}), 400

    pairs_all, stats = [], []
    for page_index in range(len(doc)):
        page = doc[page_index]

        rd = page.get_text("rawdict")
        raw_lines = []
        for block in (rd.get("blocks") or []):
            if block.get("type") == 0:
                for line in (block.get("lines") or []):
                    parts, x0, y0, x1, y1 = [], None, None, None, None
                    for span in (line.get("spans") or []):
                        s = (span.get("text") or "").strip()
                        if not s:
                            continue
                        parts.append(s)
                        bx0, by0, bx1, by1 = span.get("bbox") or [0, 0, 0, 0]
                        x0 = bx0 if x0 is None else min(x0, bx0)
                        y0 = by0 if y0 is None else min(y0, by0)
                        x1 = bx1 if x1 is None else max(x1, bx1)
                        y1 = by1 if y1 is None else max(y1, by1)
                    if parts:
                        raw_lines.append({"text": " ".join(parts).strip(), "bbox": (x0, y0, x1, y1)})

        seen, xrefs = set(), []
        for info in page.get_images(full=True):
            xr = info[0]
            if xr not in seen:
                seen.add(xr)
                xrefs.append(xr)

        images_with_bbox = []
        for xr in xrefs:
            rects = page.get_image_rects(xr)
            if not rects:
                continue
            r = rects[0]
            bbox = (r.x0, r.y0, r.x1, r.y1)
            dataurl = xref_to_dataurl(doc, xr)
            if dataurl:
                images_with_bbox.append({"dataurl": dataurl, "bbox": bbox, "xref": xr})

        stats.append({
            "page": page_index + 1,
            "lines_raw": len(raw_lines),
            "images_with_bbox": len(images_with_bbox),
            "pairs": 0
        })

    return jsonify({"pairs": pairs_all, "stats": stats, "version": VERSION})

# ---------------- NEW: photo + two label crops (below/above) for client-side OCR ----------------
@app.route("/api/extract_photo_labels", methods=["POST", "OPTIONS")
def extract_photo_labels():
    if request.method == "OPTIONS":
        return ("", 204)

    if "pdf" not in request.files:
        return jsonify({"error": "missing file field 'pdf'"}), 400

    data = request.files["pdf"].read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"cannot open pdf: {e}"}), 400

    results = []
    for page_index in range(len(doc)):
        page = doc[page_index]

        seen, xrefs = set(), []
        for info in page.get_images(full=True):
            xr = info[0]
            if xr not in seen:
                seen.add(xr)
                xrefs.append(xr)

        page_items = []
        for xr in xrefs:
            rects = page.get_image_rects(xr)
            if not rects:
                continue
            r = rects[0]
            photo = xref_to_dataurl(doc, xr)
            if not photo:
                continue

            w = r.width
            h = r.height
            hx = w * 0.10
            gap = max(3.0, h * 0.03)
            band = max(14.0, min(h * 0.20, h * 0.22))

            below = fitz.Rect(r.x0 - hx, r.y1 + gap, r.x1 + hx, r.y1 + gap + band)
            above = fitz.Rect(r.x0 - hx, r.y0 - gap - band, r.x1 + hx, r.y0 - gap)

            label_below = rect_to_dataurl(page, below, scale=3.0)
            label_above = rect_to_dataurl(page, above, scale=3.0)

            page_items.append({
                "photo": photo,
                "bbox": [r.x0, r.y0, r.x1, r.y1],
                "label_below": label_below,
                "label_above": label_above
            })

        results.append({"page": page_index + 1, "items": page_items})

    return jsonify({"pages": results, "version": VERSION})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
