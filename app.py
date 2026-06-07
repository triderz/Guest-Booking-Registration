"""
Guest Registration Automation
------------------------------
Receives Hospitable webhooks when guests send messages.
Extracts names, contact info, and ID images.
Sends a formatted email to your building's front desk automatically.
"""
 
import os
import json
import sqlite3
import requests
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
 
from flask import Flask, request, jsonify
import anthropic
 
app = Flask(__name__)
 
# ─────────────────────────────────────────────
# Configuration  (set these as environment variables — see instructions)
# ─────────────────────────────────────────────
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
EMAIL_USER     = os.environ.get("EMAIL_USER")      # your full email address (e.g. you@yourdomain.com)
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")  # your email account password
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtpout.secureserver.net")  # GoDaddy default
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "465"))
# Note: unit number is read automatically from each booking — no env var needed
BUILDING_EMAIL = os.environ.get("BUILDING_EMAIL")  # front desk email
DB_PATH        = os.environ.get("DB_PATH", "reservations.db")
# Unit number is read automatically from each booking — works across all your units
 
claude = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
 
# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            reservation_id   TEXT PRIMARY KEY,
            guest_names      TEXT,
            contact_number   TEXT,
            contact_email    TEXT,
            checkin_date     TEXT,
            checkout_date    TEXT,
            unit_number      TEXT,
            id_image_urls    TEXT,
            email_sent       INTEGER DEFAULT 0,
            raw_messages     TEXT,
            updated_at       TEXT
        )
    """)
    # Store raw webhook payloads for debugging
    c.execute("""
        CREATE TABLE IF NOT EXISTS webhook_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT,
            payload    TEXT
        )
    """)
    conn.commit()
    conn.close()
 
 
# ─────────────────────────────────────────────
# AI: extract guest info from accumulated messages
# ─────────────────────────────────────────────
def extract_guest_info(all_messages_text, reservation_context):
    prompt = f"""You are analyzing messages sent by a short-term rental guest to extract their registration information.
 
Reservation context (from booking system):
- Guest name on booking: {reservation_context.get('guest_name', 'Unknown')}
- Check-in date:  {reservation_context.get('checkin', 'Unknown')}
- Check-out date: {reservation_context.get('checkout', 'Unknown')}
- Unit/Property:  {reservation_context.get('unit', 'Unknown')}
 
All messages received from this guest so far:
{all_messages_text}
 
Extract whatever registration information is present. Return ONLY a JSON object with these fields (use null if not found):
 
{{
  "guest_names":     ["Full Name 1", "Full Name 2"],
  "contact_number":  "phone number as string",
  "contact_email":   "email address",
  "checkin_date":    "YYYY-MM-DD or null",
  "checkout_date":   "YYYY-MM-DD or null",
  "unit_number":     "unit number as string or null"
}}
 
Return ONLY the JSON — no explanation, no markdown."""
 
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}]
    )
 
    try:
        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"[WARN] Could not parse Claude response: {e}")
        return {}
 
 
# ─────────────────────────────────────────────
# Email sender
# ─────────────────────────────────────────────
def send_building_email(info, id_attachments):
    guest_names = info.get("guest_names") or []
    checkin     = info.get("checkin_date", "TBD")
    checkout    = info.get("checkout_date", "TBD")
    unit        = info.get("unit_number") or "N/A"
    phone       = info.get("contact_number", "Not provided")
    email_addr  = info.get("contact_email", "Not provided")
 
    subject = f"Guest Registration – Unit {unit} | Check-in: {checkin} / Check-out: {checkout}"
 
    # Numbered guest name list
    names_list = "\n".join(f"   {i+1}. {name}" for i, name in enumerate(guest_names)) \
                 if guest_names else "   1. (not provided)"
 
    body = f"""Dear Front Desk,
 
Please find the guest registration information below for the upcoming reservation.
 
1. Unit: {unit}
2. Check-in Date: {checkin}
3. Check-out Date: {checkout}
4. Guest Names:
{names_list}
5. Contact Number: {phone}
6. Contact Email: {email_addr}
7. Government ID: Attached for all guests 18 and over
 
