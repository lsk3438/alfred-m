# -*- coding: utf-8 -*-
"""
ALFRED-M — Serveur web du panneau admin.
Reutilise les donnees du bot (bot.py) : memes archives, memes photos, meme IA.
Ne demarre PAS le bot Telegram (bot.py est protege par `if __name__ == "__main__"`).

Principes :
  - Les archives JSON du bot restent la SOURCE DE VERITE (preuve, lisibles).
  - Le web ne les modifie JAMAIS. Le "traitement" (traite / note) est stocke
    a part dans traitements.json -> le rapport d'origine reste intact.
  - Toutes les lectures passent par un CACHE memoire (rafraichi automatiquement),
    ce qui evite de relire tous les fichiers a chaque appel.

Lancement :  python app.py   (ou via gunicorn / systemd)
Config (.env) :
    WEB_USER=admin
    WEB_PASS=ton_mot_de_passe        # OBLIGATOIRE
    WEB_SECRET=une_chaine_aleatoire  # recommande (sinon sessions perdues au redemarrage)
    WEB_PORT=8000
"""
import os
import json
import time
import asyncio
import datetime
import mimetypes
import unicodedata

from flask import (Flask, request, session, jsonify, send_file,
                   redirect, Response, abort)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

import bot  # donnees + IA + generateur de rapport

BASE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.secret_key = os.getenv("WEB_SECRET") or os.urandom(24).hex()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

WEB_USER = os.getenv("WEB_USER", "admin")
WEB_PASS = os.getenv("WEB_PASS", "")
COMPANY = os.getenv("WEB_COMPANY", "Cosmopolitan Colours")

TRAITEMENTS_FILE = os.path.join(BASE, "traitements.json")
LOGEMENTS_INFOS_FILE = os.path.join(BASE, "logements_infos.json")


