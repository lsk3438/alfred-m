"""
ALFRED-M - Bot Telegram de coordination menage (Genius BnB)
============================================================
Paliers 1 a 6 + MULTILINGUE (fr, en, es, ar, ro).

- Au 1er contact : choix de la langue (boutons drapeaux), memorise par agent.
- Tous les textes fixes sont traduits via un dictionnaire (pas d'IA = fiable/gratuit).
- Claude n'est appele QUE pour comprendre/resumer un incident en texte libre.
- Les archives gardent les libelles en FRANCAIS (rapports uniformes).
"""

import asyncio
import base64
import contextvars
import datetime
import glob
import json
import logging
import os
import re

import httpx
from dotenv import load_dotenv
from telegram import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

# --- Cles ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LODGIFY_API_KEY = os.getenv("LODGIFY_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
# Modele plus puissant pour les questions admin (raisonnement / croisement de donnees)
ANTHROPIC_ADMIN_MODEL = os.getenv("ANTHROPIC_ADMIN_MODEL", "claude-opus-4-8")
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID")
LODGIFY_BASE = "https://api.lodgify.com/v2"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(BASE_DIR, "media")
ARCHIVES_DIR = os.path.join(BASE_DIR, "archives")
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")
os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(ARCHIVES_DIR, exist_ok=True)
os.makedirs(EXPORTS_DIR, exist_ok=True)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("alfred-m")

# --- Admins (sauvegardes sur disque ; le principal = MANAGER_CHAT_ID) ---
ADMINS_FILE = os.path.join(BASE_DIR, "admins.json")


