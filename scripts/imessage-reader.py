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
import subprocess
import sys
from collections.abc import Callable
from typing import Any

MESSAGES_DB = os.path.expanduser("~/Library/Messages/chat.db")
ADDRESSBOOK_PATTERN = os.path.expanduser(
    "~/Library/Application Support/AddressBook/**/AddressBook-v22.abcddb"
)
HEIC_CONVERT_DIR = "/tmp/imessage-attachments"


# ── Contact resolution via AddressBook ──────────────────────────────────────


def _load_addressbook() -> tuple[dict[str, str], dict[str, list[str]]]:
    """Load phone->name and name->phones mappings from the macOS AddressBook.

    Returns (phone_to_name, name_to_phones) where phone keys are last-10-digits.
    A contact with multiple numbers maps to a list of all of them — collapsing
    to a single number is what made a name search miss a contact's other numbers
    (e.g. a parent who switched phones but kept the same contact card).
    """
    phone_to_name: dict[str, str] = {}
    name_to_phones: dict[str, list[str]] = {}

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
                    phone_to_name.setdefault(key, name)
                    keys = name_to_phones.setdefault(name.lower(), [])
                    if key not in keys:  # dedup on normalized key, not raw string
                        keys.append(key)
            db.close()
        except Exception:
            continue

    return phone_to_name, name_to_phones


# Module-level cache (loaded once)
_phone_to_name: dict[str, str] | None = None
_name_to_phones: dict[str, list[str]] | None = None


def _ensure_addressbook() -> tuple[dict[str, str], dict[str, list[str]]]:
    global _phone_to_name, _name_to_phones
    if _phone_to_name is None:
        _phone_to_name, _name_to_phones = _load_addressbook()
    return _phone_to_name, _name_to_phones


def resolve_name_from_phone(phone: str) -> str | None:
    """Look up a contact name by phone number."""
    p2n, _ = _ensure_addressbook()
    digits = re.sub(r"[^\d]", "", phone)
    key = digits[-10:] if len(digits) >= 10 else digits
    return p2n.get(key)


def resolve_phones_from_name(name: str) -> list[str]:
    """Look up ALL phone numbers for a contact name (case-insensitive).

    Exact match wins (returns just that contact's numbers, so an exact name
    like "Sam" stays distinct from a different card "Sam Work"). Otherwise falls
    back to a partial match, unioning the numbers of every contact whose name
    contains the query. Returns a deduped list of last-10-digit keys, or [] if
    no contact matches.
    """
    _, n2p = _ensure_addressbook()
    lower = name.lower()
    # Exact match first
    if lower in n2p:
        return list(n2p[lower])
    # Partial match: union numbers across every matching contact
    keys: list[str] = []
    for contact_name, phones in n2p.items():
        if lower in contact_name:
            for key in phones:
                if key not in keys:
                    keys.append(key)
    return keys


# ── Phone normalization ────────────────────────────────────────────────────


def matching_contact_names(name: str) -> list[str]:
    p2n, n2p = _ensure_addressbook()
    lower = name.lower()
    proper = {n.lower(): n for n in p2n.values()}
    return sorted(proper.get(k, k) for k in n2p if lower in k)


APPLE_EPOCH = 978307200


