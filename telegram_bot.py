"""
Docti CRM — Agent Telegram vocal.

Envoie un vocal ou un texte au bot, il met à jour le CRM automatiquement.

Exemples de messages vocaux/texte :
  "J'ai eu medfit au tel, ils sont intéressés"
  "La démo avec Physio-Station est faite, ils veulent signer"
  "Rappeler Cabinet DynaMed la semaine prochaine"

Commandes :
  /status    — Résumé du pipeline
  /relances  — Leads à relancer
  /lead <nom> — Infos sur un lead
  /hot       — Top leads HOT non contactés
  /help      — Aide

Lancer :  python3 crm/telegram_bot.py
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Load env (override=True to ensure .env values take precedence)
load_dotenv(Path(__file__).parent / ".env", override=True)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# Supabase client (shared)
_supabase = None

def get_supabase():
    global _supabase
    if _supabase is None:
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase

VALID_STAGES = [
    "Lead", "1er Contact", "Réponse",
    "Démo proposée", "Démo faite", "Client signé", "Perdu",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("docti-bot")


# ---------------------------------------------------------------------------
# Database helpers (Supabase)
# ---------------------------------------------------------------------------

def find_lead_by_name(name: str) -> dict | None:
    """Fuzzy match a cabinet name in the database."""
    sb = get_supabase()
    # Paginate to get all leads (Supabase default limit is 1000)
    all_leads = []
    offset = 0
    while True:
        batch = sb.table("leads").select("id, cabinet, city, email, phone, stage, qualification, score").range(offset, offset + 999).execute().data
        all_leads.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    if not all_leads:
        return None

    cabinet_names = [row["cabinet"] for row in all_leads]
    matches = difflib.get_close_matches(name, cabinet_names, n=1, cutoff=0.4)

    if matches:
        for row in all_leads:
            if row["cabinet"] == matches[0]:
                return row
    return None


def update_lead(lead_id: int, stage: str = "", notes: str = ""):
    """Update a lead's stage and/or notes."""
    sb = get_supabase()
    updates = {"updated_at": datetime.now().isoformat()}
    if stage and stage in VALID_STAGES:
        updates["stage"] = stage
    if notes:
        # Get current notes first
        current = sb.table("leads").select("notes").eq("id", lead_id).single().execute()
        old_notes = current.data.get("notes", "") or ""
        updates["notes"] = old_notes + f"\n[{datetime.now().strftime('%d/%m %H:%M')}] {notes}"
    sb.table("leads").update(updates).eq("id", lead_id).execute()


def log_interaction(lead_id: int, channel: str, action_type: str, content: str, direction: str = "entrant"):
    """Log an interaction in the CRM."""
    sb = get_supabase()
    sb.table("interactions").insert({
        "lead_id": lead_id,
        "channel": channel,
        "action_type": action_type,
        "direction": direction,
        "content": content,
        "response_received": 1 if direction == "entrant" else 0,
    }).execute()


def get_pipeline_summary() -> str:
    """Get a summary of the pipeline."""
    sb = get_supabase()
    all_leads = []
    offset = 0
    while True:
        batch = sb.table("leads").select("stage").range(offset, offset + 999).execute().data
        all_leads.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    from collections import Counter
    counts = Counter(l["stage"] for l in all_leads)
    total = len(all_leads)

    lines = [f"📊 **Pipeline Docti** — {total} leads\n"]
    stage_icons = {
        "Lead": "🔵", "1er Contact": "📤", "Réponse": "💬",
        "Démo proposée": "📅", "Démo faite": "🎬",
        "Client signé": "🏆", "Perdu": "❌",
    }
    for stage, cnt in counts.most_common():
        icon = stage_icons.get(stage, "•")
        lines.append(f"{icon} {stage}: **{cnt}**")

    return "\n".join(lines)


def get_relances_summary() -> str:
    """Get pending relances."""
    sb = get_supabase()
    leads = sb.table("leads").select("id, cabinet, city, email, phone").eq("stage", "1er Contact").neq("email", "").execute().data

    if not leads:
        return "✅ Aucune relance en attente !"

    lines = ["🔔 **Relances à faire** :\n"]
    now = datetime.now()
    for lead in leads[:10]:
        # Get last interaction
        interactions = sb.table("interactions").select("created_at").eq("lead_id", lead["id"]).eq("direction", "sortant").order("created_at", desc=True).limit(1).execute().data
        days = 0
        if interactions:
            try:
                last = datetime.fromisoformat(interactions[0]["created_at"].replace("Z", "+00:00").replace("+00:00", ""))
                days = (now - last).days
            except (ValueError, TypeError):
                pass
        urgency = "🔴" if days > 7 else "🟡" if days > 3 else "🟢"
        lines.append(f"{urgency} **{lead['cabinet']}** ({lead['city']}) — {days}j")
        lines.append(f"   📧 {lead['email']}")

    return "\n".join(lines)


