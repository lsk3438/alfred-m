"""
ALFRED-M - Bot Telegram de coordination menage (Genius BnB)
============================================================
Paliers 1 a 6 + MULTILINGUE (fr, en, es, ar, ro).

- Au 1er contact : choix de la langue (boutons drapeaux), memorise par agent.
- Tous les textes fixes sont traduits via un dictionnaire (pas d'IA = fiable/gratuit).
- Claude n'est appele QUE pour comprendre/resumer un incident en texte libre.
- Les archives gardent les libelles en FRANCAIS (rapports uniformes).
"""

import base64
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
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
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
        "menage_done": "Bravo ! 👏 On passe au contrôle final, point par point (quelques photos + vérifications). C'est rapide, laisse-toi guider.",
        "point_photo": "📸 Étape {num}/{n} — {label}\nEnvoie une photo comme preuve.",
        "btn_yes": "✅ Oui", "btn_no": "⚠️ Non",
        "point_confirm": "Étape {num}/{n} — {label}",
        "point_done": "Étape {num}/{n} — {label} → {mark}",
        "checklist_done": "Checklist terminée, beau travail ! 🎉 Dernière étape : filme une courte vidéo du logement propre et prêt à accueillir les voyageurs. 📹",
        "photo_ok": "Photo reçue ✓ Merci !",
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
        "follow": "Suis simplement les étapes en cours 🙂 Utilise les boutons et envoie les photos/vidéos demandées.",
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
        "follow": "Just follow the current steps 🙂 Use the buttons and send the requested photos/videos.",
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
        "follow": "Solo sigue los pasos actuales 🙂 Usa los botones y envía las fotos/vídeos pedidos.",
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
        "follow": "فقط اتبع الخطوات الحالية 🙂 استخدم الأزرار وأرسل الصور/الفيديوهات المطلوبة.",
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
        "follow": "Urmează pur și simplu pașii curenți 🙂 Folosește butoanele și trimite pozele/videourile cerute.",
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
    {"titre": "1. Chambre(s)", "items": [
        {"type": "confirm", "label": "Lit refait avec linge propre (draps + housse + taies), borde, sans tache"},
        {"type": "confirm", "label": "Aucun poil / cheveu sur le lit"},
        {"type": "confirm", "label": "Surfaces depoussierees (tables de nuit, lampes, etageres, plinthes, hauteur)"},
        {"type": "confirm", "label": "Poignees + interrupteurs essuyes"},
        {"type": "photo",   "label": "Sous le lit verifie (rien oublie, pas de poussiere)"},
        {"type": "confirm", "label": "Miroir sans traces"},
    ]},
    {"titre": "2. Salle de bain / WC", "items": [
        {"type": "photo",   "label": "WC entierement lave (interieur sous le rebord + exterieur)"},
        {"type": "photo",   "label": "Douche / baignoire lavee (parois, robinetterie, sol detartre)"},
        {"type": "confirm", "label": "Lavabo lave (vasque + robinetterie)"},
        {"type": "confirm", "label": "Miroir, carrelage et joints propres (sans traces ni moisissure)"},
        {"type": "photo",   "label": "Siphon douche vide (cheveux retires)"},
        {"type": "number",  "label": "Serviettes propres posees — nombre exact"},
        {"type": "confirm", "label": "Papier toilette present (au moins 2 rouleaux)"},
        {"type": "confirm", "label": "Savon mains present"},
    ]},
    {"titre": "3. Cuisine", "items": [
        {"type": "photo",   "label": "Vaisselle propre et rangee (casseroles, verres, couverts, assiettes)"},
        {"type": "confirm", "label": "Lave-vaisselle vide"},
        {"type": "confirm", "label": "Evier + plan de travail propres et degraisses"},
        {"type": "photo",   "label": "Plaques, four et micro-ondes propres (int + ext)"},
        {"type": "confirm", "label": "Bouilloire + cafetiere propres et detartrees"},
        {"type": "photo",   "label": "Frigo et congelateur vides et nettoyes"},
        {"type": "confirm", "label": "Torchons + eponge propres"},
        {"type": "confirm", "label": "Produit vaisselle present"},
    ]},
    {"titre": "4. Salon / Sejour", "items": [
        {"type": "confirm", "label": "Canape propre, sans tache (sinon : Signaler un souci + photo)"},
        {"type": "confirm", "label": "Coussins remis en place"},
        {"type": "confirm", "label": "Surfaces depoussierees + poignees / interrupteurs essuyes"},
        {"type": "confirm", "label": "TV presente"},
        {"type": "confirm", "label": "Telecommande(s) presente(s) + piles OK"},
    ]},
    {"titre": "5. Sols, vitres & controles generaux", "items": [
        {"type": "confirm", "label": "Tous les sols aspires (toutes les pieces)"},
        {"type": "confirm", "label": "Tous les sols laves (toutes les pieces)"},
        {"type": "confirm", "label": "Toutes les vitres et fenetres sans traces"},
        {"type": "confirm", "label": "Toutes les poubelles videes + sacs neufs"},
        {"type": "confirm", "label": "Aucune toile d'araignee"},
        {"type": "confirm", "label": "Aucun insecte / salete residuelle"},
        {"type": "confirm", "label": "Toutes les ampoules fonctionnent"},
        {"type": "confirm", "label": "Seche-cheveux present et fonctionnel"},
        {"type": "confirm", "label": "Fer a repasser present"},
        {"type": "confirm", "label": "Sacs poubelle en reserve (3-4)"},
        {"type": "confirm", "label": "Chauffage eteint"},
    ]},
    {"titre": "6. Avant de partir", "items": [
        {"type": "confirm", "label": "Lumieres eteintes + fenetres fermees"},
        {"type": "confirm", "label": "Porte verrouillee"},
        {"type": "photo",   "label": "Cles remises dans la boite a cles"},
    ]},
]

