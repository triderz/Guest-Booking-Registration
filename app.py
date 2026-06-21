"""
Guest Registration Automation
------------------------------
Receives Hospitable webhooks when guests send messages.
Extracts names, contact info, and ID images.
Sends a formatted email to your building's front desk automatically.
"""

import os
import re
import json
import base64
import sqlite3
import requests
import traceback
from datetime import datetime, date

from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

# Stores the last unexpected error so it can be viewed at /debug/last-error
LAST_ERROR = None

# ─────────────────────────────────────────────
# Configuration  (set these as environment variables — see instructions)
# ─────────────────────────────────────────────
CLAUDE_API_KEY  = os.environ.get("CLAUDE_API_KEY")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")  # from sendgrid.com
EMAIL_FROM       = os.environ.get("EMAIL_USER", "info@atozvacationhomes.com")  # verified sender
# Comma-separated list of keywords. A property is "in the building" if its
# Hospitable property name CONTAINS any of these keywords (case-insensitive).
# Example: "Crosby" will match "Crosby 3201 Gian Top Floor Penthouse",
# "Crosby 1205 ...", etc. Leave blank to apply to ALL properties.
ELIGIBLE_UNITS_RAW = os.environ.get("ELIGIBLE_UNITS", "")
ELIGIBLE_UNITS = [u.strip().lower() for u in ELIGIBLE_UNITS_RAW.split(",") if u.strip()]
BUILDING_EMAIL = os.environ.get("BUILDING_EMAIL")  # fallback front desk email
CC_EMAILS_RAW  = os.environ.get("CC_EMAILS", "")   # comma-separated list of CC addresses
CC_EMAILS      = [e.strip() for e in CC_EMAILS_RAW.split(",") if e.strip()]

# Multi-building routing: maps property name keywords to specific front desk emails.
# Format: "keyword1:email1;keyword2:email2"
# Example: "crosby:frontdesk@thecrosbymiami.com;district 225:frontdesk@district225miami.com"
# The property name is checked (case-insensitive) for each keyword in order.
# Falls back to BUILDING_EMAIL if no keyword matches.
_BUILDING_ROUTING_RAW = os.environ.get("BUILDING_ROUTING", "")
BUILDING_ROUTING = []  # list of (keyword_lower, email) tuples
for _entry in _BUILDING_ROUTING_RAW.split(";"):
    _entry = _entry.strip()
    if ":" in _entry:
        _kw, _em = _entry.split(":", 1)
        if _kw.strip() and _em.strip():
            BUILDING_ROUTING.append((_kw.strip().lower(), _em.strip()))


def resolve_building_email(property_name):
    """Return the front desk email for the given property name, using BUILDING_ROUTING
    keyword matching. Falls back to BUILDING_EMAIL if nothing matches."""
    name_lower = (property_name or "").lower()
    for keyword, email in BUILDING_ROUTING:
        if keyword in name_lower:
            return email
    return BUILDING_EMAIL
DB_PATH        = os.environ.get("DB_PATH", "reservations.db")
# Unit number is read automatically from each booking — works across all your units

# Hospitable Public API (used to look up check-in/check-out dates and guest name,
# since the message webhook itself doesn't include them)
HOSPITABLE_API_TOKEN = os.environ.get("HOSPITABLE_API_TOKEN")
HOSPITABLE_API_BASE  = os.environ.get("HOSPITABLE_API_BASE", "https://public.api.hospitable.com/v2")

# Lazily created so a missing CLAUDE_API_KEY doesn't crash the whole app at startup
_claude_client = None
def get_claude():
    global _claude_client
    if _claude_client is None:
        if not CLAUDE_API_KEY:
            raise RuntimeError("CLAUDE_API_KEY environment variable is not set")
        _claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    return _claude_client


# ─────────────────────────────────────────────
# Helpers: property/unit matching + Hospitable API lookups
# ─────────────────────────────────────────────
def extract_unit_label(property_name):
    """
    Pull a short unit label (e.g. 'Unit 801') out of a full property name
    like 'District 225 Unit 801 Cecil'. Falls back to the full property
    name if no 'Unit ___' pattern is found.
    """
    if not property_name:
        return ""
    match = re.search(r'unit\s*#?\s*([\w-]+)', property_name, re.IGNORECASE)
    if match:
        return f"Unit {match.group(1)}"
    return property_name


