# -*- coding: utf-8 -*-
"""
ALFRED-M — Serveur web du panneau admin.
Reutilise les fonctions et les donnees du bot (bot.py) : memes archives,
meme generateur de rapport, meme IA. Ne demarre PAS le bot Telegram
(bot.py est protege par `if __name__ == "__main__"`).

Lancement :  python app.py   (ou via gunicorn / systemd)
Config (.env) :
    WEB_USER=admin
    WEB_PASS=ton_mot_de_passe        # OBLIGATOIRE pour se connecter
    WEB_SECRET=une_chaine_aleatoire  # optionnel (sinon genere au demarrage)
    WEB_PORT=8000                    # optionnel
"""
import os
import asyncio
import datetime
import mimetypes

from flask import (Flask, request, session, jsonify, send_file,
                   redirect, Response, abort)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

import bot  # reutilise les donnees + l'IA + le generateur de rapport

BASE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.secret_key = os.getenv("WEB_SECRET") or os.urandom(24).hex()

WEB_USER = os.getenv("WEB_USER", "admin")
WEB_PASS = os.getenv("WEB_PASS", "")          # doit etre defini dans .env
COMPANY = os.getenv("WEB_COMPANY", "Cosmopolitan Colours")


# --------------------------------------------------------------------- utils
def _run(coro):
    """Execute une coroutine async (fonctions du bot) depuis Flask (sync)."""
    return asyncio.new_event_loop().run_until_complete(coro)


def logged() -> bool:
    return bool(session.get("ok"))


def need_login():
    return jsonify({"error": "auth"}), 401


def _statut_view(code: str):
    if (code or "").lower().startswith("valid"):
        return "ok", "✓ Validé"
    return "warn", "⚠ À vérifier"


def _hhmm(iso: str) -> str:
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return ""


def _date10(iso: str) -> str:
    return str(iso or "")[:10]


def _archive_row(d: dict) -> dict:
    deb = d.get("heure_debut", "")
    fin = d.get("heure_fin", "")
    st, lab = _statut_view(d.get("statut", ""))
    if d.get("incidents"):
        st, lab = "warn", "⚠ Incident"
    hr = _hhmm(deb)
    if _hhmm(fin):
        hr += " → " + _hhmm(fin)
    return {
        "id": d.get("mission_id", ""),
        "n": d.get("appart", {}).get("nom_interne", "?"),
        "ad": "",
        "ag": d.get("agent", {}).get("prenom", "—"),
        "hr": hr or _date10(deb),
        "date": _date10(deb),
        "st": st, "lab": lab,
        "photos": len(d.get("photos", []) or []),
    }