# =====================================================================
# Utilitaires
# =====================================================================
def _run(coro):
    """Execute une coroutine (fonctions async du bot) depuis Flask."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def logged() -> bool:
    return bool(session.get("ok"))


def need_login():
    return jsonify({"error": "auth"}), 401


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data) -> bool:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        app.logger.exception("Ecriture %s", path)
        return False


def _d10(v) -> str:
    return str(v or "")[:10]


def _hhmm(iso: str) -> str:
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return ""


def _duree(deb: str, fin: str) -> str:
    """Duree lisible entre deux horodatages ISO ('1h25')."""
    try:
        a = datetime.datetime.fromisoformat(deb)
        b = datetime.datetime.fromisoformat(fin)
        mn = int((b - a).total_seconds() // 60)
        if mn < 0:
            return "—"
        return (str(mn // 60) + "h" + ("%02d" % (mn % 60))) if mn >= 60 else (str(mn) + " min")
    except Exception:
        return "—"


def _ref(nom: str, date10: str, rang: int = 0) -> str:
    """Reference lisible : 3 lettres du logement + JJMMAA (+ -2, -3 si meme jour)."""
    base = unicodedata.normalize("NFD", nom or "")
    base = "".join(c for c in base if unicodedata.category(c) != "Mn")
    base = "".join(c for c in base if c.isalpha()).upper()[:3] or "MIS"
    p = (date10 or "").split("-")
    dd = (p[2] + p[1] + p[0][2:]) if len(p) == 3 and len(p[0]) == 4 else ""
    return base + "-" + dd + (("-" + str(rang + 1)) if rang else "")


# =====================================================================
# Cache des missions (les archives restent la source de verite)
# =====================================================================
_CACHE = {"t": 0.0, "missions": None}
_CACHE_TTL = 20  # secondes


def invalidate_cache():
    _CACHE["missions"] = None


def _motif(d: dict) -> str:
    """Pourquoi la mission est 'a verifier' : incidents + controles photo."""
    bits = []
    for i in (d.get("incidents") or []):
        r = (i.get("resume") or i.get("texte") or "").strip()
        if r:
            bits.append(r)
    for k, v in (d.get("confirmations") or {}).items():
        if v is False:
            bits.append("Non fait : " + str(k))
    return " · ".join(bits[:4])


def _build_missions() -> list:
    """Toutes les missions archivees + les missions en cours, format panneau web."""
    tr = _read_json(TRAITEMENTS_FILE, {})
    out, par_jour = [], {}
    archives = bot.load_full_reports()
    archives.sort(key=lambda d: str(d.get("heure_debut", "")))
    for d in archives:
        nom = (d.get("appart") or {}).get("nom_interne") or "?"
        date = _d10(d.get("heure_debut"))
        cle = (nom, date)
        rang = par_jour.get(cle, 0)
        par_jour[cle] = rang + 1
        mid = d.get("mission_id") or ""
        code = str(d.get("statut", "")).lower()
        warn = not code.startswith("valid")
        t = tr.get(mid) or {}
        hr = _hhmm(d.get("heure_debut", ""))
        if _hhmm(d.get("heure_fin", "")):
            hr += " → " + _hhmm(d.get("heure_fin", ""))
        out.append({
            "id": mid,
            "ref": _ref(nom, date, rang),
            "n": nom,
            "pid": str((d.get("appart") or {}).get("property_id") or ""),
            "ag": (d.get("agent") or {}).get("prenom") or "—",
            "agid": str((d.get("agent") or {}).get("chat_id") or ""),
            "date": date,
            "hr": hr,
            "dur": _duree(d.get("heure_debut", ""), d.get("heure_fin", "")),
            "st": "warn" if warn else "ok",
            "lab": "⚠ À vérifier" if warn else "✓ Validé",
            "why": _motif(d) if warn else "",
            "pb": len(d.get("incidents") or []),
            "nph": len(d.get("photos") or []),
            "done": 1 if t.get("done") else 0,
            "note": t.get("note") or "",
        })
    # Missions en cours (etat vivant du bot)
    for cid, st in (_read_json(getattr(bot, "STATE_FILE", ""), {}) or {}).items():
        m = (st or {}).get("mission")
        if not isinstance(m, dict) or not m.get("name"):
            continue
        deb = m.get("debut") or ""
        out.append({
            "id": "run_" + str(cid),
            "ref": _ref(m.get("name"), _d10(deb)),
            "n": m.get("name"), "pid": str(m.get("property_id") or ""),
            "ag": st.get("prenom") or "—", "agid": str(cid),
            "date": _d10(deb), "hr": (_hhmm(deb) + " → en cours") if deb else "en cours",
            "dur": "—", "st": "run", "lab": "● En cours", "why": "",
            "pb": len(m.get("incidents") or []),
            "nph": len(((m.get("media") or {}).get("photos")) or []),
            "done": 0, "note": "",
        })
    out.sort(key=lambda x: (x["date"], x["hr"]), reverse=True)
    return out


def missions_all() -> list:
    if _CACHE["missions"] is None or (time.time() - _CACHE["t"]) > _CACHE_TTL:
        _CACHE["missions"] = _build_missions()
        _CACHE["t"] = time.time()
    return _CACHE["missions"]


# =====================================================================
# Pages
# =====================================================================
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


# --------------------------------------------------------------- connexion
_LOGIN_FAILS = {}
_LOGIN_MAX = 5
_LOGIN_WINDOW = 300


def _login_blocked(ip: str) -> int:
    rec = _LOGIN_FAILS.get(ip)
    if not rec:
        return 0
    count, first = rec
    if count >= _LOGIN_MAX and (time.time() - first) < _LOGIN_WINDOW:
        return int(_LOGIN_WINDOW - (time.time() - first))
    if (time.time() - first) >= _LOGIN_WINDOW:
        _LOGIN_FAILS.pop(ip, None)
    return 0


def _login_fail(ip: str) -> None:
    count, first = _LOGIN_FAILS.get(ip, (0, time.time()))
    if (time.time() - first) >= _LOGIN_WINDOW:
        count, first = 0, time.time()
    _LOGIN_FAILS[ip] = (count + 1, first)


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or request.form
    user = (data.get("user") or "").strip()
    pwd = (data.get("pass") or "").strip()
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    if not WEB_PASS:
        return jsonify({"error": "Le mot de passe du site n'est pas configuré (WEB_PASS)."}), 403
    reste = _login_blocked(ip)
    if reste:
        return jsonify({"error": "Trop de tentatives. Réessaie dans %d min." % (reste // 60 + 1)}), 429
    if user == WEB_USER and pwd == WEB_PASS:
        _LOGIN_FAILS.pop(ip, None)
        session["ok"] = True
        return jsonify({"ok": True})
    _login_fail(ip)
    return jsonify({"error": "Identifiant ou mot de passe incorrect."}), 401


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    return jsonify({"logged": logged(), "company": COMPANY, "user": WEB_USER})


# =====================================================================
# API — missions
# =====================================================================
@app.get("/api/missions")
def api_missions():
    if not logged():
        return need_login()
    return jsonify(missions_all())


@app.get("/api/mission")
def api_mission():
    if not logged():
        return need_login()
    mid = request.args.get("id", "")
    for d in bot.load_full_reports():
        if d.get("mission_id") != mid:
            continue
        photos = []
        for ph in (d.get("photos") or []):
            p = ph.get("path") if isinstance(ph, dict) else ph
            if p:
                photos.append({"url": "/api/photo?path=" + p,
                               "cap": (ph.get("point", "") if isinstance(ph, dict) else "")})
        confs = [{"label": k, "ok": (v is True), "no": (v is False)}
                 for k, v in (d.get("confirmations") or {}).items()]
        incs = [{"resume": i.get("resume") or i.get("texte"), "urgent": bool(i.get("urgent"))}
                for i in (d.get("incidents") or [])]
        return jsonify({"confs": confs, "incidents": incs, "photos": photos,
                        "video_avant": bool(d.get("video_avant"))})
    return jsonify({"confs": [], "incidents": [], "photos": []})


@app.post("/api/mission/stop")
def api_mission_stop():
    """Demande au BOT d'arreter une mission en cours.
    mode = "archive" (cloture en gardant les photos) ou "suppr" (efface).
    C'est le bot qui execute : lui seul possede l'etat vivant de la mission."""
    if not logged():
        return need_login()
    data = request.get_json(silent=True) or {}
    agid = str(data.get("agid") or "").strip()
    mode = "suppr" if data.get("mode") == "suppr" else "archive"
    if not agid:
        return jsonify({"error": "agent manquant"}), 400
    dem = _read_json(os.path.join(BASE, "arrets_missions.json"), {})
    dem[agid] = {"mode": mode, "par": WEB_USER,
                 "le": datetime.datetime.now().isoformat(timespec="seconds")}
    if not _write_json(os.path.join(BASE, "arrets_missions.json"), dem):
        return jsonify({"error": "enregistrement impossible"}), 500
    invalidate_cache()
    return jsonify({"ok": True, "mode": mode})


@app.post("/api/traitement")
def api_traitement():
    """Marque une mission traitee / non traitee + note. N'ecrit JAMAIS dans l'archive."""
    if not logged():
        return need_login()
    data = request.get_json(silent=True) or {}
    mid = (data.get("id") or "").strip()
    if not mid:
        return jsonify({"error": "id manquant"}), 400
    tr = _read_json(TRAITEMENTS_FILE, {})
    tr[mid] = {"done": 1 if data.get("done") else 0,
               "note": (data.get("note") or "").strip(),
               "par": WEB_USER,
               "le": datetime.datetime.now().isoformat(timespec="seconds")}
    if not _write_json(TRAITEMENTS_FILE, tr):
        return jsonify({"error": "enregistrement impossible"}), 500
    invalidate_cache()
    return jsonify({"ok": True, "traitement": tr[mid]})


# =====================================================================
# API — agents
# =====================================================================
@app.get("/api/agents")
def api_agents():
    if not logged():
        return need_login()
    try:
        auth = bot._load_agents_auth() or {}
    except Exception:
        auth = getattr(bot, "AGENTS_AUTH", {}) or {}
    try:
        admins = bot.load_admins() or {}
    except Exception:
        admins = getattr(bot, "ADMINS", {}) or {}

    ms = missions_all()
    today = datetime.date.today()
    lundi = (today - datetime.timedelta(days=today.weekday())).isoformat()
    mois = today.isoformat()[:7]

    def initiales(nom):
        parts = [w for w in str(nom or "").split() if w]
        return ("".join(p[0] for p in parts[:2]) or "A").upper()

    fiches, vus = [], set()

    def ajoute(cid, info, role):
        cid = str(cid)
        if cid in vus:
            return
        vus.add(cid)
        mine = [m for m in ms if m["agid"] == cid]
        faits = [m for m in mine if m["st"] != "run"]
        nom = info.get("prenom") or "Agent"
        fiches.append({
            "n": nom, "in": initiales(nom), "tg": cid, "role": role,
            "st": "up" if info.get("actif", True) else "off",
            "tot": len(faits),
            "sem": len([m for m in faits if m["date"] >= lundi]),
            "mois": len([m for m in faits if m["date"][:7] == mois]),
            "last": (max(m["date"] for m in faits) if faits else "—"),
            "pb": len([m for m in faits if m["pb"]]),
            "ent": info.get("entreprise", ""),
        })

    for cid, info in admins.items():
        ajoute(cid, info, "admin")
    for cid, info in auth.items():
        ajoute(cid, info, "agent")
    fiches.sort(key=lambda a: (-a["tot"], a["n"]))
    return jsonify(fiches)


@app.post("/api/agent")
def api_agent_maj():
    """Active / desactive un agent. Le bot lit le meme fichier : effet immediat."""
    if not logged():
        return need_login()
    data = request.get_json(silent=True) or {}
    cid = str(data.get("tg") or "").strip()
    if not cid:
        return jsonify({"error": "identifiant manquant"}), 400
    path = getattr(bot, "AGENTS_AUTH_FILE", os.path.join(BASE, "agents_autorises.json"))
    auth = _read_json(path, {})
    if cid not in auth:
        return jsonify({"error": "agent introuvable"}), 404
    auth[cid]["actif"] = bool(data.get("actif"))
    if not _write_json(path, auth):
        return jsonify({"error": "enregistrement impossible"}), 500
    try:
        bot.AGENTS_AUTH = bot._load_agents_auth()
    except Exception:
        pass
    return jsonify({"ok": True})


# =====================================================================
# API — logements
# =====================================================================
@app.get("/api/logements")
def api_logements():
    if not logged():
        return need_login()
    infos = _read_json(LOGEMENTS_INFOS_FILE, {})
    ms = missions_all()
    props = []
    try:
        props = _run(bot.get_all_properties())
    except Exception:
        app.logger.warning("Lodgify indisponible, repli sur le cache local")
        try:
            props = bot._load_properties_cache() or []
        except Exception:
            props = []
    if not props:  # dernier repli : les noms vus dans les archives
        noms = []
        for m in ms:
            if m["n"] not in noms:
                noms.append(m["n"])
        props = [{"property_id": "", "name": n} for n in noms]

    out = []
    for p in props:
        nom = p.get("name") or "?"
        pid = str(p.get("property_id") or "")
        inf = infos.get(pid) or infos.get(nom) or {}
        faites = [m["date"] for m in ms if m["n"] == nom and m["st"] != "run"]
        out.append({
            "n": nom, "pid": pid, "ad": inf.get("ad", ""),
            "last": (max(faites) if faites else "—"),
            "st": inf.get("st", "up"),
            "acces": inf.get("acces", "—"),
            "code": inf.get("code", "—"),
            "note": inf.get("note", ""),
            "nb": len(faites),
        })
    return jsonify(out)


@app.post("/api/logement")
def api_logement_maj():
    """Infos d'acces propres a ALFRED (Lodgify reste la source pour le reste)."""
    if not logged():
        return need_login()
    data = request.get_json(silent=True) or {}
    cle = str(data.get("pid") or data.get("n") or "").strip()
    if not cle:
        return jsonify({"error": "logement manquant"}), 400
    infos = _read_json(LOGEMENTS_INFOS_FILE, {})
    cur = infos.get(cle, {})
    for k in ("ad", "acces", "code", "note", "st"):
        if k in data:
            cur[k] = str(data.get(k) or "")
    infos[cle] = cur
    if not _write_json(LOGEMENTS_INFOS_FILE, infos):
        return jsonify({"error": "enregistrement impossible"}), 500
    return jsonify({"ok": True})