def get_reservation_details(reservation_id):
    """
    Look up check-in/check-out dates and the guest's name from the
    Hospitable Public API. Returns {} on any failure so the rest of the
    app can keep working (it'll just rely on info collected from messages).
    """
    if not HOSPITABLE_API_TOKEN or not reservation_id:
        return {}
    try:
        url = f"{HOSPITABLE_API_BASE}/reservations/{reservation_id}"
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {HOSPITABLE_API_TOKEN}",
                "Accept": "application/json",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[WARN] Hospitable API returned {resp.status_code} for reservation {reservation_id}")
            return {}

        payload = resp.json()
        res = payload.get("data", payload)

        guest_name = (
            (res.get("guest") or {}).get("name")
            or res.get("guest_name")
            or ""
        )
        checkin = (
            res.get("check_in")
            or res.get("checkin")
            or res.get("arrival_date")
            or ""
        )
        checkout = (
            res.get("check_out")
            or res.get("checkout")
            or res.get("departure_date")
            or ""
        )
        # Dates sometimes come back as full timestamps — keep just the date part
        if checkin and "T" in checkin:
            checkin = checkin.split("T")[0]
        if checkout and "T" in checkout:
            checkout = checkout.split("T")[0]

        return {"guest_name": guest_name, "checkin": checkin, "checkout": checkout}
    except Exception as e:
        print(f"[WARN] Could not fetch reservation details for {reservation_id}: {e}")
        return {}


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
            id_image_data    TEXT DEFAULT '[]',
            email_sent       INTEGER DEFAULT 0,
            raw_messages     TEXT,
            updated_at       TEXT
        )
    """)
    # Migration: add id_image_data column to existing DBs that predate this column
    try:
        c.execute("ALTER TABLE reservations ADD COLUMN id_image_data TEXT DEFAULT '[]'")
        conn.commit()
    except Exception:
        pass  # Column already exists
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

    try:
        response = get_claude().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"[WARN] Could not get/parse Claude response: {e}")
        return {}


# ─────────────────────────────────────────────
# Email sender (SendGrid HTTP API — no SMTP ports needed)
# ─────────────────────────────────────────────
def send_building_email(info, id_attachments, to_email=None):
    """Send registration email to the building front desk via SendGrid.

    to_email overrides BUILDING_EMAIL (used for per-building routing).
    """
    recipient = to_email or BUILDING_EMAIL
    guest_names = info.get("guest_names") or []

    def fmt_date(d):
        """Convert YYYY-MM-DD to MM/DD/YYYY for the email."""
        try:
            return date.fromisoformat(d).strftime("%m/%d/%Y")
        except Exception:
            return d or "TBD"

    checkin  = fmt_date(info.get("checkin_date", ""))
    checkout = fmt_date(info.get("checkout_date", ""))
    unit        = info.get("unit_number") or "N/A"
    phone       = info.get("contact_number", "Not provided")
    email_addr  = info.get("contact_email", "Not provided")

    subject = f"Guest Registration – {unit} | Check-in: {checkin} / Check-out: {checkout}"

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

    # Build SendGrid API payload
    to_list = [{"email": recipient}]
    cc_list = [{"email": e} for e in CC_EMAILS] if CC_EMAILS else []

    personalizations = [{"to": to_list, "subject": subject}]
    if cc_list:
        personalizations[0]["cc"] = cc_list

    payload = {
        "personalizations": personalizations,
        "from": {"email": EMAIL_FROM, "name": "A to Z Vacation Homes"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }

    # Attach ID images
    if id_attachments:
        payload["attachments"] = []
        for filename, file_bytes in id_attachments:
            payload["attachments"].append({
                "content":     base64.b64encode(file_bytes).decode(),
                "filename":    filename,
                "type":        "application/octet-stream",
                "disposition": "attachment",
            })

    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=30,
    )

    if resp.status_code not in (200, 202):
        raise Exception(f"SendGrid error {resp.status_code}: {resp.text}")

    print(f"[✅] Email sent via SendGrid to {recipient}")


# ─────────────────────────────────────────────
# Webhook endpoint
# ─────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def handle_webhook():
    global LAST_ERROR
    raw = request.get_json(force=True, silent=True)
    if not raw:
        return jsonify({"error": "Empty payload"}), 400

    # Log every webhook for debugging
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO webhook_log (received_at, payload) VALUES (?, ?)",
              (datetime.now().isoformat(), json.dumps(raw)))
    conn.commit()

    try:
        action = raw.get("action", "")
        if action and action != "message.created":
            conn.close()
            return jsonify({"status": f"ignored action: {action}"}), 200

        # ── Parse Hospitable v2 message.created webhook structure ──
        # The real payload looks like:
        # {
        #   "action": "message.created",
        #   "data": {
        #     "id": 123, "body": "...", "attachments": [...],
        #     "reservation_id": "...", "property": {"name": "...", "public_name": "..."},
        #     "sender_role": "guest" | "host", "sender_type": "guest" | "host", ...
        #   }
        # }
        data = raw.get("data", raw)

        # Only handle inbound guest messages — skip anything sent by the host/team
        sender_role = (data.get("sender_role") or data.get("sender_type") or "").lower()
        if sender_role in ("host", "team", "assistant", "owner"):
            conn.close()
            return jsonify({"status": "ignored - host message"}), 200

        reservation_id = data.get("reservation_id") or data.get("reservation", {}).get("id") or ""
        if not reservation_id:
            conn.close()
            return jsonify({"error": "No reservation ID found"}), 400

        # Property / unit info comes directly on the message payload
        prop_obj = data.get("property") or {}
        unit_from_booking = (
            prop_obj.get("name") or
            prop_obj.get("public_name") or
            prop_obj.get("internal_name") or
            ""
        )

        # If ELIGIBLE_UNITS is set, skip properties whose name doesn't contain any of the listed keywords
        property_name_lower = unit_from_booking.lower()
        if ELIGIBLE_UNITS and not any(keyword in property_name_lower for keyword in ELIGIBLE_UNITS):
            print(f"[⏭] Skipping property '{unit_from_booking}' — not in ELIGIBLE_UNITS list")
            conn.close()
            return jsonify({"status": f"ignored - property '{unit_from_booking}' not eligible"}), 200

        # Resolve the correct front desk email for this property
        building_email_to = resolve_building_email(unit_from_booking)

        # Check-in/check-out dates and the guest's name aren't included in the
        # message webhook itself, so fetch them from the Hospitable API.
        res_details = get_reservation_details(reservation_id)

        context = {
            "guest_name": res_details.get("guest_name", ""),
            "checkin":    res_details.get("checkin", ""),
            "checkout":   res_details.get("checkout", ""),
            "unit":       extract_unit_label(unit_from_booking) or unit_from_booking,
        }

        # Message content + attachments
        message_text = data.get("body", "")
        attachments  = data.get("attachments") or []

        # ── Load existing record ──
        c.execute("SELECT email_sent, raw_messages, id_image_urls, id_image_data FROM reservations WHERE reservation_id = ?",
                  (reservation_id,))
        row = c.fetchone()
        if row and row[0]:  # email already sent
            conn.close()
            return jsonify({"status": "already sent"}), 200

        existing_messages   = json.loads(row[1]) if row and row[1] else []
        existing_image_urls = json.loads(row[2]) if row and row[2] else []
        try:
            existing_image_data = json.loads(row[3]) if row and row[3] else []
        except Exception:
            existing_image_data = []

        # Add this message
        existing_messages.append({
            "content":     message_text,
            "attachments": attachments,
            "timestamp":   datetime.now().isoformat(),
        })

        # Collect new image URLs and download bytes immediately (Airbnb URLs expire in ~1 hour)
        for att in attachments:
            url       = att.get("url", att.get("download_url", ""))
            mime_type = att.get("type", att.get("mime_type", att.get("content_type", "")))
            if url and url not in existing_image_urls:
                is_image = "image" in mime_type.lower() if mime_type else (
                    any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".pdf", ".heic"))
                )
                if is_image:
                    existing_image_urls.append(url)
                    # Download immediately before the signed URL expires
                    try:
                        img_resp = requests.get(url, timeout=30)
                        if img_resp.status_code == 200:
                            ct  = img_resp.headers.get("Content-Type", "")
                            ext = "pdf" if "pdf" in ct else "heic" if "heic" in ct else "jpg"
                            idx = len(existing_image_data) + 1
                            existing_image_data.append({
                                "filename": f"guest_id_{idx}.{ext}",
                                "data":     base64.b64encode(img_resp.content).decode(),
                            })
                            print(f"[📎] Downloaded ID image {idx} for {reservation_id}")
                        else:
                            print(f"[WARN] ID image returned HTTP {img_resp.status_code}: {url}")
                    except Exception as e:
                        print(f"[WARN] Could not download ID image: {e}")

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

        # ── Merge with existing DB values — never overwrite a good value with null ──
        # This handles cases where Claude extracts only partial info from the latest message
        # but we already have complete data stored from a previous message.
        if row:
            c.execute("""SELECT guest_names, contact_number, contact_email,
                                checkin_date, checkout_date, unit_number
                         FROM reservations WHERE reservation_id = ?""", (reservation_id,))
            db_row = c.fetchone()
            if db_row:
                db_names, db_phone, db_email, db_checkin, db_checkout, db_unit = db_row
                try:
                    db_names_parsed = json.loads(db_names) if db_names and db_names not in ("null", "[]", "") else []
                except Exception:
                    db_names_parsed = []
                if not extracted.get("guest_names") and db_names_parsed:
                    extracted["guest_names"] = db_names_parsed
                if not extracted.get("contact_number") and db_phone:
                    extracted["contact_number"] = db_phone
                if not extracted.get("contact_email") and db_email:
                    extracted["contact_email"] = db_email
                if not extracted.get("checkin_date") and db_checkin:
                    extracted["checkin_date"] = db_checkin
                if not extracted.get("checkout_date") and db_checkout:
                    extracted["checkout_date"] = db_checkout
                if not extracted.get("unit_number") and db_unit:
                    extracted["unit_number"] = db_unit

        # ── Completeness check ──
        has_names  = bool(extracted.get("guest_names"))
        has_phone  = bool(extracted.get("contact_number"))
        has_email  = bool(extracted.get("contact_email"))
        has_dates  = bool(extracted.get("checkin_date") and extracted.get("checkout_date"))
        has_unit   = bool(extracted.get("unit_number"))

        # ID logic: wait until we have one ID per guest name, OR it's the day before check-in
        guest_names_list  = extracted.get("guest_names") or []
        expected_id_count = len(guest_names_list)
        actual_id_count   = len(existing_image_data)  # use downloaded count, not URL count
        has_some_ids      = actual_id_count > 0
        has_all_ids       = expected_id_count > 0 and actual_id_count >= expected_id_count

        # Check whether today is the day before (or the day of) check-in
        is_day_before_checkin = False
        checkin_str = extracted.get("checkin_date", "")
        if checkin_str:
            try:
                checkin_dt = date.fromisoformat(checkin_str)
                days_until_checkin = (checkin_dt - date.today()).days
                is_day_before_checkin = days_until_checkin <= 1
            except Exception:
                pass

        # Send when: all non-ID fields complete AND (all IDs received OR day-before deadline)
        id_ready   = has_all_ids or (has_some_ids and is_day_before_checkin)
        all_complete = has_names and has_phone and has_email and has_dates and has_unit and id_ready

        # ── Save / update DB ──
        c.execute("""
            INSERT OR REPLACE INTO reservations
            (reservation_id, guest_names, contact_number, contact_email,
             checkin_date, checkout_date, unit_number, id_image_urls, id_image_data, email_sent, raw_messages, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, (
            reservation_id,
            json.dumps(extracted.get("guest_names", [])),
            extracted.get("contact_number", ""),
            extracted.get("contact_email", ""),
            extracted.get("checkin_date", ""),
            extracted.get("checkout_date", ""),
            extracted.get("unit_number", ""),
            json.dumps(existing_image_urls),
            json.dumps(existing_image_data),
            json.dumps(existing_messages),
            datetime.now().isoformat(),
        ))
        conn.commit()

        # ── If complete, send email using pre-downloaded image bytes ──
        if all_complete:
            id_attachments = [
                (img["filename"], base64.b64decode(img["data"]))
                for img in existing_image_data
                if img.get("data")
            ]

            try:
                send_building_email(extracted, id_attachments, to_email=building_email_to)
                c.execute("UPDATE reservations SET email_sent = 1 WHERE reservation_id = ?", (reservation_id,))
                conn.commit()
            except Exception as e:
                LAST_ERROR = traceback.format_exc()
                print(f"[ERROR] Email failed: {e}")
                conn.close()
                return jsonify({"error": f"Email failed: {e}"}), 500
        else:
            missing = []
            if not has_names:    missing.append("guest names")
            if not has_phone:    missing.append("contact number")
            if not has_email:    missing.append("contact email")
            if not has_dates:    missing.append("check-in/out dates")
            if not has_some_ids: missing.append("ID photo(s)")
            elif not has_all_ids:
                missing.append(f"IDs ({actual_id_count}/{expected_id_count} received — will send day before check-in if still incomplete)")
            print(f"[⏳] Reservation {reservation_id}: still waiting for {', '.join(missing)}")

        conn.close()
        return jsonify({"status": "ok", "email_sent": all_complete}), 200

    except Exception as e:
        LAST_ERROR = traceback.format_exc()
        print(f"[ERROR] Webhook processing failed:\n{LAST_ERROR}")
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"error": str(e), "detail": "see /debug/last-error"}), 500


