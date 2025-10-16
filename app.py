# app.py — Render (Flask + CORS + PyMuPDF) v2.5
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import base64, io, re
import fitz  # PyMuPDF

VERSION = "v2.5"

app = Flask(__name__, static_folder="static", static_url_path="/")
CORS(app)

# ---------- Utils ----------
def xref_to_dataurl(doc, xref):
    """Extraction robuste d'une image à partir d'un xref (extract_image puis Pixmap fallback)."""
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

def rect_to_dataurl(page, rect, scale=2.0):
    """
    Rendre un clip rectangulaire de la page en PNG base64.
    scale=2 => ~144 DPI ; ajuste si besoin (plus haut = plus net, plus lourd).
    """
    mat = fitz.Matrix(scale, scale)
    clip = fitz.Rect(rect).intersect(page.rect)
    if clip.is_empty:
        return None
    pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
    buf = io.BytesIO(pix.tobytes("png"))
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/png;base64," + b64

def overlap_ratio(a0, a1, b0, b1):
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    width = max(a1 - a0, 1e-6)
    return inter / width

# ---------- Routes basiques ----------
@app.get("/")
def index():
    try:
        return send_from_directory(app.static_folder, "index.html")
    except Exception:
        return jsonify({"ok": True, "msg": "Backend Render en ligne.", "version": VERSION})

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "version": VERSION})

# ---------- Extraction simple (inchangé) ----------
@app.route("/api/extract_images", methods=["GET", "POST", "OPTIONS"])
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

# ---------- Appariement “classique” (peut rester à 0 si pas de texte vectoriel) ----------
@app.route("/api/extract_pairs", methods=["GET", "POST", "OPTIONS"])
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

    # Ici, on essaye via texte vectoriel (peut renvoyer 0 si PDF scanné)
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
                        txt = " ".join(parts).strip()
                        raw_lines.append({"text": txt, "bbox": (x0, y0, x1, y1)})

        seen, xrefs = set(), []
        for info in page.get_images(full=True):
            xr = info[0]
            if xr not in seen:
                seen.add(xr)
                xrefs.append(xr)

        # BBox via get_image_rects
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

        # Pas d’appariement si pas de texte reconnu
        pairs_page = []
        stats.append({
            "page": page_index + 1,
            "lines_raw": len(raw_lines),
            "images_with_bbox": len(images_with_bbox),
            "pairs": len(pairs_page)
        })
        for p in pairs_page:
            pairs_all.append({"page": page_index + 1, **p})

    return jsonify({"pairs": pairs_all, "stats": stats, "version": VERSION})

# ---------- NOUVEAU : photo + crop de la zone du nom (pour OCR client) ----------
@app.route("/api/extract_photo_labels", methods=["POST", "OPTIONS"])
def extract_photo_labels():
    """
    Renvoie, pour chaque image, la photo + un crop (bandeau) juste sous la photo,
    afin de faire l'OCR côté client (Tesseract.js).
    """
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

        # Tous les xrefs d'images (dédupliqués)
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
            r = rects[0]  # on suppose 1 position par portrait
            img_w = r.width
            img_h = r.height

            # Photo (data URL, via xref)
            photo = xref_to_dataurl(doc, xr)
            if not photo:
                continue

            # Bandeau sous la photo: même largeur, hauteur ~30% de la hauteur photo (min 14pt, max 45%).
            band_h = max(14, min(img_h * 0.45, img_h * 0.30))  # borne pour éviter trop grand/petit
            label_rect = fitz.Rect(r.x0, r.y1, r.x1, r.y1 + band_h)
            label = rect_to_dataurl(page, label_rect, scale=2.0)

            page_items.append({
                "photo": photo,
                "label": label,
                "bbox": [r.x0, r.y0, r.x1, r.y1]
            })

        results.append({
            "page": page_index + 1,
            "items": page_items
        })

    return jsonify({"pages": results, "version": VERSION})
