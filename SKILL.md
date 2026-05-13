---
name: imessage
description: Read iMessage, SMS, and RCS conversations from the macOS Messages database. Use when the user asks to read texts, check messages, see what someone said, or look at a group chat. Handles all message types including RCS blobs, resolves contact names from AddressBook, surfaces attachment paths inline (with optional HEIC→JPEG conversion), and filters by date.
license: MIT
metadata:
  author: br-schneider
  version: "1.2.0"
---

# iMessage Reader

Read iMessage, SMS, and RCS conversations from the local macOS Messages database.

## Usage

Find and run the reader script:

```bash
# Find the script (works for both skills.sh install and manual install)
IMSG_SCRIPT="$(find ~/.claude -name imessage-reader.py -path '*/imessage*' 2>/dev/null | head -1)"
[ -z "$IMSG_SCRIPT" ] && IMSG_SCRIPT="$(find .claude -name imessage-reader.py -path '*/imessage*' 2>/dev/null | head -1)"
python3 "$IMSG_SCRIPT" "<contact>" [options]
```

Or reference it directly if you know the install path:
```bash
# skills.sh install (project-level)
python3 .claude/skills/imessage/scripts/imessage-reader.py "<contact>" [options]

# skills.sh install (global)
python3 ~/.claude/skills/imessage/scripts/imessage-reader.py "<contact>" [options]

# Manual install
python3 ~/.claude/scripts/imessage-reader.py "<contact>" [options]
```

### Contact formats
- **Contact name**: `"Mom"`, `"John Smith"` (looks up phone in macOS AddressBook, partial match works)
- **Phone number**: `"+15551234567"`, `"(555) 123-4567"`, `"5551234567"`
- **Group chat name**: `"Family"`, `"Work Chat"` (partial match on group display name)
- **Specific chat by ID**: `--chat-id N` (use `--list-chats` to discover IDs; works for unnamed groups)

### Time range options
- `--today` — today's messages (default if no range specified)
- `--days N` — last N days
- `--date YYYY-MM-DD` — specific date
- `--all --limit N` — all messages, most recent N (default limit: 100)

### Discovery options
- `--list-chats <contact>` — list every chat involving the contact (1:1 + named groups + **unnamed groups**), with each chat's ROWID and last-activity date. Use this when you know someone is in a group chat but don't know its name. Exits after printing.
- `--chat-id N` — read a specific chat by ROWID. Pair with `--list-chats` to read an unnamed group chat that the default contact search can't reach.
- `--include-groups` — when searching by contact, return the 1:1 chat AND all group chats containing that contact (instead of just the 1:1). Useful for "show me everything with this person."

### Attachment options
- `--convert-heic` — auto-convert HEIC attachments to JPEG (cached in `/tmp/imessage-attachments/<rowid>-<basename>.jpg`) so the output includes a readable JPEG path alongside the original HEIC. Required when the agent needs to actually read the image content (HEIC is not directly readable by most image tools). Cached and idempotent across re-runs.

### Examples

```bash
IMSG="$(find ~/.claude .claude -name imessage-reader.py -path '*/imessage*' 2>/dev/null | head -1)"
python3 "$IMSG" "Mom" --today
python3 "$IMSG" "Family" --days 7
python3 "$IMSG" "John Smith" --date 2026-03-29
python3 "$IMSG" "Work Chat" --all --limit 50
python3 "$IMSG" "+15551234567" --today

# Discover and read an unnamed group chat
python3 "$IMSG" "John Smith" --list-chats        # prints all chats including unnamed groups
python3 "$IMSG" --chat-id 2816 --today           # read the unnamed group directly

# Read everything (1:1 + all groups) with a contact
python3 "$IMSG" "John Smith" --include-groups --days 7

# Pull a thread AND auto-convert any HEIC images so the JPEGs are read-ready
python3 "$IMSG" "Mom" --today --convert-heic
```

## How it works

The script reads `~/Library/Messages/chat.db` (the macOS iMessage SQLite database). It handles:
- **iMessage**: text stored in the `text` column
- **SMS/RCS**: text stored in the `attributedBody` blob (Apple typedstream format), decoded with proper multi-byte length support for messages of any length
- **Contact names**: resolved from the macOS AddressBook SQLite database (supports name-based lookup and display)
- **Tapback reactions**: filtered out automatically
- **Attachments**: surfaced inline as `[attachment: <mime>, <absolute_path>]` tokens. Messages with 0–1 attachments stay on one line; messages with 2+ attachments use indented continuation lines for readability. The iOS U+FFFC placeholder character is stripped automatically. Link-preview rows (`.pluginPayloadAttachment` with no MIME) and hidden attachments are filtered out.
- **HEIC handling**: HEIC files are not directly readable by most image tools. Pass `--convert-heic` to auto-convert via macOS `sips` and surface a JPEG path alongside the original. Conversions are cached at `/tmp/imessage-attachments/<rowid>-<basename>.jpg` and reused on subsequent runs.
- **Group chats**: resolves participant names from AddressBook where possible
- **Read-only**: opens databases in read-only mode for safety

## Requirements

- macOS 14+ with Messages.app signed in
- Full Disk Access for your terminal (System Settings > Privacy & Security > Full Disk Access)
- Python 3.10+ (no external dependencies, stdlib only)

## When the user asks

When the user says things like "read my messages with Mom" or "what did John text me today" or "show me the Family group chat from last week":

1. Figure out the contact identifier (phone number, group name, or contact name)
2. Figure out the time range (default to today if not specified)
3. Run the script
4. Present the conversation to the user, and work with the content as requested

The script resolves contact names from the macOS AddressBook automatically. If a name doesn't match, try a phone number instead.

### When the default search fails

If a user asks about a thread you know exists but `<contact>` returns "No chat found", it's almost certainly an **unnamed group chat** — Apple stores its `chat.chat_identifier` as a GUID and `display_name` is empty, so neither the name nor phone-number searches find it.

Recover with:

```bash
python3 "$IMSG" "<contact>" --list-chats
```

This lists every chat the contact appears in, including unnamed groups, with each chat's ROWID. Then read the right one with `--chat-id N`. If the user describes a chat by its participants ("the group chat with Dad and Jorge"), `--list-chats` on either participant will surface it.