def load_admins() -> dict:
    try:
        with open(ADMINS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_admins() -> None:
    try:
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump(ADMINS, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Echec sauvegarde admins")


ADMINS = load_admins()  # {"<chat_id>": {"prenom": ..., "ajoute_le": ...}}

# --- Langue memorisee par agent (survit aux redemarrages) ---
AGENT_LANG_FILE = os.path.join(BASE_DIR, "agents_lang.json")


def _load_agent_lang() -> dict:
    try:
        with open(AGENT_LANG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_agent_lang() -> None:
    try:
        with open(AGENT_LANG_FILE, "w", encoding="utf-8") as f:
            json.dump(AGENT_LANG, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Echec sauvegarde langues agents")


AGENT_LANG = _load_agent_lang()  # {"<chat_id>": "fr"/"es"/...}

# --- Agents de menage autorises (liste blanche, sauvegardee sur disque) ---
AGENTS_AUTH_FILE = os.path.join(BASE_DIR, "agents_autorises.json")


def _load_agents_auth() -> dict:
    try:
        with open(AGENTS_AUTH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_agents_auth() -> None:
    try:
        with open(AGENTS_AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(AGENTS_AUTH, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Echec sauvegarde agents autorises")


AGENTS_AUTH = _load_agents_auth()  # {"<chat_id>": {"prenom": ..., "ajoute_le": ...}}

# --- Inscriptions en attente de validation ---
# PENDING[code] = {"type":"admin"|"agent", "nom":..., "entreprise":..., "role":..., "lang":..., "date":...}
PENDING_FILE = os.path.join(BASE_DIR, "inscriptions_en_attente.json")


def _load_pending() -> dict:
    try:
        with open(PENDING_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_pending() -> None:
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(PENDING, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Echec sauvegarde inscriptions en attente")


PENDING = _load_pending()

# --- Profil du super admin (son entreprise + son role) ---
SUPER_PROFILE_FILE = os.path.join(BASE_DIR, "super_admin.json")


def _load_super_profile() -> dict:
    try:
        with open(SUPER_PROFILE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_super_profile() -> None:
    try:
        with open(SUPER_PROFILE_FILE, "w", encoding="utf-8") as f:
            json.dump(SUPER_PROFILE, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Echec sauvegarde profil super admin")


SUPER_PROFILE = _load_super_profile()  # {"entreprise":..., "role":...}

# --- Attribution des logements aux entreprises ---
# PROPERTY_COMPANY[property_id] = "Nom entreprise"
PROPERTY_COMPANY_FILE = os.path.join(BASE_DIR, "logements_entreprise.json")


def _load_property_company() -> dict:
    try:
        with open(PROPERTY_COMPANY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_property_company() -> None:
    try:
        with open(PROPERTY_COMPANY_FILE, "w", encoding="utf-8") as f:
            json.dump(PROPERTY_COMPANY, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Echec sauvegarde logements/entreprise")


PROPERTY_COMPANY = _load_property_company()


def property_company(pid) -> str:
    return PROPERTY_COMPANY.get(str(pid), "")


def co_key(s: str) -> str:
    """Cle normalisee d'une entreprise (insensible casse/espaces)."""
    return " ".join((s or "").split()).strip().lower()


def all_companies() -> dict:
    """Toutes les entreprises connues -> {cle: nom_affiche}."""
    cos = {}
    e = SUPER_PROFILE.get("entreprise")
    if e:
        cos[co_key(e)] = e
    for info in ADMINS.values():
        e = info.get("entreprise")
        if e:
            cos.setdefault(co_key(e), e)
    return cos


def admin_company(chat_id) -> str | None:
    """Entreprise d'un admin (super = son profil)."""
    if is_super(chat_id):
        return SUPER_PROFILE.get("entreprise")
    info = ADMINS.get(str(chat_id))
    return info.get("entreprise") if info else None


def person_company(chat_id) -> str:
    """Entreprise d'une personne (admin ou agent), pour l'affichage."""
    c = admin_company(chat_id)
    if c:
        return c
    info = AGENTS_AUTH.get(str(chat_id))
    return (info or {}).get("entreprise") or ""

TXT_BLOQUE = ("⛔ Tu n'es pas encore autorisé à utiliser ce bot.\n"
              "Tape /start pour t'inscrire.\n\n"
              "⛔ You are not authorized yet. Type /start to register.")

# =====================================================================
# TRADUCTIONS
# =====================================================================
SUPPORTED = ["fr", "en", "es", "ar", "ro"]
LANG_NAMES = {
    "fr": "🇫🇷 Français", "en": "🇬🇧 English", "es": "🇪🇸 Español",
    "ar": "🇸🇦 العربية", "ro": "🇷🇴 Română",
}
CHOOSE_LANG = "🌍 Choisis ta langue / Choose your language / Elige tu idioma / اختر لغتك / Alege limba :"

T = {
    "fr": {
        "welcome": "Bonjour {prenom} ! 👋 Je suis ALFRED, ton assistant ménage de {soc}. Je t'accompagne pas à pas tout au long de ta mission.\n\nPrêt(e) à commencer ?",
        "btn_start": "🧹 Démarrer ma mission",
        "btn_lang": "🌐 Changer de langue",
        "which_appart": "Super ! 🏠 Choisis l'appartement que tu vas nettoyer aujourd'hui :",
        "appart_chosen": "Parfait, c'est parti pour {name} ✅\n\nPremière étape : filme une courte vidéo de l'état du logement à ton arrivée (séjour, chambres, cuisine, salle de bain). 📹",
        "video_avant_ok": "Vidéo d'arrivée bien reçue ✓\n\nTu peux commencer le ménage 🧽 Prends ton temps et fais les choses bien. Quand tout est terminé, appuie sur le bouton ci-dessous. 👇",
        "btn_done": "✅ J'ai terminé le ménage",
        "btn_incident": "⚠️ Signaler un problème",
        "sec_points_intro": "Un coup d'œil rapide 👇",
        "sec_photos_intro": "📸 Photos à envoyer ({n}) :",
        "sec_instructions": "Envoie les photos 📷 puis appuie sur ✅",
        "sec_validate": "✅ Section validée",
        "sec_count_light": "📷 {c}/{n}",
        "sec_need_more": "📸 Encore {r} — {c}/{n}",
        "sec_review": "🔎 J'ai regardé tes photos. À revoir :\n{list}\n\nTu peux les refaire, ou valider quand même 👇",
        "sec_btn_force": "✅ Valider quand même",
        "sec_btn_redo": "📷 Refaire des photos",
        "sec_redo": "Ok 👍 renvoie les photos concernées.",
        "sec_done": "✅ {titre} — c'est bon !",
        "sec_next": "✅ Suivant",
        "sec_photos_list": "📸 Photos à envoyer :",
        "sec_photo_seen": "📷 Reçue — {x} ✓",
        "sec_photo_unseen": "📷 Reçue ✓",
        "sec_recap_missing": "⚠️ Il manque : {miss}",
        "sec_recap_issues": "⚠️ À revoir :\n{iss}",
        "sec_complete": "📷 Compléter",
        "sec_pass": "➡️ Passer quand même",
        "sec_again_q": "Section terminée ✅ Une autre pièce identique à photographier ?",
        "sec_again": "➕ Autre (même pièce)",
        "sec_cont": "➡️ Pièce suivante",
        "menage_done": "Bravo ! 👏 On passe au contrôle final, pièce par pièce. Pour chaque pièce : envoie les photos demandées, puis valide. Laisse-toi guider.",
        "point_photo": "📸 Étape {num}/{n} — {label}\nEnvoie une photo comme preuve.",
        "btn_yes": "✅ Oui", "btn_no": "⚠️ Non",
        "point_confirm": "Étape {num}/{n} — {label}",
        "point_done": "Étape {num}/{n} — {label} → {mark}",
        "checklist_done": "Checklist terminée, beau travail ! 🎉 Dernière étape : filme une courte vidéo du logement propre et prêt à accueillir les voyageurs. 📹",
        "photo_ok": "Photo reçue ✓ Merci !",
        "photo_doute": "🤔 Hmm, cette photo ne semble pas montrer « {label} » ({raison}). Tu peux la garder quand même, ou en reprendre une.",
        "btn_keep_photo": "✅ Garder quand même",
        "btn_retake_photo": "📷 Reprendre la photo",
        "photo_retake": "Pas de souci, renvoie la bonne photo 📷",
        "a_panel": "🔧 Espace admin — bonjour {prenom} !\n\nÉcris-moi directement tes questions sur tes rapports, photos et données de ménage (ex. « quels logements nettoyés aujourd'hui ? », « photos de X hier », « génère un rapport »).\nLes boutons ci-dessous servent à gérer ton équipe. Tape /start pour quitter.",
        "a_b_agents": "👥 Gérer les agents",
        "a_b_logements": "🏠 Assigner les logements",
        "a_b_admins": "🧑‍💼 Gérer les responsables",
        "a_b_lodgify": "🏨 Gestion Lodgify",
        "a_lodgify": ("🏨 Gestion Lodgify\n\nÉcris-moi directement ce que tu veux faire — je m'occupe de Lodgify. Exemples :\n\n"
                      "📖 « Liste mes réservations à venir »\n"
                      "📅 « Montre le calendrier du Studio Hydraulique »\n"
                      "💬 « Lis la conversation de la résa 6142944261 »\n"
                      "🔒 « Bloque le Studio Hydraulique du 10 au 12 »\n"
                      "✉️ « Écris au voyageur de la résa X : bienvenue, code 1234 »\n"
                      "💳 « Crée un lien de paiement de 150€ pour la résa Y »\n\n"
                      "⚠️ Chaque action qui modifie Lodgify te demandera confirmation avant de partir. Tape /start pour quitter."),
        "a_reports": "📊 Mode rapport activé.\nPose ta question (logements nettoyés aujourd'hui, incidents urgents de la semaine, « génère un rapport »…). Tape /start pour quitter.",
        "a_agents_none": "👥 Aucun agent pour l'instant. Quand un agent s'inscrit pour ton entreprise, tu reçois un message avec un bouton Autoriser.",
        "a_agents_mine": "👥 Tes agents :",
        "a_agents_all": "👥 Tous les agents :",
        "a_remove": "❌ Retirer {nom}",
        "a_admins_title": "🧑‍💼 Responsables :",
        "a_admins_none": "(Aucun autre responsable pour l'instant.)",
        "a_super_only": "Réservé à l'admin principal.",
        "a_log_title": "🏠 Assigner les logements\nClique un logement pour choisir son entreprise (❓ = non assigné).",
        "a_log_none": "Aucun logement trouvé dans Lodgify.",
        "a_log_err": "Je n'arrive pas à récupérer la liste des logements. Réessaie.",
        "a_log_pick": "À quelle entreprise appartient ce logement ?",
        "a_unassigned": "❌ Non assigné",
        "a_back": "⬅️ Retour",
        "a_super_co": "Avant tout, configurons ton profil. Quel est le nom de ton entreprise ?",
        "a_super_co_short": "Nom d'entreprise trop court, réessaie :",
        "a_super_role": "Parfait. Et quel est ton rôle ? (ex : gérant)",
        "a_super_done": "✅ Profil enregistré.",
        "a_new_admin": "🆕 Nouvelle demande RESPONSABLE\n👤 {nom}\n🏢 {ent}\n💼 {role}\n\nValider ?",
        "a_new_agent": "🆕 Nouvelle demande AGENT\n👤 {nom}\n🏢 {ent}\n\nValider ?",
        "a_btn_auth": "✅ Autoriser",
        "a_btn_refuse": "❌ Refuser",
        "a_done_admin": "✅ {nom} validé (responsable — {ent}).",
        "a_done_agent": "✅ {nom} validé (agent — {ent}).",
        "a_refused": "❌ Demande de {nom} refusée.",
        "a_already": "Cette demande a déjà été traitée.",
        "a_agent_removed": "🗑️ Agent retiré : {nom}.",
        "a_admin_removed": "🗑️ Responsable retiré : {nom}.",
        "mission_archived": "Mission terminée ✓ Tout est enregistré, merci pour ton travail ! 🙌\nStatut : {statut}.",
        "st_ok": "Validé", "st_check": "À vérifier",
        "incident_prompt": "Décris-moi le problème en quelques mots (dans ta langue), ou envoie une photo. Je préviens le responsable tout de suite. 📝",
        "incident_photo_ok": "Photo du problème reçue ✓ Ajoute une courte description en texte pour m'aider à comprendre.",
        "incident_ack": "C'est noté et transmis au responsable ✓ Tu peux continuer ta mission, merci !",
        "resume": "On reprend là où tu en étais 👍",
        "send_fin": "Quand tu es prêt(e), envoie la vidéo de fin (logement propre et rangé). 📹",
        "send_avant": "Quand tu es prêt(e), envoie la vidéo d'arrivée. 📹",
        "not_video": "Je n'attends pas de vidéo pour le moment 🙂",
        "not_photo": "Je n'attends pas de photo à cette étape 🙂",
        "tech_error": "⚠️ Un petit souci technique est survenu. Réessaie dans un instant. Si ça continue, tape /start pour repartir proprement.",
        "lodgify_offline": "⚠️ Lodgify est momentanément indisponible. J'utilise la dernière liste connue des logements.",
        "follow": "Suis simplement les étapes en cours 🙂 Utilise les boutons et envoie les photos/vidéos demandées.",
        "mission_cancel": "🚫 Mission en cours annulée. Tu peux repartir à zéro avec /start.",
        "mission_none": "Aucune mission en cours à annuler. 🙂",
        "press_start": "Appuie sur le bouton ci-dessous pour démarrer une mission. 👇",
        "reg_ask_name": "Bienvenue ! 👋 Avant de commencer, écris-moi ton nom et ton prénom :",
        "reg_name_short": "Il me faut ton nom complet (nom + prénom) pour continuer :",
        "reg_thanks": "Merci {name} ✅ Voici ton code : {code}\nTransmets-le à ton responsable pour qu'il valide ton accès. Je te préviens dès que c'est bon !",
        "reg_blocked": "{name}, ton accès n'est pas encore validé ⏳\nDonne ce code à ton responsable : {code}",
        "reg_authorized": "Ça y est, ton accès est validé ✅ Bienvenue dans l'équipe {soc} ! Appuie sur /start pour démarrer ta première mission.",
        "reg_choose_role": "Bienvenue ! 👋 Pour commencer, dis-moi qui tu es :",
        "btn_role_admin": "👔 Responsable / admin",
        "btn_role_agent": "🧹 Agent de ménage",
        "reg_ask_nom": "Parfait ! Quel est ton nom et ton prénom ?",
        "reg_admin_entreprise": "Quel est le nom de ton entreprise ?",
        "reg_admin_choose_co": "Rejoins une entreprise existante, ou crée la tienne :",
        "btn_new_company": "➕ Créer une nouvelle entreprise",
        "reg_admin_role": "Quel est ton rôle (ex : gérant, responsable ménage) ?",
        "reg_agent_choose_co": "Pour quelle entreprise travailles-tu ? Choisis dans la liste 👇",
        "reg_no_company": "Aucune entreprise n'est encore enregistrée. Demande à ton responsable de créer d'abord son compte (en tant que responsable).",
        "reg_pending": "Merci {name} ✅ Ta demande pour {soc} a bien été envoyée. Tu recevras un message dès qu'un responsable l'aura validée. ⏳",
        "reg_already_pending": "Ta demande est déjà en attente de validation ⏳ On te prévient dès que c'est bon.",
        "reg_authorized_admin": "Ton compte responsable est validé ✅ Bienvenue ! Tape /admin pour ouvrir ton panneau.",
        "reg_refused": "Ta demande n'a pas été acceptée. Rapproche-toi de ton responsable pour en savoir plus.",
        "no_appart": "Aucun appartement avec un départ à venir pour le moment.",
        "lodgify_err": "Oups, je n'arrive pas à récupérer la liste des appartements. Réessaie dans un instant.",
        "cl_sdb": "Salle de bain", "cl_wc": "WC", "cl_cuisine": "Cuisine",
        "cl_frigo": "Intérieur du frigo", "cl_lit": "Sous le lit",
        "cl_chauffage": "Chauffage coupé ?", "cl_fenetres": "Fenêtres fermées ?",
        "cl_pq": "Papier toilette en réserve ?", "cl_poubelles": "Poubelles vidées ?",
    },
    "en": {
        "welcome": "Hello {prenom}! 👋 I'm ALFRED, the cleaning assistant of {soc}. I'll guide you step by step throughout your mission.\n\nReady to start?",
        "btn_start": "🧹 Start my mission",
        "btn_lang": "🌐 Change language",
        "which_appart": "Great! 🏠 Choose the apartment you're cleaning today:",
        "appart_chosen": "Perfect, let's go with {name} ✅\n\nFirst step: film a short video of the apartment's condition when you arrive (living room, bedrooms, kitchen, bathroom). 📹",
        "video_avant_ok": "Arrival video received ✓\n\nYou can start cleaning 🧽 Take your time and do it well. When everything is done, tap the button below. 👇",
        "btn_done": "✅ I've finished cleaning",
        "btn_incident": "⚠️ Report a problem",
        "menage_done": "Well done! 👏 Now the final check, step by step (a few photos + verifications). It's quick, just follow along.",
        "point_photo": "📸 Step {num}/{n} — {label}\nSend a photo as proof.",
        "btn_yes": "✅ Yes", "btn_no": "⚠️ No",
        "point_confirm": "Step {num}/{n} — {label}",
        "point_done": "Step {num}/{n} — {label} → {mark}",
        "checklist_done": "Checklist done, great work! 🎉 Last step: film a short video of the clean apartment, ready to welcome guests. 📹",
        "photo_ok": "Photo received ✓ Thanks!",
        "photo_doute": "🤔 Hmm, this photo doesn't seem to show « {label} » ({raison}). You can keep it anyway, or take a new one.",
        "btn_keep_photo": "✅ Keep it anyway",
        "btn_retake_photo": "📷 Retake the photo",
        "photo_retake": "No problem, send the correct photo 📷",
        "a_panel": "🔧 Admin space — hello {prenom}!\n\nWrite your questions directly about your reports, photos and cleaning data (e.g. « which units cleaned today? », « photos of X yesterday », « generate a report »).\nThe buttons below manage your team. Type /start to exit.",
        "a_b_agents": "👥 Manage agents",
        "a_b_logements": "🏠 Assign properties",
        "a_b_admins": "🧑‍💼 Manage managers",
        "a_reports": "📊 Report mode on.\nAsk your question (units cleaned today, urgent incidents this week, « generate a report »…). Type /start to exit.",
        "a_agents_none": "👥 No agent yet. When an agent registers for your company, you'll get a message with an Authorize button.",
        "a_agents_mine": "👥 Your agents:",
        "a_agents_all": "👥 All agents:",
        "a_remove": "❌ Remove {nom}",
        "a_admins_title": "🧑‍💼 Managers:",
        "a_admins_none": "(No other manager yet.)",
        "a_super_only": "Main admin only.",
        "a_log_title": "🏠 Assign properties\nTap a property to choose its company (❓ = unassigned).",
        "a_log_none": "No property found in Lodgify.",
        "a_log_err": "I can't fetch the property list. Please try again.",
        "a_log_pick": "Which company does this property belong to?",
        "a_unassigned": "❌ Unassigned",
        "a_back": "⬅️ Back",
        "a_super_co": "First, let's set up your profile. What's your company name?",
        "a_super_co_short": "Company name too short, try again:",
        "a_super_role": "Great. And what's your role? (e.g. manager)",
        "a_super_done": "✅ Profile saved.",
        "a_new_admin": "🆕 New MANAGER request\n👤 {nom}\n🏢 {ent}\n💼 {role}\n\nApprove?",
        "a_new_agent": "🆕 New AGENT request\n👤 {nom}\n🏢 {ent}\n\nApprove?",
        "a_btn_auth": "✅ Authorize",
        "a_btn_refuse": "❌ Refuse",
        "a_done_admin": "✅ {nom} approved (manager — {ent}).",
        "a_done_agent": "✅ {nom} approved (agent — {ent}).",
        "a_refused": "❌ Request from {nom} refused.",
        "a_already": "This request has already been handled.",
        "a_agent_removed": "🗑️ Agent removed: {nom}.",
        "a_admin_removed": "🗑️ Manager removed: {nom}.",
        "mission_archived": "Mission complete ✓ Everything is saved, thank you for your work! 🙌\nStatus: {statut}.",
        "st_ok": "Validated", "st_check": "To check",
        "incident_prompt": "Describe the problem in a few words (in your language), or send a photo. I'll notify the manager right away. 📝",
        "incident_photo_ok": "Problem photo received ✓ Add a short text description to help me understand.",
        "incident_ack": "Noted and forwarded to the manager ✓ You can continue your mission, thank you!",
        "resume": "Let's pick up where you left off 👍",
        "send_fin": "When you're ready, send the final video (clean, tidy apartment). 📹",
        "send_avant": "When you're ready, send the arrival video. 📹",
        "not_video": "I'm not expecting a video right now 🙂",
        "not_photo": "I'm not expecting a photo at this step 🙂",
        "tech_error": "⚠️ A small technical issue occurred. Please try again in a moment. If it keeps happening, type /start to restart cleanly.",
        "lodgify_offline": "⚠️ Lodgify is temporarily unavailable. I'm using the last known list of properties.",
        "sec_points_intro": "Quick check 👇",
        "sec_photos_list": "📸 Photos to send:",
        "sec_instructions": "Send the photos 📷 then tap ✅",
        "sec_next": "✅ Next",
        "sec_photo_seen": "📷 Received — {x} ✓",
        "sec_photo_unseen": "📷 Received ✓",
        "sec_recap_missing": "⚠️ Missing: {miss}",
        "sec_recap_issues": "⚠️ To review:\n{iss}",
        "sec_complete": "📷 Add photos",
        "sec_pass": "➡️ Skip anyway",
        "sec_done": "✅ {titre} — done!",
        "sec_again_q": "Section done ✅ Another identical room to photograph?",
        "sec_again": "➕ Another (same room)",
        "sec_cont": "➡️ Next room",
        "sec_redo": "Ok 👍 resend the relevant photos.",
        "follow": "Just follow the current steps 🙂 Use the buttons and send the requested photos/videos.",
        "mission_cancel": "🚫 Current mission cancelled. You can start over with /start.",
        "mission_none": "No mission in progress to cancel. 🙂",
        "press_start": "Tap the button below to start a mission. 👇",
        "reg_ask_name": "Welcome to Genius BnB! 👋 Before we start, write me your first and last name:",
        "reg_name_short": "I need your full name (first and last) to continue:",
        "reg_thanks": "Thank you {name} ✅ Here is your code: {code}\nSend it to your manager so they can grant your access. I'll let you know as soon as it's done!",
        "reg_blocked": "{name}, your access isn't approved yet ⏳\nGive this code to your manager: {code}",
        "reg_authorized": "You're all set, your access is approved ✅ Welcome to the {soc} team! Tap /start to begin your first mission.",
        "reg_choose_role": "Welcome! 👋 To get started, tell me who you are:",
        "btn_role_admin": "👔 Manager / admin",
        "btn_role_agent": "🧹 Cleaning agent",
        "reg_ask_nom": "Great! What's your first and last name?",
        "reg_admin_entreprise": "What's the name of your company?",
        "reg_admin_role": "What's your role (e.g. manager, cleaning supervisor)?",
        "reg_agent_choose_co": "Which company do you work for? Pick from the list 👇",
        "reg_no_company": "No company is registered yet. Ask your manager to create their account first (as a manager).",
        "reg_pending": "Thank you {name} ✅ Your request for {soc} has been sent. You'll get a message as soon as a manager approves it. ⏳",
        "reg_already_pending": "Your request is already awaiting approval ⏳ We'll let you know as soon as it's done.",
        "reg_authorized_admin": "Your manager account is approved ✅ Welcome! Tap /admin to open your panel.",
        "reg_refused": "Your request wasn't approved. Please reach out to your manager for more details.",
        "no_appart": "No apartment with an upcoming departure.",
        "lodgify_err": "I can't fetch the apartment list. Please try again.",
        "cl_sdb": "Bathroom", "cl_wc": "Toilet", "cl_cuisine": "Kitchen",
        "cl_frigo": "Inside the fridge", "cl_lit": "Under the bed",
        "cl_chauffage": "Heating off?", "cl_fenetres": "Windows closed?",
        "cl_pq": "Toilet paper in stock?", "cl_poubelles": "Bins emptied?",
    },
    "es": {
        "welcome": "¡Hola {prenom}! 👋 Soy ALFRED, el asistente de limpieza de {soc}. Te acompaño paso a paso durante toda tu misión.\n\n¿List@ para empezar?",
        "btn_start": "🧹 Empezar mi misión",
        "btn_lang": "🌐 Cambiar idioma",
        "which_appart": "¡Genial! 🏠 Elige el apartamento que vas a limpiar hoy:",
        "appart_chosen": "Perfecto, vamos con {name} ✅\n\nPrimer paso: graba un vídeo corto del estado del apartamento al llegar (salón, dormitorios, cocina, baño). 📹",
        "video_avant_ok": "Vídeo de llegada recibido ✓\n\nPuedes empezar la limpieza 🧽 Tómate tu tiempo y hazlo bien. Cuando todo esté listo, pulsa el botón de abajo. 👇",
        "btn_done": "✅ He terminado la limpieza",
        "btn_incident": "⚠️ Reportar un problema",
        "menage_done": "¡Bien hecho! 👏 Pasamos al control final, paso a paso (algunas fotos + verificaciones). Es rápido, solo déjate guiar.",
        "point_photo": "📸 Paso {num}/{n} — {label}\nEnvía una foto como prueba.",
        "btn_yes": "✅ Sí", "btn_no": "⚠️ No",
        "point_confirm": "Paso {num}/{n} — {label}",
        "point_done": "Paso {num}/{n} — {label} → {mark}",
        "checklist_done": "¡Checklist completada, buen trabajo! 🎉 Último paso: graba un vídeo corto del apartamento limpio y listo para recibir huéspedes. 📹",
        "photo_ok": "Foto recibida ✓ ¡Gracias!",
        "photo_doute": "🤔 Mmm, esta foto no parece mostrar « {label} » ({raison}). Puedes conservarla igualmente o hacer otra.",
        "btn_keep_photo": "✅ Conservar igualmente",
        "btn_retake_photo": "📷 Repetir la foto",
        "photo_retake": "Sin problema, envía la foto correcta 📷",
        "a_panel": "🔧 Espacio admin — ¡hola {prenom}!\n\nEscríbeme directamente tus preguntas sobre tus informes, fotos y datos de limpieza (ej. « ¿qué alojamientos se limpiaron hoy? », « fotos de X ayer », « genera un informe »).\nLos botones de abajo sirven para gestionar tu equipo. Pulsa /start para salir.",
        "a_b_agents": "👥 Gestionar agentes",
        "a_b_logements": "🏠 Asignar alojamientos",
        "a_b_admins": "🧑‍💼 Gestionar responsables",
        "a_reports": "📊 Modo informe activado.\nHaz tu pregunta (alojamientos limpiados hoy, incidentes urgentes de la semana, « genera un informe »…). Pulsa /start para salir.",
        "a_agents_none": "👥 Ningún agente por ahora. Cuando un agente se registre para tu empresa, recibirás un mensaje con un botón Autorizar.",
        "a_agents_mine": "👥 Tus agentes:",
        "a_agents_all": "👥 Todos los agentes:",
        "a_remove": "❌ Quitar {nom}",
        "a_admins_title": "🧑‍💼 Responsables:",
        "a_admins_none": "(Ningún otro responsable por ahora.)",
        "a_super_only": "Reservado al admin principal.",
        "a_log_title": "🏠 Asignar alojamientos\nPulsa un alojamiento para elegir su empresa (❓ = sin asignar).",
        "a_log_none": "Ningún alojamiento encontrado en Lodgify.",
        "a_log_err": "No puedo obtener la lista de alojamientos. Inténtalo de nuevo.",
        "a_log_pick": "¿A qué empresa pertenece este alojamiento?",
        "a_unassigned": "❌ Sin asignar",
        "a_back": "⬅️ Volver",
        "a_super_co": "Antes de nada, configuremos tu perfil. ¿Cuál es el nombre de tu empresa?",
        "a_super_co_short": "Nombre de empresa demasiado corto, inténtalo de nuevo:",
        "a_super_role": "Perfecto. ¿Y cuál es tu rol? (ej. gerente)",
        "a_super_done": "✅ Perfil guardado.",
        "a_new_admin": "🆕 Nueva solicitud RESPONSABLE\n👤 {nom}\n🏢 {ent}\n💼 {role}\n\n¿Validar?",
        "a_new_agent": "🆕 Nueva solicitud AGENTE\n👤 {nom}\n🏢 {ent}\n\n¿Validar?",
        "a_btn_auth": "✅ Autorizar",
        "a_btn_refuse": "❌ Rechazar",
        "a_done_admin": "✅ {nom} validado (responsable — {ent}).",
        "a_done_agent": "✅ {nom} validado (agente — {ent}).",
        "a_refused": "❌ Solicitud de {nom} rechazada.",
        "a_already": "Esta solicitud ya ha sido tratada.",
        "a_agent_removed": "🗑️ Agente quitado: {nom}.",
        "a_admin_removed": "🗑️ Responsable quitado: {nom}.",
        "mission_archived": "Misión completada ✓ Todo está guardado, ¡gracias por tu trabajo! 🙌\nEstado: {statut}.",
        "st_ok": "Validado", "st_check": "Por revisar",
        "incident_prompt": "Describe el problema en pocas palabras (en tu idioma), o envía una foto. Aviso al responsable enseguida. 📝",
        "incident_photo_ok": "Foto del problema recibida ✓ Añade una breve descripción de texto para ayudarme a entender.",
        "incident_ack": "Anotado y enviado al responsable ✓ Puedes continuar tu misión, ¡gracias!",
        "resume": "Seguimos donde lo dejaste 👍",
        "send_fin": "Cuando estés list@, envía el vídeo final (apartamento limpio y ordenado). 📹",
        "send_avant": "Cuando estés list@, envía el vídeo de llegada. 📹",
        "not_video": "No espero un vídeo ahora mismo 🙂",
        "not_photo": "No espero una foto en este paso 🙂",
        "tech_error": "⚠️ Ha ocurrido un pequeño problema técnico. Inténtalo de nuevo en un momento. Si continúa, escribe /start para empezar de nuevo.",
        "lodgify_offline": "⚠️ Lodgify no está disponible por el momento. Uso la última lista conocida de alojamientos.",
        "sec_points_intro": "Un vistazo rápido 👇",
        "sec_photos_list": "📸 Fotos a enviar:",
        "sec_instructions": "Envía las fotos 📷 y pulsa ✅",
        "sec_next": "✅ Siguiente",
        "sec_photo_seen": "📷 Recibida — {x} ✓",
        "sec_photo_unseen": "📷 Recibida ✓",
        "sec_recap_missing": "⚠️ Falta: {miss}",
        "sec_recap_issues": "⚠️ Revisar:\n{iss}",
        "sec_complete": "📷 Añadir fotos",
        "sec_pass": "➡️ Continuar igualmente",
        "sec_done": "✅ {titre} — ¡listo!",
        "sec_again_q": "Sección terminada ✅ ¿Otra estancia idéntica para fotografiar?",
        "sec_again": "➕ Otra (misma estancia)",
        "sec_cont": "➡️ Siguiente estancia",
        "sec_redo": "Ok 👍 reenvía las fotos correspondientes.",
        "follow": "Solo sigue los pasos actuales 🙂 Usa los botones y envía las fotos/vídeos pedidos.",
        "mission_cancel": "🚫 Misión en curso cancelada. Puedes empezar de nuevo con /start.",
        "mission_none": "No hay ninguna misión en curso que cancelar. 🙂",
        "press_start": "Pulsa el botón de abajo para empezar una misión. 👇",
        "reg_ask_name": "¡Bienvenid@ a Genius BnB! 👋 Antes de empezar, escríbeme tu nombre y apellido:",
        "reg_name_short": "Necesito tu nombre completo (nombre y apellido) para continuar:",
        "reg_thanks": "Gracias {name} ✅ Aquí tienes tu código: {code}\nEnvíaselo a tu responsable para que valide tu acceso. ¡Te aviso en cuanto esté listo!",
        "reg_blocked": "{name}, tu acceso aún no está aprobado ⏳\nDa este código a tu responsable: {code}",
        "reg_authorized": "¡Listo, tu acceso está aprobado ✅ Bienvenid@ al equipo de {soc}! Pulsa /start para empezar tu primera misión.",
        "reg_choose_role": "¡Bienvenid@! 👋 Para empezar, dime quién eres:",
        "btn_role_admin": "👔 Responsable / admin",
        "btn_role_agent": "🧹 Agente de limpieza",
        "reg_ask_nom": "¡Perfecto! ¿Cuál es tu nombre y apellido?",
        "reg_admin_entreprise": "¿Cuál es el nombre de tu empresa?",
        "reg_admin_role": "¿Cuál es tu rol (ej. gerente, responsable de limpieza)?",
        "reg_agent_choose_co": "¿Para qué empresa trabajas? Elige en la lista 👇",
        "reg_no_company": "Aún no hay ninguna empresa registrada. Pide a tu responsable que cree primero su cuenta (como responsable).",
        "reg_pending": "Gracias {name} ✅ Tu solicitud para {soc} ha sido enviada. Recibirás un mensaje en cuanto un responsable la valide. ⏳",
        "reg_already_pending": "Tu solicitud ya está esperando validación ⏳ Te avisamos en cuanto esté lista.",
        "reg_authorized_admin": "Tu cuenta de responsable está validada ✅ ¡Bienvenid@! Pulsa /admin para abrir tu panel.",
        "reg_refused": "Tu solicitud no fue aceptada. Contacta con tu responsable para más detalles.",
        "no_appart": "Ningún apartamento con salida próxima.",
        "lodgify_err": "No puedo obtener la lista de apartamentos. Inténtalo de nuevo.",
        "cl_sdb": "Baño", "cl_wc": "WC", "cl_cuisine": "Cocina",
        "cl_frigo": "Interior del frigorífico", "cl_lit": "Debajo de la cama",
        "cl_chauffage": "¿Calefacción apagada?", "cl_fenetres": "¿Ventanas cerradas?",
        "cl_pq": "¿Papel higiénico de reserva?", "cl_poubelles": "¿Basuras vaciadas?",
    },
    "ar": {
        "welcome": "مرحباً {prenom}! 👋 أنا ALFRED، مساعد التنظيف في {soc}. سأرافقك خطوة بخطوة طوال مهمتك.\n\nهل أنت مستعد للبدء؟",
        "btn_start": "🧹 ابدأ مهمتي",
        "btn_lang": "🌐 تغيير اللغة",
        "which_appart": "رائع! 🏠 اختر الشقة التي ستنظفها اليوم:",
        "appart_chosen": "ممتاز، لنبدأ مع {name} ✅\n\nالخطوة الأولى: صوّر فيديو قصيراً لحالة الشقة عند وصولك (الصالة، غرف النوم، المطبخ، الحمام). 📹",
        "video_avant_ok": "تم استلام فيديو الوصول ✓\n\nيمكنك البدء بالتنظيف 🧽 خذ وقتك وأنجز العمل جيداً. عند الانتهاء من كل شيء، اضغط الزر بالأسفل. 👇",
        "btn_done": "✅ أنهيت التنظيف",
        "btn_incident": "⚠️ الإبلاغ عن مشكلة",
        "menage_done": "أحسنت! 👏 ننتقل الآن إلى الفحص النهائي، خطوة بخطوة (بعض الصور + تأكيدات). الأمر سريع، فقط اتبع الإرشادات.",
        "point_photo": "📸 الخطوة {num}/{n} — {label}\nأرسل صورة كدليل.",
        "btn_yes": "✅ نعم", "btn_no": "⚠️ لا",
        "point_confirm": "الخطوة {num}/{n} — {label}",
        "point_done": "الخطوة {num}/{n} — {label} ← {mark}",
        "checklist_done": "اكتملت القائمة، عمل رائع! 🎉 الخطوة الأخيرة: صوّر فيديو قصيراً للشقة نظيفة وجاهزة لاستقبال الضيوف. 📹",
        "photo_ok": "تم استلام الصورة ✓ شكراً!",
        "photo_doute": "🤔 يبدو أن هذه الصورة لا تُظهر « {label} » ({raison}). يمكنك الاحتفاظ بها على أي حال، أو التقاط صورة أخرى.",
        "btn_keep_photo": "✅ الاحتفاظ بها",
        "btn_retake_photo": "📷 إعادة التقاط الصورة",
        "photo_retake": "لا مشكلة، أرسل الصورة الصحيحة 📷",
        "a_panel": "🔧 مساحة المسؤول — مرحباً {prenom}!\n\nاكتب لي أسئلتك مباشرة عن تقاريرك وصورك وبيانات التنظيف (مثال: « ما الشقق التي نُظّفت اليوم؟ »، « صور X أمس »، « أنشئ تقريراً »).\nالأزرار بالأسفل لإدارة فريقك. اضغط /start للخروج.",
        "a_b_agents": "👥 إدارة العمال",
        "a_b_logements": "🏠 تعيين الشقق",
        "a_b_admins": "🧑‍💼 إدارة المسؤولين",
        "a_reports": "📊 وضع التقارير مُفعّل.\nاطرح سؤالك (الشقق المنظّفة اليوم، الحوادث العاجلة هذا الأسبوع، « أنشئ تقريراً »…). اضغط /start للخروج.",
        "a_agents_none": "👥 لا يوجد عامل بعد. عندما يسجّل عامل لشركتك، ستصلك رسالة بزر التفعيل.",
        "a_agents_mine": "👥 عمالك:",
        "a_agents_all": "👥 جميع العمال:",
        "a_remove": "❌ إزالة {nom}",
        "a_admins_title": "🧑‍💼 المسؤولون:",
        "a_admins_none": "(لا يوجد مسؤول آخر بعد.)",
        "a_super_only": "خاص بالمسؤول الرئيسي.",
        "a_log_title": "🏠 تعيين الشقق\nاضغط على شقة لاختيار شركتها (❓ = غير معيّنة).",
        "a_log_none": "لا توجد شقة في Lodgify.",
        "a_log_err": "لا أستطيع جلب قائمة الشقق. حاول مرة أخرى.",
        "a_log_pick": "لأي شركة تنتمي هذه الشقة؟",
        "a_unassigned": "❌ غير معيّنة",
        "a_back": "⬅️ رجوع",
        "a_super_co": "أولاً، لنُعدّ ملفك. ما اسم شركتك؟",
        "a_super_co_short": "اسم الشركة قصير جداً، حاول مرة أخرى:",
        "a_super_role": "ممتاز. وما هو دورك؟ (مثال: مدير)",
        "a_super_done": "✅ تم حفظ الملف.",
        "a_new_admin": "🆕 طلب مسؤول جديد\n👤 {nom}\n🏢 {ent}\n💼 {role}\n\nالموافقة؟",
        "a_new_agent": "🆕 طلب عامل جديد\n👤 {nom}\n🏢 {ent}\n\nالموافقة؟",
        "a_btn_auth": "✅ تفعيل",
        "a_btn_refuse": "❌ رفض",
        "a_done_admin": "✅ تم تفعيل {nom} (مسؤول — {ent}).",
        "a_done_agent": "✅ تم تفعيل {nom} (عامل — {ent}).",
        "a_refused": "❌ تم رفض طلب {nom}.",
        "a_already": "تمت معالجة هذا الطلب بالفعل.",
        "a_agent_removed": "🗑️ تمت إزالة العامل: {nom}.",
        "a_admin_removed": "🗑️ تمت إزالة المسؤول: {nom}.",
        "mission_archived": "اكتملت المهمة ✓ تم حفظ كل شيء، شكراً على عملك! 🙌\nالحالة: {statut}.",
        "st_ok": "صالح", "st_check": "للمراجعة",
        "incident_prompt": "صِف المشكلة بكلمات قليلة (بلغتك)، أو أرسل صورة. سأبلّغ المسؤول على الفور. 📝",
        "incident_photo_ok": "تم استلام صورة المشكلة ✓ أضف وصفاً نصياً قصيراً ليساعدني على الفهم.",
        "incident_ack": "تم التسجيل والإرسال إلى المسؤول ✓ يمكنك متابعة مهمتك، شكراً!",
        "resume": "لنكمل من حيث توقفت 👍",
        "send_fin": "عندما تكون جاهزاً، أرسل فيديو النهاية (الشقة نظيفة ومرتبة). 📹",
        "send_avant": "عندما تكون جاهزاً، أرسل فيديو الوصول. 📹",
        "not_video": "لا أنتظر فيديو الآن 🙂",
        "not_photo": "لا أنتظر صورة في هذه الخطوة 🙂",
        "tech_error": "⚠️ حدث خلل تقني بسيط. حاول مرة أخرى بعد لحظات. إذا استمر الأمر، اكتب /start للبدء من جديد.",
        "lodgify_offline": "⚠️ Lodgify غير متاح مؤقتاً. أستخدم آخر قائمة معروفة للعقارات.",
        "sec_points_intro": "نظرة سريعة 👇",
        "sec_photos_list": "📸 الصور المطلوبة:",
        "sec_instructions": "أرسل الصور 📷 ثم اضغط ✅",
        "sec_next": "✅ التالي",
        "sec_photo_seen": "📷 تم الاستلام — {x} ✓",
        "sec_photo_unseen": "📷 تم الاستلام ✓",
        "sec_recap_missing": "⚠️ ناقص: {miss}",
        "sec_recap_issues": "⚠️ للمراجعة:\n{iss}",
        "sec_complete": "📷 إضافة صور",
        "sec_pass": "➡️ المتابعة على أي حال",
        "sec_done": "✅ {titre} — تم!",
        "sec_again_q": "انتهى القسم ✅ هل هناك غرفة أخرى مماثلة لتصويرها؟",
        "sec_again": "➕ أخرى (نفس الغرفة)",
        "sec_cont": "➡️ الغرفة التالية",
        "sec_redo": "حسناً 👍 أعد إرسال الصور المعنية.",
        "follow": "فقط اتبع الخطوات الحالية 🙂 استخدم الأزرار وأرسل الصور/الفيديوهات المطلوبة.",
        "mission_cancel": "🚫 تم إلغاء المهمة الجارية. يمكنك البدء من جديد عبر /start.",
        "mission_none": "لا توجد مهمة جارية لإلغائها. 🙂",
        "press_start": "اضغط الزر بالأسفل لبدء مهمة. 👇",
        "reg_ask_name": "مرحباً بك في Genius BnB! 👋 قبل أن نبدأ، اكتب لي اسمك الأول واسم العائلة:",
        "reg_name_short": "أحتاج اسمك الكامل (الاسم واللقب) للمتابعة:",
        "reg_thanks": "شكراً {name} ✅ هذا رمزك: {code}\nأرسله إلى المسؤول ليُفعّل وصولك. سأخبرك بمجرد أن يصبح جاهزاً!",
        "reg_blocked": "{name}، لم تتم الموافقة على وصولك بعد ⏳\nأعطِ هذا الرمز للمسؤول: {code}",
        "reg_authorized": "تم، تمت الموافقة على وصولك ✅ مرحباً بك في فريق {soc}! اضغط /start لبدء مهمتك الأولى.",
        "reg_choose_role": "مرحباً بك! 👋 لنبدأ، أخبرني من أنت:",
        "btn_role_admin": "👔 مسؤول / مدير",
        "btn_role_agent": "🧹 عامل تنظيف",
        "reg_ask_nom": "ممتاز! ما اسمك الأول واسم العائلة؟",
        "reg_admin_entreprise": "ما اسم شركتك؟",
        "reg_admin_role": "ما هو دورك (مثال: مدير، مسؤول تنظيف)؟",
        "reg_agent_choose_co": "لأي شركة تعمل؟ اختر من القائمة 👇",
        "reg_no_company": "لا توجد أي شركة مسجّلة بعد. اطلب من مسؤولك إنشاء حسابه أولاً (كمسؤول).",
        "reg_pending": "شكراً {name} ✅ تم إرسال طلبك إلى {soc}. ستصلك رسالة بمجرد موافقة المسؤول عليه. ⏳",
        "reg_already_pending": "طلبك قيد المراجعة بالفعل ⏳ سنخبرك بمجرد أن يصبح جاهزاً.",
        "reg_authorized_admin": "تمت الموافقة على حساب المسؤول الخاص بك ✅ مرحباً! اضغط /admin لفتح لوحتك.",
        "reg_refused": "لم تتم الموافقة على طلبك. تواصل مع مسؤولك لمزيد من التفاصيل.",
        "no_appart": "لا توجد شقة بمغادرة قادمة.",
        "lodgify_err": "لا أستطيع جلب قائمة الشقق. حاول مرة أخرى.",
        "cl_sdb": "الحمام", "cl_wc": "المرحاض", "cl_cuisine": "المطبخ",
        "cl_frigo": "داخل الثلاجة", "cl_lit": "تحت السرير",
        "cl_chauffage": "هل التدفئة مطفأة؟", "cl_fenetres": "هل النوافذ مغلقة؟",
        "cl_pq": "ورق مرحاض احتياطي؟", "cl_poubelles": "هل أُفرغت القمامة؟",
    },
    "ro": {
        "welcome": "Bună {prenom}! 👋 Sunt ALFRED, asistentul de curățenie al {soc}. Te ghidez pas cu pas pe tot parcursul misiunii.\n\nGata să începi?",
        "btn_start": "🧹 Începe misiunea mea",
        "btn_lang": "🌐 Schimbă limba",
        "which_appart": "Super! 🏠 Alege apartamentul pe care îl cureți azi:",
        "appart_chosen": "Perfect, mergem cu {name} ✅\n\nPrimul pas: filmează un video scurt cu starea apartamentului la sosire (living, dormitoare, bucătărie, baie). 📹",
        "video_avant_ok": "Video de sosire primit ✓\n\nPoți începe curățenia 🧽 Lucrează pe îndelete și fă treabă bună. Când ai terminat tot, apasă butonul de mai jos. 👇",
        "btn_done": "✅ Am terminat curățenia",
        "btn_incident": "⚠️ Raportează o problemă",
        "menage_done": "Bravo! 👏 Trecem la verificarea finală, pas cu pas (câteva poze + confirmări). E rapid, lasă-te ghidat.",
        "point_photo": "📸 Pasul {num}/{n} — {label}\nTrimite o poză ca dovadă.",
        "btn_yes": "✅ Da", "btn_no": "⚠️ Nu",
        "point_confirm": "Pasul {num}/{n} — {label}",
        "point_done": "Pasul {num}/{n} — {label} → {mark}",
        "checklist_done": "Listă completă, treabă bună! 🎉 Ultimul pas: filmează un video scurt cu apartamentul curat și pregătit să primească oaspeții. 📹",
        "photo_ok": "Poză primită ✓ Mulțumesc!",
        "photo_doute": "🤔 Hmm, această poză nu pare să arate « {label} » ({raison}). O poți păstra oricum sau poți face alta.",
        "btn_keep_photo": "✅ Păstrează oricum",
        "btn_retake_photo": "📷 Refă poza",
        "photo_retake": "Nicio problemă, trimite poza corectă 📷",
        "a_panel": "🔧 Spațiu admin — bună {prenom}!\n\nScrie-mi direct întrebările despre rapoartele, pozele și datele tale de curățenie (ex. « ce locuințe au fost curățate azi? », « pozele de la X ieri », « generează un raport »).\nButoanele de mai jos servesc la gestionarea echipei. Apasă /start pentru a ieși.",
        "a_b_agents": "👥 Gestionează agenții",
        "a_b_logements": "🏠 Atribuie locuințele",
        "a_b_admins": "🧑‍💼 Gestionează responsabilii",
        "a_reports": "📊 Mod raport activat.\nPune-ți întrebarea (locuințe curățate azi, incidente urgente din săptămână, « generează un raport »…). Apasă /start pentru a ieși.",
        "a_agents_none": "👥 Niciun agent deocamdată. Când un agent se înscrie pentru firma ta, primești un mesaj cu un buton Autorizează.",
        "a_agents_mine": "👥 Agenții tăi:",
        "a_agents_all": "👥 Toți agenții:",
        "a_remove": "❌ Elimină {nom}",
        "a_admins_title": "🧑‍💼 Responsabili:",
        "a_admins_none": "(Niciun alt responsabil deocamdată.)",
        "a_super_only": "Rezervat administratorului principal.",
        "a_log_title": "🏠 Atribuie locuințele\nApasă o locuință pentru a alege firma ei (❓ = neatribuită).",
        "a_log_none": "Nicio locuință găsită în Lodgify.",
        "a_log_err": "Nu pot prelua lista locuințelor. Încearcă din nou.",
        "a_log_pick": "Cărei firme îi aparține această locuință?",
        "a_unassigned": "❌ Neatribuită",
        "a_back": "⬅️ Înapoi",
        "a_super_co": "Mai întâi, să-ți configurăm profilul. Care este numele firmei tale?",
        "a_super_co_short": "Numele firmei e prea scurt, încearcă din nou:",
        "a_super_role": "Perfect. Și care este rolul tău? (ex: manager)",
        "a_super_done": "✅ Profil salvat.",
        "a_new_admin": "🆕 Cerere nouă RESPONSABIL\n👤 {nom}\n🏢 {ent}\n💼 {role}\n\nValidezi?",
        "a_new_agent": "🆕 Cerere nouă AGENT\n👤 {nom}\n🏢 {ent}\n\nValidezi?",
        "a_btn_auth": "✅ Autorizează",
        "a_btn_refuse": "❌ Refuză",
        "a_done_admin": "✅ {nom} validat (responsabil — {ent}).",
        "a_done_agent": "✅ {nom} validat (agent — {ent}).",
        "a_refused": "❌ Cerere de la {nom} refuzată.",
        "a_already": "Această cerere a fost deja tratată.",
        "a_agent_removed": "🗑️ Agent eliminat: {nom}.",
        "a_admin_removed": "🗑️ Responsabil eliminat: {nom}.",
        "mission_archived": "Misiune completă ✓ Totul este salvat, mulțumesc pentru munca ta! 🙌\nStare: {statut}.",
        "st_ok": "Validat", "st_check": "De verificat",
        "incident_prompt": "Descrie problema în câteva cuvinte (în limba ta), sau trimite o poză. Anunț responsabilul imediat. 📝",
        "incident_photo_ok": "Poza problemei primită ✓ Adaugă o scurtă descriere text ca să mă ajuți să înțeleg.",
        "incident_ack": "Notat și transmis responsabilului ✓ Poți continua misiunea, mulțumesc!",
        "resume": "Continuăm de unde ai rămas 👍",
        "send_fin": "Când ești gata, trimite videoul final (apartament curat și aranjat). 📹",
        "send_avant": "Când ești gata, trimite videoul de sosire. 📹",
        "not_video": "Nu aștept un video acum 🙂",
        "not_photo": "Nu aștept o poză la acest pas 🙂",
        "tech_error": "⚠️ A apărut o mică problemă tehnică. Încearcă din nou într-o clipă. Dacă persistă, scrie /start pentru a reîncepe.",
        "lodgify_offline": "⚠️ Lodgify este temporar indisponibil. Folosesc ultima listă cunoscută de locuințe.",
        "sec_points_intro": "O privire rapidă 👇",
        "sec_photos_list": "📸 Poze de trimis:",
        "sec_instructions": "Trimite pozele 📷 apoi apasă ✅",
        "sec_next": "✅ Următorul",
        "sec_photo_seen": "📷 Primită — {x} ✓",
        "sec_photo_unseen": "📷 Primită ✓",
        "sec_recap_missing": "⚠️ Lipsește: {miss}",
        "sec_recap_issues": "⚠️ De verificat:\n{iss}",
        "sec_complete": "📷 Adaugă poze",
        "sec_pass": "➡️ Continuă oricum",
        "sec_done": "✅ {titre} — gata!",
        "sec_again_q": "Secțiune gata ✅ Mai e o cameră identică de fotografiat?",
        "sec_again": "➕ Alta (aceeași cameră)",
        "sec_cont": "➡️ Camera următoare",
        "sec_redo": "Ok 👍 retrimite pozele vizate.",
        "follow": "Urmează pur și simplu pașii curenți 🙂 Folosește butoanele și trimite pozele/videourile cerute.",
        "mission_cancel": "🚫 Misiune în curs anulată. Poți reîncepe cu /start.",
        "mission_none": "Nicio misiune în curs de anulat. 🙂",
        "press_start": "Apasă butonul de mai jos pentru a începe o misiune. 👇",
        "reg_ask_name": "Bun venit la Genius BnB! 👋 Înainte să începem, scrie-mi numele și prenumele tău:",
        "reg_name_short": "Am nevoie de numele tău complet (nume și prenume) ca să continui:",
        "reg_thanks": "Mulțumesc {name} ✅ Acesta este codul tău: {code}\nTrimite-l responsabilului ca să-ți aprobe accesul. Te anunț imediat ce e gata!",
        "reg_blocked": "{name}, accesul tău nu este încă aprobat ⏳\nDă acest cod responsabilului: {code}",
        "reg_authorized": "Gata, accesul tău este aprobat ✅ Bun venit în echipa {soc}! Apasă /start pentru a începe prima misiune.",
        "reg_choose_role": "Bun venit! 👋 Ca să începem, spune-mi cine ești:",
        "btn_role_admin": "👔 Responsabil / admin",
        "btn_role_agent": "🧹 Agent de curățenie",
        "reg_ask_nom": "Perfect! Care este numele și prenumele tău?",
        "reg_admin_entreprise": "Care este numele firmei tale?",
        "reg_admin_role": "Care este rolul tău (ex: manager, responsabil curățenie)?",
        "reg_agent_choose_co": "Pentru ce firmă lucrezi? Alege din listă 👇",
        "reg_no_company": "Încă nu este înregistrată nicio firmă. Roagă-ți responsabilul să-și creeze mai întâi contul (ca responsabil).",
        "reg_pending": "Mulțumesc {name} ✅ Cererea ta pentru {soc} a fost trimisă. Vei primi un mesaj imediat ce un responsabil o validează. ⏳",
        "reg_already_pending": "Cererea ta așteaptă deja validarea ⏳ Te anunțăm imediat ce e gata.",
        "reg_authorized_admin": "Contul tău de responsabil este validat ✅ Bun venit! Apasă /admin pentru a deschide panoul.",
        "reg_refused": "Cererea ta nu a fost acceptată. Contactează responsabilul pentru mai multe detalii.",
        "no_appart": "Niciun apartament cu plecare apropiată.",
        "lodgify_err": "Nu pot prelua lista apartamentelor. Încearcă din nou.",
        "cl_sdb": "Baie", "cl_wc": "Toaletă", "cl_cuisine": "Bucătărie",
        "cl_frigo": "Interiorul frigiderului", "cl_lit": "Sub pat",
        "cl_chauffage": "Încălzire oprită?", "cl_fenetres": "Ferestre închise?",
        "cl_pq": "Hârtie igienică în rezervă?", "cl_poubelles": "Gunoi golit?",
    },
}


def norm_lang(code: str | None) -> str:
    code = (code or "").lower()
    for l in SUPPORTED:
        if code.startswith(l):
            return l
    return "fr"


def t(lang: str, key: str, **kw) -> str:
    table = T.get(lang) or T["fr"]
    s = table.get(key) or T["fr"].get(key, key)
    return s.format(**kw) if kw else s


def label_fr(key: str) -> str:
    return T["fr"].get(key, key)


# =====================================================================
# CHECKLIST (codee en dur ; libelles traduits via les cles cl_*)
# =====================================================================
CHECKLIST = [
    {"titre": "1. 🛏️ Chambres", "repeat": True, "photos_min": 3, "points": [
        "Lit refait avec linge propre (draps + housse + taies), borde, sans tache",
        "Aucun poil / cheveu sur le lit",
        "Surfaces depoussierees (tables de nuit, lampes, etageres, plinthes)",
        "Poignees + interrupteurs essuyes",
        "Miroir sans traces",
    ], "photos": [
        "Vue d'ensemble",
        "Lit fait",
        "Sous le lit",
        "Sol",
    ]},
    {"titre": "2. 🍳 Cuisine", "photos_min": 4, "points": [
        "Lave-vaisselle vide",
        "Evier + plan de travail propres et degraisses",
        "Bouilloire + cafetiere propres et detartrees",
        "Torchons + eponge propres",
        "Produit vaisselle present",
    ], "photos": [
        "Vue d'ensemble",
        "Interieur four",
        "Interieur micro-ondes",
        "Interieur frigo / congelateur",
        "Interieur lave-vaisselle",
        "Evier",
        "Tiroir / armoires vaisselle et casseroles",
    ]},
    {"titre": "3. 🚿 Salle de bain", "repeat": True, "photos_min": 3, "points": [
        "Lavabo lave (vasque + robinetterie)",
        "Miroir, carrelage et joints propres (sans traces ni moisissure)",
    ], "photos": [
        "Vue d'ensemble",
        "Miroir",
        "Evier",
        "Douche / baignoire",
        "Siphon",
        "Sol",
    ]},
    {"titre": "4. 🚽 Toilettes", "repeat": True, "photos_min": 2, "points": [
        "Papier toilette present (au moins 2 rouleaux)",
    ], "photos": [
        "Vue d'ensemble",
        "WC lunette ouverte",
    ]},
    {"titre": "5. 🛋️ Salon", "photos_min": 2, "points": [
        "Canape propre, sans tache",
        "Coussins remis en place",
        "Surfaces depoussierees + poignees / interrupteurs essuyes",
        "TV presente",
        "Telecommande(s) presente(s) + piles OK",
    ], "photos": [
        "Vue d'ensemble",
        "Sol",
        "Table / chaises",
        "Canapes",
    ]},
    {"titre": "6. ✅ General (fin de menage)", "photos_min": 1, "points": [
        "Tous les sols aspires (toutes les pieces)",
        "Tous les sols laves (toutes les pieces)",
        "Toutes les vitres et fenetres sans traces",
        "Toutes les poubelles videes + sacs neufs",
        "Aucune toile d'araignee",
        "Aucun insecte / salete residuelle",
        "Toutes les ampoules fonctionnent",
        "Seche-cheveux present et fonctionnel",
        "Fer a repasser present",
        "Sacs poubelle en reserve (3-4)",
        "Chauffage eteint",
        "Serviettes et draps fournis pour le nombre de voyageurs",
        "Lumieres eteintes + fenetres fermees",
        "Porte verrouillee",
    ], "photos": [
        "Cles remises dans la boite a cles",
    ]},
]

# --- Traductions de la checklist (auto par Claude, mises en cache sur disque) ---
CHECKLIST_I18N_FILE = os.path.join(BASE_DIR, "checklist_i18n_v3.json")
CHECKLIST_CACHE = {"fr": CHECKLIST}
try:
    with open(CHECKLIST_I18N_FILE, encoding="utf-8") as _f:
        CHECKLIST_CACHE.update(json.load(_f))
except Exception:
    pass


def _save_checklist_cache() -> None:
    try:
        data = {k: v for k, v in CHECKLIST_CACHE.items() if k != "fr"}
        with open(CHECKLIST_I18N_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Echec sauvegarde checklist i18n")


async def translate_checklist(lang: str) -> list:
    payload = [{"titre": s["titre"], "points": s["points"], "photos": s["photos"]} for s in CHECKLIST]
    system = (
        f"Traduis fidelement en langue '{lang}' tous les textes du JSON (titre + points + photos). "
        "Garde EXACTEMENT la meme structure, les memes cles, le meme nombre d'elements et le meme ordre. "
        "Ne traduis pas les emojis. Reponds UNIQUEMENT avec le JSON traduit, rien d'autre."
    )
    raw = await claude_text(system, json.dumps(payload, ensure_ascii=False), max_tokens=6000)
    data = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
    if len(data) != len(CHECKLIST):
        raise ValueError("structure traduite incoherente")
    result = []
    for i, sec in enumerate(CHECKLIST):
        tr = data[i]
        pts = tr.get("points", []) or []
        phs = tr.get("photos", []) or []
        points = [pts[j] if j < len(pts) else sec["points"][j] for j in range(len(sec["points"]))]
        photos = [phs[j] if j < len(phs) else sec["photos"][j] for j in range(len(sec["photos"]))]
        result.append({"titre": tr.get("titre", sec["titre"]),
                        "photos_min": sec["photos_min"], "points": points, "photos": photos})
    return result


async def get_checklist(lang: str) -> list:
    if lang == "fr" or lang not in SUPPORTED:
        return CHECKLIST
    if lang in CHECKLIST_CACHE:
        return CHECKLIST_CACHE[lang]
    try:
        tr = await translate_checklist(lang)
    except Exception:
        logger.exception("Echec traduction checklist %s", lang)
        return CHECKLIST
    CHECKLIST_CACHE[lang] = tr
    _save_checklist_cache()
    return tr

# =====================================================================
# MEMOIRE D'ETAT
# =====================================================================
AGENTS: dict[int, dict] = {}

ETAPE_VIDEO_AVANT = "attente_video_avant"
ETAPE_MENAGE = "menage_en_cours"
ETAPE_CHECKLIST = "checklist"
ETAPE_VIDEO_FIN = "attente_video_fin"
ETAPE_INCIDENT = "incident"


def get_state(chat_id: int) -> dict:
    if chat_id not in AGENTS:
        AGENTS[chat_id] = {"prenom": None, "lang": AGENT_LANG.get(str(chat_id)),
                           "apparts_today": {}, "mission": None, "admin_mode": False,
                           "reg": None}
    return AGENTS[chat_id]


# --- Persistance des etats (survie aux redemarrages) ---
STATE_FILE = os.path.join(BASE_DIR, "state.json")


def save_state() -> None:
    """Sauvegarde les etats (missions en cours, etc.) sur disque, best-effort."""
    try:
        data = {}
        for cid, st in AGENTS.items():
            try:
                json.dumps(st)  # ne garde que ce qui est serialisable
            except Exception:
                continue
            data[str(cid)] = st
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        logger.exception("Echec sauvegarde etat")


def _sanitize_mission(st: dict) -> None:
    """Repare une mission rechargee du disque pour eviter tout plantage
    (champs manquants apres une mise a jour). Mission trop incomplete -> abandonnee."""
    if not isinstance(st, dict):
        return
    m = st.get("mission")
    if not isinstance(m, dict):
        return
    if not all(k in m for k in ("etape", "name", "property_id")):
        st["mission"] = None
        return
    media = m.get("media")
    if not isinstance(media, dict):
        media = {}
    media.setdefault("photos", [])
    media.setdefault("video_avant", None)
    media.setdefault("video_fin", None)
    m["media"] = media
    m.setdefault("sec_index", 0)
    m.setdefault("sec_photos", 0)
    m.setdefault("sec_seen", [])
    m.setdefault("sec_issues", [])
    m.setdefault("confirmations", {})
    m.setdefault("incidents", [])
    m.setdefault("controles", [])


def load_state() -> None:
    """Recharge les etats au demarrage : une mission en cours n'est plus perdue."""
    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for cid, st in data.items():
            try:
                _sanitize_mission(st)
                AGENTS[int(cid)] = st
            except Exception:
                logger.exception("Etat ignore pour %s", cid)
                continue
        logger.info("Etat recharge : %d conversation(s)", len(AGENTS))
    except Exception:
        logger.exception("Echec chargement etat")


async def _persist_state(update, context) -> None:
    """Sauvegarde l'etat apres chaque update (handler en dernier groupe)."""
    save_state()


async def on_error(update, context) -> None:
    """Filet de securite global : loggue toute erreur non geree et previent
    l'utilisateur au lieu de le laisser bloque sans message."""
    err = getattr(context, "error", None)
    # Erreur benigne de Telegram : on a voulu re-afficher un message deja identique
    # (double-appui sur un bouton). Aucun impact -> on ignore sans alerter l'utilisateur.
    if err is not None and "Message is not modified" in str(err):
        logger.info("Edition ignoree (message identique)")
        return
    logger.exception("Erreur non geree : %s", err)
    try:
        chat_id = None
        if isinstance(update, Update) and update.effective_chat:
            chat_id = update.effective_chat.id
        if not chat_id:
            return
        lang = "fr"
        try:
            st = AGENTS.get(chat_id) or {}
            lang = st.get("lang") or AGENT_LANG.get(str(chat_id)) or "fr"
        except Exception:
            pass
        msg = t(lang, "tech_error")
        # Diagnostic reserve au super-admin : il voit la cause technique exacte
        if is_super(chat_id):
            import traceback
            tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
            msg = (msg + "\n\n———\n🔧 Debug (super-admin) :\n"
                   + f"{type(err).__name__}: {err}\n\n" + tb)
            msg = msg[-3500:]
        await context.bot.send_message(chat_id, msg)
    except Exception:
        logger.exception("Echec envoi du message d'erreur a l'utilisateur")


def ui_lang(chat_id) -> str:
    """Langue d'interface d'une personne (admin/agent)."""
    return AGENT_LANG.get(str(chat_id)) or "fr"


def display_name(chat_id, state=None) -> str:
    """Nom a afficher : on prend le nom saisi a l'inscription en priorite (pas le pseudo Telegram)."""
    sc = str(chat_id)
    if sc in ADMINS and ADMINS[sc].get("prenom"):
        return ADMINS[sc]["prenom"]
    if sc in AGENTS_AUTH and AGENTS_AUTH[sc].get("prenom"):
        return AGENTS_AUTH[sc]["prenom"]
    if is_super(chat_id) and SUPER_PROFILE.get("prenom"):
        return SUPER_PROFILE["prenom"]
    return (state or {}).get("prenom") or ""


def new_mission(property_id: str, name: str) -> dict:
    return {
        "property_id": property_id, "name": name,
        "etape": ETAPE_VIDEO_AVANT,
        "sec_index": 0, "sec_photos": 0, "checklist": None,
        "media": {"video_avant": None, "photos": [], "video_fin": None},
        "confirmations": {}, "incidents": [], "controles": [],
        "incident_retour": None, "incident_pending": {},
        "debut": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(v, callback_data=f"lang:{k}")]
                                 for k, v in LANG_NAMES.items()])


def welcome_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_start"), callback_data="begin")],
        [InlineKeyboardButton(t(lang, "btn_lang"), callback_data="changelang")],
    ])


def menage_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_done"), callback_data="finmenage")],
        [InlineKeyboardButton(t(lang, "btn_incident"), callback_data="incident")],
    ])


# =====================================================================
# LODGIFY
# =====================================================================
RETRY_STATUSES = {429, 500, 502, 503, 529}


async def _http_retry(method: str, url: str, *, headers=None, params=None,
                      json_body=None, timeout: float = 60, tries: int = 3):
    """Requete HTTP avec 2-3 tentatives + pause croissante (1s, 2s...) sur erreurs
    passageres : 429/500/502/503/529, coupures reseau, timeouts. Leve au dernier echec.
    Les autres codes (400/404/409...) sont renvoyes tels quels, sans reessai."""
    delay = 1.0
    last_exc = None
    for attempt in range(tries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method.upper(), url, headers=headers,
                                            params=params, json=json_body)
            if resp.status_code in RETRY_STATUSES and attempt < tries - 1:
                logger.warning("HTTP %s sur %s -> nouvelle tentative dans %.0fs",
                               resp.status_code, url, delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return resp
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
            if attempt < tries - 1:
                logger.warning("Erreur reseau (%s) sur %s -> nouvelle tentative dans %.0fs",
                               type(e).__name__, url, delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise
    if last_exc:
        raise last_exc


async def _lodgify_get(path: str, params: dict | None = None):
    headers = {"accept": "application/json", "X-ApiKey": LODGIFY_API_KEY}
    resp = await _http_retry("GET", LODGIFY_BASE + path, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


LODGIFY_ROOT = "https://api.lodgify.com"


async def lodgify_api(method: str, path: str, params: dict | None = None, body: dict | None = None):
    """Appel generique a l'API Lodgify (tous verbes, v1 ou v2). Renvoie (status, data)."""
    if not path.startswith("/"):
        path = "/" + path
    url = LODGIFY_ROOT + path
    headers = {"accept": "application/json", "X-ApiKey": LODGIFY_API_KEY,
               "content-type": "application/json"}
    resp = await _http_retry(method, url, headers=headers, params=params, json_body=body, timeout=40)
    try:
        data = resp.json()
    except Exception:
        data = resp.text
    return resp.status_code, data


def _items(data):
    if isinstance(data, dict):
        return data.get("items") or data.get("data") or []
    if isinstance(data, list):
        return data
    return []


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


# =====================================================================
# CLAUDE (incident)
# =====================================================================
async def analyser_incident(texte: str, lang: str) -> dict | None:
    if not ANTHROPIC_API_KEY:
        return None
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    system = (
        "Tu assistes une societe de conciergerie (locations courte duree). "
        "Un agent de menage signale un probleme rencontre dans un logement. "
        "Reponds STRICTEMENT en JSON (aucun texte autour), avec exactement ces cles : "
        '"langue" (code court: fr, ar, ro, en, es), '
        '"resume" (1 a 2 phrases claires EN FRANCAIS pour le responsable), '
        '"urgent" (true si securite/degat des eaux/serrure/chauffage casse/danger, sinon false), '
        f'"reponse_agent" (courte confirmation rassurante dans la langue de l agent, code "{lang}").'
    )
    body = {"model": ANTHROPIC_MODEL, "max_tokens": 400, "system": system,
            "messages": [{"role": "user", "content": texte}]}
    r = await _http_retry("POST", "https://api.anthropic.com/v1/messages",
                          headers=headers, json_body=body, timeout=40)
    r.raise_for_status()
    data = r.json()
    txt = data["content"][0]["text"]
    return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])


# =====================================================================
# ADMIN : questions en langage naturel sur les rapports (Claude sur les archives)
# =====================================================================
def is_super(chat_id) -> bool:
    """Admin principal (defini dans .env)."""
    return bool(MANAGER_CHAT_ID) and str(chat_id) == str(MANAGER_CHAT_ID)


def is_admin(chat_id) -> bool:
    return is_super(chat_id) or str(chat_id) in ADMINS


def all_admin_ids() -> list:
    ids = []
    if MANAGER_CHAT_ID:
        ids.append(str(MANAGER_CHAT_ID))
    for k in ADMINS:
        if k not in ids:
            ids.append(k)
    return ids


def load_reports() -> list[dict]:
    """Lit tous les rapports JSON et renvoie une version compacte pour l'analyse."""
    out = []
    for fp in glob.glob(os.path.join(ARCHIVES_DIR, "**", "*.json"), recursive=True):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        out.append({
            "date_debut": d.get("heure_debut"),
            "date_fin": d.get("heure_fin"),
            "appartement": d.get("appart", {}).get("nom_interne"),
            "property_id": d.get("appart", {}).get("property_id"),
            "agent": d.get("agent", {}).get("prenom"),
            "statut": d.get("statut"),
            "nb_photos": len(d.get("photos", [])),
            "confirmations": d.get("confirmations", {}),
            "incidents": [{"resume": i.get("resume"), "urgent": i.get("urgent")}
                          for i in d.get("incidents", [])],
        })
    return out


async def _fetch_bookings_pages() -> list:
    """Recupere les reservations (passees + futures) via stayFilter=All, paginees.
    Repli sur la requete simple si stayFilter=All n'est pas accepte."""
    all_items: list = []
    try:
        page = 1
        while page <= 15:
            data = await _lodgify_get("/reservations/bookings",
                                      params={"stayFilter": "All", "size": 200, "page": page})
            items = _items(data)
            if not items:
                break
            all_items.extend(items)
            if len(items) < 200:
                break
            page += 1
    except Exception:
        logger.exception("stayFilter=All refuse, repli simple")
        all_items = _items(await _lodgify_get("/reservations/bookings", params={"size": 200}))
    return all_items


async def load_checkouts() -> list[dict]:
    """Departs / check-outs Lodgify (planning), pour l'assistant admin.
    On garde une fenetre [aujourd'hui-90j ; aujourd'hui+120j] pour rester rapide."""
    props = _items(await _lodgify_get("/properties", params={"size": 200}))
    name_by_id: dict = {}
    for p in props:
        pid = _first(p, "id", "property_id")
        internal = str(_first(p, "internal_name", default="")).strip()
        if not internal or internal.lower() == "empty":
            internal = str(_first(p, "name", default="")).strip() or f"Appart {pid}"
        if pid is not None:
            name_by_id[str(pid)] = internal

    today = datetime.date.today()
    win_start = today - datetime.timedelta(days=90)
    win_end = today + datetime.timedelta(days=120)

    out = []
    for b in await _fetch_bookings_pages():
        pid = _first(b, "property_id", "propertyId")
        dep = _first(b, "departure", "checkOut", "check_out", default="")
        arr = _first(b, "arrival", "checkIn", "check_in", default="")
        dep10 = str(dep)[:10] if dep else None
        if dep10:
            try:
                d = datetime.date.fromisoformat(dep10)
                if not (win_start <= d <= win_end):
                    continue
            except ValueError:
                pass
        out.append({
            "appartement": name_by_id.get(str(pid), f"Appart {pid}"),
            "property_id": str(pid) if pid is not None else None,
            "check_out": dep10,
            "check_in": str(arr)[:10] if arr else None,
        })
    out.sort(key=lambda x: x["check_out"] or "")
    return out


def _photo_b64(path: str, max_side: int = 1280) -> str:
    """Encode une photo en base64, reduite si possible (moins de cout et plus rapide cote IA).
    Si Pillow n'est pas installe, envoie l'image telle quelle (aucun blocage)."""
    try:
        import io as _io
        from PIL import Image
        img = Image.open(path).convert("RGB")
        w, h = img.size
        if max(w, h) > max_side:
            ratio = max_side / float(max(w, h))
            img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))))
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()


async def claude_text(system: str, user: str, max_tokens: int = 900, model: str | None = None) -> str | None:
    if not ANTHROPIC_API_KEY:
        return None
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    body = {"model": model or ANTHROPIC_MODEL, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    r = await _http_retry("POST", "https://api.anthropic.com/v1/messages",
                          headers=headers, json_body=body, timeout=60)
    r.raise_for_status()
    return r.json()["content"][0]["text"]


async def claude_photo_recognize(path: str, shots: list, titre: str) -> tuple:
    """Analyse une photo : reconnait A QUELLE photo attendue elle correspond, et signale
    si elle semble sale/anormale. Retourne (match, probleme).
      match    = le libelle EXACT de 'shots' reconnu, ou '' si aucun ne correspond.
      probleme = courte description (<=8 mots) si salete/manque visible, sinon ''.
    Tolerant (angle/lumiere/flou). En cas de souci technique : ('', '') pour ne jamais bloquer."""
    if not ANTHROPIC_API_KEY or not shots:
        return "", ""
    try:
        b64 = _photo_b64(path)
    except Exception:
        return "", ""
    liste = "\n".join(f"- {s}" for s in shots)
    system = (
        "Tu analyses une photo prise par un agent de menage dans une piece. "
        "On te donne la LISTE des photos attendues pour cette piece. "
        "Reponds UNIQUEMENT en JSON compact : "
        "{\"match\": \"<libelle EXACT de la liste, ou vide>\", \"probleme\": \"<8 mots max, ou vide>\"}. "
        "Regle pour 'match' : choisis le libelle de la liste qui correspond le mieux a ce que montre la photo. "
        "Si vraiment aucun ne correspond, mets une chaine vide. Recopie le libelle a l'identique. "
        "Regle pour 'probleme' : remplis-le SEULEMENT si on voit clairement de la salete, des cheveux, "
        "de la poussiere, des taches, du desordre, une poubelle pleine ou un element manquant/non nettoye. "
        "Sinon laisse vide. Sois tolerant sur l'angle, la luminosite, le flou et le cadrage."
    )
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text", "text": f"Piece : {titre}.\nPhotos attendues :\n{liste}\n\n"
                                 f"A quelle photo attendue correspond cette image, et y a-t-il un probleme de proprete ?"},
    ]
    try:
        raw = await claude_text(system, content, max_tokens=150, model=ANTHROPIC_MODEL)
        mt = re.search(r"\{.*\}", raw or "", re.S)
        data = json.loads(mt.group(0)) if mt else {}
        match = str(data.get("match", "") or "").strip()
        # securite : ne garder le match que s'il est bien dans la liste (tolerant a la casse)
        if match and match not in shots:
            low = match.lower()
            match = next((s for s in shots if s.lower() == low), "")
        return match, str(data.get("probleme", "") or "").strip()
    except Exception:
        logger.exception("Echec reconnaissance photo")
        return "", ""


async def claude_report_summary(matches, lang="fr") -> str:
    """Courte synthèse (2-3 phrases) du rapport, dans la langue de l'admin. '' si indisponible."""
    if not ANTHROPIC_API_KEY or not matches:
        return ""
    resume = []
    for d in matches:
        resume.append({
            "appartement": d.get("appart", {}).get("nom_interne"),
            "date": str(d.get("heure_debut", ""))[:10],
            "agent": d.get("agent", {}).get("prenom"),
            "statut": d.get("statut"),
            "incidents": [{"resume": i.get("resume"), "urgent": i.get("urgent")}
                          for i in d.get("incidents", [])],
        })
    nom_langue = {"fr": "francais", "en": "English", "es": "espanol",
                  "ar": "Arabic", "ro": "romana"}.get(lang, "francais")
    system = (f"Tu rediges une synthese tres breve (2-3 phrases max) pour un rapport de menage. "
              f"Ecris en {nom_langue}. Mets en avant : nombre de missions, missions a verifier, "
              f"et surtout les incidents urgents s'il y en a. Ton factuel et professionnel. Pas de liste, du texte continu.")
    try:
        out = await claude_text(system, json.dumps(resume, ensure_ascii=False),
                                max_tokens=200, model=ANTHROPIC_MODEL)
        return (out or "").strip()
    except Exception:
        logger.exception("Echec synthese rapport")
        return ""


async def on_monid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    await update.message.reply_text(f"Ton code : {chat_id}")


async def on_ajouter_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_super(chat_id):
        await update.message.reply_text("Seul l'admin principal peut ajouter un admin.")
        return
    args = context.args
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "Usage : /ajouter_admin <code> <prenom>\n"
            "Le code s'obtient en demandant a la personne de taper /monid."
        )
        return
    code = args[0].strip()
    prenom = " ".join(args[1:]).strip() or "Admin"
    ADMINS[code] = {"prenom": prenom,
                    "ajoute_le": datetime.datetime.now().isoformat(timespec="seconds")}
    save_admins()
    logger.info("Admin ajoute : %s (code %s)", prenom, code)
    await update.message.reply_text(f"✅ Admin ajoute : {prenom} (code {code}). Il peut utiliser /admin.")
    await apply_admin_menu(context.bot, code)
    try:
        await context.bot.send_message(
            int(code), "Tu as ete ajoute comme admin d'ALFRED-M. Tape /admin pour consulter les rapports."
        )
    except Exception:
        logger.exception("Impossible de notifier le nouvel admin")


async def on_retirer_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_super(update.effective_chat.id):
        await update.message.reply_text("Seul l'admin principal peut retirer un admin.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage : /retirer_admin <code>")
        return
    code = args[0].strip()
    if code in ADMINS:
        nom = ADMINS.pop(code).get("prenom", "")
        save_admins()
        await apply_agent_menu(context.bot, code)
        await update.message.reply_text(f"🗑️ Admin retire : {nom} (code {code}).")
    else:
        await update.message.reply_text("Ce code n'est pas dans la liste des admins.")


async def on_admins_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_super(update.effective_chat.id):
        await update.message.reply_text("Reserve a l'admin principal.")
        return
    lignes = [f"• (principal) code {MANAGER_CHAT_ID}"]
    for code, info in ADMINS.items():
        lignes.append(f"• {info.get('prenom', '?')} — code {code}")
    await update.message.reply_text("Admins autorises :\n" + "\n".join(lignes))


# =====================================================================
# AGENTS DE MENAGE AUTORISES (liste blanche ; gerée par les admins)
# =====================================================================
def is_agent_authorized(chat_id) -> bool:
    return is_admin(chat_id) or str(chat_id) in AGENTS_AUTH


def role_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_role_admin"), callback_data="reg:role:admin")],
        [InlineKeyboardButton(t(lang, "btn_role_agent"), callback_data="reg:role:agent")],
    ])


def company_keyboard(lang: str, with_new: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🏢 {disp}", callback_data=f"regco:{key}")]
            for key, disp in all_companies().items()]
    if with_new:
        rows.append([InlineKeyboardButton(t(lang, "btn_new_company"), callback_data="regnewco")])
    return InlineKeyboardMarkup(rows)


async def ask_or_block(update, context, chat_id, state) -> None:
    """Personne pas encore autorisee : langue -> choix du role -> inscription (ou statut en attente)."""
    lang = AGENT_LANG.get(str(chat_id))
    if not lang:
        await update.message.reply_text(CHOOSE_LANG, reply_markup=lang_keyboard())
        return
    if str(chat_id) in PENDING:
        await update.message.reply_text(t(lang, "reg_already_pending"))
        return
    state["reg"] = {"step": "role"}
    await update.message.reply_text(t(lang, "reg_choose_role"), reply_markup=role_keyboard(lang))


async def on_reg_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """L'utilisateur choisit son role a l'inscription (responsable ou agent)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    state = get_state(chat_id)
    lang = state.get("lang") or AGENT_LANG.get(str(chat_id)) or "fr"
    if is_agent_authorized(chat_id) or str(chat_id) in PENDING:
        return
    role = query.data.split(":", 2)[2]
    state["reg"] = {"type": role, "step": "admin_nom" if role == "admin" else "agent_nom"}
    await query.edit_message_text(t(lang, "reg_ask_nom"))


async def handle_reg_step(update, context, state, reg) -> None:
    """Saisies texte pendant l'inscription."""
    chat_id = update.effective_chat.id
    ll = state.get("lang") or AGENT_LANG.get(str(chat_id)) or "fr"
    txt = (update.message.text or "").strip()
    step = reg.get("step")
    if step in ("admin_nom", "agent_nom"):
        if len(txt) < 2:
            await update.message.reply_text(t(ll, "reg_name_short"))
            return
        reg["nom"] = txt
        if step == "admin_nom":
            if all_companies():
                reg["step"] = "admin_co"
                await update.message.reply_text(t(ll, "reg_admin_choose_co"),
                                                reply_markup=company_keyboard(ll, with_new=True))
            else:
                reg["step"] = "admin_entreprise"
                await update.message.reply_text(t(ll, "reg_admin_entreprise"))
        else:
            if not all_companies():
                state["reg"] = None
                await update.message.reply_text(t(ll, "reg_no_company"))
                return
            reg["step"] = "agent_entreprise"
            await update.message.reply_text(t(ll, "reg_agent_choose_co"),
                                            reply_markup=company_keyboard(ll))
        return
    if step == "admin_co":
        # L'utilisateur doit choisir via les boutons ; on re-affiche le choix
        await update.message.reply_text(t(ll, "reg_admin_choose_co"),
                                        reply_markup=company_keyboard(ll, with_new=True))
        return
    if step == "admin_entreprise":
        reg["entreprise"] = txt
        reg["step"] = "admin_role"
        await update.message.reply_text(t(ll, "reg_admin_role"))
        return
    if step == "admin_role":
        reg["role"] = txt
        nom = reg.get("nom", "Responsable")
        PENDING[str(chat_id)] = {"type": "admin", "nom": nom,
                                 "entreprise": reg.get("entreprise", ""), "role": txt,
                                 "lang": ll,
                                 "date": datetime.datetime.now().isoformat(timespec="seconds")}
        _save_pending()
        state["reg"] = None
        await update.message.reply_text(t(ll, "reg_pending", name=nom, soc=reg.get("entreprise", "")))
        await notify_validators(context, str(chat_id))


async def on_reg_company(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """L'agent choisit son entreprise dans la liste."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    state = get_state(chat_id)
    ll = state.get("lang") or AGENT_LANG.get(str(chat_id)) or "fr"
    reg = state.get("reg") or {}
    key = query.data.split(":", 1)[1]
    disp = all_companies().get(key)
    if not disp:
        await query.answer("Entreprise introuvable.", show_alert=True)
        return
    # Un responsable rejoint une entreprise existante -> on continue vers son role
    if reg.get("type") == "admin":
        reg["entreprise"] = disp
        reg["step"] = "admin_role"
        state["reg"] = reg
        await query.edit_message_text(t(ll, "reg_admin_role"))
        return
    nom = reg.get("nom", "Agent")
    PENDING[str(chat_id)] = {"type": "agent", "nom": nom, "entreprise": disp, "lang": ll,
                             "date": datetime.datetime.now().isoformat(timespec="seconds")}
    _save_pending()
    state["reg"] = None
    await query.edit_message_text(t(ll, "reg_pending", name=nom, soc=disp))
    await notify_validators(context, str(chat_id))


async def on_reg_new_company(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Le responsable choisit de creer une nouvelle entreprise -> saisie du nom."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    state = get_state(chat_id)
    ll = state.get("lang") or AGENT_LANG.get(str(chat_id)) or "fr"
    reg = state.get("reg") or {}
    reg["step"] = "admin_entreprise"
    state["reg"] = reg
    await query.edit_message_text(t(ll, "reg_admin_entreprise"))


async def handle_super_profile_step(update, context, state, reg) -> None:
    """Configuration de l'entreprise et du role du super admin (1re fois)."""
    chat_id = update.effective_chat.id
    lang = ui_lang(chat_id)
    txt = (update.message.text or "").strip()
    if reg.get("step") == "super_entreprise":
        if len(txt) < 2:
            await update.message.reply_text(t(lang, "a_super_co_short"))
            return
        SUPER_PROFILE["entreprise"] = txt
        _save_super_profile()
        reg["step"] = "super_role"
        await update.message.reply_text(t(lang, "a_super_role"))
        return
    SUPER_PROFILE["role"] = txt or "Gérant"
    _save_super_profile()
    state["reg"] = None
    state["admin_mode"] = True
    prenom = display_name(chat_id, state) or "admin"
    await update.message.reply_text(
        t(lang, "a_super_done") + "\n\n" + t(lang, "a_panel", prenom=prenom),
        reply_markup=admin_panel_kb(chat_id),
    )


async def on_ajouter_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("Reserve aux admins.")
        return
    args = context.args
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "Usage : /ajouter_agent <code> [prenom]\n"
            "Le prenom est optionnel : si l'agent l'a deja saisi, il est repris tout seul.\n"
            "Le code s'obtient en demandant a la personne de taper /monid."
        )
        return
    code = args[0].strip()
    prenom = " ".join(args[1:]).strip() or "Agent"
    AGENTS_AUTH[code] = {"prenom": prenom, "entreprise": admin_company(chat_id) or "",
                         "ajoute_le": datetime.datetime.now().isoformat(timespec="seconds")}
    _save_agents_auth()
    PENDING.pop(code, None)
    _save_pending()
    logger.info("Agent autorise : %s (code %s)", prenom, code)
    await update.message.reply_text(f"✅ Agent autorise : {prenom} (code {code}). Il peut utiliser le bot.")
    try:
        ll = AGENT_LANG.get(code) or "fr"
        await context.bot.send_message(int(code), t(ll, "reg_authorized", soc=admin_company(chat_id) or ""))
    except Exception:
        logger.exception("Impossible de notifier le nouvel agent")


async def on_retirer_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Reserve aux admins.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage : /retirer_agent <code>")
        return
    code = args[0].strip()
    if code in AGENTS_AUTH:
        nom = AGENTS_AUTH.pop(code).get("prenom", "")
        _save_agents_auth()
        await update.message.reply_text(f"🗑️ Agent retire : {nom} (code {code}).")
    else:
        await update.message.reply_text("Ce code n'est pas dans la liste des agents.")


async def on_agents_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_chat.id):
        await update.message.reply_text("Reserve aux admins.")
        return
    if not AGENTS_AUTH:
        await update.message.reply_text("Aucun agent autorise pour l'instant.")
        return
    lignes = [f"• {info.get('prenom', '?')} — code {code}" for code, info in AGENTS_AUTH.items()]
    await update.message.reply_text("Agents autorises :\n" + "\n".join(lignes))