# ─────────────────────────────────────────────
# Utility endpoints
# ─────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "running", "time": datetime.now().isoformat()}), 200


@app.route("/debug/env-check", methods=["GET"])
def debug_env_check():
    """Check what environment variables the running process actually sees."""
    token_live = os.environ.get("HOSPITABLE_API_TOKEN", "")
    sg_key = os.environ.get("SENDGRID_API_KEY", "")
    return jsonify({
        "HOSPITABLE_API_TOKEN_present": "HOSPITABLE_API_TOKEN" in os.environ,
        "HOSPITABLE_API_TOKEN_length": len(token_live),
        "HOSPITABLE_API_TOKEN_first_10_chars": token_live[:10] if token_live else "(empty)",
        "CLAUDE_API_KEY_present": "CLAUDE_API_KEY" in os.environ,
        "SENDGRID_API_KEY_present": bool(sg_key),
        "SENDGRID_API_KEY_first_5": sg_key[:5] if sg_key else "(empty)",
        "EMAIL_FROM": os.environ.get("EMAIL_USER", "(not set)"),
        "BUILDING_EMAIL": os.environ.get("BUILDING_EMAIL", "(not set)"),
        "CC_EMAILS_value": os.environ.get("CC_EMAILS", "(not set)"),
        "ELIGIBLE_UNITS_value": os.environ.get("ELIGIBLE_UNITS", "(not set)"),
    }), 200