def apple_to_datetime(value: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(value / 1e9 + APPLE_EPOCH)


def datetime_to_apple(value: datetime.datetime) -> int:
    return int((value.timestamp() - APPLE_EPOCH) * 1e9)


def parse_since(raw: str) -> datetime.datetime:
    raw = raw.strip()
    today = datetime.date.today()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.datetime.strptime(raw, fmt).time()
            return datetime.datetime.combine(today, t)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(raw, fmt)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(
        f"can't read --since {raw!r}; use HH:MM (today) or 'YYYY-MM-DD HH:MM'"
    )


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


def find_chats_with_participant(db: sqlite3.Connection, digits: str) -> list[int]:
    """Find ALL chat ROWIDs (1:1 and group, named or unnamed) containing a given phone.

    Group chats have a GUID in chat_identifier — the participant phones live in
    chat_handle_join → handle. This catches unnamed groups that the display_name
    and chat_identifier searches miss.
    """
    cursor = db.cursor()
    last10 = digits[-10:]
    patterns = [f"%+{digits}%", f"%+1{last10}%", f"%{digits}%", f"%{last10}%"]
    seen: list[int] = []
    for pattern in patterns:
        cursor.execute(
            """
            SELECT DISTINCT c.ROWID FROM chat c
            JOIN chat_handle_join chj ON c.ROWID = chj.chat_id
            JOIN handle h ON chj.handle_id = h.ROWID
            WHERE h.id LIKE ?
            """,
            (pattern,),
        )
        for (rowid,) in cursor.fetchall():
            if rowid not in seen:
                seen.append(rowid)
    return seen


def describe_chat(db: sqlite3.Connection, chat_id: int) -> str:
    """Return a short human-readable description of a chat (for disambiguation)."""
    cursor = db.cursor()
    cursor.execute(
        "SELECT display_name, chat_identifier FROM chat WHERE ROWID = ?",
        (chat_id,),
    )
    row = cursor.fetchone()
    if not row:
        return f"chat {chat_id}"
    display_name, chat_identifier = row

    participants = get_chat_participants(db, chat_id)
    names = sorted(set(participants.values()))

    cursor.execute(
        """
        SELECT m.date, m.service FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        WHERE cmj.chat_id = ?
        ORDER BY m.date DESC LIMIT 1
        """,
        (chat_id,),
    )
    last_row = cursor.fetchone()
    last_str = ""
    if last_row and last_row[0]:
        last_str = f"  last: {apple_to_datetime(last_row[0]).strftime('%Y-%m-%d %H:%M')}"
        if last_row[1]:
            last_str += f" via {last_row[1]}"

    if display_name:
        kind = f'group: "{display_name}"'
    elif len(names) <= 1:
        name = names[0] if names else chat_identifier
        kind = f"1:1 with {name}" if name == chat_identifier else f"1:1 with {name} ({chat_identifier})"
    else:
        kind = f"group (unnamed, {len(names)} participants: {', '.join(names)})"

    return f"[{chat_id}] {kind}{last_str}"


def is_group_chat(db: sqlite3.Connection, chat_id: int) -> bool:
    cursor = db.cursor()
    cursor.execute("SELECT display_name FROM chat WHERE ROWID = ?", (chat_id,))
    row = cursor.fetchone()
    if row and row[0]:
        return True
    return len(set(get_chat_participants(db, chat_id).values())) > 1


def chats_by_recent_activity(
    db: sqlite3.Connection,
    chat_ids: list[int] | None = None,
    since: datetime.datetime | None = None,
    limit: int | None = None,
    until: datetime.datetime | None = None,
) -> list[int]:
    where: list[str] = []
    params: list[int] = []
    if until is not None:
        where.append("m.date < ?")
        params.append(datetime_to_apple(until))
    if chat_ids is not None:
        if not chat_ids:
            return []
        where.append(f"cmj.chat_id IN ({','.join('?' * len(chat_ids))})")
        params.extend(chat_ids)
    having = ""
    if since is not None:
        having = "HAVING MAX(m.date) >= ?"
        params.append(datetime_to_apple(since))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    cursor = db.cursor()
    cursor.execute(
        f"""
        SELECT cmj.chat_id, MAX(m.date) FROM chat_message_join cmj
        JOIN message m ON m.ROWID = cmj.message_id
        {where_sql}
        GROUP BY cmj.chat_id
        {having}
        ORDER BY MAX(m.date) DESC
        {limit_sql}
        """,
        params,
    )
    ranked = [r[0] for r in cursor.fetchall()]
    if chat_ids is not None and since is None:
        ranked += [c for c in chat_ids if c not in ranked]
    return ranked


def find_chat_ids(
    db: sqlite3.Connection,
    contact: str,
    include_groups: bool = False,
) -> list[int]:
    """Find chat ROWIDs matching the contact identifier.

    Search order:
      1. Phone-number match on chat_identifier (1:1 chats). A contact name can
         resolve to MULTIPLE numbers (e.g. someone who switched phones); every
         number is searched and the matching chats are unioned so a name search
         sees all of a contact's threads, not just one number's.
      2. Display-name match (named group chats)
      3. Phone-number participant match (named + unnamed group chats)  -- fallback
         or always-on when `include_groups=True`
      4. Substring match on chat_identifier (legacy catch-all)
    """
    cursor = db.cursor()

    digits = normalize_phone(contact)
    if len(digits) >= 10:
        number_keys = [digits]
    else:
        number_keys = [normalize_phone(p) for p in resolve_phones_from_name(contact)]

    if number_keys:
        rowids: list[int] = []
        for digits in number_keys:
            phone_patterns = [
                f"+{digits}",
                f"+1{digits[-10:]}",
                f"{digits}",
                f"{digits[-10:]}",
            ]
            # 1:1 chats: chat_identifier IS the phone
            one_to_one: list[int] = []
            for pattern in phone_patterns:
                cursor.execute(
                    "SELECT ROWID FROM chat WHERE chat_identifier LIKE ?",
                    (f"%{pattern}%",),
                )
                for (rid,) in cursor.fetchall():
                    if rid not in one_to_one:
                        one_to_one.append(rid)

            if one_to_one:
                for rid in one_to_one:
                    if rid not in rowids:
                        rowids.append(rid)
                if include_groups:
                    # Add group chats with the same participant
                    for gid in find_chats_with_participant(db, digits):
                        if gid not in rowids:
                            rowids.append(gid)
            else:
                # No 1:1 for this number; fall back to participant (group) match
                for gid in find_chats_with_participant(db, digits):
                    if gid not in rowids:
                        rowids.append(gid)

        if rowids:
            return rowids

    # Try display name match (named group chats)
    cursor.execute(
        "SELECT ROWID FROM chat WHERE display_name LIKE ?",
        (f"%{contact}%",),
    )
    rows = cursor.fetchall()
    if rows:
        return [r[0] for r in rows]

    # Substring match on chat_identifier (legacy catch-all)
    cursor.execute(
        "SELECT ROWID FROM chat WHERE chat_identifier LIKE ?",
        (f"%{contact}%",),
    )
    rows = cursor.fetchall()
    if rows:
        return [r[0] for r in rows]

    return []


def list_chats_for_contact(db: sqlite3.Connection, contact: str) -> list[int]:
    """Return every chat (1:1 + named groups + unnamed groups) involving a contact.

    Unions across all of a contact's numbers when resolving by name.
    """
    digits = normalize_phone(contact)
    if len(digits) >= 10:
        number_keys = [digits]
    else:
        number_keys = [normalize_phone(p) for p in resolve_phones_from_name(contact)]
        if not number_keys:
            return []
    rowids: list[int] = []
    for digits in number_keys:
        for rid in find_chats_with_participant(db, digits):
            if rid not in rowids:
                rowids.append(rid)
    return chats_by_recent_activity(db, rowids)


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


# ── Attachments ─────────────────────────────────────────────────────────────


def get_attachments_for_messages(
    db: sqlite3.Connection, message_rowids: list[int]
) -> dict[int, list[dict]]:
    """Fetch attachment metadata for a batch of messages.

    Returns {message_rowid: [{rowid, path, mime, name, size, is_sticker, exists}, ...]}.
    Filters out:
      - Rows with NULL mime_type (these are .pluginPayloadAttachment link previews,
        not user content).
      - Rows with hide_attachment=1 (Apple flags these as not-for-display).
      - Rows with no filename.
    """
    if not message_rowids:
        return {}
    cursor = db.cursor()
    placeholders = ",".join("?" * len(message_rowids))
    cursor.execute(
        f"""
        SELECT maj.message_id, a.ROWID, a.filename, a.mime_type,
               a.transfer_name, a.total_bytes, a.is_sticker
        FROM attachment a
        JOIN message_attachment_join maj ON a.ROWID = maj.attachment_id
        WHERE maj.message_id IN ({placeholders})
          AND a.filename IS NOT NULL
          AND a.mime_type IS NOT NULL
          AND a.hide_attachment = 0
        ORDER BY maj.message_id, a.ROWID
        """,
        message_rowids,
    )
    result: dict[int, list[dict]] = {}
    for msg_id, rowid, filename, mime, name, size, sticker in cursor.fetchall():
        abs_path = os.path.expanduser(filename)
        result.setdefault(msg_id, []).append({
            "rowid": rowid,
            "path": abs_path,
            "mime": mime,
            "name": name or os.path.basename(abs_path),
            "size": size or 0,
            "is_sticker": bool(sticker),
            "exists": os.path.exists(abs_path),
        })
    return result


def convert_heic_to_jpeg(att: dict) -> str | None:
    """Convert a HEIC attachment to JPEG using macOS `sips`.

    Output filename is `<attachment_rowid>-<basename>.jpg` in HEIC_CONVERT_DIR.
    The ROWID prefix prevents collisions when two threads send files with the
    same basename (e.g., iOS reuses IMG_XXXX.heic numbers across senders).
    Idempotent: if the JPEG already exists, return its path without re-running.
    """
    src = att["path"]
    if not os.path.exists(src):
        return None
    os.makedirs(HEIC_CONVERT_DIR, exist_ok=True)
    base = os.path.basename(src).rsplit(".", 1)[0]
    out_path = os.path.join(HEIC_CONVERT_DIR, f"{att['rowid']}-{base}.jpg")
    if os.path.exists(out_path):
        return out_path
    try:
        result = subprocess.run(
            ["sips", "-s", "format", "jpeg", "-Z", "1600", src, "--out", out_path],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0 and os.path.exists(out_path):
            return out_path
    except Exception:
        pass
    return None


def format_attachment(att: dict, convert_heic: bool) -> str:
    """Render an attachment as a `[attachment: ...]` token."""
    mime = att["mime"]
    path = att["path"]

    if not att["exists"]:
        return f"[attachment: {mime}, {path} (missing on disk)]"

    if att["is_sticker"]:
        return f"[sticker: {mime}, {path}]"

    if convert_heic and mime in ("image/heic", "image/heif"):
        jpeg = convert_heic_to_jpeg(att)
        if jpeg:
            return f"[attachment: {mime}, {path} | converted: {jpeg}]"

    return f"[attachment: {mime}, {path}]"


def render_message_line(
    time_str: str, sender: str, text: str, attachments: list[dict], convert_heic: bool
) -> str:
    """Build the chat-line output for one message.

    Layout rules:
      - 0 attachments: `HH:MM | Sender: text`
      - 1 attachment:  `HH:MM | Sender: text [attachment: ...]` (or just attachment if no text)
      - 2+ attachments: text on the first line, each attachment on its own indented line
    """
    prefix = f"{time_str} | {sender}:"
    n = len(attachments)

    if n == 0:
        return f"{prefix} {text}"

    if n == 1:
        token = format_attachment(attachments[0], convert_heic)
        if text:
            return f"{prefix} {text} {token}"
        return f"{prefix} {token}"

    # 2+: multi-line for readability
    lines = []
    lines.append(f"{prefix} {text}" if text else prefix)
    for att in attachments:
        lines.append(f"    {format_attachment(att, convert_heic)}")
    return "\n".join(lines)


# ── Messages ────────────────────────────────────────────────────────────────


def read_messages(
    db: sqlite3.Connection,
    chat_ids: list[int],
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
    search: str | None = None,
    limit: int | None = None,
) -> tuple[list[dict], int]:
    cursor = db.cursor()

    placeholders = ",".join("?" * len(chat_ids))
    where_clauses = [f"cmj.chat_id IN ({placeholders})"]
    params: list = list(chat_ids)

    # Skip tapback reactions
    where_clauses.append("m.associated_message_type = 0")

    if start is not None:
        where_clauses.append("m.date >= ?")
        params.append(datetime_to_apple(start))
    if end is not None:
        where_clauses.append("m.date < ?")
        params.append(datetime_to_apple(end))

    cursor.execute(
        f"""
        SELECT DISTINCT m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody,
               m.handle_id, m.cache_has_attachments
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        WHERE {" AND ".join(where_clauses)}
        ORDER BY m.date ASC
        """,
        params,
    )
    rows = cursor.fetchall()

    needle = search.lower() if search else None
    decoded = []
    for rowid, date_val, is_from_me, text, attributed_body, handle_id, has_attach in rows:
        msg = text
        if not msg and attributed_body:
            msg = extract_text_from_blob(attributed_body)

        # Strip iOS's U+FFFC OBJECT REPLACEMENT CHARACTER — it's the inline
        # placeholder for "attachment goes here". Once we render the attachment
        # token explicitly, the placeholder is just noise. Collapse runs of
        # spaces left behind, but preserve newlines (multi-line messages).
        if msg:
            msg = msg.replace("￼", "")
            msg = "\n".join(re.sub(r" +", " ", line).strip() for line in msg.split("\n"))
            msg = msg.strip()

        if needle and needle not in (msg or "").lower():
            continue
        if not msg and not has_attach:
            continue
        decoded.append((rowid, date_val, is_from_me, msg, handle_id, has_attach))

    total = len(decoded)
    if limit and total > limit:
        decoded = decoded[-limit:]

    # Batch-fetch attachments for messages that flag them.
    atts_by_msg = get_attachments_for_messages(db, [d[0] for d in decoded if d[5]])

    messages = []
    for rowid, date_val, is_from_me, msg, handle_id, _ in decoded:
        attachments = atts_by_msg.get(rowid, [])

        # Skip messages with neither text nor resolvable attachments.
        # (has_attach=1 with no resolvable attachments usually means the only
        # "attachment" was a link preview, which we filter out.)
        if not msg and not attachments:
            continue

        messages.append({
            "timestamp": apple_to_datetime(date_val),
            "is_from_me": bool(is_from_me),
            "text": msg or "",
            "handle_id": handle_id,
            "attachments": attachments,
        })

    return messages, total


# ── Main ────────────────────────────────────────────────────────────────────


def print_messages(messages: list[dict[str, Any]], resolve_sender: Callable[[int], str], convert_heic: bool) -> None:
    current_date = None
    for msg in messages:
        msg_date = msg["timestamp"].date()
        if msg_date != current_date:
            current_date = msg_date
            print(f"\n--- {msg_date.strftime('%A, %B %d, %Y')} ---\n")
        sender = "You" if msg["is_from_me"] else resolve_sender(msg["handle_id"])
        print(render_message_line(
            msg["timestamp"].strftime("%H:%M"), sender, msg["text"], msg["attachments"], convert_heic
        ))


def resolve_sender_for_chat(db: sqlite3.Connection, chat_id: int) -> str:
    return next(iter(get_chat_participants(db, chat_id).values()), str(chat_id))


def print_chat_list(db: sqlite3.Connection, title: str, rowids: list[int]) -> None:
    print(title)
    for rid in rowids:
        print(f"  {describe_chat(db, rid)}")


def main():
    parser = argparse.ArgumentParser(
        description="Read iMessage/SMS/RCS conversations",
        epilog=(
            "Range: --today (default), --date, --days, --since, or --all. "
            "--since HH:MM shows everything from that minute on, ready for checking what is new."
        ),
    )
    parser.add_argument(
        "contact",
        nargs="?",
        help="Phone number, contact name, or group chat name (omit if using --chat-id)",
    )
    parser.add_argument("--today", action="store_true", help="Today's messages only (the default range)")
    parser.add_argument("--days", type=int, help="Messages from the last N days (rolling, N*24 hours back from now)")
    parser.add_argument("--date", help="Messages from one calendar day (YYYY-MM-DD)")
    parser.add_argument(
        "--since",
        type=parse_since,
        help="Messages at or after a time: HH:MM (today) or 'YYYY-MM-DD HH:MM'",
    )
    parser.add_argument("--all", action="store_true", help="The whole history (newest --limit messages)")
    parser.add_argument(
        "--limit",
        type=int,
        help=(
            "Keep the newest N messages (default: 100 for --days and --all, unlimited otherwise). "
            "With --list-chats: number of chats (default 25)."
        ),
    )
    parser.add_argument("--search", help="Only messages whose text contains this (case-insensitive)")
    parser.add_argument(
        "--chat-id",
        type=int,
        action="append",
        help="Read a chat by ROWID; repeat for several chats, each printed in its own section",
    )
    parser.add_argument(
        "--list-chats",
        action="store_true",
        help=(
            "List chats newest first, then exit: every chat involving the contact, "
            "or with no contact the most recently active chats"
        ),
    )
    parser.add_argument(
        "--include-groups",
        action="store_true",
        help="When searching by contact, also include named + unnamed group chats with them",
    )
    parser.add_argument(
        "--convert-heic",
        action="store_true",
        help=(
            "Auto-convert HEIC attachments to JPEG (cached in /tmp/imessage-attachments/) "
            "so the output includes a readable JPEG path alongside the original HEIC. "
            "Requires macOS `sips`."
        ),
    )

    args = parser.parse_args()

    if not os.path.exists(MESSAGES_DB):
        print(f"Error: iMessage database not found at {MESSAGES_DB}", file=sys.stderr)
        sys.exit(1)

    if not args.contact and not args.chat_id and not args.list_chats:
        parser.error("provide a contact name/phone, a --chat-id, or --list-chats")

    try:
        db = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
        db.execute("SELECT 1 FROM message LIMIT 1")
    except sqlite3.Error as exc:
        print(
            f"Error: can't read {MESSAGES_DB} ({exc}). Grant Full Disk Access to the app running "
            "this terminal: System Settings > Privacy & Security > Full Disk Access.",
            file=sys.stderr,
        )
        sys.exit(1)

    now = datetime.datetime.now()
    today_start = datetime.datetime.combine(datetime.date.today(), datetime.time())
    start: datetime.datetime | None = None
    end: datetime.datetime | None = None
    default_limit = None
    if args.date:
        try:
            day = datetime.datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            parser.error(f"--date expects YYYY-MM-DD, got {args.date!r}")
        start, end = day, day + datetime.timedelta(days=1)
    elif args.days:
        start = now - datetime.timedelta(days=args.days)
        default_limit = 100
    elif args.all:
        default_limit = 100
    elif not args.since:
        start = today_start
    if args.since:
        start = args.since if start is None else max(start, args.since)
    limit = args.limit if args.limit is not None else default_limit

    if args.list_chats:
        if args.contact:
            rowids = list_chats_for_contact(db, args.contact)
            if not rowids:
                print(f"No chats found involving '{args.contact}'", file=sys.stderr)
                sys.exit(1)
            print_chat_list(db, f"Chats involving '{args.contact}', newest first:", rowids[: args.limit] if args.limit else rowids)
        else:
            since = start if (args.days or args.since or args.date or args.today) else None
            rowids = chats_by_recent_activity(db, since=since, limit=args.limit or 25)
            print_chat_list(db, "Most recently active chats:", rowids)
        sys.exit(0)

    if args.chat_id:
        cursor = db.cursor()
        for cid in args.chat_id:
            cursor.execute("SELECT ROWID FROM chat WHERE ROWID = ?", (cid,))
            if not cursor.fetchone():
                print(f"No chat with ROWID {cid}", file=sys.stderr)
                sys.exit(1)
        sections = [[cid] for cid in dict.fromkeys(args.chat_id)]
    else:
        chat_ids = find_chat_ids(db, args.contact, include_groups=args.include_groups)
        if not chat_ids:
            print(f"No chat found matching '{args.contact}'.", file=sys.stderr)
            names = matching_contact_names(args.contact)
            if names:
                print(f"Contacts matching it: {', '.join(names[:10])}", file=sys.stderr)
            print(
                "If it is a group or a sender that isn't a contact, pick its ROWID below "
                "and rerun with --chat-id N.",
                file=sys.stderr,
            )
            print_chat_list(db, "Most recently active chats:", chats_by_recent_activity(db, limit=15))
            sys.exit(1)
        by_person: dict[str, list[int]] = {}
        groups: list[int] = []
        for cid in chat_ids:
            if is_group_chat(db, cid):
                groups.append(cid)
            else:
                person = resolve_sender_for_chat(db, cid)
                by_person.setdefault(person, []).append(cid)
        sections = list(by_person.values()) + [[c] for c in groups]
        if len(chat_ids) > 1:
            # We resolved the name/number to several threads (e.g. a contact with
            # multiple numbers) and merge them by timestamp below — say so, so a
            # mixed timeline isn't mistaken for a single thread.
            merged = [f"{p} ({', '.join(str(c) for c in ids)})" for p, ids in by_person.items() if len(ids) > 1]
            print(
                f"Note: '{args.contact}' matched {len(chat_ids)} chats. "
                + (f"Merged by time per person: {'; '.join(merged)}. " if merged else "")
                + ("Each other person or group gets its own section." if len(sections) > 1 else ""),
                file=sys.stderr,
            )

    participants: dict[int, str] = {}

    def resolve_sender(handle_id: int) -> str:
        if handle_id in participants:
            return participants[handle_id]
        cur = db.cursor()
        cur.execute("SELECT id FROM handle WHERE ROWID = ?", (handle_id,))
        row = cur.fetchone()
        if row:
            name = resolve_name_from_phone(row[0])
            resolved = name if name else row[0]
        else:
            resolved = "Other"
        participants[handle_id] = resolved
        return resolved

    found_any = False
    for section in sections:
        for cid in section:
            participants.update(get_chat_participants(db, cid))
        messages, total = read_messages(db, section, start=start, end=end, search=args.search, limit=limit)
        label = describe_chat(db, section[0]) if len(section) == 1 else (
            f"{resolve_sender_for_chat(db, section[0])}, 1:1 threads {', '.join(str(c) for c in section)} merged"
        )
        if not messages:
            if len(sections) > 1 and args.chat_id:
                print(f"\n=== {label} ===\n(no messages in range)")
            continue
        if len(sections) > 1:
            print(f"\n=== {label} ===")
        found_any = True
        if total > len(messages) and limit and total > limit:
            kind = f"messages matching {args.search!r}" if args.search else "messages in range"
            print(f"(showing the newest {limit} of {total} {kind}; raise --limit or narrow the range for more)")
        print_messages(messages, resolve_sender, args.convert_heic)

    if not found_any:
        sys.stdout.flush()
        all_ids = [c for s in sections for c in s]
        if len(sections) == 1 or not args.chat_id:
            print("No messages found for the given criteria. Last activity:", file=sys.stderr)
            for cid in all_ids:
                print(f"  {describe_chat(db, cid)}", file=sys.stderr)
        if args.contact and not args.chat_id and start is not None:
            others = [c for c in list_chats_for_contact(db, args.contact) if c not in all_ids]
            active = chats_by_recent_activity(db, others, since=start, until=end)
            if active:
                print("Other chats with them that do have messages in range (read with --chat-id N):", file=sys.stderr)
                for cid in active:
                    print(f"  {describe_chat(db, cid)}", file=sys.stderr)
            names = [n for n in matching_contact_names(args.contact) if n.lower() != args.contact.lower()]
            if names:
                print(f"Other contacts containing '{args.contact}': {', '.join(names[:10])}", file=sys.stderr)

    db.close()


if __name__ == "__main__":
    main()
