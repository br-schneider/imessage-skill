#!/usr/bin/env python3
"""Read iMessage/SMS/RCS conversations from chat.db.

Usage:
    imessage-reader.py <contact> [--days N | --date YYYY-MM-DD | --today | --all] [--limit N]

Contact can be:
    - Phone number: "+15551234567" or "(555) 123-4567" or "5551234567"
    - Contact name: "John Smith" (looks up in AddressBook)
    - Group chat name: "Family" or "Work Chat" (partial match)

Examples:
    imessage-reader.py "Mom" --today
    imessage-reader.py "Family" --days 7
    imessage-reader.py "+15551234567" --date 2026-03-29
    imessage-reader.py "Work Chat" --all --limit 50
"""

import sqlite3
import argparse
import datetime
import glob
import os
import re
import sys

MESSAGES_DB = os.path.expanduser("~/Library/Messages/chat.db")
ADDRESSBOOK_PATTERN = os.path.expanduser(
    "~/Library/Application Support/AddressBook/**/AddressBook-v22.abcddb"
)


# ── Contact resolution via AddressBook ──────────────────────────────────────


def _load_addressbook() -> tuple[dict[str, str], dict[str, str]]:
    """Load phone->name and name->phone mappings from the macOS AddressBook.

    Returns (phone_to_name, name_to_phone) where phone keys are last-10-digits.
    """
    phone_to_name: dict[str, str] = {}
    name_to_phone: dict[str, str] = {}

    for dbpath in glob.glob(ADDRESSBOOK_PATTERN, recursive=True):
        try:
            db = sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True)
            cursor = db.cursor()
            cursor.execute("""
                SELECT r.ZFIRSTNAME, r.ZLASTNAME, p.ZFULLNUMBER
                FROM ZABCDRECORD r
                JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
                WHERE p.ZFULLNUMBER IS NOT NULL
            """)
            for first, last, phone in cursor.fetchall():
                name_parts = [p for p in (first, last) if p]
                if not name_parts:
                    continue
                name = " ".join(name_parts)
                digits = re.sub(r"[^\d]", "", phone)
                if len(digits) >= 7:
                    key = digits[-10:] if len(digits) >= 10 else digits
                    phone_to_name[key] = name
                    name_to_phone[name.lower()] = key
            db.close()
        except Exception:
            continue

    return phone_to_name, name_to_phone


# Module-level cache (loaded once)
_phone_to_name: dict[str, str] | None = None
_name_to_phone: dict[str, str] | None = None


def _ensure_addressbook() -> tuple[dict[str, str], dict[str, str]]:
    global _phone_to_name, _name_to_phone
    if _phone_to_name is None:
        _phone_to_name, _name_to_phone = _load_addressbook()
    return _phone_to_name, _name_to_phone


def resolve_name_from_phone(phone: str) -> str | None:
    """Look up a contact name by phone number."""
    p2n, _ = _ensure_addressbook()
    digits = re.sub(r"[^\d]", "", phone)
    key = digits[-10:] if len(digits) >= 10 else digits
    return p2n.get(key)


def resolve_phone_from_name(name: str) -> str | None:
    """Look up a phone number by contact name (case-insensitive partial match)."""
    _, n2p = _ensure_addressbook()
    lower = name.lower()
    # Exact match first
    if lower in n2p:
        return n2p[lower]
    # Partial match
    for contact_name, phone in n2p.items():
        if lower in contact_name:
            return phone
    return None


# ── Phone normalization ────────────────────────────────────────────────────


def normalize_phone(raw: str) -> str:
    """Strip a phone string down to digits only, with leading 1 if 10 digits."""
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    return digits


# ── Blob parsing ────────────────────────────────────────────────────────────