@app.route("/debug/last-error", methods=["GET"])
def debug_last_error():
    """Show the most recent unexpected error from /webhook processing, if any."""
    if LAST_ERROR:
        return jsonify({"last_error": LAST_ERROR}), 200
    return jsonify({"last_error": None}), 200


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


@app.route("/debug/test-reservation/<reservation_id>", methods=["GET"])
def debug_test_reservation(reservation_id):
    """
    Manually test the Hospitable API lookup for a given reservation ID.
    Useful for checking whether HOSPITABLE_API_TOKEN is set up correctly.
    """
    result = {
        "hospitable_api_token_set": bool(HOSPITABLE_API_TOKEN),
        "hospitable_api_base": HOSPITABLE_API_BASE,
    }
    try:
        result["reservation_details"] = get_reservation_details(reservation_id)
    except Exception as e:
        result["error"] = str(e)
    return jsonify(result), 200


@app.route("/debug/test-extract/<reservation_id>", methods=["GET"])
def debug_test_extract(reservation_id):
    """
    Re-run the AI extraction on the stored messages for a reservation, and
    show exactly what Claude returned. Useful for checking CLAUDE_API_KEY
    and whether the guest's message actually contained registration info.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT raw_messages FROM reservations WHERE reservation_id = ?", (reservation_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "No reservation found with that ID"}), 404

    existing_messages = json.loads(row[0]) if row[0] else []
    all_text = "\n\n---\n\n".join(
        m["content"] for m in existing_messages if m.get("content")
    )

    result = {
        "claude_api_key_set": bool(CLAUDE_API_KEY),
        "message_count": len(existing_messages),
        "combined_message_text": all_text,
        "extracted": extract_guest_info(all_text, {}),
    }
    return jsonify(result), 200


@app.route("/debug/test-email", methods=["GET"])
def debug_test_email():
    """
    Send a test email via SendGrid to confirm the API key and sender are working.
    This does NOT require a reservation — just checks that the email pipeline is functional.
    """
    result = {
        "SENDGRID_API_KEY_present": bool(SENDGRID_API_KEY),
        "EMAIL_FROM":               EMAIL_FROM,
        "BUILDING_EMAIL":           BUILDING_EMAIL or "(not set)",
    }
    if not SENDGRID_API_KEY or not BUILDING_EMAIL:
        result["error"] = "Missing SENDGRID_API_KEY or BUILDING_EMAIL env var"
        return jsonify(result), 500
    try:
        payload = {
            "personalizations": [{"to": [{"email": BUILDING_EMAIL}]}],
            "from": {"email": EMAIL_FROM, "name": "A to Z Vacation Homes"},
            "subject": "[TEST] Guest Registration App — SendGrid Check",
            "content": [{"type": "text/plain", "value": "This is a test email from your Guest Registration app.\n\nIf you received this, SendGrid is configured correctly!"}],
        }
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if resp.status_code in (200, 202):
            result["status"] = "Test email sent successfully!"
            return jsonify(result), 200
        else:
            result["error"] = f"SendGrid error {resp.status_code}: {resp.text}"
            return jsonify(result), 500
    except Exception as e:
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
        return jsonify(result), 500


@app.route("/debug/trigger-send/<reservation_id>", methods=["GET"])
def debug_trigger_send(reservation_id):
    """
    Manually trigger the registration email for a reservation that already has all required
    data in the DB (email_sent=0 but all fields complete). Useful when the email was missed
    due to a transient SMTP error or a code deployment that reset LAST_ERROR.
    """
    global LAST_ERROR
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT guest_names, contact_number, contact_email,
               checkin_date, checkout_date, unit_number, id_image_urls, id_image_data, email_sent
        FROM reservations WHERE reservation_id = ?
    """, (reservation_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "No reservation found with that ID"}), 404

    guest_names, contact_number, contact_email, checkin_date, checkout_date, unit_number, id_image_urls_raw, id_image_data_raw, email_sent = row

    if email_sent:
        conn.close()
        return jsonify({"status": "email already marked as sent"}), 200

    try:
        guest_names_list = json.loads(guest_names) if guest_names and guest_names not in ("null", "[]", "") else []
    except Exception:
        guest_names_list = []

    try:
        image_data = json.loads(id_image_data_raw) if id_image_data_raw else []
    except Exception:
        image_data = []

    missing = []
    if not guest_names_list: missing.append("guest names")
    if not contact_number:   missing.append("contact number")
    if not contact_email:    missing.append("contact email")
    if not checkin_date or not checkout_date: missing.append("dates")
    if not unit_number:      missing.append("unit number")
    if not image_data:       missing.append("ID photo(s)")

    if missing:
        conn.close()
        return jsonify({"error": f"Cannot send — still missing: {', '.join(missing)}"}), 400

    extracted = {
        "guest_names":    guest_names_list,
        "contact_number": contact_number,
        "contact_email":  contact_email,
        "checkin_date":   checkin_date,
        "checkout_date":  checkout_date,
        "unit_number":    unit_number,
    }

    # Use pre-downloaded image bytes (URLs expire after ~1 hour)
    id_attachments = [
        (img["filename"], base64.b64decode(img["data"]))
        for img in image_data if img.get("data")
    ]

    try:
        send_building_email(extracted, id_attachments)
        c.execute("UPDATE reservations SET email_sent = 1 WHERE reservation_id = ?", (reservation_id,))
        conn.commit()
        conn.close()
        return jsonify({
            "status": "Email sent successfully!",
            "reservation_id": reservation_id,
            "sent_to": BUILDING_EMAIL,
            "cc": CC_EMAILS,
            "guests": guest_names_list,
            "unit": unit_number,
            "id_attachments_count": len(id_attachments),
        }), 200
    except Exception as e:
        LAST_ERROR = traceback.format_exc()
        conn.close()
        return jsonify({"error": str(e), "detail": "see /debug/last-error"}), 500