# =====================================================================
# API — medias (chargement a la demande)
# =====================================================================
@app.get("/api/media")
def api_media():
    if not logged():
        return need_login()
    lg = (request.args.get("lg") or "").strip()
    ag = (request.args.get("ag") or "").strip()
    rm = (request.args.get("rm") or "").strip().lower()
    d1 = (request.args.get("d1") or "").strip()
    d2 = (request.args.get("d2") or "").strip()
    limite = min(int(request.args.get("max") or 300), 600)

    out = []
    for d in bot.load_full_reports():
        nom = (d.get("appart") or {}).get("nom_interne") or "?"
        agent = (d.get("agent") or {}).get("prenom") or "—"
        date = _d10(d.get("heure_debut"))
        if lg and nom != lg:
            continue
        if ag and agent != ag:
            continue
        if d1 and date < d1:
            continue
        if d2 and date > d2:
            continue
        for ph in (d.get("photos") or []):
            p = ph.get("path") if isinstance(ph, dict) else ph
            point = (ph.get("point", "") if isinstance(ph, dict) else "")
            if not p:
                continue
            if rm and rm not in point.lower():
                continue
            out.append({"url": "/api/photo?path=" + p, "cap": point or "Photo",
                        "lg": nom, "ag": agent, "date": date})
            if len(out) >= limite:
                break
        if len(out) >= limite:
            break
    out.sort(key=lambda x: x["date"], reverse=True)
    return jsonify(out)