async def notify_validators(context, code: str) -> None:
    """Previent les bonnes personnes d'une nouvelle inscription, avec boutons Autoriser/Refuser.
    - demande responsable -> super admin uniquement
    - demande agent -> super admin + admins de l'entreprise concernee
    """
    reg = PENDING.get(code)
    if not reg:
        return
    typ = reg.get("type")
    nom = reg.get("nom", "?")
    ent = reg.get("entreprise", "?")
    role = reg.get("role", "")
    if typ == "admin":
        targets = [str(MANAGER_CHAT_ID)] if MANAGER_CHAT_ID else []
    else:
        targets = []
        if MANAGER_CHAT_ID:
            targets.append(str(MANAGER_CHAT_ID))
        for c, i in ADMINS.items():
            if co_key(i.get("entreprise", "")) == co_key(ent) and c not in targets:
                targets.append(c)
    for aid in targets:
        al = ui_lang(aid)
        if typ == "admin":
            info = t(al, "a_new_admin", nom=nom, ent=ent, role=role)
        else:
            info = t(al, "a_new_agent", nom=nom, ent=ent)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(t(al, "a_btn_auth"), callback_data=f"auth:ok:{code}"),
            InlineKeyboardButton(t(al, "a_btn_refuse"), callback_data=f"auth:no:{code}"),
        ]])
        try:
            await context.bot.send_message(int(aid), info, reply_markup=kb)
        except Exception:
            logger.exception("Echec notification validateur %s", aid)