def get_hot_leads_summary() -> str:
    """Get top HOT leads not yet contacted."""
    sb = get_supabase()
    leads = sb.table("leads").select("cabinet, city, email, score, qualification").eq("stage", "Lead").neq("email", "").in_("qualification", ["Tres chaud", "Chaud"]).order("score", desc=True).limit(10).execute().data

    if not leads:
        return "✅ Tous les leads HOT ont été contactés !"

    lines = ["🎯 **Top leads HOT à contacter** :\n"]
    for lead in leads:
        icon = "🔥" if lead["qualification"] == "Tres chaud" else "🟠"
        lines.append(f"{icon} **{lead['cabinet']}** ({lead['city']}) — Score: {lead['score']:.0f}")
        lines.append(f"   📧 {lead['email']}")

    return "\n".join(lines)


def get_lead_info(name: str) -> str:
    """Get detailed info about a lead."""
    lead = find_lead_by_name(name)
    if not lead:
        return f"❌ Aucun lead trouvé pour '{name}'"

    return (
        f"📋 **{lead['cabinet']}**\n"
        f"📍 {lead['city']}\n"
        f"📧 {lead['email'] or '—'}\n"
        f"📞 {lead['phone'] or '—'}\n"
        f"📊 Stage: {lead['stage']}\n"
        f"🏷️ Qualification: {lead['qualification']}\n"
        f"⭐ Score: {lead['score']:.0f}\n"
        f"🆔 ID: {lead['id']}"
    )


# ---------------------------------------------------------------------------
# Whisper transcription
# ---------------------------------------------------------------------------

async def transcribe_voice(file_path: str) -> str:
    """Transcribe voice message using OpenAI Whisper API."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        with open(file_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="fr",
            )
        return transcript.text
    except Exception as e:
        log.error("Whisper transcription failed: %s", e)
        return f"[Erreur transcription: {e}]"


# ---------------------------------------------------------------------------
# Intent parsing with Claude
# ---------------------------------------------------------------------------

def parse_intent_local(transcription: str) -> dict:
    """
    Parse intent from transcription using regex (no API needed).
    Works for common French prospection phrases.
    """
    text = transcription.lower().strip()
    result = {
        "cabinet": "",
        "stage": "",
        "channel": "",
        "action": "",
        "notes": transcription,
        "create_task": False,
        "task_description": "",
    }

    # Detect channel
    if any(w in text for w in ["tel", "téléphone", "telephone", "appel", "appelé"]):
        result["channel"] = "Téléphone"
    elif any(w in text for w in ["mail", "email", "e-mail"]):
        result["channel"] = "Email"
    elif "linkedin" in text:
        result["channel"] = "LinkedIn"
    elif "whatsapp" in text:
        result["channel"] = "WhatsApp"

    # Detect stage from keywords
    if any(w in text for w in ["signé", "signer", "signent", "client"]):
        result["stage"] = "Client signé"
        result["action"] = "Client signé"
    elif any(w in text for w in ["démo faite", "demo faite", "fait la démo", "fait la demo"]):
        result["stage"] = "Démo faite"
        result["action"] = "Démo réalisée"
    elif any(w in text for w in ["démo", "demo", "présentation", "demonstration"]):
        result["stage"] = "Démo proposée"
        result["action"] = "Démo proposée"
    elif any(w in text for w in ["intéressé", "interesse", "intéressés", "interesses", "positif", "motivé"]):
        result["stage"] = "Réponse"
        result["action"] = "Intéressé"
    elif any(w in text for w in ["répondu", "repondu", "réponse", "reponse", "retour"]):
        result["stage"] = "Réponse"
        result["action"] = "Réponse reçue"
    elif any(w in text for w in ["contacté", "contacte", "envoyé", "envoye", "appelé", "appele"]):
        result["stage"] = "1er Contact"
        result["action"] = "Premier contact"
    elif any(w in text for w in ["perdu", "pas intéressé", "pas interesse", "refus", "non"]):
        result["stage"] = "Perdu"
        result["action"] = "Pas intéressé"

    # Detect task creation
    if any(w in text for w in ["rappeler", "relancer", "recontacter", "suivre"]):
        result["create_task"] = True
        result["task_description"] = "Rappeler / relancer"

    # Extract cabinet name — try to find known names from DB
    sb = get_supabase()
    all_cabinets = []
    offset = 0
    while True:
        batch = sb.table("leads").select("cabinet").range(offset, offset + 999).execute().data
        all_cabinets.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    cabinet_names = [r["cabinet"] for r in all_cabinets]

    # Try fuzzy matching each word combination in the message
    words = transcription.split()
    best_match = None
    best_score = 0

    # Try combinations of 1-4 consecutive words
    for length in range(4, 0, -1):
        for i in range(len(words) - length + 1):
            fragment = " ".join(words[i:i + length])
            matches = difflib.get_close_matches(fragment, cabinet_names, n=1, cutoff=0.5)
            if matches:
                # Calculate match quality
                score = difflib.SequenceMatcher(None, fragment.lower(), matches[0].lower()).ratio()
                if score > best_score:
                    best_score = score
                    best_match = fragment
                    result["cabinet"] = fragment

    if not result["cabinet"]:
        # Fallback: try common patterns like "avec X", "de X", "chez X"
        patterns = [
            r"(?:avec|de|chez|pour|à|a)\s+([A-Z][a-zà-ü]+(?:\s+[A-Z][a-zà-ü]+)*)",
            r"(?:avec|de|chez|pour|à|a)\s+(\S+(?:\s+\S+){0,3})",
        ]
        for pattern in patterns:
            match = re.search(pattern, transcription)
            if match:
                candidate = match.group(1).strip().rstrip(".,!?")
                # Remove common trailing words
                for stop in ["au", "ils", "elles", "il", "elle", "qui", "est", "sont", "veut", "veulent"]:
                    if candidate.lower().endswith(f" {stop}"):
                        candidate = candidate[:-(len(stop) + 1)]
                if len(candidate) > 2:
                    result["cabinet"] = candidate
                    break

    return result


async def parse_intent(transcription: str) -> dict:
    """
    Parse intent from transcription.
    Tries Claude API first, falls back to local regex parsing.
    """
    # Try Claude API if available
    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                messages=[{
                    "role": "user",
                    "content": f"""Tu es un assistant CRM pour un vendeur de logiciel médical (Docti) qui prospecte des cabinets de physiothérapie en Suisse.