# --- Traductions de la checklist (auto par Claude, mises en cache sur disque) ---
CHECKLIST_I18N_FILE = os.path.join(BASE_DIR, "checklist_i18n.json")
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
    payload = [{"titre": s["titre"], "items": [it["label"] for it in s["items"]]} for s in CHECKLIST]
    system = (
        f"Traduis fidelement en langue '{lang}' tous les textes du JSON (titre + items). "
        "Garde EXACTEMENT la meme structure, le meme nombre d'elements et le meme ordre. "
        "Ne traduis pas les emojis. Reponds UNIQUEMENT avec le JSON traduit, rien d'autre."
    )
    raw = await claude_text(system, json.dumps(payload, ensure_ascii=False), max_tokens=6000)
    data = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
    if len(data) != len(CHECKLIST):
        raise ValueError("structure traduite incoherente")
    result = []
    for i, sec in enumerate(CHECKLIST):
        tr = data[i]
        labels = tr.get("items", [])
        items = [{"type": sec["items"][j]["type"],
                  "label": labels[j] if j < len(labels) else sec["items"][j]["label"]}
                 for j in range(len(sec["items"]))]
        result.append({"titre": tr.get("titre", sec["titre"]), "items": items})
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
        "sec_index": 0, "item_index": 0, "checklist": None,
        "media": {"video_avant": None, "photos": [], "video_fin": None},
        "confirmations": {}, "incidents": [],
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
async def _lodgify_get(path: str, params: dict | None = None):
    headers = {"accept": "application/json", "X-ApiKey": LODGIFY_API_KEY}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(LODGIFY_BASE + path, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


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


async def get_today_apparts() -> list[dict]:
    props = _items(await _lodgify_get("/properties", params={"size": 200}))
    name_by_id: dict = {}
    for p in props:
        pid = _first(p, "id", "property_id")
        internal = str(_first(p, "internal_name", default="")).strip()
        if not internal or internal.lower() == "empty":
            internal = str(_first(p, "name", default="")).strip() or f"Appart {pid}"
        if pid is not None:
            name_by_id[str(pid)] = internal
    books = _items(await _lodgify_get("/reservations/bookings", params={"size": 200}))
    today = datetime.date.today()
    apparts: dict = {}
    for b in books:
        pid = _first(b, "property_id", "propertyId")
        dep = _first(b, "departure", "checkOut", "check_out", default="")
        if pid is None or not dep:
            continue
        try:
            dep_date = datetime.date.fromisoformat(str(dep)[:10])
        except ValueError:
            continue
        if dep_date < today:
            continue
        key = str(pid)
        if key not in apparts or dep_date < apparts[key]["date"]:
            apparts[key] = {"property_id": key,
                            "name": name_by_id.get(key, f"Appart {key}"),
                            "date": dep_date}
    result = list(apparts.values())
    result.sort(key=lambda x: x["date"])
    return result


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
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
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


async def claude_text(system: str, user: str, max_tokens: int = 900, model: str | None = None) -> str | None:
    if not ANTHROPIC_API_KEY:
        return None
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    body = {"model": model or ANTHROPIC_MODEL, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
        r.raise_for_status()
        return r.json()["content"][0]["text"]


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


def company_keyboard(lang: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🏢 {disp}", callback_data=f"regco:{key}")]
            for key, disp in all_companies().items()]
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
    nom = reg.get("nom", "Agent")
    PENDING[str(chat_id)] = {"type": "agent", "nom": nom, "entreprise": disp, "lang": ll,
                             "date": datetime.datetime.now().isoformat(timespec="seconds")}
    _save_pending()
    state["reg"] = None
    await query.edit_message_text(t(ll, "reg_pending", name=nom, soc=disp))
    await notify_validators(context, str(chat_id))


async def handle_super_profile_step(update, context, state, reg) -> None:
    """Configuration de l'entreprise et du role du super admin (1re fois)."""
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()
    if reg.get("step") == "super_entreprise":
        if len(txt) < 2:
            await update.message.reply_text("Nom d'entreprise trop court, réessaie :")
            return
        SUPER_PROFILE["entreprise"] = txt
        _save_super_profile()
        reg["step"] = "super_role"
        await update.message.reply_text("Parfait. Et quel est ton rôle ? (ex : gérant)")
        return
    SUPER_PROFILE["role"] = txt or "Gérant"
    _save_super_profile()
    state["reg"] = None
    state["admin_mode"] = True
    prenom = state.get("prenom") or "admin"
    await update.message.reply_text(
        "✅ Profil enregistré.\n\n" + ADMIN_PANEL_TXT.format(prenom=prenom),
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
        info = f"🆕 Nouvelle demande RESPONSABLE\n👤 {nom}\n🏢 {ent}\n💼 {role}\n🔑 {code}"
    else:
        targets = []
        if MANAGER_CHAT_ID:
            targets.append(str(MANAGER_CHAT_ID))
        for c, i in ADMINS.items():
            if co_key(i.get("entreprise", "")) == co_key(ent) and c not in targets:
                targets.append(c)
        info = f"🆕 Nouvelle demande AGENT\n👤 {nom}\n🏢 {ent}\n🔑 {code}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Autoriser", callback_data=f"auth:ok:{code}"),
        InlineKeyboardButton("❌ Refuser", callback_data=f"auth:no:{code}"),
    ]])
    for aid in targets:
        try:
            await context.bot.send_message(int(aid), info + "\n\nValider ?", reply_markup=kb)
        except Exception:
            logger.exception("Echec notification validateur %s", aid)


