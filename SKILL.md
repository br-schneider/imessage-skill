---
name: imessage
description: Read iMessage, SMS, and RCS conversations from the macOS Messages database. Use when the user asks to read texts, check messages, see what someone said, find a group chat, follow a conversation as it happens, or search old texts. Resolves contact names from AddressBook, surfaces attachment paths inline (with optional HEIC→JPEG conversion), and filters by date or time.
license: MIT
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/imessage-reader.py *)
metadata:
  author: br-schneider
  version: "1.4.0"
---

# iMessage Reader

Run the bundled script directly, with no lookup step first:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/imessage-reader.py "<contact>" [options]
```

If `${CLAUDE_SKILL_DIR}` reached you unexpanded (an agent other than Claude Code), the script is `scripts/imessage-reader.py` in the directory holding this `SKILL.md`; use that absolute path.

## Pick the target

- **A person**: their contact name (`"Mom"`, `"John Smith"`, partial match works) or a phone number in any format. Every number on the contact card is searched and that person's 1:1 threads merge into one timeline. When a partial name matches several people, each person gets their own section.
- **A named group**: its display name (`"Family"`).
- **Anything else** (an unnamed group, a short code, a sender who isn't a contact, "the chat with Dad and Eric"): run `--list-chats`, then read it with `--chat-id N`.
  - `--list-chats "<contact>"` lists every chat that person is in, newest first.
  - `--list-chats` with no contact lists the 25 most recently active chats across the whole database. Narrow it with `--today` or `--days N`, or change the count with `--limit N`.
  - Each line shows the ROWID, the participants, the number for a 1:1, and when and how the last message arrived (`last: 2026-10-07 18:59 via iMessage`). That last part tells you which of someone's numbers they are using now.
- **Several chats at once**: repeat `--chat-id` (`--chat-id 2618 --chat-id 2631`). Each chat prints in its own section.
- `--include-groups` adds every group the person is in to a contact search, one section per group. A contact search alone reads only the 1:1, so add it whenever the question is about a person rather than one thread ("what has Maddie said today", "anything from Dad").

## Pick the range

- `--today` is the default.
- `--date YYYY-MM-DD` covers one calendar day.
- `--days N` covers a rolling N×24 hours back from now.
- `--all` covers the whole history.
- `--since HH:MM` (today) or `--since "YYYY-MM-DD HH:MM"` shows every message from that minute on. **When following a live conversation, re-run with `--since` set to the time of the last message you saw.** It replaces piping output through `awk`, `sed`, or `tail`, which cut multi-line messages in half. `--since` is inclusive, so the last message you saw prints again as the first line.
- `--search TEXT` keeps only messages whose text contains TEXT (case-insensitive). Combine it with `--all` to search a person's whole history; day headers stay in, so every match keeps its date.
- `--limit N` keeps the newest N messages. The default is 100 for `--days` and `--all` and unlimited otherwise. When the limit cuts anything, the first line of output says `(showing the newest N of M messages in range ...)`; raise `--limit` or narrow the range when you need the rest.

## Attachments

Attachments print inline as `[attachment: <mime>, <absolute path>]`; read the path directly. Pass `--convert-heic` whenever you need to look at photos: HEIC files are converted to JPEG (cached in `/tmp/imessage-attachments/`) and the token gains `| converted: <jpeg path>`.

## When a read comes back empty

The script explains why on stderr: when each matched chat last had activity, any other chats with that person that do have messages in range, and other contacts whose names contain the query. Follow that hint (usually `--chat-id N` or a wider range) instead of querying `chat.db` yourself.

## Requirements

macOS 14+ with Messages signed in, Full Disk Access for the terminal app (System Settings > Privacy & Security > Full Disk Access), and Python 3.10+ (stdlib only). The databases are opened read-only.