async def on_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bouton Autoriser/Refuser une inscription (selon le type et l'entreprise)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    al = ui_lang(chat_id)
    if not is_admin(chat_id):
        await query.answer(t(al, "a_super_only"), show_alert=True)
        return
    _, action, code = query.data.split(":", 2)
    reg = PENDING.get(code)
    if not reg:
        await query.edit_message_text(t(al, "a_already"))
        return
    typ = reg.get("type")
    nom = reg.get("nom", "?")
    ent = reg.get("entreprise", "")
    role = reg.get("role", "")
    ll = reg.get("lang", "fr")
    # Permissions
    if typ == "admin" and not is_super(chat_id):
        await query.answer(t(al, "a_super_only"), show_alert=True)
        return
    if typ == "agent" and not is_super(chat_id) and co_key(admin_company(chat_id) or "") != co_key(ent):
        await query.answer(t(al, "a_super_only"), show_alert=True)
        return
    if action == "no":
        PENDING.pop(code, None)
        _save_pending()
        try:
            await context.bot.send_message(int(code), t(ll, "reg_refused"))
        except Exception:
            pass
        await query.edit_message_text(t(al, "a_refused", nom=nom))
        return
    now = datetime.datetime.now().isoformat(timespec="seconds")
    if typ == "admin":
        ADMINS[code] = {"prenom": nom, "entreprise": ent, "role": role, "ajoute_le": now}
        save_admins()
        await apply_admin_menu(context.bot, code)
        msg = t(ll, "reg_authorized_admin")
        logger.info("Responsable valide : %s (%s, code %s)", nom, ent, code)
    else:
        AGENTS_AUTH[code] = {"prenom": nom, "entreprise": ent, "ajoute_le": now}
        _save_agents_auth()
        msg = t(ll, "reg_authorized", soc=ent)
        logger.info("Agent valide : %s (%s, code %s)", nom, ent, code)
    PENDING.pop(code, None)
    _save_pending()
    try:
        await context.bot.send_message(int(code), msg)
    except Exception:
        logger.exception("Impossible de notifier la personne validee")
    await query.edit_message_text(
        t(al, "a_done_admin" if typ == "admin" else "a_done_agent", nom=nom, ent=ent))


# Entreprise active pour le cloisonnement des rapports.
# ContextVar = valeur PROPRE a chaque conversation/tache asyncio : deux responsables
# de deux entreprises differentes ne peuvent plus se marcher dessus (pas de fuite de donnees).
_SCOPE_COMPANY = contextvars.ContextVar("scope_company", default=None)


def _company_agent_ids(company: str) -> set:
    ck = co_key(company or "")
    return {str(c) for c, i in AGENTS_AUTH.items() if co_key(i.get("entreprise", "")) == ck}


def load_full_reports() -> list[dict]:
    """Rapports complets (avec chemins des photos). Filtre par entreprise si un scope est defini."""
    scope = _SCOPE_COMPANY.get()
    scope_ids = _company_agent_ids(scope) if scope else None
    out = []
    for fp in glob.glob(os.path.join(ARCHIVES_DIR, "**", "*.json"), recursive=True):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if scope_ids is not None and str(d.get("agent", {}).get("chat_id", "")) not in scope_ids:
            continue
        out.append(d)
    return out


def _extraire_date(text: str):
    """Trouve une date dans le texte. Retourne (date_iso ou None, texte_sans_date)."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}", (text[:m.start()] + text[m.end():]).strip()
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
    if m:
        iso = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        return iso, (text[:m.start()] + text[m.end():]).strip()
    m = re.search(r"(\d{1,2})[/-](\d{1,2})(?!\d)", text)
    if m:
        iso = f"{datetime.date.today().year}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        return iso, (text[:m.start()] + text[m.end():]).strip()
    return None, text


async def on_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("Cette commande est reservee aux admins.")
        return
    _SCOPE_COMPANY.set(None if is_super(chat_id) else admin_company(chat_id))
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text(
            "Usage : /photos <appartement> [date]\nEx : /photos churchill 79 21-06-2026"
        )
        return
    iso, reste = _extraire_date(query)
    tokens = [t.lower() for t in reste.split() if t]
    matches = []
    for d in load_full_reports():
        nom = str(d.get("appart", {}).get("nom_interne", "")).lower()
        if tokens and not all(t in nom for t in tokens):
            continue
        if iso and str(d.get("heure_debut", ""))[:10] != iso:
            continue
        matches.append(d)
    if not matches:
        await update.message.reply_text("Aucune mission trouvee pour ces criteres.")
        return
    matches.sort(key=lambda d: d.get("heure_debut", ""))
    total = 0
    for d in matches:
        appart = d.get("appart", {}).get("nom_interne", "?")
        date = str(d.get("heure_debut", ""))[:10]
        photos = d.get("photos", [])
        await update.message.reply_text(
            f"📂 {appart} — {date} — {len(photos)} photo(s) — statut {d.get('statut')}"
        )
        for ph in photos:
            if total >= 30:
                await update.message.reply_text("(Je m'arrete a 30 photos. Precise une date pour affiner.)")
                return
            p = ph.get("path")
            if p and os.path.exists(p):
                try:
                    with open(p, "rb") as f:
                        await context.bot.send_photo(chat_id, photo=f,
                                                     caption=f"{appart} — {date} — {ph.get('point', '')}")
                    total += 1
                except Exception:
                    logger.exception("Echec envoi photo %s", p)
            else:
                await update.message.reply_text(f"⚠️ Fichier introuvable : {ph.get('point', '')}")
    if total == 0:
        await update.message.reply_text("Aucune photo disponible pour ces missions.")


def admin_panel_kb(chat_id) -> InlineKeyboardMarkup:
    lang = ui_lang(chat_id)
    rows = [[InlineKeyboardButton(t(lang, "a_b_agents"), callback_data="adm:agents")]]
    if is_super(chat_id):
        rows.append([InlineKeyboardButton(t(lang, "a_b_lodgify"), callback_data="adm:lodgify")])
        rows.append([InlineKeyboardButton(t(lang, "a_b_logements"), callback_data="adm:logements")])
        rows.append([InlineKeyboardButton(t(lang, "a_b_admins"), callback_data="adm:admins")])
    return InlineKeyboardMarkup(rows)


ADMIN_PANEL_TXT = (
    "🔧 Panneau admin — bonjour {prenom} !\n\n"
    "Tu peux me parler directement ici : pose-moi toutes tes questions sur tes "
    "rapports, tes photos et tes données de ménage, et je te réponds. Par exemple :\n\n"
    "• « Quels appartements ont été nettoyés aujourd'hui ? »\n"
    "• « Montre-moi les photos de Churchill 79 d'hier. »\n"
    "• « Combien d'incidents cette semaine, et lesquels étaient urgents ? »\n"
    "• « Génère un rapport des missions à vérifier. »\n\n"
    "Écris simplement ta demande, comme à un collègue. 💬\n"
    "Les boutons ci-dessous servent à gérer ton équipe. Tape /start pour quitter."
)


async def on_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    lang = ui_lang(chat_id)
    if not is_admin(chat_id):
        await update.message.reply_text(t(lang, "a_super_only"))
        return
    # 1re fois pour le super admin : configurer son entreprise + role
    if is_super(chat_id) and not SUPER_PROFILE.get("entreprise"):
        state["reg"] = {"step": "super_entreprise"}
        await update.message.reply_text(t(lang, "a_super_co"))
        return
    state["admin_mode"] = True
    state["mission"] = None  # une mission inachevee ne doit pas bloquer le mode rapport
    prenom = display_name(chat_id, state) or update.effective_user.first_name or "admin"
    logger.info("Panneau admin ouvert par %s (chat_id=%s)", prenom, chat_id)
    await update.message.reply_text(
        t(lang, "a_panel", prenom=prenom),
        reply_markup=admin_panel_kb(chat_id),
    )


async def on_lodgify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande /lodgify : ouvre la gestion Lodgify (super admin uniquement)."""
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    lang = ui_lang(chat_id)
    if not is_super(chat_id):
        await update.message.reply_text(t(lang, "a_super_only"))
        return
    state["admin_mode"] = True
    state["mission"] = None
    await update.message.reply_text(t(lang, "a_lodgify"))


async def on_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boutons du panneau admin (admins uniquement)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    lang = ui_lang(chat_id)
    if not is_admin(chat_id):
        await query.answer(t(lang, "a_super_only"), show_alert=True)
        return
    state = get_state(chat_id)
    action = query.data.split(":", 1)[1]
    if action == "reports":
        state["admin_mode"] = True
        await query.edit_message_text(t(lang, "a_reports"))
    elif action == "lodgify":
        if not is_super(chat_id):
            await query.answer(t(lang, "a_super_only"), show_alert=True)
            return
        state["admin_mode"] = True
        await query.edit_message_text(t(lang, "a_lodgify"))
    elif action == "agents":
        super_ = is_super(chat_id)
        macomp = admin_company(chat_id)
        # super admin voit tout ; un responsable ne voit que SON entreprise
        items = [(c, i) for c, i in AGENTS_AUTH.items()
                 if super_ or co_key(i.get("entreprise", "")) == co_key(macomp or "")]
        if not items:
            await query.edit_message_text(t(lang, "a_agents_none"))
            return
        txt = (t(lang, "a_agents_all") if super_ else t(lang, "a_agents_mine")) + "\n"
        rows = []
        for code, info in items:
            nom = info.get("prenom", "?")
            ent = info.get("entreprise", "")
            txt += f"• {nom}" + (f" — {ent}" if super_ and ent else "") + "\n"
            rows.append([InlineKeyboardButton(t(lang, "a_remove", nom=nom), callback_data=f"delagent:{code}")])
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows))
    elif action == "logements":
        await render_logements(query, chat_id)
    elif action == "admins":
        if not is_super(chat_id):
            await query.answer(t(lang, "a_super_only"), show_alert=True)
            return
        moi = SUPER_PROFILE.get("entreprise", "")
        txt = t(lang, "a_admins_title") + f"\n• ⭐ {moi}\n"
        rows = []
        for code, info in ADMINS.items():
            nom = info.get("prenom", "?")
            txt += f"• {nom} — {info.get('entreprise', '')} ({info.get('role', '')})\n"
            rows.append([InlineKeyboardButton(t(lang, "a_remove", nom=nom), callback_data=f"deladmin:{code}")])
        if not rows:
            txt += "\n" + t(lang, "a_admins_none")
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows) if rows else None)