async def on_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bouton Autoriser/Refuser une inscription (selon le type et l'entreprise)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    if not is_admin(chat_id):
        await query.answer("Reserve aux admins.", show_alert=True)
        return
    _, action, code = query.data.split(":", 2)
    reg = PENDING.get(code)
    if not reg:
        await query.edit_message_text("Cette demande a déjà été traitée.")
        return
    typ = reg.get("type")
    nom = reg.get("nom", "?")
    ent = reg.get("entreprise", "")
    role = reg.get("role", "")
    ll = reg.get("lang", "fr")
    # Permissions
    if typ == "admin" and not is_super(chat_id):
        await query.answer("Seul l'admin principal valide les responsables.", show_alert=True)
        return
    if typ == "agent" and not is_super(chat_id) and co_key(admin_company(chat_id) or "") != co_key(ent):
        await query.answer("Tu ne peux valider que les agents de ton entreprise.", show_alert=True)
        return
    if action == "no":
        PENDING.pop(code, None)
        _save_pending()
        try:
            await context.bot.send_message(int(code), t(ll, "reg_refused"))
        except Exception:
            pass
        await query.edit_message_text(f"❌ Demande de {nom} refusée.")
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
    kind = "responsable" if typ == "admin" else "agent"
    await query.edit_message_text(f"✅ {nom} validé ({kind} — {ent}).")


# Entreprise active pour le cloisonnement des rapports (None = super admin, voit tout)
_SCOPE_COMPANY = None


def _company_agent_ids(company: str) -> set:
    ck = co_key(company or "")
    return {str(c) for c, i in AGENTS_AUTH.items() if co_key(i.get("entreprise", "")) == ck}


def load_full_reports() -> list[dict]:
    """Rapports complets (avec chemins des photos). Filtre par entreprise si _SCOPE_COMPANY est defini."""
    scope_ids = _company_agent_ids(_SCOPE_COMPANY) if _SCOPE_COMPANY else None
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
    global _SCOPE_COMPANY
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.message.reply_text("Cette commande est reservee aux admins.")
        return
    _SCOPE_COMPANY = None if is_super(chat_id) else admin_company(chat_id)
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
    rows = [[InlineKeyboardButton("👥 Gérer les agents", callback_data="adm:agents")]]
    if is_super(chat_id):
        rows.append([InlineKeyboardButton("🏠 Assigner les logements", callback_data="adm:logements")])
        rows.append([InlineKeyboardButton("🧑‍💼 Gérer les admins", callback_data="adm:admins")])
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
    if not is_admin(chat_id):
        await update.message.reply_text("Cette commande est reservee aux admins.")
        return
    # 1re fois pour le super admin : configurer son entreprise + role
    if is_super(chat_id) and not SUPER_PROFILE.get("entreprise"):
        state["reg"] = {"step": "super_entreprise"}
        await update.message.reply_text(
            "Avant tout, configurons ton profil. Quel est le nom de ton entreprise ?")
        return
    state["admin_mode"] = True
    prenom = display_name(chat_id, state) or update.effective_user.first_name or "admin"
    logger.info("Panneau admin ouvert par %s (chat_id=%s)", prenom, chat_id)
    await update.message.reply_text(
        ADMIN_PANEL_TXT.format(prenom=prenom),
        reply_markup=admin_panel_kb(chat_id),
    )


async def on_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boutons du panneau admin (admins uniquement)."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    if not is_admin(chat_id):
        await query.answer("Reserve aux admins.", show_alert=True)
        return
    state = get_state(chat_id)
    action = query.data.split(":", 1)[1]
    if action == "reports":
        state["admin_mode"] = True
        await query.edit_message_text(
            "📊 Mode rapport activé.\n\nPose ta question, par exemple :\n"
            "• quels appartements ont été nettoyés aujourd'hui ?\n"
            "• quel agent fait souvent churchill 79 ?\n"
            "• combien d'incidents cette semaine, et lesquels urgents ?\n"
            "• liste les missions « À vérifier » et pourquoi.\n\n"
            "Pose autant de questions que tu veux. Tape /start pour quitter."
        )
    elif action == "agents":
        super_ = is_super(chat_id)
        macomp = admin_company(chat_id)
        # super admin voit tout ; un responsable ne voit que SON entreprise
        items = [(c, i) for c, i in AGENTS_AUTH.items()
                 if super_ or co_key(i.get("entreprise", "")) == co_key(macomp or "")]
        if not items:
            await query.edit_message_text(
                "👥 Aucun agent pour l'instant.\n"
                "(Quand un agent s'inscrit pour ton entreprise, tu reçois un message avec un bouton Autoriser.)")
            return
        txt = "👥 Tes agents :\n" if not super_ else "👥 Tous les agents :\n"
        rows = []
        for code, info in items:
            nom = info.get("prenom", "?")
            ent = info.get("entreprise", "")
            txt += f"• {nom}" + (f" — {ent}" if super_ and ent else "") + "\n"
            rows.append([InlineKeyboardButton(f"❌ Retirer {nom}", callback_data=f"delagent:{code}")])
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows))
    elif action == "logements":
        await render_logements(query, chat_id)
    elif action == "admins":
        if not is_super(chat_id):
            await query.answer("Reserve a l'admin principal.", show_alert=True)
            return
        moi = SUPER_PROFILE.get("entreprise", "")
        txt = f"🧑‍💼 Responsables :\n• (toi, principal) {moi}\n"
        rows = []
        for code, info in ADMINS.items():
            nom = info.get("prenom", "?")
            txt += f"• {nom} — {info.get('entreprise', '')} ({info.get('role', '')})\n"
            rows.append([InlineKeyboardButton(f"❌ Retirer {nom}", callback_data=f"deladmin:{code}")])
        if not rows:
            txt += "\n(Aucun autre responsable pour l'instant.)"
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(rows) if rows else None)