Thank you,
Property Management
"""
 
    msg = MIMEMultipart()
    msg["From"]    = EMAIL_USER
    msg["To"]      = BUILDING_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
 
    # Attach ID images
    for filename, file_bytes in id_attachments:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(file_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)
 
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
 
    print(f"[✅] Email sent to {BUILDING_EMAIL}")
 
 
# ─────────────────────────────────────────────
# Webhook endpoint
# ─────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def handle_webhook():
    raw = request.get_json(force=True, silent=True)
    if not raw:
        return jsonify({"error": "Empty payload"}), 400
 
    # Log every webhook for debugging
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO webhook_log (received_at, payload) VALUES (?, ?)",
              (datetime.now().isoformat(), json.dumps(raw)))
    conn.commit()
 
    action = raw.get("action", "")
    if action != "message.created":
        conn.close()
        return jsonify({"status": f"ignored action: {action}"}), 200
 
    # ── Parse Hospitable webhook structure ──
    data        = raw.get("data", raw)          # v2 wraps in "data", v1 may not
    message     = data.get("message", {})
    reservation = data.get("reservation", {})
 
    # Only handle inbound guest messages
    direction = message.get("direction", message.get("type", "")).lower()
    if direction in ("outgoing", "host", "outbound"):
        conn.close()
        return jsonify({"status": "ignored - host message"}), 200
 
    reservation_id = (reservation.get("id") or reservation.get("reservation_id") or "")
    if not reservation_id:
        conn.close()
        return jsonify({"error": "No reservation ID found"}), 400
 
    # Reservation context from booking data
    guest_obj = reservation.get("guest", {})
    prop_obj  = reservation.get("property", reservation.get("listing", {}))
    # Try multiple field names Hospitable may use for the unit/property name
    unit_from_booking = (
        prop_obj.get("unit_number") or
        prop_obj.get("internal_name") or
        prop_obj.get("name") or
        reservation.get("unit_number") or
        ""
    )
    context = {
        "guest_name": guest_obj.get("name", ""),
        "checkin":    reservation.get("check_in", reservation.get("checkin", "")),
        "checkout":   reservation.get("check_out", reservation.get("checkout", "")),
        "unit":       unit_from_booking,
    }
 
    # Message content + attachments
    message_text  = message.get("content", message.get("body", message.get("text", "")))
    attachments   = message.get("attachments", message.get("files", []))
 
    # ── Load existing record ──
    c.execute("SELECT email_sent, raw_messages, id_image_urls FROM reservations WHERE reservation_id = ?",
              (reservation_id,))
    row = c.fetchone()
    if row and row[0]:  # email already sent
        conn.close()
        return jsonify({"status": "already sent"}), 200
 
    existing_messages  = json.loads(row[1]) if row and row[1] else []
    existing_image_urls = json.loads(row[2]) if row and row[2] else []
 
    # Add this message
    existing_messages.append({
        "content":     message_text,
        "attachments": attachments,
        "timestamp":   datetime.now().isoformat(),
    })
 
    # Collect image URLs across all messages
    for att in attachments:
        url       = att.get("url", att.get("download_url", ""))
        mime_type = att.get("type", att.get("mime_type", att.get("content_type", "")))
        if url and url not in existing_image_urls:
            is_image = "image" in mime_type.lower() if mime_type else (
                any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".pdf", ".heic"))
            )
            if is_image:
                existing_image_urls.append(url)
 
    # ── Extract info with AI ──
    all_text = "\n\n---\n\n".join(
        m["content"] for m in existing_messages if m.get("content")
    )
    extracted = extract_guest_info(all_text, context)
 
    # Fall back to booking-level data for dates / unit if AI didn't find them in messages
    if not extracted.get("checkin_date")  and context.get("checkin"):
        extracted["checkin_date"]  = context["checkin"]
    if not extracted.get("checkout_date") and context.get("checkout"):
        extracted["checkout_date"] = context["checkout"]
    if not extracted.get("unit_number") and context.get("unit"):
        extracted["unit_number"] = context["unit"]
    if not extracted.get("guest_names")   and context.get("guest_name"):
        extracted["guest_names"]   = [context["guest_name"]]
 
    # ── Completeness check ──
    has_names  = bool(extracted.get("guest_names"))
    has_phone  = bool(extracted.get("contact_number"))
    has_email  = bool(extracted.get("contact_email"))
    has_dates  = bool(extracted.get("checkin_date") and extracted.get("checkout_date"))
    has_unit   = bool(extracted.get("unit_number"))
    has_ids    = len(existing_image_urls) > 0
 
    all_complete = has_names and has_phone and has_email and has_dates and has_unit and has_ids
 
    # ── Save / update DB ──
    c.execute("""
        INSERT OR REPLACE INTO reservations
        (reservation_id, guest_names, contact_number, contact_email,
         checkin_date, checkout_date, unit_number, id_image_urls, email_sent, raw_messages, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
    """, (
        reservation_id,
        json.dumps(extracted.get("guest_names", [])),
        extracted.get("contact_number", ""),
        extracted.get("contact_email", ""),
        extracted.get("checkin_date", ""),
        extracted.get("checkout_date", ""),
        extracted.get("unit_number", ""),
        json.dumps(existing_image_urls),
        json.dumps(existing_messages),
        datetime.now().isoformat(),
    ))
    conn.commit()
 
    # ── If complete, download IDs and send email ──
    if all_complete:
        id_attachments = []
        for i, url in enumerate(existing_image_urls):
            try:
                resp = requests.get(url, timeout=30)
                if resp.status_code == 200:
                    ct  = resp.headers.get("Content-Type", "")
                    ext = "pdf" if "pdf" in ct else "heic" if "heic" in ct else "jpg"
                    id_attachments.append((f"guest_id_{i + 1}.{ext}", resp.content))
            except Exception as e:
                print(f"[WARN] Could not download ID image {url}: {e}")
 
        try:
            send_building_email(extracted, id_attachments)
            c.execute("UPDATE reservations SET email_sent = 1 WHERE reservation_id = ?", (reservation_id,))
            conn.commit()
        except Exception as e:
            print(f"[ERROR] Email failed: {e}")
            conn.close()
            return jsonify({"error": f"Email failed: {e}"}), 500
    else:
        missing = []
        if not has_names:  missing.append("guest names")
        if not has_phone:  missing.append("contact number")
        if not has_email:  missing.append("contact email")
        if not has_dates:  missing.append("check-in/out dates")
        if not has_ids:    missing.append("ID photo(s)")
        print(f"[⏳] Reservation {reservation_id}: still waiting for {', '.join(missing)}")
 
    conn.close()
    return jsonify({"status": "ok", "email_sent": all_complete}), 200
 
 
# ─────────────────────────────────────────────
# Utility endpoints
# ─────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running", "time": datetime.now().isoformat()}), 200
 
 
@app.route("/debug/webhooks", methods=["GET"])
def debug_webhooks():
    """Show the last 10 webhook payloads received — useful for troubleshooting."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT received_at, payload FROM webhook_log ORDER BY id DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    return jsonify([{"received_at": r[0], "payload": json.loads(r[1])} for r in rows]), 200
 
 
@app.route("/debug/reservations", methods=["GET"])
def debug_reservations():
    """Show all tracked reservations and their current status."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT reservation_id, guest_names, contact_number, contact_email,
               checkin_date, checkout_date, unit_number, id_image_urls, email_sent, updated_at
        FROM reservations ORDER BY updated_at DESC
    """)
    rows = c.fetchall()
    conn.close()
    keys = ["reservation_id", "guest_names", "contact_number", "contact_email",
            "checkin_date", "checkout_date", "unit_number", "id_image_urls", "email_sent", "updated_at"]
    return jsonify([dict(zip(keys, r)) for r in rows]), 200
 
 
# ─────────────────────────────────────────────
# Start
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    print(f"[🚀] Server starting on port {port}")
    app.run(host="0.0.0.0", port=port)
 