async def on_delagent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bouton ❌ Retirer un agent depuis le panneau admin."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    lang = ui_lang(chat_id)
    if not is_admin(chat_id):
        await query.answer(t(lang, "a_super_only"), show_alert=True)
        return
    code = query.data.split(":", 1)[1]
    info = AGENTS_AUTH.get(code)
    if not info:
        await query.edit_message_text(t(lang, "a_agent_removed", nom=""))
        return
    if not is_super(chat_id) and co_key(info.get("entreprise", "")) != co_key(admin_company(chat_id) or ""):
        await query.answer(t(lang, "a_super_only"), show_alert=True)
        return
    nom = AGENTS_AUTH.pop(code).get("prenom", "")
    _save_agents_auth()
    await query.edit_message_text(t(lang, "a_agent_removed", nom=nom))


async def render_logements(query, chat_id) -> None:
    """Affiche la liste des logements avec leur entreprise (super admin uniquement)."""
    lang = ui_lang(chat_id)
    if not is_super(chat_id):
        await query.answer(t(lang, "a_super_only"), show_alert=True)
        return
    try:
        props = await get_all_properties()
    except Exception:
        logger.exception("Erreur Lodgify (logements)")
        await query.edit_message_text(t(lang, "a_log_err"))
        return
    if not props:
        await query.edit_message_text(t(lang, "a_log_none"))
        return
    txt = t(lang, "a_log_title") + "\n"
    rows = []
    for p in props:
        pid = p["property_id"]
        ent = property_company(pid)
        label = f"{p['name']} · {ent if ent else '❓'}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"logpick:{pid}")])
    await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows))


async def on_logpick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Super admin : choisir l'entreprise d'un logement."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    lang = ui_lang(chat_id)
    if not is_super(chat_id):
        await query.answer(t(lang, "a_super_only"), show_alert=True)
        return
    pid = query.data.split(":", 1)[1]
    companies = list(all_companies().values())
    rows = [[InlineKeyboardButton(f"🏢 {disp}", callback_data=f"logset:{pid}:{idx}")]
            for idx, disp in enumerate(companies)]
    rows.append([InlineKeyboardButton(t(lang, "a_unassigned"), callback_data=f"logset:{pid}:x")])
    rows.append([InlineKeyboardButton(t(lang, "a_back"), callback_data="adm:logements")])
    await query.edit_message_text(t(lang, "a_log_pick"), reply_markup=InlineKeyboardMarkup(rows))


async def on_logset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Super admin : enregistre l'entreprise choisie pour un logement."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    if not is_super(chat_id):
        await query.answer("Reserve a l'admin principal.", show_alert=True)
        return
    _, pid, idx = query.data.split(":", 2)
    if idx == "x":
        PROPERTY_COMPANY.pop(str(pid), None)
    else:
        companies = list(all_companies().values())
        try:
            PROPERTY_COMPANY[str(pid)] = companies[int(idx)]
        except (ValueError, IndexError):
            await query.answer("Entreprise introuvable.", show_alert=True)
            return
    _save_property_company()
    await render_logements(query, chat_id)