async def on_delagent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bouton ❌ Retirer un agent depuis le panneau admin."""
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    if not is_admin(chat_id):
        await query.answer("Reserve aux admins.", show_alert=True)
        return
    code = query.data.split(":", 1)[1]
    info = AGENTS_AUTH.get(code)
    if not info:
        await query.edit_message_text("Cet agent n'est plus dans la liste.")
        return
    if not is_super(chat_id) and co_key(info.get("entreprise", "")) != co_key(admin_company(chat_id) or ""):
        await query.answer("Tu ne peux retirer que les agents de ton entreprise.", show_alert=True)
        return
    nom = AGENTS_AUTH.pop(code).get("prenom", "")
    _save_agents_auth()
    await query.edit_message_text(f"🗑️ Agent retire : {nom}.")


async def render_logements(query, chat_id) -> None:
    """Affiche la liste des logements avec leur entreprise (super admin uniquement)."""
    if not is_super(chat_id):
        await query.answer("Réservé à l'admin principal.", show_alert=True)
        return
    try:
        props = await get_all_properties()
    except Exception:
        logger.exception("Erreur Lodgify (logements)")
        await query.edit_message_text("Je n'arrive pas à récupérer la liste des logements. Réessaie.")
        return
    if not props:
        await query.edit_message_text("Aucun logement trouvé dans Lodgify.")
        return
    txt = ("🏠 Assigner les logements\nClique sur un logement pour choisir son entreprise.\n"
           "(❓ = pas encore assigné)\n")
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
    if not is_super(chat_id):
        await query.answer("Reserve a l'admin principal.", show_alert=True)
        return
    pid = query.data.split(":", 1)[1]
    companies = list(all_companies().values())
    rows = [[InlineKeyboardButton(f"🏢 {disp}", callback_data=f"logset:{pid}:{idx}")]
            for idx, disp in enumerate(companies)]
    rows.append([InlineKeyboardButton("❌ Non assigne", callback_data=f"logset:{pid}:x")])
    rows.append([InlineKeyboardButton("⬅️ Retour", callback_data="adm:logements")])
    await query.edit_message_text("A quelle entreprise appartient ce logement ?",
                                  reply_markup=InlineKeyboardMarkup(rows))


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
    if not is_super(chat_id):
        await query.answer("Seul l'admin principal peut retirer un responsable.", show_alert=True)
        return
    code = query.data.split(":", 1)[1]
    if code in ADMINS:
        nom = ADMINS.pop(code).get("prenom", "")
        save_admins()
        await apply_agent_menu(context.bot, code)
        await query.edit_message_text(f"🗑️ Responsable retire : {nom}.")
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
*{box-sizing:border-box}
body{font-family:'Segoe UI',Arial,Helvetica,sans-serif;margin:0;background:#f4f6f8;color:#1f2937}
.wrap{max-width:900px;margin:0 auto;padding:0 0 40px}
.header{background:linear-gradient(135deg,#0f5132,#16a34a);color:#fff;padding:28px 32px}
.header .brand{font-size:22px;font-weight:700;letter-spacing:.5px}
.header .sub{opacity:.9;margin-top:2px;font-size:14px}
.header .meta{margin-top:14px;font-size:13px;opacity:.95}
.content{padding:24px 32px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.06);
      padding:20px 22px;margin:0 0 22px}
.card-top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;
          border-bottom:1px solid #eef0f2;padding-bottom:12px;margin-bottom:14px}
.card-top h2{margin:0;font-size:18px;color:#111827}
.badge{font-size:12px;font-weight:700;padding:5px 12px;border-radius:999px;white-space:nowrap}
.badge.ok{background:#dcfce7;color:#15803d}
.badge.warn{background:#fef3c7;color:#b45309}
.info{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:6px 0 16px}
.info .lab{font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:#6b7280}
.info .val{font-size:14px;font-weight:600;color:#111827}
.sec{font-size:13px;font-weight:700;color:#374151;margin:16px 0 8px;text-transform:uppercase;letter-spacing:.4px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #eef0f2}
th{background:#f9fafb;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.3px}
.pill{font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px}
.pill.y{background:#dcfce7;color:#15803d}
.pill.n{background:#fee2e2;color:#b91c1c}
.inc{background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:10px 14px;margin:6px 0;font-size:14px}
.inc.urg{background:#fef2f2;border-color:#fecaca}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-top:10px}
.gallery figure{margin:0;background:#f9fafb;border:1px solid #eef0f2;border-radius:8px;overflow:hidden}
.gallery img{width:100%;height:150px;object-fit:cover;display:block}
.gallery figcaption{padding:6px 8px;font-size:12px;color:#4b5563;text-align:center}
.footer{text-align:center;color:#9ca3af;font-size:12px;margin-top:24px}
@media print{body{background:#fff}.card{box-shadow:none;break-inside:avoid}.header{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""


def _build_html_report(matches, titre) -> str:
    n_ok = sum(1 for d in matches if d.get("statut") == "Valide")
    n_warn = len(matches) - n_ok
    gen = datetime.datetime.now().strftime("%d/%m/%Y a %H:%M")
    h = [f"<!doctype html><html lang='fr'><head><meta charset='utf-8'>",
         f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{_esc(titre)}</title>",
         f"<style>{REPORT_CSS}</style></head><body><div class='wrap'>",
         "<div class='header'><div class='brand'>Genius BnB</div>",
         "<div class='sub'>Rapport de menage — preuve d'intervention</div>",
         f"<div class='meta'>Genere le {gen} &nbsp;•&nbsp; {len(matches)} mission(s) &nbsp;•&nbsp; "
         f"{n_ok} validee(s), {n_warn} a verifier</div></div>",
         "<div class='content'>"]
    for d in matches:
        appart = d.get("appart", {}).get("nom_interne", "?")
        date = str(d.get("heure_debut", ""))[:10]
        statut = d.get("statut", "")
        ok = statut == "Valide"
        h.append("<div class='card'>")
        h.append(f"<div class='card-top'><h2>🏠 {_esc(appart)}</h2>"
                 f"<span class='badge {'ok' if ok else 'warn'}'>{_esc(statut)}</span></div>")
        deb = str(d.get("heure_debut", ""))[11:16]
        fin = str(d.get("heure_fin", ""))[11:16]
        h.append("<div class='info'>"
                 f"<div><div class='lab'>Date</div><div class='val'>{_esc(date)}</div></div>"
                 f"<div><div class='lab'>Agent</div><div class='val'>{_esc(d.get('agent', {}).get('prenom') or '-')}</div></div>"
                 f"<div><div class='lab'>Horaire</div><div class='val'>{_esc(deb)} → {_esc(fin)}</div></div>"
                 "</div>")
        conf = d.get("confirmations", {})
        if conf:
            h.append("<div class='sec'>Verifications</div><table><tr><th>Point</th><th>Reponse</th></tr>")
            for k, v in conf.items():
                if v is True:
                    pill = "<span class='pill y'>Fait</span>"
                elif v is False:
                    pill = "<span class='pill n'>Non</span>"
                else:
                    pill = f"<span class='pill'>{_esc(v)}</span>"  # N/A ou valeur (ex. nb serviettes)
                h.append(f"<tr><td>{_esc(k)}</td><td>{pill}</td></tr>")
            h.append("</table>")
        inc = d.get("incidents", [])
        if inc:
            h.append("<div class='sec'>Incidents signales</div>")
            for i in inc:
                urg = " urg" if i.get("urgent") else ""
                tag = " <b>(URGENT)</b>" if i.get("urgent") else ""
                h.append(f"<div class='inc{urg}'>⚠️ {_esc(i.get('resume'))}{tag}</div>")
        photos = [p for p in d.get("photos", []) if _img_data_uri(p.get("path", ""))]
        if photos:
            h.append(f"<div class='sec'>Photos preuve ({len(photos)})</div><div class='gallery'>")
            for ph in photos:
                uri = _img_data_uri(ph.get("path", ""))
                h.append(f"<figure><img src='{uri}'><figcaption>{_esc(ph.get('point', ''))}</figcaption></figure>")
            h.append("</div>")
        h.append("</div>")
    h.append(f"<div class='footer'>Document genere automatiquement par ALFRED-M • Genius BnB</div>")
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


async def claude_tools_call(system: str, messages: list, tools: list, model: str, max_tokens: int = 2000) -> dict:
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    body = {"model": model, "max_tokens": max_tokens, "system": system, "tools": tools, "messages": messages}
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
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
            path = _build_html_report(matches, "Rapport de menage — Genius BnB")
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
    return "Outil inconnu."


async def answer_admin(update, context, state, question) -> None:
    global _SCOPE_COMPANY
    chat_id = update.effective_chat.id
    # Cloisonnement : un responsable ne voit que son entreprise ; le super admin voit tout
    _SCOPE_COMPANY = None if is_super(chat_id) else admin_company(chat_id)
    logger.info("Question admin de %s (chat_id=%s) : %s", state.get("prenom"), chat_id, question)
    await update.message.reply_text("🔎 L'agent analyse...")
    try:
        checkouts = await load_checkouts()
    except Exception:
        logger.exception("Erreur Lodgify (admin)")
        checkouts = []
    # Cloisonnement du planning : un responsable ne voit que les logements de son entreprise
    if _SCOPE_COMPANY:
        ck = co_key(_SCOPE_COMPANY)
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
    system = (
        "Tu es l'agent admin de Genius BnB (conciergerie / menage). Tu aides le responsable, en francais.\n"
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
    user_content = (f"MISSIONS:\n{json.dumps(missions, ensure_ascii=False)}\n\n"
                    f"CHECKOUTS:\n{json.dumps(checkouts, ensure_ascii=False)}\n\n"
                    f"DEMANDE DU RESPONSABLE : {question}")
    messages = [{"role": "user", "content": user_content}]
    model = ANTHROPIC_ADMIN_MODEL

    for _ in range(6):
        try:
            resp = await claude_tools_call(system, messages, ADMIN_TOOLS, model)
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


# =====================================================================
# ACCUEIL / LANGUE
# =====================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent = update.effective_user
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    state["prenom"] = agent.first_name
    state["admin_mode"] = False
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
    if reg and reg.get("step") in ("admin_nom", "admin_entreprise", "admin_role", "agent_nom"):
        await handle_reg_step(update, context, state, reg)
        return
    # Mode admin : questions en langage naturel sur les rapports (responsable uniquement)
    if state.get("admin_mode") and is_admin(chat_id) and not m:
        await answer_admin(update, context, state, update.message.text)
        return
    if m and m["etape"] == ETAPE_INCIDENT:
        await finaliser_incident(update, context, chat_id, state, update.message.text)
        return
    # Saisie d'un nombre demande par la checklist (ex. nombre de serviettes)
    if (m and m["etape"] == ETAPE_CHECKLIST and m["sec_index"] < len(_cl(m))
            and _cur_type(m) == "number"):
        val = (update.message.text or "").strip()
        m["confirmations"][_fr_label(m)] = val
        await update.message.reply_text(f"✅ {val}")
        await advance_step(context, chat_id, state)
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


async def get_all_properties() -> list[dict]:
    """Tous les appartements (on peut en choisir n'importe lequel, meme pour un menage imprevu)."""
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
        await query.edit_message_text(t(lang, "lodgify_err"))
        return
    if not items:
        await query.edit_message_text(t(lang, "no_appart"))
        return
    # On limite aux logements de l'entreprise de la personne (repli sur tout si rien d'assigne)
    soc = person_company(chat_id)
    if soc:
        scoped = [it for it in items if co_key(property_company(it["property_id"])) == co_key(soc)]
        if scoped:
            items = scoped
    state["apparts_today"] = {it["property_id"]: it["name"] for it in items}
    await query.edit_message_text(t(lang, "which_appart"), reply_markup=_appart_kb(items, lang))


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
    m["item_index"] = 0
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

CHECKLIST_TOTAL = sum(len(s["items"]) for s in CHECKLIST)


def _cl(m) -> list:
    """Checklist a afficher pour l'agent (traduite si dispo), sinon FR."""
    return m.get("checklist") or CHECKLIST


def _progress(m):
    cl = _cl(m)
    num = sum(len(cl[i]["items"]) for i in range(m["sec_index"])) + m["item_index"] + 1
    return num, CHECKLIST_TOTAL


def _bar(num, total) -> str:
    filled = max(1, min(10, round(num / total * 10)))
    return "▰" * filled + "▱" * (10 - filled)


def _recap_text(state) -> str:
    lang = state.get("lang") or "fr"
    m = state["mission"]
    conf = m.get("confirmations", {})
    faits = sum(1 for v in conf.values() if v is True)
    na = sum(1 for v in conf.values() if v == "N/A")
    soucis = len(m.get("incidents", []))
    photos = len(m.get("media", {}).get("photos", []))
    return (f"📋 {m['name']}\n"
            f"✅ {faits}   ➖ {na}   ⚠️ {soucis}   📷 {photos}\n\n"
            f"{t(lang, 'checklist_done')}")


def _fr_label(m) -> str:
    return CHECKLIST[m["sec_index"]]["items"][m["item_index"]]["label"]


async def send_step(context, chat_id, state) -> None:
    lang = state.get("lang") or "fr"
    m = state["mission"]
    cl = _cl(m)
    sec = cl[m["sec_index"]]
    item = sec["items"][m["item_index"]]
    typ, label = item["type"], item["label"]
    num, total = _progress(m)
    header = f"━━━━━━━━━━\n📋 {sec['titre'].upper()}\n━━━━━━━━━━\n\n" if m["item_index"] == 0 else ""
    prog = f"{_bar(num, total)}   {num}/{total}\n\n"
    na = InlineKeyboardButton(TXT_NA.get(lang, TXT_NA["fr"]), callback_data="ck:na")
    inc = InlineKeyboardButton(t(lang, "btn_incident"), callback_data="incident")
    if typ == "confirm":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(TXT_FAIT.get(lang, TXT_FAIT["fr"]), callback_data="ck:ok"), na], [inc]])
        txt = f"{header}{prog}👉 {label}"
    elif typ == "photo":
        kb = InlineKeyboardMarkup([[na], [inc]])
        txt = f"{header}{prog}📷 {label}\n\n{TXT_ENVOIE_PHOTO.get(lang, TXT_ENVOIE_PHOTO['fr'])}"
    elif typ == "photos":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(TXT_TERMINE.get(lang, TXT_TERMINE["fr"]), callback_data="ck:done")], [na], [inc]])
        txt = f"{header}{prog}📷 {label}\n\n{TXT_ENVOIE_PHOTOS.get(lang, TXT_ENVOIE_PHOTOS['fr'])}"
    else:  # number
        kb = InlineKeyboardMarkup([[na]])
        txt = f"{header}{prog}🔢 {label}\n\n{TXT_NOMBRE.get(lang, TXT_NOMBRE['fr'])}"
    await context.bot.send_message(chat_id, txt, reply_markup=kb)


