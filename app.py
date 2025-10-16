# app.py — Render (Flask + CORS + PyMuPDF) v2.3
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import base64, io, re
import fitz  # PyMuPDF

VERSION = "v2.3"

app = Flask(__name__, static_folder="static", static_url_path="/")
CORS(app)

NAME_RX = re.compile(r"[A-Za-zÀ-ÿ'’\- ]{3,}")

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

# ---------- Extraction simple ----------
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

# ---------- Appariement (images -> noms) ----------
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

    pairs_all, stats = [], []

    for page_index in range(len(doc)):
        page = doc[page_index]

        # 1) TEXTE (candidats noms) via rawdict
        rd = page.get_text("rawdict")
        text_lines = []
        for block in (rd.get("blocks") or []):
            if block.get("type") == 0:
                for line in (block.get("lines") or []):
                    parts, x0, y0, x1, y1 = [], None, None, None, None
                    for span in (line.get("spans") or []):
                        s = (span.get("text") or "").strip()
                        if not s:
                            continue
                        parts.append(s)
                        bx0, by0, bx1, by1 = span.get("bbox") or [0,0,0,0]
                        x0 = bx0 if x0 is None else min(x0, bx0)
                        y0 = by0 if y0 is None else min(y0, by0)
                        x1 = bx1 if x1 is None else max(x1, bx1)
                        y1 = by1 if y1 is None else max(y1, by1)
                    if parts:
                        text = " ".join(parts).strip()
                        # on garde large (noms/prénoms, accents, tirets)
                        if NAME_RX.search(text) and len(text.split()) >= 2:
                            text_lines.append({"text": text, "bbox": (x0, y0, x1, y1)})

        # 2) IMAGES: xrefs (garanti) + BBOX par get_image_rects(xref)
        seen, xrefs = set(), []
        for info in page.get_images(full=True):
            xr = info[0]
            if xr not in seen:
                seen.add(xr)
                xrefs.append(xr)

        images_with_bbox = []
        for xr in xrefs:
            rects = page.get_image_rects(xr)  # <- clé : récupère les rectangles où l'image est affichée
            if not rects:
                continue
            # on prend le premier rectangle (cas général trombinoscope)
            r = rects[0]
            bbox = (r.x0, r.y0, r.x1, r.y1)
            dataurl = xref_to_dataurl(doc, xr)
            if dataurl:
                images_with_bbox.append({"dataurl": dataurl, "bbox": bbox, "xref": xr})

        # 3) MATCHING image -> texte
        pairs_page = []
        used = [False] * len(text_lines)
        dy_limit = max(page.rect.height * 0.08, 14)  # un peu plus large qu'avant

        for im in images_with_bbox:
            ix0, iy0, ix1, iy1 = im["bbox"]
            best_j, best_score = -1, 1e9

            # a) priorité aux noms sous la photo avec chevauchement horizontal
            for j, tl in enumerate(text_lines):
                if used[j]:
                    continue
                tx0, ty0, tx1, ty1 = tl["bbox"]
                if ty0 < iy1:
                    continue
                dy = ty0 - iy1
                if dy > dy_limit:
                    continue
                overlap = overlap_ratio(ix0, ix1, tx0, tx1)
                if overlap < 0.25:
                    continue
                score = dy - (overlap * 6.0)  # favorise fort chevauchement
                if score < best_score:
                    best_score, best_j = score, j

            # b) fallback : plus proche verticalement (au-dessus/au-dessous) avec petit chevauchement
            if best_j == -1:
                nearest_j, nearest_d = -1, 1e9
                for j, tl in enumerate(text_lines):
                    if used[j]:
                        continue
                    tx0, ty0, tx1, ty1 = tl["bbox"]
                    cy_img = 0.5 * (iy0 + iy1)
                    cy_txt = 0.5 * (ty0 + ty1)
                    d = abs(cy_txt - cy_img)
                    if overlap_ratio(ix0, ix1, tx0, tx1) < 0.15:
                        continue
                    if d < nearest_d:
                        nearest_d, nearest_j = d, j
                best_j = nearest_j

            if best_j != -1:
                used[best_j] = True
                name = text_lines[best_j]["text"].strip()
                pairs_page.append({
                    "name": name,
                    "photo": im["dataurl"],
                    "img_bbox": im["bbox"],
                    "txt_bbox": text_lines[best_j]["bbox"]
                })

        stats.append({
            "page": page_index + 1,
            "text_lines": len(text_lines),
            "xrefs": len(xrefs),
            "images_with_bbox": len(images_with_bbox),
            "pairs": len(pairs_page)
        })
        for p in pairs_page:
            pairs_all.append({"page": page_index + 1, **p})

    return jsonify({"pairs": pairs_all, "stats": stats, "version": VERSION})