@app.get("/api/photo")
def api_photo():
    if not logged():
        return need_login()
    raw = request.args.get("path", "")
    full = os.path.realpath(raw)
    media = os.path.realpath(bot.MEDIA_DIR) + os.sep
    if not full.startswith(media) or not os.path.exists(full):
        abort(404)
    mt = mimetypes.guess_type(full)[0] or "application/octet-stream"
    return send_file(full, mimetype=mt)


# =====================================================================
# API — rapports (documents)
# =====================================================================
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
    return jsonify(out[:60])


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
    """Genere un document a partir des missions selectionnees (ids) ou de tout."""
    if not logged():
        return need_login()
    data = request.get_json(silent=True) or {}
    ids = set(data.get("ids") or [])
    matches = bot.load_full_reports()
    if ids:
        matches = [d for d in matches if d.get("mission_id") in ids]
    matches.sort(key=lambda d: str(d.get("heure_debut", "")))
    if not matches:
        return jsonify({"error": "Aucune mission dans la sélection."}), 400
    try:
        synth = _run(bot.claude_report_summary(matches, "fr")) or ""
    except Exception:
        synth = ""
    path = bot._build_html_report(matches, "Rapport de ménage — " + COMPANY, COMPANY, synth)
    return jsonify({"ok": True, "file": os.path.basename(path)})


# =====================================================================
# API — assistant
# =====================================================================
@app.post("/api/ask")
def api_ask():
    if not logged():
        return need_login()
    question = ((request.get_json(silent=True) or {}).get("q") or "").strip()
    if not question:
        return jsonify({"answer": ""})
    try:
        contexte = bot.load_reports()
    except Exception:
        contexte = []
    today = datetime.date.today().isoformat()
    system = ("Tu es ALFRED, l'assistant de ménage de " + COMPANY + ". "
              "Tu réponds en français, brièvement et clairement, à partir des données fournies "
              "(missions archivées : appartement, agent, date, statut, incidents). "
              "Aujourd'hui = " + today + ". Si la donnée n'existe pas, dis-le simplement.")
    user = ("Données (JSON) :\n" + json.dumps(contexte, ensure_ascii=False)[:12000]
            + "\n\nQuestion : " + question)
    try:
        ans = _run(bot.claude_text(system, user, max_tokens=600, model=bot.ANTHROPIC_ADMIN_MODEL))
    except Exception:
        app.logger.exception("ask")
        ans = None
    return jsonify({"answer": ans or "Désolé, je n'ai pas pu répondre pour le moment."})


if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
