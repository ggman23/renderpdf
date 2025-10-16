# app.py — Render (Flask + CORS + PyMuPDF) v2.4
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import base64, io, re
import fitz  # PyMuPDF

VERSION = "v2.4"

app = Flask(__name__, static_folder="static", static_url_path="/")
CORS(app)

# assez large: lettres, accents, apostrophes/traits d’union, éventuellement chiffres
LINE_RX = re.compile(r"[A-Za-zÀ-ÿ0-9'’\- ]{2,}")

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

def overlap_ratio(a0, a1, b0, b1):
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    width = max(a1 - a0, 1e-6)
    return inter / width

@app.get("/")
def index():
    try:
        return send_from_directory(app.static_folder, "index.html")
    except Exception:
        return jsonify({"ok": True, "msg": "Backend Render en ligne.", "version": VERSION})

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "version": VERSION})

# ----- extraction simple (inchangé) -----
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

# ----- appariement image -> nom (avec groupement de lignes) -----
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

        # --- 1) lignes de texte brutes ---
        rd = page.get_text("rawdict")
        raw_lines = []  # [{text, bbox}]
        for block in (rd.get("blocks") or []):
            if block.get("type") == 0:
                for line in (block.get("lines") or []):
                    parts, x0, y0, x1, y1 = [], None, None, None, None
                    for span in (line.get("spans") or []):
                        s = (span.get("text") or "").strip()
                        if not s:
                            continue
                        if not LINE_RX.search(s):
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

        # --- 2) regroupement de lignes empilées (NOM + PRÉNOM) ---
        raw_lines.sort(key=lambda t: (t["bbox"][1], t["bbox"][0]))  # tri par y puis x
        grouped = []
        for ln in raw_lines:
            merged = False
            # essaie de fusionner avec le dernier groupe si proche en Y et bon chevauchement horizontal
            if grouped:
                g = grouped[-1]
                gx0, gy0, gx1, gy1 = g["bbox"]
                lx0, ly0, lx1, ly1 = ln["bbox"]
                # proximité verticale (petit saut entre lignes)
                vgap = ly0 - gy1
                # grille Pronote: on tolère ~2.5% de la hauteur page + 4px
                vlimit = max(page.rect.height * 0.025, 4)
                if vgap >= -2 and vgap <= vlimit and overlap_ratio(gx0, gx1, lx0, lx1) >= 0.4:
                    # fusion
                    g["text"] = (g["text"] + " " + ln["text"]).strip()
                    g["bbox"] = (min(gx0, lx0), min(gy0, ly0), max(gx1, lx1), max(gy1, ly1))
                    merged = True
            if not merged:
                grouped.append(dict(text=ln["text"], bbox=ln["bbox"]))

        # --- 3) toutes les images + BBOX via get_image_rects ---
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

        # --- 4) appariement ---
        pairs_page = []
        used = [False] * len(grouped)
        dy_limit = max(page.rect.height * 0.09, 16)  # fenêtre verticale un peu plus large

        for im in images_with_bbox:
            ix0, iy0, ix1, iy1 = im["bbox"]
            best_j, best_score = -1, 1e9

            # a) candidats sous la photo avec chevauchement horizontal
            for j, tl in enumerate(grouped):
                if used[j]:
                    continue
                tx0, ty0, tx1, ty1 = tl["bbox"]
                if ty0 < iy1:
                    continue
                dy = ty0 - iy1
                if dy > dy_limit:
                    continue
                ov = overlap_ratio(ix0, ix1, tx0, tx1)
                if ov < 0.22:
                    continue
                score = dy - (ov * 6.0)
                if score < best_score:
                    best_score, best_j = score, j

            # b) fallback: plus proche verticalement (au-dessus/au-dessous) avec léger chevauchement
            if best_j == -1:
                nearest_j, nearest_d = -1, 1e9
                for j, tl in enumerate(grouped):
                    if used[j]:
                        continue
                    tx0, ty0, tx1, ty1 = tl["bbox"]
                    cy_img = 0.5 * (iy0 + iy1)
                    cy_txt = 0.5 * (ty0 + ty1)
                    d = abs(cy_txt - cy_img)
                    if overlap_ratio(ix0, ix1, tx0, tx1) < 0.12:
                        continue
                    if d < nearest_d:
                        nearest_d, nearest_j = d, j
                best_j = nearest_j

            if best_j != -1:
                used[best_j] = True
                name = grouped[best_j]["text"].strip()
                pairs_page.append({
                    "name": name,
                    "photo": im["dataurl"],
                    "img_bbox": im["bbox"],
                    "txt_bbox": grouped[best_j]["bbox"]
                })

        stats.append({
            "page": page_index + 1,
            "lines_raw": len(raw_lines),
            "lines_grouped": len(grouped),
            "images_with_bbox": len(images_with_bbox),
            "pairs": len(pairs_page)
        })
        for p in pairs_page:
            pairs_all.append({"page": page_index + 1, **p})

    return jsonify({"pairs": pairs_all, "stats": stats, "version": VERSION})