def extract_text_from_blob(blob: bytes) -> str | None:
    """Extract text from NSKeyedArchiver streamtyped attributedBody blob.

    Anchors on the last NSString marker, finds the type marker (0x2B/0x2A),
    reads the length (single-byte or multi-byte via 0x81), and extracts text.

    Length encoding (Apple typedstream format):
        - 0x00-0x80: single byte = length (0-128)
        - 0x81: next 2 bytes are length as 16-bit little-endian (for messages >127 chars)
    """
    if not blob:
        return None
    try:
        marker = b"NSString"
        idx = blob.rfind(marker)
        if idx == -1:
            return None

        # Find the type marker (0x2B='+' or 0x2A='*') after NSString
        pos = idx + len(marker)
        while pos < len(blob) and blob[pos] not in (0x2B, 0x2A):
            pos += 1
        if pos >= len(blob):
            return None
        pos += 1  # skip type marker

        # Read length: single-byte or multi-byte (0x81 = 16-bit little-endian follows)
        if pos >= len(blob):
            return None
        length_indicator = blob[pos]
        pos += 1

        if length_indicator == 0x81:
            # Multi-byte length: next 2 bytes as 16-bit little-endian
            if pos + 2 > len(blob):
                return None
            text_len = blob[pos] | (blob[pos + 1] << 8)
            pos += 2
        else:
            text_len = length_indicator

        # Extract exactly text_len bytes
        segment = blob[pos:pos + text_len]
        text = segment.decode("utf-8", errors="replace").strip()
        return text if text else None
    except Exception:
        return None


# ── Chat and message queries ────────────────────────────────────────────────


def find_chat_ids(db: sqlite3.Connection, contact: str) -> list[int]:
    """Find chat ROWIDs matching the contact identifier."""
    cursor = db.cursor()

    # Check if input looks like a phone number
    digits = normalize_phone(contact)
    is_phone = len(digits) >= 10

    if not is_phone:
        # Try contact name -> phone number lookup first
        phone = resolve_phone_from_name(contact)
        if phone:
            digits = normalize_phone(phone)
            is_phone = True

    if is_phone:
        phone_patterns = [
            f"+{digits}",
            f"+1{digits[-10:]}",
            f"{digits}",
            f"{digits[-10:]}",
        ]
        for pattern in phone_patterns:
            cursor.execute(
                "SELECT ROWID FROM chat WHERE chat_identifier LIKE ?",
                (f"%{pattern}%",),
            )
            rows = cursor.fetchall()
            if rows:
                return [r[0] for r in rows]

    # Try display name match (group chats)
    cursor.execute(
        "SELECT ROWID FROM chat WHERE display_name LIKE ?",
        (f"%{contact}%",),
    )
    rows = cursor.fetchall()
    if rows:
        return [r[0] for r in rows]

    # Try chat_identifier contains
    cursor.execute(
        "SELECT ROWID FROM chat WHERE chat_identifier LIKE ?",
        (f"%{contact}%",),
    )
    rows = cursor.fetchall()
    if rows:
        return [r[0] for r in rows]

    return []


def get_chat_participants(db: sqlite3.Connection, chat_id: int) -> dict[int, str]:
    """Get handle_id -> display name mapping for a chat."""
    cursor = db.cursor()
    cursor.execute("""
        SELECT h.ROWID, h.id
        FROM handle h
        JOIN chat_handle_join chj ON h.ROWID = chj.handle_id
        WHERE chj.chat_id = ?
    """, (chat_id,))

    participants = {}
    for handle_rowid, handle_id in cursor.fetchall():
        name = resolve_name_from_phone(handle_id)
        if not name:
            name = handle_id  # Fall back to raw phone/email
        participants[handle_rowid] = name

    return participants