async def on_logtog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin : ajoute/retire un logement de SON entreprise."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    if not is_admin(chat_id):
        await query.answer("Reserve aux admins.", show_alert=True)
        return
    macomp = admin_company(chat_id) or ""
    if not macomp:
        await query.answer("Ton entreprise n'est pas definie.", show_alert=True)
        return
    pid = query.data.split(":", 1)[1]
    if co_key(property_company(pid)) == co_key(macomp):
        PROPERTY_COMPANY.pop(str(pid), None)
    else:
        PROPERTY_COMPANY[str(pid)] = macomp
    _save_property_company()
    await render_logements(query, chat_id)


async def on_deladmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bouton ❌ Retirer un responsable (admin principal uniquement)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    lang = ui_lang(chat_id)
    if not is_super(chat_id):
        await query.answer(t(lang, "a_super_only"), show_alert=True)
        return
    code = query.data.split(":", 1)[1]
    if code in ADMINS:
        nom = ADMINS.pop(code).get("prenom", "")
        save_admins()
        await apply_agent_menu(context.bot, code)
        await query.edit_message_text(t(lang, "a_admin_removed", nom=nom))
    else:
        await query.edit_message_text("Ce responsable n'est plus dans la liste.")


def build_missions_data() -> list[dict]:
    """Version riche des missions pour l'agent (sans les photos en base64)."""
    data = []
    for d in load_full_reports():
        data.append({
            "mission_id": d.get("mission_id"),
            "appartement": d.get("appart", {}).get("nom_interne"),
            "property_id": d.get("appart", {}).get("property_id"),
            "agent": d.get("agent", {}).get("prenom"),
            "agent_chat_id": d.get("agent", {}).get("chat_id"),
            "date": str(d.get("heure_debut", ""))[:10],
            "heure_debut": d.get("heure_debut"),
            "heure_fin": d.get("heure_fin"),
            "statut": d.get("statut"),
            "nb_photos": len(d.get("photos", [])),
            "video_avant": bool(d.get("video_avant")),
            "video_fin": bool(d.get("video_fin")),
            "confirmations": d.get("confirmations", {}),
            "incidents": [{"resume": i.get("resume"), "urgent": i.get("urgent")}
                          for i in d.get("incidents", [])],
        })
    return data


def match_missions(appartement="", date="", agent="", date_debut="", date_fin="") -> list[dict]:
    toks = [t for t in str(appartement or "").lower().split() if t]
    ag = str(agent or "").lower().strip()
    out = []
    for d in load_full_reports():
        nom = str(d.get("appart", {}).get("nom_interne", "")).lower()
        dd = str(d.get("heure_debut", ""))[:10]
        if toks and not all(t in nom for t in toks):
            continue
        if date and dd != date:
            continue
        if date_debut and dd < date_debut:
            continue
        if date_fin and dd > date_fin:
            continue
        if ag and ag not in str(d.get("agent", {}).get("prenom", "")).lower():
            continue
        out.append(d)
    out.sort(key=lambda x: x.get("heure_debut", ""))
    return out


def _has_criteria(a: dict) -> bool:
    return any(a.get(k) for k in ("appartement", "date", "agent", "date_debut", "date_fin"))


async def _send_photos(context, chat_id, matches, cap=30) -> int:
    total = 0
    for d in matches:
        appart = d.get("appart", {}).get("nom_interne", "?")
        date = str(d.get("heure_debut", ""))[:10]
        for ph in d.get("photos", []):
            if total >= cap:
                await context.bot.send_message(chat_id, "(Je m'arrete a 30 photos — precise une date.)")
                return total
            p = ph.get("path")
            if p and os.path.exists(p):
                try:
                    with open(p, "rb") as f:
                        await context.bot.send_photo(chat_id, photo=f,
                                                     caption=f"{appart} — {date} — {ph.get('point', '')}")
                    total += 1
                except Exception:
                    logger.exception("Echec envoi photo %s", p)
    return total


async def _send_videos(context, chat_id, matches, quelles="les_deux") -> int:
    total = 0
    for d in matches:
        appart = d.get("appart", {}).get("nom_interne", "?")
        date = str(d.get("heure_debut", ""))[:10]
        cibles = []
        if quelles in ("avant", "les_deux") and d.get("video_avant"):
            cibles.append(("arrivee", d["video_avant"]))
        if quelles in ("fin", "les_deux") and d.get("video_fin"):
            cibles.append(("fin", d["video_fin"]))
        for libelle, p in cibles:
            if p and os.path.exists(p):
                try:
                    with open(p, "rb") as f:
                        await context.bot.send_video(chat_id, video=f,
                                                     caption=f"{appart} — {date} — video {libelle}")
                    total += 1
                except Exception:
                    logger.exception("Echec envoi video %s", p)
    return total