async def advance_step(context, chat_id, state) -> None:
    m = state["mission"]
    cl = _cl(m)
    m["item_index"] += 1
    if m["item_index"] >= len(cl[m["sec_index"]]["items"]):
        m["sec_index"] += 1
        m["item_index"] = 0
    if m["sec_index"] >= len(cl):
        m["etape"] = ETAPE_VIDEO_FIN
        await context.bot.send_message(chat_id, _recap_text(state))
    else:
        await send_step(context, chat_id, state)


def _cur_type(m) -> str:
    return _cl(m)[m["sec_index"]]["items"][m["item_index"]]["type"]


async def on_ck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    state = get_state(chat_id)
    m = state.get("mission")
    if not m or m["etape"] != ETAPE_CHECKLIST:
        return
    action = query.data.split(":", 1)[1]
    label = _cl(m)[m["sec_index"]]["items"][m["item_index"]]["label"]
    fr = _fr_label(m)
    if action == "ok":
        m["confirmations"][fr] = True
        await query.edit_message_text(f"✅ {label}")
    elif action == "na":
        m["confirmations"][fr] = "N/A"
        await query.edit_message_text(f"➖ {label} — N/A")
    elif action == "done":
        await query.edit_message_text("✅ OK")
    await advance_step(context, chat_id, state)


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

    typ = None
    if m and m["etape"] == ETAPE_CHECKLIST and m["sec_index"] < len(_cl(m)):
        typ = _cur_type(m)
    if typ not in ("photo", "photos"):
        await update.message.reply_text(t(lang, "not_photo"))
        return

    fr = _fr_label(m)
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    path = os.path.join(MEDIA_DIR, f"{chat_id}_{_stamp()}_{len(m['media']['photos']) + 1}.jpg")
    await tg_file.download_to_drive(path)
    m["media"]["photos"].append({"point": fr, "path": path})
    logger.info("Photo recue (%s) : %s", fr, path)
    await update.message.reply_text(t(lang, "photo_ok"))
    if typ == "photo":  # une seule photo attendue -> on avance
        await advance_step(context, chat_id, state)
    # type "photos" (lot) : on reste, l'agent envoie d'autres photos ou « Photos terminees »


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
    BotCommand("langue", "Changer de langue / Language"),
]
ADMIN_CMDS = [
    BotCommand("start", "Commencer / Start / Empezar"),
    BotCommand("langue", "Changer de langue / Language"),
    BotCommand("admin", "Panneau admin (rapports, agents)"),
]


async def apply_admin_menu(bot, chat_id) -> None:
    """Affiche le menu admin (enrichi) pour ce chat precis."""
    try:
        await bot.set_my_commands(ADMIN_CMDS, scope=BotCommandScopeChat(chat_id=int(chat_id)))
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
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", on_admin))
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
    app.add_handler(CallbackQueryHandler(on_auth, pattern=r"^auth:"))
    app.add_handler(CallbackQueryHandler(on_admin_panel, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(on_delagent, pattern=r"^delagent:"))
    app.add_handler(CallbackQueryHandler(on_deladmin, pattern=r"^deladmin:"))
    app.add_handler(CallbackQueryHandler(on_logpick, pattern=r"^logpick:"))
    app.add_handler(CallbackQueryHandler(on_logset, pattern=r"^logset:"))
    app.add_handler(CallbackQueryHandler(on_logtog, pattern=r"^logtog:"))
    app.add_handler(CallbackQueryHandler(on_reg_role, pattern=r"^reg:role:"))
    app.add_handler(CallbackQueryHandler(on_reg_company, pattern=r"^regco:"))
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

    logger.info("ALFRED-M (multilingue) demarre. En attente... (Ctrl+C pour arreter)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
