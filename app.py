# app.py
import os
import sqlite3
import logging
from flask import Flask, request, jsonify
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatInviteLink
from telegram.utils.request import Request
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAIN_CHAT_ID = int(os.environ.get("MAIN_CHAT_ID"))  # de groep chat_id (negatief int)
PRIVATE_GROUP_LINK = os.environ.get("PRIVATE_GROUP_LINK")  # b.v. https://t.me/+GBef4zESkcdmODdk
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")  # optioneel: simpel secret pad

if not BOT_TOKEN or not MAIN_CHAT_ID or not PRIVATE_GROUP_LINK:
    logger.error("Environment variables BOT_TOKEN, MAIN_CHAT_ID en PRIVATE_GROUP_LINK zijn vereist.")
    raise SystemExit("BOT_TOKEN, MAIN_CHAT_ID and PRIVATE_GROUP_LINK required")

req = Request(con_pool_size=8)
bot = Bot(token=BOT_TOKEN, request=req)

app = Flask(__name__)
DB_PATH = os.environ.get("DATABASE_PATH", "invites.db")


# --- DB helpers ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS invites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inviter_id INTEGER NOT NULL,
        inviter_username TEXT,
        invite_link TEXT NOT NULL UNIQUE,
        uses INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def db_execute(query, params=(), fetch=False, one=False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    if fetch:
        rows = cur.fetchall()
        conn.commit()
        conn.close()
        return rows
    else:
        conn.commit()
        lastrow = cur.lastrowid
        conn.close()
        if one:
            return lastrow
        return None


def set_meta(k, v):
    db_execute("REPLACE INTO meta (k,v) VALUES (?,?)", (k, v))


def get_meta(k):
    rows = db_execute("SELECT v FROM meta WHERE k=?", (k,), fetch=True)
    return rows[0][0] if rows else None


# --- Invite management ---
def create_personal_invite(inviter_id, inviter_username):
    """
    Maak een invite link voor MAIN_CHAT_ID met member_limit=2 en een herkenbare name.
    Retourneer de invite_link string.
    """
    name = f"share_for_{inviter_id}_{int(datetime.utcnow().timestamp())}"
    # Maak invite link via Bot API (bot moet admin in group)
    logger.info("Creating invite link for %s (%s)", inviter_id, inviter_username)
    try:
        chat_invite: ChatInviteLink = bot.create_chat_invite_link(
            chat_id=MAIN_CHAT_ID,
            name=name,
            member_limit=2
        )
    except Exception as e:
        logger.exception("Failed to create invite link: %s", e)
        raise

    invite_url = chat_invite.invite_link
    db_execute(
        "INSERT OR IGNORE INTO invites (inviter_id, inviter_username, invite_link, uses, active, created_at) VALUES (?,?,?,?,?,?)",
        (inviter_id, inviter_username or '', invite_url, 0, 1, datetime.utcnow().isoformat())
    )
    # update group pinned message display
    refresh_pinned_message()
    return invite_url


def increment_invite_usage(invite_url):
    rows = db_execute("SELECT id, uses, inviter_id FROM invites WHERE invite_link=? AND active=1", (invite_url,), fetch=True)
    if not rows:
        logger.info("No tracked invite for url %s", invite_url)
        return None
    id_, uses, inviter_id = rows[0]
    uses += 1
    active = 1
    if uses >= 2:
        active = 0  # reached target -> deactivate
    db_execute("UPDATE invites SET uses=?, active=? WHERE id=?", (uses, active, id_))
    refresh_pinned_message()
    return {"id": id_, "inviter_id": inviter_id, "uses": uses, "active": active}


def get_all_invites():
    return db_execute("SELECT inviter_id, inviter_username, invite_link, uses, active FROM invites ORDER BY created_at DESC", fetch=True)


# --- UI / message formatting ---
PINNED_MESSAGE_TEXT = (
    "Please share this group to 2 other to unlock the button to get access to our request group:\n\n"
    "Klik op de knop hieronder om jouw persoonlijke invite link te genereren — deel die met 2 vrienden."
)


def build_pinned_keyboard():
    """
    Bouw inline keyboard die per uitnodiger hun voortgang toont.
    De eerste knop is de algemene 'Genereer / Share' knop (callback).
    Daarna tonen we per actieve/inactieve uitnodiger hun label en (indien 2/2) de Open de instructions knop.
    """
    rows = []
    # primary general button
    rows.append([InlineKeyboardButton("Share to unlock Instructions (get your link)", callback_data="generate_link")])

    invites = get_all_invites()
    # For each inviter show a non-actionable button with progress (callback opens PM)
    for inviter_id, inviter_username, invite_link, uses, active in invites:
        label_name = inviter_username if inviter_username else f"user_{inviter_id}"
        # show progress and allow people to click to message inviter privately via bot (callback includes inviter id)
        rows.append([InlineKeyboardButton(f"{label_name}: ({uses}/2)", callback_data=f"status:{inviter_id}")])
        if uses >= 2:
            # add open instructions button for that inviter; it opens the private group link
            rows.append([InlineKeyboardButton(f"Open de instructions for {label_name}", url=PRIVATE_GROUP_LINK)])
    return InlineKeyboardMarkup(rows)


def refresh_pinned_message():
    """
    Zorg dat er altijd een vast bericht onderaan staat (pinned). We bewaren pinned_message_id in meta DB.
    Als bericht niet bestaat of is verwijderd -> maak nieuw en pin het.
    Werk de inline keyboard bij met actuele status.
    """
    pinned_id = get_meta("pinned_message_id")
    keyboard = build_pinned_keyboard()
    try:
        if pinned_id:
            try:
                bot.edit_message_text(
                    chat_id=MAIN_CHAT_ID,
                    message_id=int(pinned_id),
                    text=PINNED_MESSAGE_TEXT,
                    reply_markup=keyboard,
                    parse_mode=None
                )
                logger.info("Updated pinned message (id=%s).", pinned_id)
                return
            except Exception as e:
                # mogelijk verwijderd of te oud -> maak nieuw
                logger.warning("Could not edit pinned message (%s): %s. Will (re)create.", pinned_id, e)

        # create new message and pin it
        msg = bot.send_message(chat_id=MAIN_CHAT_ID, text=PINNED_MESSAGE_TEXT, reply_markup=keyboard)
        bot.pin_chat_message(chat_id=MAIN_CHAT_ID, message_id=msg.message_id, disable_notification=True)
        set_meta("pinned_message_id", str(msg.message_id))
        logger.info("Created & pinned new message id=%s", msg.message_id)
    except Exception as e:
        logger.exception("Failed to refresh pinned message: %s", e)


# --- Webhook and update processing ---
@app.route(f"/{WEBHOOK_SECRET}/webhook", methods=["POST"])
def webhook():
    """Main webhook endpoint to receive Telegram updates."""
    try:
        update = Update.de_json(request.get_json(force=True), bot)
    except Exception as e:
        logger.exception("Invalid update: %s", e)
        return jsonify(ok=False)

    # handle callback_query
    if update.callback_query:
        cq = update.callback_query
        user = cq.from_user
        data = cq.data or ""
        logger.info("Callback by %s: %s", user.id, data)

        if data == "generate_link":
            # create personal invite link and send to user in private
            try:
                invite = create_personal_invite(user.id, user.username or user.full_name)
                text = (
                    "Je persoonlijke invite link is aangemaakt — deel deze met 2 personen.\n\n"
                    f"{invite}\n\n"
                    "Wanneer 2 personen via deze link joinen zal de knop 'Open de instructions' voor jou verschijnen."
                )
                bot.send_message(chat_id=user.id, text=text)
                cq.answer(text="Ik heb je persoonlijke link gestuurd in privé!", show_alert=False)
            except Exception as e:
                logger.exception("Error creating invite for %s", user.id)
                cq.answer(text="Fout bij aanmaken link. Zorg dat de bot admin is in de groep.", show_alert=True)
            return jsonify(ok=True)

        if data.startswith("status:"):
            # show inviter status in an alert
            _, inviter_id_s = data.split(":", 1)
            rows = db_execute("SELECT inviter_username, uses FROM invites WHERE inviter_id=? ORDER BY created_at DESC LIMIT 1", (int(inviter_id_s),), fetch=True)
            if rows:
                uname, uses = rows[0]
                msg = f"{uname or 'user'} has {uses}/2 referred."
            else:
                msg = "Geen data over deze gebruiker."
            cq.answer(text=msg, show_alert=True)
            return jsonify(ok=True)

        cq.answer()  # generic
        return jsonify(ok=True)

    # handle messages with new_chat_members (someone joined)
    if update.message:
        msg = update.message
        # Telegram includes invite link info when user joins via a invite link:
        # message.new_chat_members + message.invite_link
        if msg.new_chat_members:
            # check if invite_link present in message (may be None if join via join via username)
            invite_link_obj = getattr(msg, "invite_link", None)
            if invite_link_obj:
                # invite_link_obj has .invite_link string
                invite_url = getattr(invite_link_obj, "invite_link", None) or getattr(invite_link_obj, "url", None) or str(invite_link_obj)
                logger.info("New members joined via invite link: %s", invite_url)
                r = increment_invite_usage(invite_url)
                if r:
                    # optionally message the inviter in private about progress
                    rows = db_execute("SELECT inviter_username, uses FROM invites WHERE invite_link=?", (invite_url,), fetch=True)
                    if rows:
                        inviter_username, uses = rows[0]
                        # notify inviter privately (if bot can)
                        try:
                            # find inviter id
                            inviter_id_rows = db_execute("SELECT inviter_id FROM invites WHERE invite_link=?", (invite_url,), fetch=True)
                            if inviter_id_rows:
                                inviter_id = inviter_id_rows[0][0]
                                bot.send_message(chat_id=inviter_id, text=f"Je invite heeft nu {uses}/2 succesvolle joins.")
                                if uses >= 2:
                                    bot.send_message(chat_id=inviter_id, text=f"Gefeliciteerd — je hebt 2 leden uitgenodigd. Open de instructions: {PRIVATE_GROUP_LINK}")
                        except Exception:
                            logger.debug("Could not notify inviter privately (maybe blocked bot).")
                else:
                    logger.debug("Invite link not tracked by DB: %s", invite_url)

    return jsonify(ok=True)


# admin route to force refresh pinned message (optional)
@app.route("/refresh", methods=["POST"])
def refresh_endpoint():
    secret = request.args.get("secret", "")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return "forbidden", 403
    try:
        refresh_pinned_message()
        return "ok"
    except Exception as e:
        logger.exception("refresh failed")
        return "error", 500


if __name__ == "__main__":
    init_db()
    # on startup ensure pinned message exists
    try:
        refresh_pinned_message()
    except Exception as e:
        logger.warning("Could not create pinned message on startup: %s", e)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