# --------------------------------------------------------------------- pages
def _page(name: str):
    p = os.path.join(BASE, name)
    if not os.path.exists(p):
        abort(404)
    with open(p, encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


@app.get("/")
def root():
    return redirect("/dashboard" if logged() else "/login")


@app.get("/login")
def login_page():
    return _page("login.html")


@app.get("/dashboard")
def dashboard_page():
    if not logged():
        return redirect("/login")
    return _page("dashboard.html")


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or request.form
    user = (data.get("user") or "").strip()
    pwd = (data.get("pass") or "").strip()
    if not WEB_PASS:
        return jsonify({"error": "Le mot de passe du site n'est pas configuré (WEB_PASS)."}), 403
    if user == WEB_USER and pwd == WEB_PASS:
        session["ok"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Identifiant ou mot de passe incorrect."}), 401


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


# --------------------------------------------------------------------- API
@app.get("/api/missions")
def api_missions():
    if not logged():
        return need_login()
    today = datetime.date.today().isoformat()
    archives = bot.load_full_reports()
    done, today_rows = [], []
    for d in sorted(archives, key=lambda x: x.get("heure_debut", ""), reverse=True):
        row = _archive_row(d)
        (today_rows if row["date"] == today else done).append(row)

    soon = []
    try:
        for c in _run(bot.load_checkouts()):
            co = c.get("check_out")
            if not co:
                continue
            if co == today:
                today_rows.append({"id": "", "n": c["appartement"], "ad": "",
                                   "ag": "À assigner", "hr": "Check-out", "date": co,
                                   "st": "plan", "lab": "Planifiée", "photos": 0})
            elif co > today:
                soon.append({"id": "", "n": c["appartement"], "ad": "",
                             "ag": "À assigner",
                             "hr": "Check-out " + co, "date": co,
                             "st": "plan", "lab": "Planifiée", "photos": 0})
        soon.sort(key=lambda x: x["date"])
    except Exception as e:
        app.logger.warning("Lodgify indisponible: %s", e)

    return jsonify({"today": today_rows, "soon": soon[:40], "done": done[:60]})


@app.get("/api/mission")
def api_mission():
    if not logged():
        return need_login()
    mid = request.args.get("id", "")
    for d in bot.load_full_reports():
        if d.get("mission_id") == mid:
            photos = []
            for ph in (d.get("photos", []) or []):
                p = ph.get("path") if isinstance(ph, dict) else ph
                if p:
                    photos.append({"url": "/api/photo?path=" + p,
                                   "cap": (ph.get("label", "") if isinstance(ph, dict) else "")})
            confs = []
            for k, v in (d.get("confirmations", {}) or {}).items():
                confs.append({"label": k, "ok": (v is True), "val": ("" if isinstance(v, bool) else v),
                              "no": (v is False)})
            incs = [{"resume": i.get("resume") or i.get("texte"), "urgent": i.get("urgent")}
                    for i in (d.get("incidents", []) or [])]
            st, lab = _statut_view(d.get("statut", ""))
            return jsonify({
                "n": d.get("appart", {}).get("nom_interne", "?"),
                "ag": d.get("agent", {}).get("prenom", "—"),
                "deb": d.get("heure_debut", ""), "fin": d.get("heure_fin", ""),
                "st": st, "lab": lab, "confs": confs, "incidents": incs, "photos": photos,
            })
    abort(404)


@app.get("/api/agents")
def api_agents():
    if not logged():
        return need_login()
    out = []
    for cid, info in (bot.AGENTS_AUTH or {}).items():
        out.append({
            "n": info.get("prenom", "Agent"),
            "in": "".join([w[0] for w in str(info.get("prenom", "A")).split()[:2]]).upper() or "A",
            "rl": "Agent · " + (info.get("entreprise", "") or COMPANY),
            "depuis": info.get("ajoute_le", ""),
            "st": "up",
        })
    return jsonify(out)


@app.get("/api/logements")
def api_logements():
    if not logged():
        return need_login()
    out = []
    try:
        seen = {}
        for c in _run(bot.load_checkouts()):
            nm = c["appartement"]
            if nm not in seen:
                seen[nm] = {"n": nm, "ad": "", "last": c.get("check_out") or "—", "st": "up"}
        out = list(seen.values())
    except Exception as e:
        app.logger.warning("Lodgify indisponible: %s", e)
    if not out:  # repli : noms vus dans les archives
        names = {}
        for d in bot.load_full_reports():
            nm = d.get("appart", {}).get("nom_interne", "")
            if nm:
                names[nm] = {"n": nm, "ad": "", "last": _date10(d.get("heure_debut")), "st": "up"}
        out = list(names.values())
    return jsonify(out)


@app.get("/api/reports")
def api_reports():
    if not logged():
        return need_login()
    out = []
    exp = bot.EXPORTS_DIR
    if os.path.isdir(exp):
        for fn in sorted(os.listdir(exp), reverse=True):
            if fn.endswith(".html"):
                p = os.path.join(exp, fn)
                ts = datetime.datetime.fromtimestamp(os.path.getmtime(p))
                out.append({"file": fn, "ti": "Rapport de ménage",
                            "mt": ts.strftime("%d/%m/%Y %H:%M")})
    return jsonify(out[:50])


@app.get("/api/report-file")
def api_report_file():
    if not logged():
        return need_login()
    fn = os.path.basename(request.args.get("file", ""))
    p = os.path.join(bot.EXPORTS_DIR, fn)
    if not (fn.endswith(".html") and os.path.exists(p)):
        abort(404)
    return send_file(p, mimetype="text/html")


@app.post("/api/report")
def api_report_make():
    if not logged():
        return need_login()
    q = (request.get_json(silent=True) or {}).get("q", "").strip().lower()
    matches = bot.load_full_reports()
    if "verif" in q or "vérif" in q:
        matches = [d for d in matches if (d.get("statut", "").lower().startswith("a ") or d.get("incidents"))]
    matches.sort(key=lambda d: d.get("heure_debut", ""))
    try:
        synth = _run(bot.claude_report_summary(matches, "fr")) or ""
    except Exception:
        synth = ""
    path = bot._build_html_report(matches, "Rapport de ménage — " + COMPANY, COMPANY, synth)
    return jsonify({"ok": True, "file": os.path.basename(path)})


@app.get("/api/photo")
def api_photo():
    if not logged():
        return need_login()
    raw = request.args.get("path", "")
    full = os.path.realpath(raw)
    media = os.path.realpath(bot.MEDIA_DIR)
    if not full.startswith(media) or not os.path.exists(full):
        abort(404)
    mt = mimetypes.guess_type(full)[0] or "application/octet-stream"
    return send_file(full, mimetype=mt)


@app.get("/api/photos")
def api_photos():
    """Renvoie les photos de la mission la plus recente (ou filtrees par ?q=)."""
    if not logged():
        return need_login()
    q = request.args.get("q", "").strip().lower()
    archives = sorted(bot.load_full_reports(), key=lambda d: d.get("heure_debut", ""), reverse=True)
    if q:
        archives = [d for d in archives if q in d.get("appart", {}).get("nom_interne", "").lower()]
    if not archives:
        return jsonify({"titre": "", "photos": []})
    d = archives[0]
    photos = []
    for ph in (d.get("photos", []) or []):
        p = ph.get("path") if isinstance(ph, dict) else ph
        if p:
            photos.append({"url": "/api/photo?path=" + p,
                           "cap": (ph.get("label", "") if isinstance(ph, dict) else "")})
    titre = d.get("appart", {}).get("nom_interne", "") + " — " + _date10(d.get("heure_debut"))
    return jsonify({"titre": titre, "photos": photos})


@app.post("/api/ask")
def api_ask():
    if not logged():
        return need_login()
    question = (request.get_json(silent=True) or {}).get("q", "").strip()
    if not question:
        return jsonify({"answer": ""})
    try:
        contexte = bot.load_reports()  # version compacte des archives
    except Exception:
        contexte = []
    import json as _json
    today = datetime.date.today().isoformat()
    system = (
        "Tu es ALFRED, l'assistant de ménage de " + COMPANY + ". "
        "Tu réponds en français, brièvement et clairement, à partir des données de ménage fournies "
        "(missions archivées : appartement, agent, date, statut, incidents). "
        "Aujourd'hui = " + today + ". Si la donnée n'existe pas, dis-le simplement."
    )
    user = ("Données (JSON) :\n" + _json.dumps(contexte, ensure_ascii=False)[:12000]
            + "\n\nQuestion : " + question)
    try:
        ans = _run(bot.claude_text(system, user, max_tokens=600, model=bot.ANTHROPIC_ADMIN_MODEL))
    except Exception as e:
        app.logger.exception("ask")
        ans = None
    return jsonify({"answer": ans or "Désolé, je n'ai pas pu répondre pour le moment."})


@app.get("/api/me")
def api_me():
    return jsonify({"logged": logged(), "company": COMPANY, "user": WEB_USER})


if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