@app.route("/check-pending", methods=["GET", "POST"])
def check_pending():
    """
    Scan all reservations with email_sent=0 and send the registration email for any
    where check-in is tomorrow (or today) and we have names/phone/email/unit + at least one ID.
    Called daily by Railway Cron Job — also callable manually via browser.
    """
    global LAST_ERROR
    today = date.today()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT reservation_id, guest_names, contact_number, contact_email,
               checkin_date, checkout_date, unit_number, id_image_data
        FROM reservations
        WHERE email_sent = 0
    """)
    rows = c.fetchall()

    sent = []
    skipped = []

    for row in rows:
        res_id, guest_names_raw, phone, email_addr, checkin_str, checkout_str, unit, image_data_raw = row

        # Parse fields
        try:
            guest_names = json.loads(guest_names_raw) if guest_names_raw and guest_names_raw not in ("null", "[]", "") else []
        except Exception:
            guest_names = []
        try:
            image_data = json.loads(image_data_raw) if image_data_raw else []
        except Exception:
            image_data = []

        # Skip if missing required non-ID fields
        if not (guest_names and phone and email_addr and checkin_str and checkout_str and unit):
            skipped.append({"reservation_id": res_id, "reason": "missing required fields"})
            continue

        # Skip if no IDs at all
        if not image_data:
            skipped.append({"reservation_id": res_id, "reason": "no ID photos yet"})
            continue

        # Only send if check-in is tomorrow or today
        try:
            checkin_dt = date.fromisoformat(checkin_str)
            days_until = (checkin_dt - today).days
        except Exception:
            skipped.append({"reservation_id": res_id, "reason": "invalid checkin date"})
            continue

        if days_until > 1:
            skipped.append({"reservation_id": res_id, "reason": f"check-in in {days_until} days — not yet"})
            continue

        # Use pre-downloaded image bytes
        extracted = {
            "guest_names":    guest_names,
            "contact_number": phone,
            "contact_email":  email_addr,
            "checkin_date":   checkin_str,
            "checkout_date":  checkout_str,
            "unit_number":    unit,
        }
        id_attachments = [
            (img["filename"], base64.b64decode(img["data"]))
            for img in image_data if img.get("data")
        ]

        building_email_to = resolve_building_email(unit)

        try:
            send_building_email(extracted, id_attachments, to_email=building_email_to)
            c.execute("UPDATE reservations SET email_sent = 1 WHERE reservation_id = ?", (res_id,))
            conn.commit()
            sent.append({"reservation_id": res_id, "unit": unit, "sent_to": building_email_to})
            print(f"[✅] check-pending: sent email for {res_id} ({unit})")
        except Exception as e:
            LAST_ERROR = traceback.format_exc()
            skipped.append({"reservation_id": res_id, "reason": f"email failed: {e}"})

    conn.close()
    return jsonify({"sent": sent, "skipped": skipped, "checked_at": today.isoformat()}), 200


# ─────────────────────────────────────────────
# Start
# ─────────────────────────────────────────────
# Initialize the database on import so it works whether started via
# "python app.py" OR via gunicorn (Procfile) — gunicorn never runs __main__.
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[🚀] Server starting on port {port}")
    app.run(host="0.0.0.0", port=port)
