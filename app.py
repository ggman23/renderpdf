# app.py — Render : extraction robuste + appariement quand bbox dispo
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import base64, io, re
import fitz  # PyMuPDF

app = Flask(__name__, static_folder="static", static_url_path="/")
CORS(app)

NAME_RX = re.compile(r"[A-Za-zÀ-ÿ'’\- ]{3,}")

@app.get("/")
def index():
    try:
        return send_from_directory(app.static_folder, "index.html")
    except Exception:
        return jsonify({"ok": True, "msg": "Backend Render en ligne. POST /api/extract_images avec un champ 'pdf'."})

@app.get("/api/extract_images")
def ok():
    return jsonify({"ok": True})

def xref_to_dataurl(doc, xref):
    # 1) extract_image
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
    # 2) Pixmap fallback
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

@app.post("/api/extract_images")
def extract_images():
    if "pdf" not in request.files:
        return jsonify({"error": "missing file field 'pdf'"}), 400

    data = request.files["pdf"].read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"cannot open pdf: {e}"}), 400

    pages_out = []
    pairs_all = []
    total_images = 0

    for page_index in range(len(doc)):
        page = doc[page_index]

        # --- 1) TOUS LES XREFS (garanti) ---
        xrefs_all = []
        seen = set()
        for info in page.get_images(full=True):
            xr = info[0]
            if xr not in seen:
                seen.add(xr)
                xrefs_all.append(xr)

        # --- 2) BBOX DISPO (quand présent dans rawdict) ---
        rd = page.get_text("rawdict")
        xref_to_bbox = {}      # xref -> (x0,y0,x1,y1)
        text_lines = []        # [{text, bbox}]
        for block in (rd.get("blocks") or []):
            t = block.get("type")
            if t == 1:  # image avec bbox
                xr = block.get("xref")
                bbox = tuple(block.get("bbox") or [0,0,0,0])
                if xr:
                    xref_to_bbox[xr] = bbox
            elif t == 0:  # texte
                for line in block.get("lines") or []:
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
                        if NAME_RX.search(text) and len(text.split()) >= 2:
                            text_lines.append({"text": text, "bbox": (x0, y0, x1, y1)})

        # --- 3) EXTRACTION DES IMAGES (comme avant, pour ne rien perdre) ---
        images_data_all = []         # toutes les images (même sans bbox)
        images_with_bbox = []        # seulement celles avec bbox (pour appariement)
        for xr in xrefs_all:
            dataurl = xref_to_dataurl(doc, xr)
            if not dataurl:
                continue
            total_images += 1
            images_data_all.append(dataurl)
            if xr in xref_to_bbox:
                images_with_bbox.append({"dataurl": dataurl, "bbox": xref_to_bbox[xr], "xref": xr})

        # --- 4) APPARIEMENT uniquement si bbox dispo ---
        pairs_page = []
        if images_with_bbox and text_lines:
            used = [False] * len(text_lines)
            dy_limit = max(page.rect.height * 0.06, 12)
            for im in images_with_bbox:
                ix0, iy0, ix1, iy1 = im["bbox"]
                best_j, best_score, best_overlap = -1, 1e9, 0.0

                # priorité aux noms "en dessous"
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
                    if overlap < 0.3:
                        continue
                    score = dy - (overlap * 5.0)
                    if score < best_score:
                        best_score, best_overlap, best_j = score, overlap, j

                # fallback: plus proche verticalement avec petit chevauchement
                if best_j == -1:
                    nearest_j, nearest_d = -1, 1e9
                    for j, tl in enumerate(text_lines):
                        if used[j]:
                            continue
                        tx0, ty0, tx1, ty1 = tl["bbox"]
                        cy_img = 0.5 * (iy0 + iy1)
                        cy_txt = 0.5 * (ty0 + ty1)
                        d = abs(cy_txt - cy_img)
                        overlap = overlap_ratio(ix0, ix1, tx0, tx1)
                        if overlap < 0.2:
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

        pages_out.append({
            "page": page_index + 1,
            "images": images_data_all,              # TOUJOURS renvoyé (comme avant)
            "names": [tl["text"] for tl in text_lines],
            "debug": {
                "xrefs_total": len(xrefs_all),
                "xrefs_with_bbox": len(xref_to_bbox),
                "text_lines": len(text_lines),
                "pairs": len(pairs_page)
            }
        })
        # on stocke toutes les paires trouvées
        for p in pairs_page:
            pairs_all.append({"page": page_index + 1, **p})

    return jsonify({
        "pages": pages_out,
        "pairs": pairs_all,   # {name, photo} quand bbox dispo
        "debug": {
            "pages": len(doc),
            "total_images": total_images
        }
    })
