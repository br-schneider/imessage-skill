---
name: imessage
description: Read iMessage, SMS, and RCS conversations from the macOS Messages database. Use when the user asks to read texts, check messages, see what someone said, or look at a group chat. Handles all message types including RCS blobs, resolves contact names from AddressBook, and filters by date.
license: MIT
metadata:
  author: br-schneider
  version: "1.0.0"
---

# iMessage Reader

Read iMessage, SMS, and RCS conversations from the local macOS Messages database.

## Usage

Run the reader script. It lives in `scripts/imessage-reader.py` relative to this skill file:

```bash
python3 "SKILL_DIR/scripts/imessage-reader.py" "<contact>" [options]
```

Replace `SKILL_DIR` with the actual directory where this skill is installed. To find it, run:
```bash
dirname "$(find ~/.claude -name imessage-reader.py -path '*/scripts/*' 2>/dev/null | head -1)"/../
```

Or if installed globally, the script may be at `~/.claude/scripts/imessage-reader.py`.

### Contact formats
- **Contact name**: `"Mom"`, `"John Smith"` (looks up phone in macOS AddressBook, partial match works)
- **Phone number**: `"+15551234567"`, `"(555) 123-4567"`, `"5551234567"`
- **Group chat name**: `"Family"`, `"Work Chat"` (partial match on group display name)

### Time range options
- `--today` — today's messages (default if no range specified)
- `--days N` — last N days
- `--date YYYY-MM-DD` — specific date
- `--all --limit N` — all messages, most recent N (default limit: 100)

### Examples

```bash
python3 ~/.claude/scripts/imessage-reader.py "Mom" --today
python3 ~/.claude/scripts/imessage-reader.py "Family" --days 7
python3 ~/.claude/scripts/imessage-reader.py "John Smith" --date 2026-03-29
python3 ~/.claude/scripts/imessage-reader.py "Work Chat" --all --limit 50
python3 ~/.claude/scripts/imessage-reader.py "+15551234567" --today
```

## How it works

The script reads `~/Library/Messages/chat.db` (the macOS iMessage SQLite database). It handles:
- **iMessage**: text stored in the `text` column
- **SMS/RCS**: text stored in the `attributedBody` blob (Apple typedstream format), decoded with proper multi-byte length support for messages of any length
- **Contact names**: resolved from the macOS AddressBook SQLite database (supports name-based lookup and display)
- **Tapback reactions**: filtered out automatically
- **Attachments**: shows `[attachment]` for image/file messages with no text
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