def _esc(s) -> str:
    return ("" if s is None else str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _img_data_uri(path) -> str | None:
    try:
        with open(path, "rb") as f:
            return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
    except Exception:
        return None


REPORT_CSS = """
:root{--ink:#0b1f17;--muted:#6b7b74;--line:#e7ece9;--bg:#eef1f0;--green:#0f7a4f;--green2:#16a34a;--gold:#c8a24a;--ok-bg:#e7f6ec;--ok-tx:#10733f}
*{box-sizing:border-box}
html,body{margin:0}
body{font-family:'Segoe UI',-apple-system,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5}
.page{max-width:880px;margin:24px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 10px 40px rgba(8,30,22,.10)}
.cover{position:relative;padding:40px 44px 34px;color:#fff;background:radial-gradient(1200px 300px at 80% -40%,rgba(255,255,255,.18),transparent),linear-gradient(135deg,#0b3d2a 0%,#0f7a4f 55%,#16a34a 100%)}
.cover .row{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.logo{display:flex;align-items:center;gap:12px}
.logo .mark{width:46px;height:46px;border-radius:12px;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.35);display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:800}
.logo .name{font-size:20px;font-weight:800;letter-spacing:.3px}
.logo .tag{font-size:12px;opacity:.85;margin-top:1px}
.cover h1{margin:26px 0 4px;font-size:26px;font-weight:800}
.cover .when{font-size:13px;opacity:.9}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:24px}
.kpi{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.22);border-radius:12px;padding:14px 16px}
.kpi .num{font-size:26px;font-weight:800;line-height:1}
.kpi .lab{font-size:11px;text-transform:uppercase;letter-spacing:.6px;opacity:.9;margin-top:6px}
.body{padding:30px 44px 12px}
.synth{background:#f6f9f7;border:1px solid var(--line);border-left:4px solid var(--green);border-radius:12px;padding:16px 18px;margin:0 0 26px;font-size:14px}
.synth h3{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--green)}
.mission{border:1px solid var(--line);border-radius:14px;margin:0 0 26px;overflow:hidden}
.m-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:16px 20px;background:#f6f9f7;border-bottom:1px solid var(--line)}
.m-title{display:flex;align-items:center;gap:10px;font-size:17px;font-weight:800;margin:0}
.m-title .ico{width:30px;height:30px;border-radius:9px;background:#e7f6ec;display:flex;align-items:center;justify-content:center;font-size:16px}
.badge{font-size:11.5px;font-weight:800;padding:6px 13px;border-radius:999px;letter-spacing:.3px;white-space:nowrap}
.badge.ok{background:var(--ok-bg);color:var(--ok-tx)}
.badge.warn{background:#fdf2e3;color:#a8650d}
.m-body{padding:18px 20px}
.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:6px}
.meta .lab{font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
.meta .val{font-size:14.5px;font-weight:700;margin-top:3px}
.sec{font-size:12px;font-weight:800;color:var(--green);text-transform:uppercase;letter-spacing:.7px;margin:18px 0 10px;display:flex;align-items:center;gap:8px}
.sec::after{content:"";flex:1;height:1px;background:var(--line)}
.checks{display:grid;grid-template-columns:1fr 1fr;gap:4px 22px}
.check{display:flex;align-items:center;gap:9px;padding:7px 0;border-bottom:1px solid #f2f5f3;font-size:13.5px}
.check .dot{width:18px;height:18px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:800;color:#fff}
.dot.y{background:var(--green2)}.dot.n{background:#dc2626}.dot.na{background:#b3bdb8}
.check .txt{flex:1}.check .num{font-weight:800;color:var(--green)}
.inc{display:flex;gap:10px;align-items:flex-start;background:#fff8ee;border:1px solid #f6e2bf;border-left:4px solid var(--gold);border-radius:10px;padding:11px 14px;margin:8px 0;font-size:13.5px}
.inc.urg{background:#fef2f2;border-color:#f6cccc;border-left-color:#dc2626}.inc .u{font-weight:800;color:#dc2626}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.gallery figure{margin:0;border:1px solid var(--line);border-radius:11px;overflow:hidden;background:#f6f9f7}
.gallery img{width:100%;height:120px;object-fit:cover;display:block}
.gallery figcaption{padding:7px 9px;font-size:11.5px;color:var(--muted);text-align:center}
.foot{padding:18px 44px 30px;text-align:center;color:var(--muted);font-size:11.5px;border-top:1px solid var(--line)}
.foot b{color:var(--green)}
@media print{body{background:#fff}.page{box-shadow:none;margin:0;max-width:none;border-radius:0}.mission,.synth{break-inside:avoid}.cover{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""


def _build_html_report(matches, titre, company="", synthese="") -> str:
    n_ok = sum(1 for d in matches if d.get("statut") == "Valide")
    n_warn = len(matches) - n_ok
    company = company or "Genius BnB"
    mono = (company.strip()[:1] or "A").upper()
    gen = datetime.datetime.now().strftime("%d/%m/%Y à %H:%M")
    h = ["<!doctype html><html lang='fr'><head><meta charset='utf-8'>",
         f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{_esc(titre)}</title>",
         f"<style>{REPORT_CSS}</style></head><body><div class='page'>",
         "<div class='cover'><div class='row'>",
         f"<div class='logo'><div class='mark'>{_esc(mono)}</div>"
         f"<div><div class='name'>{_esc(company)}</div><div class='tag'>Conciergerie &amp; ménage</div></div></div>",
         "<div style='text-align:right;font-size:12px;opacity:.9'>Rapport qualité<br>preuve d'intervention</div>",
         "</div>",
         "<h1>Rapport de ménage</h1>",
         f"<div class='when'>Généré le {gen} &nbsp;·&nbsp; {len(matches)} mission(s)</div>",
         "<div class='kpis'>"
         f"<div class='kpi'><div class='num'>{len(matches)}</div><div class='lab'>Missions</div></div>"
         f"<div class='kpi'><div class='num'>{n_ok}</div><div class='lab'>Validées</div></div>"
         f"<div class='kpi'><div class='num'>{n_warn}</div><div class='lab'>À vérifier</div></div>"
         "</div></div>",
         "<div class='body'>"]
    if synthese:
        h.append(f"<div class='synth'><h3>🧠 Synthèse</h3>{_esc(synthese)}</div>")
    for d in matches:
        appart = d.get("appart", {}).get("nom_interne", "?")
        date = str(d.get("heure_debut", ""))[:10]
        statut = d.get("statut", "")
        ok = statut == "Valide"
        deb = str(d.get("heure_debut", ""))[11:16]
        fin = str(d.get("heure_fin", ""))[11:16]
        h.append("<div class='mission'>")
        h.append(f"<div class='m-head'><h2 class='m-title'><span class='ico'>🏠</span> {_esc(appart)}</h2>"
                 f"<span class='badge {'ok' if ok else 'warn'}'>{'✓ ' if ok else '⚠ '}{_esc(statut)}</span></div>")
        h.append("<div class='m-body'>")
        h.append("<div class='meta'>"
                 f"<div><div class='lab'>Date</div><div class='val'>{_esc(date)}</div></div>"
                 f"<div><div class='lab'>Agent</div><div class='val'>{_esc(d.get('agent', {}).get('prenom') or '-')}</div></div>"
                 f"<div><div class='lab'>Horaire</div><div class='val'>{_esc(deb)} → {_esc(fin)}</div></div>"
                 "</div>")
        inc = d.get("incidents", [])
        if inc:
            h.append("<div class='sec'>Incidents signalés</div>")
            for i in inc:
                urg = " urg" if i.get("urgent") else ""
                tag = "<span class='u'>URGENT — </span>" if i.get("urgent") else ""
                h.append(f"<div class='inc{urg}'>⚠️ <div>{tag}{_esc(i.get('resume'))}</div></div>")
        conf = d.get("confirmations", {})
        if conf:
            h.append("<div class='sec'>Vérifications</div><div class='checks'>")
            for k, v in conf.items():
                if v is True:
                    h.append(f"<div class='check'><span class='dot y'>✓</span><span class='txt'>{_esc(k)}</span></div>")
                elif v is False:
                    h.append(f"<div class='check'><span class='dot n'>✗</span><span class='txt'>{_esc(k)}</span></div>")
                elif str(v).upper() == "N/A":
                    h.append(f"<div class='check'><span class='dot na'>–</span><span class='txt'>{_esc(k)}</span></div>")
                else:
                    h.append(f"<div class='check'><span class='txt'>{_esc(k)}</span><span class='num'>{_esc(v)}</span></div>")
            h.append("</div>")
        photos = [p for p in d.get("photos", []) if _img_data_uri(p.get("path", ""))]
        if photos:
            h.append(f"<div class='sec'>Photos preuve ({len(photos)})</div><div class='gallery'>")
            for ph in photos:
                uri = _img_data_uri(ph.get("path", ""))
                h.append(f"<figure><img src='{uri}'><figcaption>{_esc(ph.get('point', ''))}</figcaption></figure>")
            h.append("</div>")
        h.append("</div></div>")
    h.append(f"<div class='foot'>Document généré automatiquement par <b>ALFRED</b> · {_esc(company)} · Confidentiel</div>")
    h.append("</div></body></html>")
    path = os.path.join(EXPORTS_DIR, f"rapport_{_stamp()}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(h))
    return path


def _resolve_agent_chat_id(agent_name) -> str | None:
    ag = str(agent_name or "").lower().strip()
    if not ag:
        return None
    for d in load_full_reports():
        if ag in str(d.get("agent", {}).get("prenom", "")).lower():
            cid = d.get("agent", {}).get("chat_id")
            if cid:
                return str(cid)
    return None


async def _send_long(context, chat_id, text) -> None:
    for i in range(0, len(text), 3900):
        await context.bot.send_message(chat_id, text[i:i + 3900])


ADMIN_TOOLS = [
    {"name": "envoyer_photos",
     "description": "Envoie au responsable, dans Telegram, les vraies photos des missions correspondantes. "
                    "Precise au moins un critere (appartement, date ou agent).",
     "input_schema": {"type": "object", "properties": {
         "appartement": {"type": "string", "description": "nom interne, ex 'churchill 79'"},
         "date": {"type": "string", "description": "date AAAA-MM-JJ"},
         "agent": {"type": "string", "description": "prenom de l'agent"}}, "required": []}},
    {"name": "envoyer_videos",
     "description": "Envoie au responsable les videos (arrivee et/ou fin) des missions correspondantes.",
     "input_schema": {"type": "object", "properties": {
         "appartement": {"type": "string"}, "date": {"type": "string"}, "agent": {"type": "string"},
         "quelles": {"type": "string", "enum": ["avant", "fin", "les_deux"]}}, "required": []}},
    {"name": "exporter_rapport",
     "description": "Genere un RAPPORT (fichier HTML avec photos integrees, ouvrable et imprimable en PDF) des "
                    "missions correspondantes et l'envoie en fichier au responsable. A utiliser des que le "
                    "responsable demande un rapport, un fichier, un export, un PDF ou un document a transmettre.",
     "input_schema": {"type": "object", "properties": {
         "appartement": {"type": "string"}, "date": {"type": "string"},
         "date_debut": {"type": "string"}, "date_fin": {"type": "string"}, "agent": {"type": "string"}},
         "required": []}},
    {"name": "message_agent",
     "description": "Prepare un message a envoyer a un agent de menage (il ne partira qu'apres confirmation du responsable).",
     "input_schema": {"type": "object", "properties": {
         "agent_chat_id": {"type": "string"}, "agent": {"type": "string"},
         "texte": {"type": "string"}}, "required": ["texte"]}},
    {"name": "supprimer_mission",
     "description": "Supprime DEFINITIVEMENT une mission archivee (rapport + photos + videos). "
                    "Reserve a l'admin principal (super admin). Identifie la mission par mission_id "
                    "(preferable) ou par appartement + date. La suppression ne part qu'apres confirmation.",
     "input_schema": {"type": "object", "properties": {
         "mission_id": {"type": "string"},
         "appartement": {"type": "string"},
         "date": {"type": "string", "description": "date AAAA-MM-JJ"}}, "required": []}},
]


# --- Gestion Lodgify (super admin uniquement) : outils generiques ---
LODGIFY_TOOLS = [
    {"name": "lodgify_lire",
     "description": "Lit des donnees dans Lodgify via l'API (lecture seule). Fournis le 'path' exact "
                    "(ex: '/v2/reservations/bookings', '/v2/properties', '/v2/messaging/{threadGuid}'). "
                    "Utilise 'params' pour les filtres de requete. Renvoie le JSON brut.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "chemin API, commence par /v1 ou /v2"},
         "params": {"type": "object", "description": "parametres de requete (optionnel)"}},
         "required": ["path"]}},
    {"name": "lodgify_agir",
     "description": "Execute une action qui MODIFIE Lodgify (creer/modifier/annuler resa, bloquer dates, "
                    "changer un tarif, envoyer un message voyageur, creer un lien de paiement, etc.). "
                    "L'action ne part QU'APRES confirmation du responsable. Fournis methode, path, body "
                    "et un 'resume' clair en francais de ce que ca va faire.",
     "input_schema": {"type": "object", "properties": {
         "methode": {"type": "string", "enum": ["POST", "PUT", "DELETE"]},
         "path": {"type": "string", "description": "chemin API, ex '/v1/reservation/booking'"},
         "body": {"type": "object", "description": "corps JSON de la requete (optionnel)"},
         "resume": {"type": "string", "description": "resume clair en francais de l'action, montre au responsable"}},
         "required": ["methode", "path", "resume"]}},
]

LODGIFY_GUIDE = (
    "OUTILS LODGIFY (reserves a toi, super admin) : 'lodgify_lire' (GET) et 'lodgify_agir' (POST/PUT/DELETE, "
    "avec confirmation). Voici les principaux points d'acces de l'API Lodgify :\n"
    "LECTURE : GET /v2/properties (logements) · GET /v2/properties/{id}/rooms · GET /v2/reservations/bookings "
    "(liste resas, params: page,size,stayFilter) · GET /v2/reservations/bookings/{id} · "
    "GET /v2/availability/{propertyId} · GET /v2/quote/{propertyId} (devis, params dates) · "
    "GET /v2/rates/settings?houseId={id} · GET /v2/messaging/{threadGuid} (fil de messages) · "
    "GET /v1/reservation/booking/{id}/messages.\n"
    "ACTIONS : POST /v1/reservation/booking (creer resa) · PUT /v1/reservation/booking/{id} (modifier) · "
    "PUT /v1/reservation/booking/{id}/book | /decline | /tentative | /checkin | /checkout · "
    "DELETE /v1/reservation/booking/{id} (corbeille) · POST /v1/reservation/booking/{id}/messages (ecrire au voyageur) · "
    "POST /v1/availability/{propertyId}/{roomTypeId}/set (bloquer/ouvrir des dates) · "
    "POST /v1/rates/savetiny (changer un tarif) · POST /v2/reservations/bookings/{id}/checkin|checkout · "
    "createPaymentLink / request-payment (lien/demande de paiement) · PUT updatekeycodes (codes d'acces).\n"
    "Le property_id se trouve via GET /v2/properties. Pour un fil de messages, recupere d'abord la resa puis son thread. "
    "Interprete les dates au format AAAA-MM-JJ. Si tu n'es pas sur d'un id, LIS d'abord pour le trouver."
)


async def _lodgify_result_text(status, data) -> str:
    txt = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    if len(txt) > 6000:
        txt = txt[:6000] + " …(tronque)"
    return f"HTTP {status}\n{txt}"


async def claude_tools_call(system: str, messages: list, tools: list, model: str, max_tokens: int = 2000) -> dict:
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    body = {"model": model, "max_tokens": max_tokens, "system": system, "tools": tools, "messages": messages}
    r = await _http_retry("POST", "https://api.anthropic.com/v1/messages",
                          headers=headers, json_body=body, timeout=90)
    r.raise_for_status()
    return r.json()


def _mission_files_matching(mission_id="", appartement="", date="") -> list:
    """Retourne [(chemin_json, donnees)] des missions correspondant aux criteres."""
    out = []
    for fp in glob.glob(os.path.join(ARCHIVES_DIR, "**", "*.json"), recursive=True):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if mission_id and str(d.get("mission_id")) != str(mission_id):
            continue
        if appartement:
            toks = [t for t in str(appartement).lower().split() if t]
            nom = str(d.get("appart", {}).get("nom_interne", "")).lower()
            if not all(t in nom for t in toks):
                continue
        if date and str(d.get("heure_debut", ""))[:10] != date:
            continue
        out.append((fp, d))
    return out


def _delete_mission_files(fp: str, d: dict) -> bool:
    """Supprime les medias (photos/videos) puis le fichier JSON de la mission."""
    medias = [d.get("video_avant"), d.get("video_fin")] + [ph.get("path") for ph in d.get("photos", [])]
    for p in medias:
        if p:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                logger.exception("Echec suppression media %s", p)
    try:
        os.remove(fp)
    except Exception:
        logger.exception("Echec suppression rapport %s", fp)
        return False
    return True


async def execute_admin_tool(name, inp, context, chat_id, state) -> str:
    inp = inp or {}
    if name == "supprimer_mission":
        if not is_super(chat_id):
            return "La suppression de missions est reservee a l'admin principal."
        matches = _mission_files_matching(inp.get("mission_id", ""), inp.get("appartement", ""),
                                          inp.get("date", ""))
        if not matches:
            return "Aucune mission ne correspond. Precise l'appartement et la date exacte."
        if len(matches) > 1:
            lignes = [f"- {d.get('appart', {}).get('nom_interne', '?')} "
                      f"{str(d.get('heure_debut', ''))[:16]} (id {d.get('mission_id')})"
                      for _, d in matches[:10]]
            return ("Plusieurs missions correspondent, precise la date exacte (ou l'id) :\n"
                    + "\n".join(lignes))
        fp, d = matches[0]
        appart = d.get("appart", {}).get("nom_interne", "?")
        dt = str(d.get("heure_debut", ""))[:10]
        state["pending_delete"] = {"fp": fp, "label": f"{appart} — {dt}"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Supprimer", callback_data="delmissok"),
                                    InlineKeyboardButton("✖️ Annuler", callback_data="delmissno")]])
        await context.bot.send_message(
            chat_id,
            f"⚠️ Supprimer DEFINITIVEMENT cette mission ?\n{appart} — {dt}\n"
            "(photos + videos + rapport effaces, action irreversible)",
            reply_markup=kb)
        return "Confirmation de suppression demandee a l'admin principal."
    if name in ("envoyer_photos", "envoyer_videos", "exporter_rapport"):
        matches = match_missions(inp.get("appartement", ""), inp.get("date", ""), inp.get("agent", ""),
                                 inp.get("date_debut", ""), inp.get("date_fin", ""))
        if not matches:
            return "Aucune mission ne correspond a ces criteres."
        if name == "envoyer_photos":
            n = await _send_photos(context, chat_id, matches)
            return f"{n} photo(s) envoyee(s) au responsable."
        if name == "envoyer_videos":
            n = await _send_videos(context, chat_id, matches, inp.get("quelles", "les_deux"))
            return f"{n} video(s) envoyee(s)." if n else "Aucune video disponible pour ces missions."
        if name == "exporter_rapport":
            company = admin_company(chat_id) or SUPER_PROFILE.get("entreprise", "") or "Genius BnB"
            synth = await claude_report_summary(matches, ui_lang(chat_id))
            path = _build_html_report(matches, f"Rapport de menage — {company}", company, synth)
            with open(path, "rb") as f:
                await context.bot.send_document(
                    chat_id, document=f, filename=os.path.basename(path),
                    caption=f"📄 Rapport ({len(matches)} mission(s)). Ouvre-le, puis Imprimer > Enregistrer en PDF.")
            return f"Rapport HTML de {len(matches)} mission(s) envoye au responsable."
    if name == "message_agent":
        texte = (inp.get("texte") or "").strip()
        cid = inp.get("agent_chat_id") or _resolve_agent_chat_id(inp.get("agent"))
        if not texte or not cid:
            return "Impossible : destinataire ou texte manquant."
        state["pending_msg"] = {"chat_id": str(cid), "texte": texte}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Envoyer", callback_data="msgok"),
                                    InlineKeyboardButton("✖️ Annuler", callback_data="msgno")]])
        await context.bot.send_message(
            chat_id, f"✉️ Message a envoyer a l'agent (code {cid}) :\n\n« {texte} »\n\nConfirmer l'envoi ?",
            reply_markup=kb)
        return "Message prepare, en attente de la confirmation du responsable."
    if name == "lodgify_lire":
        if not is_super(chat_id):
            return "La gestion Lodgify est reservee a l'admin principal."
        try:
            status, data = await lodgify_api("GET", inp.get("path", ""), inp.get("params") or None)
            return await _lodgify_result_text(status, data)
        except Exception as e:
            logger.exception("lodgify_lire")
            return f"Erreur lecture Lodgify : {e}"
    if name == "lodgify_agir":
        if not is_super(chat_id):
            return "La gestion Lodgify est reservee a l'admin principal."
        methode = (inp.get("methode") or "").upper()
        path = inp.get("path") or ""
        if methode not in ("POST", "PUT", "DELETE") or not path:
            return "Action invalide : methode et path requis."
        state["pending_lodgify"] = {"methode": methode, "path": path, "body": inp.get("body") or None,
                                    "resume": inp.get("resume") or f"{methode} {path}"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirmer", callback_data="lodgok"),
                                    InlineKeyboardButton("✖️ Annuler", callback_data="lodgno")]])
        apercu = json.dumps(inp.get("body"), ensure_ascii=False)[:600] if inp.get("body") else ""
        msg = (f"⚠️ Action Lodgify a confirmer :\n\n{inp.get('resume')}\n\n"
               f"↳ {methode} {path}" + (f"\n{apercu}" if apercu else ""))
        await context.bot.send_message(chat_id, msg, reply_markup=kb)
        return "Action Lodgify preparee, en attente de la confirmation du responsable."
    return "Outil inconnu."


async def answer_admin(update, context, state, question) -> None:
    chat_id = update.effective_chat.id
    # Cloisonnement : un responsable ne voit que son entreprise ; le super admin voit tout.
    # Valeur propre a cette conversation (ContextVar) -> aucune fuite entre responsables simultanes.
    scope = None if is_super(chat_id) else admin_company(chat_id)
    _SCOPE_COMPANY.set(scope)
    logger.info("Question admin de %s (chat_id=%s) : %s", state.get("prenom"), chat_id, question)
    await update.message.reply_text("🔎 J'analyse...")
    try:
        checkouts = await load_checkouts()
    except Exception:
        logger.exception("Erreur Lodgify (admin)")
        checkouts = []
    # Cloisonnement du planning : un responsable ne voit que les logements de son entreprise
    if scope:
        ck = co_key(scope)
        checkouts = [c for c in checkouts if co_key(property_company(c.get("property_id"))) == ck]
    missions = build_missions_data()
    if not missions and not checkouts:
        await update.message.reply_text("Aucune donnee disponible pour le moment.")
        return

    today = datetime.date.today()
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    repere = (f"Aujourd'hui = {today.isoformat()} ({jours[today.weekday()]}). "
              f"hier = {(today - datetime.timedelta(days=1)).isoformat()}, "
              f"il y a 3 jours = {(today - datetime.timedelta(days=3)).isoformat()}, "
              f"il y a 7 jours = {(today - datetime.timedelta(days=7)).isoformat()}.")
    lang = state.get("lang") or AGENT_LANG.get(str(chat_id)) or "fr"
    langue_nom = {"fr": "francais", "en": "English", "es": "espanol",
                  "ar": "Arabic (العربية)", "ro": "romana"}.get(lang, "francais")
    system = (
        "Tu es l'agent admin d'une conciergerie / menage. Tu aides le responsable.\n"
        f"TRES IMPORTANT : reponds TOUJOURS et uniquement en {langue_nom}, quelle que soit la langue des donnees.\n"
        "Deux jeux de donnees te sont fournis dans le message : MISSIONS (menages realises) et "
        "CHECKOUTS (planning Lodgify). "
        f"{repere} Interprete les dates relatives par rapport a aujourd'hui. Le property_id est l'identifiant fiable.\n\n"
        "Tu disposes d'OUTILS pour AGIR : envoyer_photos, envoyer_videos, exporter_rapport "
        "(genere un fichier rapport HTML imprimable en PDF), message_agent, et supprimer_mission "
        "(effacer definitivement une mission — uniquement pour l'admin principal, avec confirmation). "
        "Des que le responsable demande des photos, des videos, un rapport / fichier / export / PDF / document, "
        "ou d'ecrire a un agent, UTILISE l'outil correspondant — ne dis JAMAIS que tu ne peux pas generer de fichier. "
        "Pour une simple question d'analyse, reponds normalement en texte (precis, avec chiffres, en croisant "
        "MISSIONS et CHECKOUTS). N'invente jamais de donnees."
    )
    tools = list(ADMIN_TOOLS)
    if is_super(chat_id):
        system = system + "\n\n" + LODGIFY_GUIDE
        tools = tools + LODGIFY_TOOLS
    user_content = (f"MISSIONS:\n{json.dumps(missions, ensure_ascii=False)}\n\n"
                    f"CHECKOUTS:\n{json.dumps(checkouts, ensure_ascii=False)}\n\n"
                    f"DEMANDE DU RESPONSABLE : {question}")
    messages = [{"role": "user", "content": user_content}]
    model = ANTHROPIC_ADMIN_MODEL

    for _ in range(6):
        try:
            resp = await claude_tools_call(system, messages, tools, model)
        except Exception:
            logger.exception("Erreur tool-use (%s)", model)
            if model != ANTHROPIC_MODEL:
                model = ANTHROPIC_MODEL
                continue
            await context.bot.send_message(chat_id, "Desole, je n'ai pas pu traiter la demande pour le moment.")
            return
        content = resp.get("content", []) or []
        for b in content:
            if b.get("type") == "text" and b.get("text", "").strip():
                await _send_long(context, chat_id, b["text"].strip())
        if resp.get("stop_reason") != "tool_use":
            return
        tool_results = []
        for b in content:
            if b.get("type") == "tool_use":
                try:
                    res = await execute_admin_tool(b.get("name"), b.get("input", {}), context, chat_id, state)
                except Exception:
                    logger.exception("Echec outil %s", b.get("name"))
                    res = "Une erreur est survenue lors de l'execution de cette action."
                tool_results.append({"type": "tool_result", "tool_use_id": b.get("id"), "content": res})
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": tool_results})
    await context.bot.send_message(chat_id, "(J'ai atteint la limite d'etapes, dis-moi si tu veux continuer.)")


async def on_msg_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    state = get_state(query.from_user.id)
    pend = state.get("pending_msg")
    if not pend:
        await query.edit_message_text("Rien a envoyer.")
        return
    try:
        await context.bot.send_message(int(pend["chat_id"]),
                                       f"📩 Message du responsable :\n\n{pend['texte']}")
        await query.edit_message_text("✅ Message envoye a l'agent.")
    except Exception:
        logger.exception("Echec envoi message agent")
        await query.edit_message_text("❌ Echec de l'envoi (l'agent n'a peut-etre jamais ouvert le bot).")
    state["pending_msg"] = None


async def on_msg_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    get_state(query.from_user.id)["pending_msg"] = None
    await query.edit_message_text("Message annule.")


async def on_del_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirme la suppression d'une mission (admin principal uniquement)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    if not is_super(chat_id):
        await query.edit_message_text("Reserve a l'admin principal.")
        return
    state = get_state(chat_id)
    pend = state.get("pending_delete")
    if not pend:
        await query.edit_message_text("Rien a supprimer.")
        return
    fp = pend["fp"]
    ok = False
    try:
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        ok = _delete_mission_files(fp, d)
    except Exception:
        logger.exception("Echec suppression mission")
    state["pending_delete"] = None
    await query.edit_message_text(
        f"🗑️ Mission supprimee : {pend['label']}." if ok else "❌ Echec de la suppression.")


async def on_del_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    get_state(query.from_user.id)["pending_delete"] = None
    await query.edit_message_text("Suppression annulee.")


async def on_lodg_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute l'action Lodgify apres confirmation (super admin uniquement)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    if not is_super(chat_id):
        await query.edit_message_text("Reserve a l'admin principal.")
        return
    state = get_state(chat_id)
    pend = state.get("pending_lodgify")
    if not pend:
        await query.edit_message_text("Rien a executer.")
        return
    state["pending_lodgify"] = None
    await query.edit_message_text("⏳ Execution dans Lodgify...")
    try:
        status, data = await lodgify_api(pend["methode"], pend["path"], None, pend.get("body"))
        ok = 200 <= int(status) < 300
        apercu = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        apercu = (apercu or "")[:800]
        tag = "✅ Fait" if ok else "❌ Refuse par Lodgify"
        await context.bot.send_message(chat_id, f"{tag} — {pend['resume']}\n(HTTP {status})\n{apercu}")
    except Exception as e:
        logger.exception("Execution Lodgify")
        await context.bot.send_message(chat_id, f"❌ Erreur lors de l'action Lodgify : {e}")


async def on_lodg_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    get_state(query.from_user.id)["pending_lodgify"] = None
    await query.edit_message_text("Action Lodgify annulee.")


# =====================================================================
# ACCUEIL / LANGUE
# =====================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent = update.effective_user
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    state["prenom"] = agent.first_name
    state["admin_mode"] = False
    state["mission"] = None  # repart toujours d'un etat propre
    logger.info("/start de %s (chat_id=%s, tg_lang=%s)", agent.first_name, chat_id, agent.language_code)
    if not is_agent_authorized(chat_id):
        await ask_or_block(update, context, chat_id, state)
        return
    if not state.get("lang"):
        state["lang"] = norm_lang(agent.language_code)  # defaut provisoire
        await update.message.reply_text(CHOOSE_LANG, reply_markup=lang_keyboard())
        return
    await update.message.reply_text(
        t(state["lang"], "welcome", prenom=display_name(chat_id, state) or agent.first_name or "",
          soc=person_company(chat_id) or "ta conciergerie"),
        reply_markup=welcome_keyboard(state["lang"]),
    )


async def on_annuler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abandonne la mission de menage en cours (sans rien archiver)."""
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    lang = state.get("lang") or AGENT_LANG.get(str(chat_id)) or "fr"
    if state.get("mission"):
        state["mission"] = None
        logger.info("Mission annulee par chat_id=%s", chat_id)
        await update.message.reply_text(t(lang, "mission_cancel"),
                                        reply_markup=welcome_keyboard(lang))
    else:
        await update.message.reply_text(t(lang, "mission_none"),
                                        reply_markup=welcome_keyboard(lang))


async def on_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    state = get_state(chat_id)
    state["lang"] = query.data.split(":", 1)[1]
    AGENT_LANG[str(chat_id)] = state["lang"]
    _save_agent_lang()
    lang = state["lang"]
    logger.info("Langue choisie chat_id=%s -> %s", chat_id, lang)
    # Personne pas encore autorisee : on lance l'inscription (choix du role) dans sa langue
    if not is_agent_authorized(chat_id):
        if str(chat_id) in PENDING:
            await query.edit_message_text(t(lang, "reg_already_pending"))
            return
        state["reg"] = {"step": "role"}
        await query.edit_message_text(t(lang, "reg_choose_role"), reply_markup=role_keyboard(lang))
        return
    await query.edit_message_text(
        t(lang, "welcome", prenom=display_name(chat_id, state) or "",
          soc=person_company(chat_id) or "ta conciergerie"),
        reply_markup=welcome_keyboard(lang),
    )


async def on_langue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    get_state(update.effective_chat.id)
    await update.message.reply_text(CHOOSE_LANG, reply_markup=lang_keyboard())


async def on_changelang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(CHOOSE_LANG, reply_markup=lang_keyboard())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    state["prenom"] = update.effective_user.first_name
    lang = state.get("lang") or norm_lang(update.effective_user.language_code)
    m = state.get("mission")
    reg = state.get("reg")
    # Configuration du profil super admin (entreprise + role)
    if reg and reg.get("step") in ("super_entreprise", "super_role"):
        await handle_super_profile_step(update, context, state, reg)
        return
    # Etapes d'inscription (responsable ou agent)
    if reg and reg.get("step") in ("admin_nom", "admin_co", "admin_entreprise", "admin_role", "agent_nom"):
        await handle_reg_step(update, context, state, reg)
        return
    # Mode admin : questions en langage naturel sur les rapports (responsable uniquement)
    if state.get("admin_mode") and is_admin(chat_id) and not m:
        await answer_admin(update, context, state, update.message.text)
        return
    if m and m["etape"] == ETAPE_INCIDENT:
        await finaliser_incident(update, context, chat_id, state, update.message.text)
        return
    if not is_agent_authorized(chat_id):
        ll = state.get("lang") or AGENT_LANG.get(str(chat_id)) or "fr"
        if str(chat_id) in PENDING:
            await update.message.reply_text(t(ll, "reg_already_pending"))
        else:
            await ask_or_block(update, context, chat_id, state)
        return
    if not state.get("lang"):
        state["lang"] = lang
        await update.message.reply_text(CHOOSE_LANG, reply_markup=lang_keyboard())
        return
    if m:
        await update.message.reply_text(t(lang, "follow"))
    else:
        await update.message.reply_text(t(lang, "press_start"), reply_markup=welcome_keyboard(lang))


# =====================================================================
# MISSION
# =====================================================================
def _appart_kb(items, lang) -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(f"🏠 {it['name']}", callback_data=f"appart:{it['property_id']}") for it in items]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]  # 2 colonnes
    return InlineKeyboardMarkup(rows)


PROPERTIES_CACHE_FILE = os.path.join(BASE_DIR, "properties_cache.json")


def _save_properties_cache(items: list[dict]) -> None:
    try:
        with open(PROPERTIES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
    except Exception:
        logger.exception("Echec sauvegarde cache logements")


def _load_properties_cache() -> list[dict]:
    try:
        with open(PROPERTIES_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


async def get_all_properties() -> list[dict]:
    """Tous les appartements (on peut en choisir n'importe lequel, meme pour un menage imprevu).
    La liste reussie est mise en cache sur disque -> secours si Lodgify est indisponible."""
    props = _items(await _lodgify_get("/properties", params={"size": 200}))
    out = []
    for p in props:
        pid = _first(p, "id", "property_id")
        if pid is None:
            continue
        internal = str(_first(p, "internal_name", default="")).strip()
        if not internal or internal.lower() == "empty":
            internal = str(_first(p, "name", default="")).strip() or f"Appart {pid}"
        out.append({"property_id": str(pid), "name": internal})
    out.sort(key=lambda x: x["name"].lower())
    if out:
        _save_properties_cache(out)
    return out


async def on_begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    state = get_state(chat_id)
    lang = state.get("lang") or "fr"
    if not is_agent_authorized(chat_id):
        await query.edit_message_text(TXT_BLOQUE)
        return
    if not LODGIFY_API_KEY:
        await query.edit_message_text("Lodgify non configure.")
        return
    try:
        items = await get_all_properties()
    except Exception:
        logger.exception("Erreur Lodgify")
        items = _load_properties_cache()   # secours : derniere liste connue
        if items:
            await context.bot.send_message(chat_id, t(lang, "lodgify_offline"))
    if not items:
        await query.edit_message_text(t(lang, "lodgify_err"))
        return
    # On limite aux logements de l'entreprise de la personne (repli sur tout si rien d'assigne)
    soc = person_company(chat_id)
    if soc:
        scoped = [it for it in items if co_key(property_company(it["property_id"])) == co_key(soc)]
        if scoped:
            items = scoped
    state["apparts_today"] = {it["property_id"]: it["name"] for it in items}
    try:
        await query.edit_message_text(t(lang, "which_appart"), reply_markup=_appart_kb(items, lang))
    except BadRequest as e:
        if "not modified" not in str(e):   # liste deja affichee (double-appui) -> sans gravite
            raise


async def on_appart_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    state = get_state(chat_id)
    lang = state.get("lang") or "fr"
    property_id = query.data.split(":", 1)[1]
    name = state.get("apparts_today", {}).get(property_id, f"Appart {property_id}")
    state["mission"] = new_mission(property_id, name)
    logger.info("Mission demarree : chat_id=%s appart=%s", chat_id, name)
    await query.edit_message_text(t(lang, "appart_chosen", name=name))


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    lang = state.get("lang") or "fr"
    m = state.get("mission")
    video = update.message.video or update.message.video_note

    if m and m["etape"] == ETAPE_VIDEO_AVANT:
        tg_file = await video.get_file()
        path = os.path.join(MEDIA_DIR, f"{chat_id}_{_stamp()}_avant.mp4")
        await tg_file.download_to_drive(path)
        m["media"]["video_avant"] = path
        m["etape"] = ETAPE_MENAGE
        logger.info("Video AVANT recue : %s", path)
        await update.message.reply_text(t(lang, "video_avant_ok"), reply_markup=menage_keyboard(lang))
        return

    if m and m["etape"] == ETAPE_VIDEO_FIN:
        tg_file = await video.get_file()
        path = os.path.join(MEDIA_DIR, f"{chat_id}_{_stamp()}_fin.mp4")
        await tg_file.download_to_drive(path)
        m["media"]["video_fin"] = path
        logger.info("Video FIN recue : %s", path)
        await finir_mission(update, context, chat_id, state)
        return

    await update.message.reply_text(t(lang, "not_video"), reply_markup=welcome_keyboard(lang))


async def on_fin_menage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    state = get_state(chat_id)
    lang = state.get("lang") or "fr"
    m = state.get("mission")
    if not m or m["etape"] != ETAPE_MENAGE:
        return
    m["etape"] = ETAPE_CHECKLIST
    m["sec_index"] = 0
    m["sec_photos"] = 0
    await query.edit_message_text(t(lang, "menage_done"))
    if lang != "fr" and lang not in CHECKLIST_CACHE:
        await context.bot.send_message(chat_id, "⏳ Preparation de la checklist...")
    m["checklist"] = await get_checklist(lang)
    await send_step(context, chat_id, state)


# --- Libelles boutons / invites de la checklist (multilingue) ---
TXT_FAIT = {"fr": "✅ Fait", "en": "✅ Done", "es": "✅ Hecho", "ar": "✅ تم", "ro": "✅ Făcut"}
TXT_NA = {"fr": "➖ Non applicable", "en": "➖ N/A", "es": "➖ No aplica",
          "ar": "➖ لا ينطبق", "ro": "➖ Nu se aplică"}
TXT_TERMINE = {"fr": "✅ Photos terminees", "en": "✅ Photos done", "es": "✅ Fotos hechas",
               "ar": "✅ انتهت الصور", "ro": "✅ Poze gata"}
TXT_ENVOIE_PHOTO = {"fr": "Envoie une photo 📷", "en": "Send a photo 📷", "es": "Envía una foto 📷",
                    "ar": "أرسل صورة 📷", "ro": "Trimite o poză 📷"}
TXT_ENVOIE_PHOTOS = {"fr": "Envoie les photos, puis « Photos terminees » 📷",
                     "en": "Send the photos, then « Photos done » 📷",
                     "es": "Envía las fotos, luego « Fotos hechas » 📷",
                     "ar": "أرسل الصور ثم « انتهت الصور » 📷",
                     "ro": "Trimite pozele, apoi « Poze gata » 📷"}
TXT_NOMBRE = {"fr": "Tape le nombre 🔢", "en": "Type the number 🔢", "es": "Escribe el número 🔢",
              "ar": "اكتب الرقم 🔢", "ro": "Scrie numărul 🔢"}

CHECKLIST_SECTIONS = len(CHECKLIST)


def _cl(m) -> list:
    """Checklist a afficher pour l'agent (traduite si dispo), sinon FR."""
    return m.get("checklist") or CHECKLIST


def _bar(num, total) -> str:
    filled = max(1, min(10, round(num / total * 10)))
    return "▰" * filled + "▱" * (10 - filled)


def _recap_text(state) -> str:
    lang = state.get("lang") or "fr"
    m = state["mission"]
    faits = sum(1 for v in m.get("confirmations", {}).values() if v is True)
    soucis = len(m.get("incidents", [])) + len(m.get("controles", []))
    photos = len(m.get("media", {}).get("photos", []))
    return (f"📋 {m['name']}\n"
            f"✅ {faits} sections   ⚠️ {soucis}   📷 {photos}\n\n"
            f"{t(lang, 'checklist_done')}")


def _fr_titre(m) -> str:
    return CHECKLIST[m["sec_index"]]["titre"]


def _section_kb(lang):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "sec_next"), callback_data="ck:next")],
        [InlineKeyboardButton(t(lang, "btn_incident"), callback_data="incident")],
    ])


