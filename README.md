# imessage-skill

An [Agent Skill](https://skills.sh) that lets AI agents read your iMessage, SMS, and RCS conversations on macOS.

## Install

```bash
npx skills add br-schneider/imessage-skill
```

Or manually copy to your agent's skill directory:
```bash
# Claude Code (global)
cp SKILL.md ~/.claude/commands/imessage.md
cp scripts/imessage-reader.py ~/.claude/scripts/imessage-reader.py

# Claude Code (project)
cp SKILL.md .claude/skills/imessage.md
mkdir -p .claude/scripts
cp scripts/imessage-reader.py .claude/scripts/imessage-reader.py
```

## What it does

- Reads conversations from `~/Library/Messages/chat.db`
- Decodes all message types: iMessage, SMS, and RCS (including `attributedBody` blobs with multi-byte length encoding)
- Resolves contact names from the macOS AddressBook database
- Filters by contact name, phone number, or group chat name
- Filters by date (today, last N days, specific date)
- Surfaces attachments inline as `[attachment: <mime>, <absolute_path>]` so the agent can read the file directly (filters out link-preview rows automatically)
- Optional `--convert-heic` flag to auto-convert HEIC to JPEG via `sips`, with idempotent caching at `/tmp/imessage-attachments/<rowid>-<basename>.jpg`
- Read-only database access, zero external dependencies (Python stdlib only)

## Usage

Once installed, ask your agent naturally:

- "Read my messages with Mom"
- "What did John text me today?"
- "Show me the Family group chat from last week"
- "Read my texts with +15551234567 from March 29"

The agent will run the script and present the conversation.

## Requirements

- macOS 14+
- Messages.app signed in
- Full Disk Access for your terminal (System Settings > Privacy & Security > Full Disk Access)
- Python 3.10+

## How the blob parser works

Apple's Messages database stores SMS/RCS message text in an `attributedBody` blob column using the [typedstream format](https://chrissardegna.com/blog/reverse-engineering-apples-typedstream-format/), not the plain `text` column. This skill decodes these blobs by:

1. Finding the `NSString` marker in the binary data
2. Locating the type marker (`0x2B` or `0x2A`)
3. Reading the length (single byte for messages up to 127 chars, `0x81` + 2-byte little-endian for longer messages)
4. Extracting exactly that many bytes as UTF-8 text

## License

MIT