def read_messages(
    db: sqlite3.Connection,
    chat_ids: list[int],
    date_filter: str | None = None,
    days: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Read messages from the given chat IDs."""
    cursor = db.cursor()

    placeholders = ",".join("?" * len(chat_ids))
    where_clauses = [f"cmj.chat_id IN ({placeholders})"]
    params: list = list(chat_ids)

    # Skip tapback reactions
    where_clauses.append("m.associated_message_type = 0")

    if date_filter:
        where_clauses.append(
            "date(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') = ?"
        )
        params.append(date_filter)
    elif days:
        where_clauses.append(
            "m.date/1000000000 + 978307200 > unixepoch('now', ?)"
        )
        params.append(f"-{days} days")

    where_sql = " AND ".join(where_clauses)
    limit_sql = f"LIMIT {limit}" if limit else ""

    if limit and not date_filter and not days:
        query = f"""
            SELECT * FROM (
                SELECT m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody,
                       m.handle_id, m.cache_has_attachments
                FROM message m
                JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
                WHERE {where_sql}
                ORDER BY m.date DESC
                {limit_sql}
            ) ORDER BY date ASC
        """
    else:
        query = f"""
            SELECT m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody,
                   m.handle_id, m.cache_has_attachments
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            WHERE {where_sql}
            ORDER BY m.date ASC
            {limit_sql}
        """

    cursor.execute(query, params)

    messages = []
    for rowid, date_val, is_from_me, text, attributed_body, handle_id, has_attach in cursor.fetchall():
        ts = datetime.datetime.fromtimestamp(date_val / 1e9 + 978307200)

        msg = text
        if not msg and attributed_body:
            msg = extract_text_from_blob(attributed_body)

        # Show attachment indicator if no text
        if not msg and has_attach:
            msg = "[attachment]"
        elif not msg:
            continue

        messages.append({
            "timestamp": ts,
            "is_from_me": bool(is_from_me),
            "text": msg,
            "handle_id": handle_id,
        })

    return messages


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Read iMessage/SMS/RCS conversations")
    parser.add_argument("contact", help="Phone number, contact name, or group chat name")
    parser.add_argument("--today", action="store_true", help="Today's messages only")
    parser.add_argument("--days", type=int, help="Messages from last N days")
    parser.add_argument("--date", help="Messages from specific date (YYYY-MM-DD)")
    parser.add_argument("--all", action="store_true", help="All messages (use with --limit)")
    parser.add_argument("--limit", type=int, default=100, help="Max messages (default: 100)")

    args = parser.parse_args()

    if not os.path.exists(MESSAGES_DB):
        print(f"Error: iMessage database not found at {MESSAGES_DB}", file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)

    # Find the chat
    chat_ids = find_chat_ids(db, args.contact)
    if not chat_ids:
        print(f"No chat found matching '{args.contact}'", file=sys.stderr)
        cursor = db.cursor()
        cursor.execute("""
            SELECT chat_identifier, display_name FROM chat
            WHERE display_name <> '' ORDER BY ROWID DESC LIMIT 20
        """)
        print("\nRecent group chats:", file=sys.stderr)
        for cid, name in cursor.fetchall():
            print(f"  {name}", file=sys.stderr)
        sys.exit(1)

    # Get participants for group chats
    participants = {}
    for cid in chat_ids:
        participants.update(get_chat_participants(db, cid))

    # Determine date filter
    date_filter = None
    days = None
    if args.today:
        date_filter = datetime.date.today().isoformat()
    elif args.date:
        date_filter = args.date
    elif args.days:
        days = args.days
    elif not args.all:
        date_filter = datetime.date.today().isoformat()

    messages = read_messages(
        db, chat_ids,
        date_filter=date_filter,
        days=days,
        limit=args.limit if not date_filter else None,
    )

    if not messages:
        print("No messages found for the given criteria.", file=sys.stderr)
        sys.exit(0)

    # Build a handle_id -> name resolver with on-the-fly fallback
    def resolve_sender(handle_id: int) -> str:
        if handle_id in participants:
            return participants[handle_id]
        # Fallback: look up handle directly from the handle table
        cur = db.cursor()
        cur.execute("SELECT id FROM handle WHERE ROWID = ?", (handle_id,))
        row = cur.fetchone()
        if row:
            name = resolve_name_from_phone(row[0])
            resolved = name if name else row[0]
        else:
            resolved = "Other"
        participants[handle_id] = resolved  # cache for next time
        return resolved

    # Print conversation
    current_date = None
    for msg in messages:
        msg_date = msg["timestamp"].date()
        if msg_date != current_date:
            current_date = msg_date
            print(f"\n--- {msg_date.strftime('%A, %B %d, %Y')} ---\n")

        time_str = msg["timestamp"].strftime("%H:%M")

        if msg["is_from_me"]:
            sender = "You"
        else:
            sender = resolve_sender(msg["handle_id"])

        print(f"{time_str} | {sender}: {msg['text']}")

    db.close()


if __name__ == "__main__":
    main()
