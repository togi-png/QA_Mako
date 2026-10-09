"""Email QA Bot: local web app.

Run:  python app.py      then open http://127.0.0.1:5050
"""
import json
import os
import re
import threading
import time
import uuid
import webbrowser
from datetime import datetime

from bs4 import BeautifulSoup
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

from qa.report import build_report
from qa.runner import run_batch
from qa.sources import load_cgen, load_locale_map, load_reference_links

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(BASE, "runs")
CONFIG = os.path.join(BASE, "config")
REF_PATH = os.path.join(CONFIG, "reference_links.xlsx")
LOCALE_MAP = os.path.join(CONFIG, "locale_map.csv")
os.makedirs(RUNS, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
STATE = {}  # run_id -> {"status", "done", "total", "current", "error"}


def _safe(name):
    return re.sub(r"[^\w.\-\[\] ]", "_", os.path.basename(name or "file"))[:180]


def _unique(name, taken):
    base, ext = os.path.splitext(name)
    n, out = 2, name
    while out in taken:
        out = f"{base} ({n}){ext}"
        n += 1
    taken.add(out)
    return out


def _ref_info():
    if not os.path.exists(REF_PATH):
        return None
    m = os.path.getmtime(REF_PATH)
    try:
        ref = load_reference_links(REF_PATH, "reference_links.xlsx", m)
        counts = (len(ref.footer), len(ref.social))
    except Exception:
        counts = (0, 0)
    return {"updated": datetime.fromtimestamp(m).strftime("%b %d, %Y"),
            "age_days": int((time.time() - m) / 86400), "footer": counts[0], "social": counts[1]}


def _list_runs():
    out = []
    for rid in sorted(os.listdir(RUNS), reverse=True):
        meta_p = os.path.join(RUNS, rid, "meta.json")
        if os.path.exists(meta_p):
            with open(meta_p) as fh:
                meta = json.load(fh)
            meta["id"] = rid
            out.append(meta)
    return out[:25]


@app.get("/")
def index():
    return render_template("index.html", ref=_ref_info(), runs=_list_runs(),
                           locale_pairs=sum(len(v) for v in load_locale_map(LOCALE_MAP).values()))


@app.post("/reference")
def upload_reference():
    f = request.files.get("reference")
    if f and f.filename.lower().endswith(".xlsx"):
        data = f.read()
        ref = load_reference_links(data, f.filename)  # validate before saving
        if not ref.footer and not ref.social:
            return render_template("message.html", title="Reference list not saved",
                                   body="No rows were found. The workbook needs a tab named 'Footer' and/or 'Social' "
                                        "with columns: locale, link label, accepted URL (and program on the Social tab).")
        os.makedirs(CONFIG, exist_ok=True)
        with open(REF_PATH, "wb") as fh:
            fh.write(data)
    return redirect(url_for("index"))


@app.get("/reference/download")
def download_reference():
    path = REF_PATH if os.path.exists(REF_PATH) else os.path.join(CONFIG, "reference_links_TEMPLATE.xlsx")
    return send_file(path, as_attachment=True)


@app.post("/run")
def start_run():
    rid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    rdir = os.path.join(RUNS, rid)
    os.makedirs(os.path.join(rdir, "inputs"), exist_ok=True)

    proofs, cgen_files, html_files, taken = [], [], {}, set()
    for f in request.files.getlist("proofs"):
        if f.filename:
            proofs.append((_unique(_safe(f.filename), taken), f.read()))
    for f in request.files.getlist("cgen"):
        if f.filename:
            cgen_files.append((_unique(_safe(f.filename), taken), f.read()))
    for f in request.files.getlist("html"):
        if f.filename:
            html_files[_safe(f.filename)] = f.read().decode("utf-8", "replace")
    follow = request.form.get("follow") == "on"
    for name, data in proofs + cgen_files:
        with open(os.path.join(rdir, "inputs", name), "wb") as fh:
            fh.write(data)

    STATE[rid] = {"status": "running", "done": 0, "total": len(proofs), "current": "", "error": ""}
    meta = {"started": datetime.now().strftime("%b %d, %Y %I:%M %p"), "proofs": len(proofs),
            "cgen": [n for n, _ in cgen_files], "html": list(html_files), "follow_links": follow}
    threading.Thread(target=_worker, args=(rid, rdir, proofs, cgen_files, html_files, follow, meta), daemon=True).start()
    return redirect(url_for("results", rid=rid))


def _worker(rid, rdir, proofs, cgen_files, html_files, follow, meta):
    try:
        cgen_rows, warnings = [], []
        for name, data in cgen_files:
            try:
                rows = load_cgen(data, name)
            except Exception as e:
                rows = []
                warnings.append(f"{name}: could not be read ({e})")
            else:
                if not rows:
                    warnings.append(f"{name}: no CGEN rows found. The header row needs 'activity id' and "
                                    f"'tag id/code' columns")
            cgen_rows += rows
        ref = load_reference_links(REF_PATH, "reference_links.xlsx", os.path.getmtime(REF_PATH)) \
            if os.path.exists(REF_PATH) else None
        lmap = load_locale_map(LOCALE_MAP)

        def progress(done, total, current):
            STATE[rid].update(done=done, total=total, current=current)

        batch = run_batch(proofs, cgen_rows, html_files, ref, lmap, follow_links=follow, progress=progress,
                          warnings=warnings)
        meta["proofs"] = len(batch)
        summary = {k: sum(1 for p in batch if p["status"] == k) for k in ("fail", "review", "pass")}
        meta.update(summary=summary, cgen_rows=len(cgen_rows), warnings=warnings, finished=datetime.now().strftime("%I:%M %p"),
                    reference_list=("reference_links.xlsx" if ref else "none"))
        with open(os.path.join(rdir, "results.json"), "w") as fh:
            json.dump(batch, fh)
        build_report(batch, os.path.join(rdir, "QA_report.xlsx"), meta)
        with open(os.path.join(rdir, "meta.json"), "w") as fh:
            json.dump(meta, fh)
        STATE[rid]["status"] = "done"
    except Exception as e:  # surface the error in the UI
        import traceback
        traceback.print_exc()
        STATE[rid].update(status="error", error=f"{type(e).__name__}: {e}")


@app.get("/run/<rid>")
def results(rid):
    if not re.fullmatch(r"[\w-]+", rid) or not os.path.isdir(os.path.join(RUNS, rid)):
        abort(404)
    return render_template("results.html", rid=rid)


@app.get("/api/run/<rid>")
def api_run(rid):
    rdir = os.path.join(RUNS, rid)
    res_p = os.path.join(rdir, "results.json")
    if os.path.exists(res_p) and STATE.get(rid, {}).get("status", "done") == "done":
        with open(res_p) as fh:
            batch = json.load(fh)
        with open(os.path.join(rdir, "meta.json")) as fh:
            meta = json.load(fh)
        for p in batch:
            p.pop("html", None)
        return jsonify({"status": "done", "meta": meta, "batch": batch})
    st = STATE.get(rid)
    if not st:
        return jsonify({"status": "error", "error": "This run was interrupted (the app was closed). Run it again."})
    return jsonify(st)


@app.get("/run/<rid>/report.xlsx")
def report(rid):
    p = os.path.join(RUNS, rid, "QA_report.xlsx")
    if not re.fullmatch(r"[\w-]+", rid) or not os.path.exists(p):
        abort(404)
    return send_file(p, as_attachment=True, download_name=f"QA_report_{rid}.xlsx")


@app.get("/run/<rid>/preview/<int:n>")
def preview(rid, n):
    """Proof HTML for the preview pane: no scripts, no tracking pixel, links disabled (no fake clicks)."""
    p = os.path.join(RUNS, rid, "results.json")
    if not re.fullmatch(r"[\w-]+", rid) or not os.path.exists(p):
        abort(404)
    with open(p) as fh:
        batch = json.load(fh)
    if n >= len(batch):
        abort(404)
    soup = BeautifulSoup(batch[n].get("html") or "<p>No HTML</p>", "html.parser")
    for t in soup(["script", "iframe", "object", "embed"]):
        t.decompose()
    for img in soup.find_all("img"):
        if "/r/?id=" in (img.get("src") or ""):
            img.decompose()
    for a in soup.find_all("a"):
        a["title"] = a.get("originalsrc") or a.get("href") or ""
        a["href"] = "#"
    resp = app.response_class(str(soup), mimetype="text/html")
    resp.headers["Content-Security-Policy"] = "script-src 'none'; frame-ancestors 'self'"
    return resp


@app.get("/help")
def help_page():
    return render_template("help.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    host = os.environ.get("HOST", "127.0.0.1")   # local only by default
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"\n  Email QA Bot running at http://127.0.0.1:{port}  (close this window to stop)\n")
    app.run(host=host, port=port, debug=False, threaded=True)