def _is_repeat(m) -> bool:
    """La section courante est-elle repetable (chambre / SDB / WC) ?"""
    i = m.get("sec_index", 0)
    return bool(CHECKLIST[i].get("repeat")) if 0 <= i < len(CHECKLIST) else False


async def send_step(context, chat_id, state) -> None:
    """Affiche une SECTION entiere : points a verifier + photos a envoyer (envoi libre)."""
    lang = state.get("lang") or "fr"
    m = state["mission"]
    cl = _cl(m)
    sec = cl[m["sec_index"]]
    m["sec_photos"] = 0
    m["sec_issues"] = []
    m["sec_seen"] = []
    n = m["sec_index"] + 1
    total = len(cl)
    points = "\n".join("✓ " + p for p in sec.get("points", []))
    photos = "\n".join("•  " + ph for ph in sec.get("photos", []))
    bloc_points = (f"{t(lang, 'sec_points_intro')}\n{points}\n\n" if points else "")
    txt = (f"{sec['titre']}     ·  {n}/{total}\n"
           f"{_bar(n, total)}\n\n"
           f"{bloc_points}"
           f"{t(lang, 'sec_photos_list')}\n{photos}\n\n"
           f"{t(lang, 'sec_instructions')}")
    await context.bot.send_message(chat_id, txt, reply_markup=_section_kb(lang))


async def advance_step(context, chat_id, state) -> None:
    m = state["mission"]
    cl = _cl(m)
    m["sec_index"] += 1
    m["sec_photos"] = 0
    if m["sec_index"] >= len(cl):
        m["etape"] = ETAPE_VIDEO_FIN
        await context.bot.send_message(chat_id, _recap_text(state))
    else:
        await send_step(context, chat_id, state)


async def on_ck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.from_user.id
    state = get_state(chat_id)
    lang = state.get("lang") or "fr"
    m = state.get("mission")
    if not m or m["etape"] != ETAPE_CHECKLIST:
        await query.answer()
        return
    action = query.data.split(":", 1)[1]
    sec = _cl(m)[m["sec_index"]]
    titre = sec["titre"]
    shots = sec.get("photos", [])

    async def _finish_section():
        # Trace pour le rapport de l'admin : soucis IA + photos manquantes
        seen = m.get("sec_seen", [])
        for i in m.get("sec_issues", []):
            m.setdefault("controles", []).append(
                {"section": titre, "desc": i.get("desc") or "-", "path": i.get("path")})
        for s in shots:
            if s not in seen:
                m.setdefault("controles", []).append(
                    {"section": titre, "desc": f"Photo non fournie : {s}", "path": None})
        m["confirmations"][_fr_titre(m)] = True
        if _is_repeat(m):
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(t(lang, "sec_again"), callback_data="ck:again")],
                [InlineKeyboardButton(t(lang, "sec_cont"), callback_data="ck:cont")],
            ])
            await query.edit_message_text(
                t(lang, "sec_done", titre=titre) + "\n\n" + t(lang, "sec_again_q"),
                reply_markup=kb)
        else:
            await query.edit_message_text(t(lang, "sec_done", titre=titre))
            await advance_step(context, chat_id, state)

    # Refaire une piece identique (chambre 2, SDB 2...) : on repart a zero sur la meme section
    if action == "again":
        await query.answer()
        await query.edit_message_text(t(lang, "sec_done", titre=titre))
        await send_step(context, chat_id, state)
        return

    # Passer a la piece suivante
    if action == "cont":
        await query.answer()
        await advance_step(context, chat_id, state)
        return

    # Completer / reprendre : on garde les photos deja reconnues, on reevalue les soucis
    if action == "back":
        await query.answer()
        m["sec_issues"] = []
        await query.edit_message_text(t(lang, "sec_redo"), reply_markup=_section_kb(lang))
        return

    # Passer quand meme (photos manquantes ou soucis assumes par l'agent)
    if action == "pass":
        await query.answer()
        await _finish_section()
        return

    if action != "next":
        await query.answer()
        return

    # --- Bouton "Suivant" : on verifie ce qui manque / ce qui semble sale ---
    await query.answer()
    seen = m.get("sec_seen", [])
    missing = [s for s in shots if s not in seen]
    issues = m.get("sec_issues", [])
    if missing or issues:
        lines = []
        if missing:
            lines.append(t(lang, "sec_recap_missing", miss=", ".join(missing)))
        if issues:
            iss = "\n".join("• " + (i.get("desc") or "-") for i in issues)
            lines.append(t(lang, "sec_recap_issues", iss=iss))
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t(lang, "sec_complete"), callback_data="ck:back")],
            [InlineKeyboardButton(t(lang, "sec_pass"), callback_data="ck:pass")],
        ])
        await query.edit_message_text("\n\n".join(lines), reply_markup=kb)
        return
    await _finish_section()


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    lang = state.get("lang") or "fr"
    m = state.get("mission")

    # Photo jointe a un incident
    if m and m["etape"] == ETAPE_INCIDENT:
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        path = os.path.join(MEDIA_DIR, f"{chat_id}_{_stamp()}_incident.jpg")
        await tg_file.download_to_drive(path)
        m["incident_pending"]["photo"] = path
        await update.message.reply_text(t(lang, "incident_photo_ok"))
        return

    if not (m and m["etape"] == ETAPE_CHECKLIST and m["sec_index"] < len(_cl(m))):
        await update.message.reply_text(t(lang, "not_photo"))
        return

    sec = _cl(m)[m["sec_index"]]
    titre = sec["titre"]
    shots = sec.get("photos", [])

    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    path = os.path.join(MEDIA_DIR, f"{chat_id}_{_stamp()}_{len(m['media']['photos']) + 1}.jpg")
    await tg_file.download_to_drive(path)
    m["sec_photos"] = m.get("sec_photos", 0) + 1

    # L'IA reconnait A QUELLE photo attendue elle correspond, et signale si sale
    match, probleme = await claude_photo_recognize(path, shots, titre)
    libelle = match or titre
    m["media"]["photos"].append({"point": f"{titre} — {libelle}", "path": path})
    if match:
        m.setdefault("sec_seen", [])
        if match not in m["sec_seen"]:
            m["sec_seen"].append(match)
    if probleme:
        m.setdefault("sec_issues", []).append(
            {"attendu": libelle, "desc": f"{libelle} : {probleme}", "path": path})
    logger.info("Photo section %s : reconnue=%s, probleme=%s -> %s", titre, match or "?", probleme or "-", path)

    if match:
        await update.message.reply_text(t(lang, "sec_photo_seen", x=match))
    else:
        await update.message.reply_text(t(lang, "sec_photo_unseen"))


async def on_photo_keep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """L'agent garde quand meme une photo signalee (le controle reste note dans le rapport)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    state = get_state(chat_id)
    lang = state.get("lang") or "fr"
    m = state.get("mission")
    if m:
        m["photo_check"] = None
    await query.edit_message_text(t(lang, "photo_ok"))


async def on_photo_retake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """L'agent reprend la photo : on retire la photo douteuse et son controle."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    state = get_state(chat_id)
    lang = state.get("lang") or "fr"
    m = state.get("mission")
    pc = m.get("photo_check") if m else None
    if m and pc:
        path = pc.get("path")
        m["media"]["photos"] = [p for p in m["media"]["photos"] if p.get("path") != path]
        m["controles"] = [c for c in m.get("controles", []) if c.get("path") != path]
        if m.get("sec_photos", 0) > 0:
            m["sec_photos"] -= 1
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            logger.exception("Echec suppression photo reprise")
        m["photo_check"] = None
    await query.edit_message_text(t(lang, "photo_retake"))


async def resume_checklist(context, chat_id, state) -> None:
    """Reprise apres un incident, a l'endroit ou on en etait."""
    await send_step(context, chat_id, state)


# =====================================================================
# INCIDENT
# =====================================================================
async def on_incident(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    state = get_state(chat_id)
    lang = state.get("lang") or "fr"
    m = state.get("mission")
    if not m:
        return
    if m["etape"] != ETAPE_INCIDENT:
        m["incident_retour"] = m["etape"]
    m["etape"] = ETAPE_INCIDENT
    m["incident_pending"] = {}
    await context.bot.send_message(chat_id, t(lang, "incident_prompt"))


async def finaliser_incident(update, context, chat_id, state, texte) -> None:
    lang = state.get("lang") or "fr"
    m = state["mission"]
    photo = m.get("incident_pending", {}).get("photo")

    analyse = None
    try:
        analyse = await analyser_incident(texte, lang)
    except Exception:
        logger.exception("Erreur analyse Claude")

    if analyse:
        resume = analyse.get("resume") or texte
        urgent = bool(analyse.get("urgent"))
        langue = analyse.get("langue")
        reponse = analyse.get("reponse_agent") or t(lang, "incident_ack")
    else:
        resume, urgent, langue = texte, False, None
        reponse = t(lang, "incident_ack")

    m["incidents"].append({
        "texte": texte, "resume": resume, "urgent": urgent, "langue": langue,
        "photo": photo, "heure": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    logger.info("Incident enregistre (urgent=%s) : %s", urgent, resume)

    tag = "🚨🚨 URGENT" if urgent else "⚠️ Incident"
    alerte = (f"{tag} - {m['name']}\nAgent : {state.get('prenom')}\n\n{resume}\n\n"
              f"(message original : {texte})")
    for admin_id in all_admin_ids():
        try:
            if photo:
                with open(photo, "rb") as ph:
                    await context.bot.send_photo(int(admin_id), photo=ph, caption=alerte[:1000])
            else:
                await context.bot.send_message(int(admin_id), alerte)
        except Exception:
            logger.exception("Echec alerte admin %s", admin_id)

    await update.message.reply_text(reponse)

    m["incident_pending"] = {}
    retour = m.get("incident_retour") or ETAPE_MENAGE
    m["etape"] = retour
    if retour == ETAPE_CHECKLIST:
        await resume_checklist(context, chat_id, state)
    elif retour == ETAPE_MENAGE:
        await update.message.reply_text(t(lang, "resume"), reply_markup=menage_keyboard(lang))
    elif retour == ETAPE_VIDEO_FIN:
        await update.message.reply_text(t(lang, "send_fin"))
    elif retour == ETAPE_VIDEO_AVANT:
        await update.message.reply_text(t(lang, "send_avant"))


# =====================================================================
# CLOTURE + ARCHIVAGE
# =====================================================================
async def finir_mission(update, context, chat_id, state) -> None:
    lang = state.get("lang") or "fr"
    m = state["mission"]
    fin = datetime.datetime.now().isoformat(timespec="seconds")
    # Controles photo IA conserves -> notes non urgentes dans le rapport
    for c in m.get("controles", []):
        m["incidents"].append({
            "texte": "", "resume": f"Contrôle photo — {c.get('section')}: {c.get('desc')}",
            "urgent": False, "langue": None, "photo": c.get("path"),
            "heure": datetime.datetime.now().isoformat(timespec="seconds"),
        })
    a_un_non = any(v is False for v in m["confirmations"].values())
    statut_code = "A verifier" if (a_un_non or m["incidents"]) else "Valide"

    mission_id = f"{chat_id}_{m['debut'].replace(':', '-')}"
    data = {
        "mission_id": mission_id,
        "agent": {"chat_id": chat_id, "prenom": state.get("prenom"), "langue": lang},
        "appart": {"property_id": m["property_id"], "nom_interne": m["name"]},
        "heure_debut": m["debut"], "heure_fin": fin,
        "video_avant": m["media"]["video_avant"], "video_fin": m["media"]["video_fin"],
        "photos": m["media"]["photos"], "confirmations": m["confirmations"],
        "incidents": m["incidents"], "statut": statut_code,
    }
    now = datetime.datetime.now()
    dossier = os.path.join(ARCHIVES_DIR, now.strftime("%Y"), now.strftime("%m"))
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, mission_id + ".json")
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("Mission archivee : %s (statut=%s)", chemin, statut_code)

    statut_aff = t(lang, "st_ok") if statut_code == "Valide" else t(lang, "st_check")
    state["mission"] = None
    await update.message.reply_text(t(lang, "mission_archived", statut=statut_aff),
                                    reply_markup=welcome_keyboard(lang))


# =====================================================================
# DEMARRAGE
# =====================================================================
AGENT_CMDS = [
    BotCommand("start", "Commencer / Start / Empezar"),
    BotCommand("annuler", "Annuler la mission en cours / Cancel"),
    BotCommand("langue", "Changer de langue / Language"),
]
ADMIN_CMDS = [
    BotCommand("start", "Commencer / Start / Empezar"),
    BotCommand("annuler", "Annuler la mission en cours / Cancel"),
    BotCommand("langue", "Changer de langue / Language"),
    BotCommand("admin", "Panneau admin (rapports, agents)"),
]
SUPER_CMDS = ADMIN_CMDS + [BotCommand("lodgify", "🏨 Gestion Lodgify")]


async def apply_admin_menu(bot, chat_id) -> None:
    """Affiche le menu admin (enrichi) pour ce chat precis. Super admin = menu + Lodgify."""
    cmds = SUPER_CMDS if is_super(chat_id) else ADMIN_CMDS
    try:
        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=int(chat_id)))
    except Exception:
        logger.exception("Echec menu admin pour %s", chat_id)


async def apply_agent_menu(bot, chat_id) -> None:
    """Remet le menu simple (agent) pour ce chat precis."""
    try:
        await bot.set_my_commands(AGENT_CMDS, scope=BotCommandScopeChat(chat_id=int(chat_id)))
    except Exception:
        logger.exception("Echec menu agent pour %s", chat_id)


async def _post_init(app: Application) -> None:
    # Menu par defaut (agents et nouveaux venus) : simple
    await app.bot.set_my_commands(AGENT_CMDS, scope=BotCommandScopeDefault())
    # Menu enrichi pour chaque admin (par conversation)
    for aid in all_admin_ids():
        await apply_admin_menu(app.bot, aid)


def main() -> None:
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "COLLE_TON_TOKEN_ICI":
        raise SystemExit("\n>>> ERREUR : TELEGRAM_TOKEN absent du fichier .env.\n")
    load_state()  # recharge les missions en cours (survie aux redemarrages)
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("annuler", on_annuler))
    app.add_handler(CommandHandler("admin", on_admin))
    app.add_handler(CommandHandler("lodgify", on_lodgify))
    app.add_handler(CommandHandler("langue", on_langue))
    app.add_handler(CommandHandler("monid", on_monid))
    app.add_handler(CommandHandler("ajouter_admin", on_ajouter_admin))
    app.add_handler(CommandHandler("retirer_admin", on_retirer_admin))
    app.add_handler(CommandHandler("admins", on_admins_list))
    app.add_handler(CommandHandler("ajouter_agent", on_ajouter_agent))
    app.add_handler(CommandHandler("retirer_agent", on_retirer_agent))
    app.add_handler(CommandHandler("agents", on_agents_list))
    app.add_handler(CommandHandler("photos", on_photos))
    app.add_handler(CallbackQueryHandler(on_msg_ok, pattern=r"^msgok$"))
    app.add_handler(CallbackQueryHandler(on_msg_no, pattern=r"^msgno$"))
    app.add_handler(CallbackQueryHandler(on_del_ok, pattern=r"^delmissok$"))
    app.add_handler(CallbackQueryHandler(on_del_no, pattern=r"^delmissno$"))
    app.add_handler(CallbackQueryHandler(on_lodg_ok, pattern=r"^lodgok$"))
    app.add_handler(CallbackQueryHandler(on_lodg_no, pattern=r"^lodgno$"))
    app.add_handler(CallbackQueryHandler(on_photo_keep, pattern=r"^pkeep$"))
    app.add_handler(CallbackQueryHandler(on_photo_retake, pattern=r"^pretake$"))
    app.add_handler(CallbackQueryHandler(on_auth, pattern=r"^auth:"))
    app.add_handler(CallbackQueryHandler(on_admin_panel, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(on_delagent, pattern=r"^delagent:"))
    app.add_handler(CallbackQueryHandler(on_deladmin, pattern=r"^deladmin:"))
    app.add_handler(CallbackQueryHandler(on_logpick, pattern=r"^logpick:"))
    app.add_handler(CallbackQueryHandler(on_logset, pattern=r"^logset:"))
    app.add_handler(CallbackQueryHandler(on_logtog, pattern=r"^logtog:"))
    app.add_handler(CallbackQueryHandler(on_reg_role, pattern=r"^reg:role:"))
    app.add_handler(CallbackQueryHandler(on_reg_company, pattern=r"^regco:"))
    app.add_handler(CallbackQueryHandler(on_reg_new_company, pattern=r"^regnewco$"))
    app.add_handler(CallbackQueryHandler(on_lang, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(on_changelang, pattern=r"^changelang$"))
    app.add_handler(CallbackQueryHandler(on_begin, pattern=r"^begin$"))
    app.add_handler(CallbackQueryHandler(on_appart_click, pattern=r"^appart:"))
    app.add_handler(CallbackQueryHandler(on_fin_menage, pattern=r"^finmenage$"))
    app.add_handler(CallbackQueryHandler(on_ck, pattern=r"^ck:"))
    app.add_handler(CallbackQueryHandler(on_incident, pattern=r"^incident$"))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, on_video))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    # Sauvegarde de l'etat apres chaque update (groupe tardif) -> survit aux redemarrages
    app.add_handler(TypeHandler(Update, _persist_state), group=90)
    # Filet de securite : tout bug non gere previent l'utilisateur au lieu de le bloquer
    app.add_error_handler(on_error)

    logger.info("ALFRED-M (multilingue) demarre. En attente... (Ctrl+C pour arreter)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