Analyse ce message vocal transcrit et extrais les informations structurées.

Message: "{transcription}"

Réponds UNIQUEMENT en JSON valide avec ces champs :
{{
  "cabinet": "nom du cabinet mentionné (ou vide)",
  "stage": "nouveau stage parmi: Lead, 1er Contact, Réponse, Démo proposée, Démo faite, Client signé, Perdu (ou vide si pas de changement)",
  "channel": "canal: Téléphone, Email, LinkedIn, WhatsApp (ou vide)",
  "action": "description courte de l'action",
  "notes": "résumé à logger dans le CRM",
  "create_task": false,
  "task_description": ""
}}

Exemples :
- "J'ai eu medfit au tel, ils sont intéressés" → {{"cabinet": "medfit", "stage": "Réponse", "channel": "Téléphone", "action": "Appel - intéressé", "notes": "Intéressé suite appel téléphonique"}}
- "Rappeler DynaMed la semaine prochaine" → {{"cabinet": "DynaMed", "stage": "", "channel": "", "action": "Créer rappel", "notes": "À rappeler", "create_task": true, "task_description": "Rappeler la semaine prochaine"}}
- "La démo avec Physio-Station est faite, ils veulent signer" → {{"cabinet": "Physio-Station", "stage": "Client signé", "channel": "", "action": "Démo faite - veut signer", "notes": "Démo faite, veut signer le contrat"}}"""
                }]
            )

            text = response.content[0].text.strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text)

        except Exception as e:
            log.warning("Claude API failed, falling back to local parsing: %s", e)

    # Fallback: local regex parsing
    log.info("Using local intent parsing")
    return parse_intent_local(transcription)


# ---------------------------------------------------------------------------
# Process voice/text and update CRM
# ---------------------------------------------------------------------------

async def process_message(text: str) -> str:
    """Process a message (transcription or text) and update the CRM."""

    # Parse intent
    intent = await parse_intent(text)
    log.info("Intent parsed: %s", intent)

    cabinet_name = intent.get("cabinet", "")
    if not cabinet_name:
        return (
            f"🎙️ Transcription : _{text}_\n\n"
            f"⚠️ Je n'ai pas trouvé de nom de cabinet dans ce message. "
            f"Essaie de mentionner le nom du cabinet."
        )

    # Find lead in DB
    lead = find_lead_by_name(cabinet_name)
    if not lead:
        return (
            f"🎙️ Transcription : _{text}_\n\n"
            f"❌ Cabinet '{cabinet_name}' non trouvé dans le CRM.\n"
            f"Essaie avec un nom plus précis ou vérifie dans l'app."
        )

    # Update lead
    new_stage = intent.get("stage", "")
    notes = intent.get("notes", "")
    channel = intent.get("channel", "Téléphone")
    action = intent.get("action", "")

    if new_stage:
        update_lead(lead["id"], stage=new_stage, notes=notes)

    # Log interaction
    log_interaction(
        lead["id"],
        channel=channel or "Telegram",
        action_type=action or "Mise à jour vocale",
        content=f"[Vocal] {text}",
        direction="entrant" if new_stage in ("Réponse", "Client signé") else "sortant",
    )

    # Create task if needed
    if intent.get("create_task"):
        sb = get_supabase()
        due_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        sb.table("tasks").insert({
            "lead_id": lead["id"],
            "task_type": "rappel",
            "channel": channel,
            "description": intent.get("task_description", "Rappel"),
            "due_date": due_date,
        }).execute()

    # Build response
    response_lines = [
        f"🎙️ _{text}_\n",
        f"✅ **{lead['cabinet']}** ({lead['city']})",
    ]
    if new_stage:
        response_lines.append(f"📊 Stage: {lead['stage']} → **{new_stage}**")
    if channel:
        response_lines.append(f"📡 Canal: {channel}")
    if action:
        response_lines.append(f"📝 {action}")
    if intent.get("create_task"):
        response_lines.append(f"📅 Tâche créée: {intent.get('task_description', '')}")

    return "\n".join(response_lines)


# ---------------------------------------------------------------------------
# Telegram Bot handlers
# ---------------------------------------------------------------------------

async def start_handler(update, context):
    await update.message.reply_text(
        "🎯 **Docti CRM Bot**\n\n"
        "Envoie-moi un vocal ou un texte pour mettre à jour le CRM.\n\n"
        "Exemples :\n"
        "🎙️ _\"J'ai eu medfit au tel, intéressés\"_\n"
        "🎙️ _\"Démo faite avec Studio 11, veulent signer\"_\n\n"
        "📊 **Pipeline & Leads**\n"
        "/status — Pipeline complet\n"
        "/hot — Leads HOT à contacter\n"
        "/lead <nom> — Infos d'un lead\n"
        "/search <terme> — Rechercher (ville, nom...)\n"
        "/relances — Leads à relancer\n\n"
        "✏️ **Actions**\n"
        "/modifier <nom> <statut> — Changer le statut\n"
        "/email <nom> — Préparer un email\n"
        "/rappel <nom> <délai> — Créer un rappel\n\n"
        "📋 **Résumé**\n"
        "/resume — Résumé du jour (envoyé auto chaque matin)",
        parse_mode="Markdown",
    )


async def status_handler(update, context):
    summary = get_pipeline_summary()
    await update.message.reply_text(summary, parse_mode="Markdown")


async def relances_handler(update, context):
    summary = get_relances_summary()
    await update.message.reply_text(summary, parse_mode="Markdown")


async def hot_handler(update, context):
    summary = get_hot_leads_summary()
    await update.message.reply_text(summary, parse_mode="Markdown")


async def lead_handler(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /lead <nom du cabinet>")
        return
    name = " ".join(context.args)
    info = get_lead_info(name)
    await update.message.reply_text(info, parse_mode="Markdown")


async def voice_handler(update, context):
    """Handle incoming voice messages."""
    await update.message.reply_text("🎙️ Transcription en cours...")

    # Download voice file
    voice = update.message.voice or update.message.audio
    if not voice:
        await update.message.reply_text("❌ Pas de fichier audio trouvé.")
        return

    file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        # Transcribe
        transcription = await transcribe_voice(tmp_path)
        log.info("Transcription: %s", transcription)

        # Process and update CRM
        response = await process_message(transcription)
        await update.message.reply_text(response, parse_mode="Markdown")

    except Exception as e:
        log.error("Erreur voice_handler: %s", e)
        await update.message.reply_text(f"❌ Erreur : {str(e)[:200]}")
    finally:
        os.unlink(tmp_path)


async def modifier_handler(update, context):
    """Handle /modifier command to change a lead's stage.
    Usage: /modifier <cabinet_name> <stage>
    Example: /modifier Studio 11 Démo proposée
    """
    if not context.args or len(context.args) < 2:
        stages_list = "\n".join(f"  • {s}" for s in VALID_STAGES)
        await update.message.reply_text(
            "📝 **Usage :** `/modifier <cabinet> <statut>`\n\n"
            f"**Statuts possibles :**\n{stages_list}\n\n"
            "**Exemples :**\n"
            "`/modifier Studio 11 Lead`\n"
            "`/modifier medfit Démo proposée`\n"
            "`/modifier Physio-Station Client signé`",
            parse_mode="Markdown",
        )
        return

    # Parse: find which part is the stage (try from the end)
    args_text = " ".join(context.args)
    found_stage = None
    cabinet_part = ""

    # Try matching stages from longest to shortest
    for stage in sorted(VALID_STAGES, key=len, reverse=True):
        if args_text.lower().endswith(stage.lower()):
            found_stage = stage
            cabinet_part = args_text[:-(len(stage))].strip()
            break

    if not found_stage or not cabinet_part:
        await update.message.reply_text(
            f"❌ Je n'ai pas compris. Vérifie le format :\n"
            f"`/modifier <cabinet> <statut>`",
            parse_mode="Markdown",
        )
        return

    # Find lead
    lead = find_lead_by_name(cabinet_part)
    if not lead:
        await update.message.reply_text(f"❌ Cabinet '{cabinet_part}' non trouvé dans le CRM.")
        return

    old_stage = lead["stage"]
    update_lead(lead["id"], stage=found_stage, notes=f"Modifié via Telegram: {old_stage} → {found_stage}")
    log_interaction(lead["id"], "Telegram", "Modification statut", f"{old_stage} → {found_stage}")

    await update.message.reply_text(
        f"✅ **{lead['cabinet']}** ({lead['city']})\n"
        f"📊 {old_stage} → **{found_stage}**",
        parse_mode="Markdown",
    )


async def text_handler(update, context):
    """Handle incoming text messages (non-command)."""
    text = update.message.text
    if not text:
        return

    try:
        response = await process_message(text)
        await update.message.reply_text(response, parse_mode="Markdown")
    except Exception as e:
        log.error("Erreur text_handler: %s", e)
        await update.message.reply_text(
            f"❌ Erreur lors du traitement : {str(e)[:200]}\n\n"
            f"💡 Essaie avec une commande :\n"
            f"/status — Pipeline\n"
            f"/lead <nom> — Infos lead\n"
            f"/hot — Leads HOT",
        )


# ---------------------------------------------------------------------------
# Feature 1: Notifications (relances en retard, nouveaux leads)
# ---------------------------------------------------------------------------

async def check_notifications(context):
    """Periodic job: send alerts for overdue relances and new leads."""
    chat_id = context.job.data.get("chat_id")
    if not chat_id:
        return

    sb = get_supabase()
    now = datetime.now()
    alerts = []

    # Check overdue relances (leads at "1er Contact" with no interaction in 5+ days)
    leads_1c = sb.table("leads").select("id, cabinet, city, email").eq("stage", "1er Contact").execute().data
    for lead in leads_1c[:20]:
        interactions = sb.table("interactions").select("created_at").eq("lead_id", lead["id"]).eq("direction", "sortant").order("created_at", desc=True).limit(1).execute().data
        if interactions:
            try:
                last = datetime.fromisoformat(interactions[0]["created_at"].replace("Z", "").split("+")[0])
                days = (now - last).days
                if days >= 5:
                    alerts.append(f"🔴 **{lead['cabinet']}** ({lead['city']}) — {days}j sans relance")
            except (ValueError, TypeError):
                pass

    # Check new leads added in last 24h
    yesterday = (now - timedelta(days=1)).isoformat()
    new_leads = sb.table("leads").select("cabinet, city, score").gt("created_at", yesterday).order("score", desc=True).limit(10).execute().data

    if new_leads:
        alerts.append(f"\n🆕 **{len(new_leads)} nouveau(x) lead(s)** ajouté(s) :")
        for nl in new_leads[:5]:
            alerts.append(f"  • {nl['cabinet']} ({nl['city']}) — Score: {nl['score']:.0f}")

    if alerts:
        msg = "🔔 **Notifications Docti CRM**\n\n" + "\n".join(alerts)
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            log.error("Notification error: %s", e)


# ---------------------------------------------------------------------------
# Feature 2: Send email from Telegram
# ---------------------------------------------------------------------------

async def email_handler(update, context):
    """Handle /email command to send a prospection email.
    Usage: /email <cabinet_name>
    """
    if not context.args:
        await update.message.reply_text(
            "📧 **Usage :** `/email <nom du cabinet>`\n\n"
            "Envoie le template de prospection adapté au stage du lead.\n\n"
            "**Exemple :**\n"
            "`/email Studio 11`",
            parse_mode="Markdown",
        )
        return

    name = " ".join(context.args)
    lead = find_lead_by_name(name)
    if not lead:
        await update.message.reply_text(f"❌ Cabinet '{name}' non trouvé dans le CRM.")
        return

    email = lead.get("email", "")
    if not email or email == "nan" or "@" not in str(email):
        await update.message.reply_text(f"❌ **{lead['cabinet']}** n'a pas d'adresse email.")
        return

    # Determine template based on stage
    stage = lead.get("stage", "Lead")
    stage_template = {
        "Lead": ("Premier contact", "Simplifier la gestion de {cabinet} ?"),
        "1er Contact": ("Relance", "Re: Simplifier la gestion de {cabinet} ?"),
        "Reponse": ("Proposition démo", "Démo Docti pour {cabinet} — quel créneau ?"),
    }
    template_name, subject_tpl = stage_template.get(stage, ("Premier contact", "Simplifier la gestion de {cabinet} ?"))
    subject = subject_tpl.replace("{cabinet}", lead["cabinet"])

    await update.message.reply_text(
        f"📧 **Email prêt pour {lead['cabinet']}**\n\n"
        f"📬 À : {email}\n"
        f"📝 Template : {template_name}\n"
        f"📋 Sujet : {subject}\n"
        f"📊 Stage actuel : {stage}\n\n"
        f"→ Envoie `/email_confirm {lead['id']}` pour envoyer\n"
        f"→ Ou ouvre le CRM pour personnaliser le message",
        parse_mode="Markdown",
    )


async def email_confirm_handler(update, context):
    """Confirm and log email sent via /email_confirm <lead_id>."""
    if not context.args:
        await update.message.reply_text("Usage: `/email_confirm <lead_id>`", parse_mode="Markdown")
        return

    try:
        lead_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide")
        return

    sb = get_supabase()
    lead = sb.table("leads").select("*").eq("id", lead_id).single().execute().data

    if not lead:
        await update.message.reply_text("❌ Lead non trouvé")
        return

    # Log the interaction
    log_interaction(lead_id, "Email", "Email envoyé", f"Envoyé via Telegram", "sortant")

    # Update stage if needed
    if lead["stage"] == "Lead":
        sb.table("leads").update({"stage": "1er Contact", "updated_at": datetime.now().isoformat()}).eq("id", lead_id).execute()

    await update.message.reply_text(
        f"✅ Email enregistré pour **{lead['cabinet']}**\n"
        f"📊 Stage: {lead['stage']} → {'1er Contact' if lead['stage'] == 'Lead' else lead['stage']}",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Feature 3: Daily summary (automatic morning push)
# ---------------------------------------------------------------------------

async def daily_summary(context):
    """Send daily morning summary."""
    chat_id = context.job.data.get("chat_id")
    if not chat_id:
        return

    sb = get_supabase()

    # Pipeline stats
    all_leads = []
    offset = 0
    while True:
        batch = sb.table("leads").select("stage, qualification").range(offset, offset + 999).execute().data
        all_leads.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    from collections import Counter
    stage_counts = Counter(l["stage"] for l in all_leads)
    total = len(all_leads)

    # Hot leads not contacted
    hot_count = sb.table("leads").select("id", count="exact").eq("stage", "Lead").in_("qualification", ["Tres chaud", "Chaud"]).execute().count

    # Relances needed
    leads_1c = sb.table("leads").select("id, cabinet, city").eq("stage", "1er Contact").limit(50).execute().data
    overdue = 0
    now = datetime.now()
    for lead in leads_1c:
        interactions = sb.table("interactions").select("created_at").eq("lead_id", lead["id"]).eq("direction", "sortant").order("created_at", desc=True).limit(1).execute().data
        if interactions:
            try:
                last = datetime.fromisoformat(interactions[0]["created_at"].replace("Z", "").split("+")[0])
                if (now - last).days >= 5:
                    overdue += 1
            except (ValueError, TypeError):
                pass

    # Tasks due today
    today_str = now.strftime("%Y-%m-%d")
    tasks = sb.table("tasks").select("id", count="exact").eq("completed", 0).lte("due_date", today_str).execute().count

    # Build message
    msg = (
        f"☀️ **Bonjour Théo ! Résumé du {now.strftime('%d/%m/%Y')}**\n\n"
        f"📊 **Pipeline** — {total} leads\n"
    )
    stage_icons = {"Lead": "🔵", "1er Contact": "📤", "Reponse": "💬", "Demo proposee": "📅", "Demo faite": "🎬", "Negociation": "🤝", "Client signe": "🏆", "Perdu": "❌"}
    for stage, cnt in stage_counts.most_common():
        icon = stage_icons.get(stage, "•")
        msg += f"  {icon} {stage}: {cnt}\n"

    msg += f"\n⚡ **Priorités du jour**\n"
    msg += f"  🔥 {hot_count} leads chauds à contacter\n"
    msg += f"  🔴 {overdue} relances en retard\n"
    msg += f"  📋 {tasks} tâche(s) du jour\n"

    if hot_count > 0 or overdue > 0:
        msg += f"\n💡 Tape /hot pour voir les leads ou /relances pour les relances"

    try:
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
    except Exception as e:
        log.error("Daily summary error: %s", e)


async def resume_handler(update, context):
    """Manual trigger for daily summary via /resume command."""
    context.job = type('obj', (object,), {'data': {'chat_id': update.effective_chat.id}})()
    await daily_summary(context)


# ---------------------------------------------------------------------------
# Feature 4: Search leads from Telegram
# ---------------------------------------------------------------------------

async def search_handler(update, context):
    """Handle /search command to find leads.
    Usage: /search <query> — search by city, canton, or keyword
    """
    if not context.args:
        await update.message.reply_text(
            "🔍 **Usage :** `/search <terme>`\n\n"
            "**Exemples :**\n"
            "`/search Genève` — leads à Genève\n"
            "`/search pas contacté` — leads jamais contactés\n"
            "`/search onedoc` — leads utilisant Onedoc",
            parse_mode="Markdown",
        )
        return

    query = " ".join(context.args).lower()
    sb = get_supabase()

    # Search by city
    results = sb.table("leads").select("id, cabinet, city, email, stage, qualification, score").ilike("city", f"%{query}%").order("score", desc=True).limit(15).execute().data

    # If no city match, try cabinet name
    if not results:
        results = sb.table("leads").select("id, cabinet, city, email, stage, qualification, score").ilike("cabinet", f"%{query}%").order("score", desc=True).limit(15).execute().data

    # Special searches
    if not results and "onedoc" in query:
        results = sb.table("leads").select("id, cabinet, city, email, stage, qualification, score").eq("utilise_onedoc", 1).order("score", desc=True).limit(15).execute().data

    if not results and ("pas contact" in query or "jamais contact" in query or "non contact" in query):
        results = sb.table("leads").select("id, cabinet, city, email, stage, qualification, score").eq("stage", "Lead").neq("email", "").order("score", desc=True).limit(15).execute().data

    # Search by canton
    if not results:
        results = sb.table("leads").select("id, cabinet, city, email, stage, qualification, score").ilike("canton", f"%{query}%").order("score", desc=True).limit(15).execute().data

    if not results:
        await update.message.reply_text(f"❌ Aucun résultat pour '{query}'")
        return

    lines = [f"🔍 **{len(results)} résultat(s) pour '{query}'** :\n"]
    for lead in results:
        icon = "🔥" if lead.get("qualification") == "Tres chaud" else "🟠" if lead.get("qualification") == "Chaud" else "⚪"
        stage_icon = {"Lead": "🔵", "1er Contact": "📤", "Reponse": "💬", "Client signe": "🏆"}.get(lead.get("stage", ""), "•")
        lines.append(
            f"{icon} **{lead['cabinet']}** ({lead['city']})\n"
            f"   {stage_icon} {lead['stage']} — Score: {lead['score']:.0f}\n"
            f"   📧 {lead.get('email') or '—'}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Feature 5: Reminders from Telegram
# ---------------------------------------------------------------------------

async def rappel_handler(update, context):
    """Handle /rappel command to create a reminder.
    Usage: /rappel <cabinet> <délai> <note>
    Examples:
        /rappel Studio 11 demain
        /rappel DynaMed 3j relancer par email
        /rappel Physio Kinea 1h vérifier email
    """
    if not context.args:
        await update.message.reply_text(
            "⏰ **Usage :** `/rappel <cabinet> <délai> [note]`\n\n"
            "**Délais possibles :**\n"
            "• `1h` — dans 1 heure\n"
            "• `3h` — dans 3 heures\n"
            "• `demain` — demain matin 9h\n"
            "• `3j` — dans 3 jours\n"
            "• `1sem` — dans 1 semaine\n\n"
            "**Exemples :**\n"
            "`/rappel Studio 11 demain`\n"
            "`/rappel DynaMed 3j relancer par email`",
            parse_mode="Markdown",
        )
        return

    args_text = " ".join(context.args)

    # Parse delay
    delay = None
    delay_text = ""
    note = ""

    delay_patterns = [
        (r'(\d+)h\b', lambda m: timedelta(hours=int(m.group(1))), lambda m: f"{m.group(1)}h"),
        (r'(\d+)j\b', lambda m: timedelta(days=int(m.group(1))), lambda m: f"{m.group(1)} jour(s)"),
        (r'(\d+)sem\b', lambda m: timedelta(weeks=int(m.group(1))), lambda m: f"{m.group(1)} semaine(s)"),
        (r'\bdemain\b', lambda m: timedelta(days=1), lambda m: "demain"),
        (r'\bauj\b', lambda m: timedelta(hours=2), lambda m: "aujourd'hui"),
    ]

    for pattern, delta_fn, text_fn in delay_patterns:
        match = re.search(pattern, args_text, re.IGNORECASE)
        if match:
            delay = delta_fn(match)
            delay_text = text_fn(match)
            # Cabinet name is before the delay, note is after
            before = args_text[:match.start()].strip()
            after = args_text[match.end():].strip()
            note = after
            cabinet_name = before
            break

    if not delay:
        await update.message.reply_text("❌ Délai non reconnu. Utilise : `1h`, `3j`, `demain`, `1sem`", parse_mode="Markdown")
        return

    # Find lead
    lead = find_lead_by_name(cabinet_name)
    if not lead:
        await update.message.reply_text(f"❌ Cabinet '{cabinet_name}' non trouvé.")
        return

    # Create task in Supabase
    due_date = (datetime.now() + delay).strftime("%Y-%m-%d")
    due_time = (datetime.now() + delay).strftime("%H:%M")
    description = note or f"Rappel programmé via Telegram"

    sb = get_supabase()
    sb.table("tasks").insert({
        "lead_id": lead["id"],
        "task_type": "rappel",
        "channel": "Telegram",
        "description": description,
        "due_date": due_date,
    }).execute()

    # Schedule Telegram reminder
    chat_id = update.effective_chat.id
    reminder_text = (
        f"⏰ **Rappel : {lead['cabinet']}**\n"
        f"📍 {lead['city']}\n"
        f"📧 {lead.get('email') or '—'}\n"
        f"📞 {lead.get('phone') or '—'}\n"
        f"📝 {description}\n"
        f"📊 Stage: {lead['stage']}"
    )

    context.application.job_queue.run_once(
        lambda ctx: ctx.bot.send_message(chat_id=chat_id, text=reminder_text, parse_mode="Markdown"),
        when=delay,
        data={"chat_id": chat_id},
        name=f"rappel_{lead['id']}_{due_date}",
    )

    await update.message.reply_text(
        f"✅ **Rappel créé !**\n\n"
        f"📋 {lead['cabinet']} ({lead['city']})\n"
        f"⏰ Dans {delay_text} ({due_date} ~{due_time})\n"
        f"📝 {description}",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN manquant dans crm/.env")
        print("\nSetup :")
        print("1. Ouvre Telegram → cherche @BotFather")
        print("2. /newbot → nomme-le 'Docti CRM Bot'")
        print("3. Copie le token dans crm/.env :")
        print("   TELEGRAM_BOT_TOKEN=<ton_token>")
        return

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("❌ SUPABASE_URL et SUPABASE_SERVICE_KEY manquants dans crm/.env")
        return

    if not OPENAI_API_KEY:
        print("⚠️  OPENAI_API_KEY manquant — les vocaux ne fonctionneront pas")

    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY manquant — le parsing intelligent ne fonctionnera pas")

    from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", start_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CommandHandler("relances", relances_handler))
    app.add_handler(CommandHandler("hot", hot_handler))
    app.add_handler(CommandHandler("lead", lead_handler))
    app.add_handler(CommandHandler("modifier", modifier_handler))
    app.add_handler(CommandHandler("email", email_handler))
    app.add_handler(CommandHandler("email_confirm", email_confirm_handler))
    app.add_handler(CommandHandler("search", search_handler))
    app.add_handler(CommandHandler("rappel", rappel_handler))
    app.add_handler(CommandHandler("resume", resume_handler))

    # Voice handler
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_handler))

    # Text handler (non-command)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    log.info("🤖 Docti CRM Bot démarré ! En attente de messages...")

    # Get chat_id for scheduled jobs (from env or first /start)
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if chat_id and app.job_queue:
        # Daily summary at 8:00 AM
        from datetime import time as dt_time
        app.job_queue.run_daily(
            daily_summary,
            time=dt_time(hour=8, minute=0),
            data={"chat_id": chat_id},
            name="daily_summary",
        )
        # Notifications check every 4 hours
        app.job_queue.run_repeating(
            check_notifications,
            interval=4 * 3600,
            first=60,
            data={"chat_id": chat_id},
            name="notifications",
        )
        log.info("📅 Jobs programmés : résumé quotidien 8h + notifications toutes les 4h")
    else:
        log.warning("⚠️ TELEGRAM_CHAT_ID non configuré — pas de notifications automatiques")
        log.info("   Envoie /start au bot, puis ajoute TELEGRAM_CHAT_ID dans les variables Koyeb")

    app.run_polling()


if __name__ == "__main__":
    main()
