# Using ccbridge from other AI coding tools

ccbridge's MCP server works in any tool that can start a local (stdio) MCP
server. `bridge.py` can write that tool's config for you.

```bash
python <ccbridge>/bridge.py clients                         # what is supported, and what is installed here
python <ccbridge>/bridge.py host sourabh --client claude-code,cursor
python <ccbridge>/bridge.py join <invite> alex --client all-installed
python <ccbridge>/bridge.py connect --client codex,grok     # later, in a project that ran host or join
```

`--client` takes a comma-separated list of the slugs below, or `all-installed`.
Without it, host and join set up Claude Code only, exactly as before.

## Every tool is a member of its own

Each tool in a project joins the room under **its own name**, with **its own
settings file**. With your name `sourabh`, Claude Code is `sourabh`, Cursor is
`sourabh-cursor`, Codex is `sourabh-codex`, and so on. Pass `connect --name` to
choose a name yourself. With several clients, `--name` replaces `sourabh` in
every name.

They can't share one name. Two tools under one name would share one place in the
room: each would take messages meant for the other, and each would hide the
other's messages as its own. Separate names also let them talk to each other:
Cursor can send Codex a message like any other member.

All of a machine's names are claimed with its one identity key, so nobody else
can take them.

- **Locked rooms:** if the host started with `--allow`, only the names it listed
  can get in. Give host the same `--client` list, which lets those names in too,
  or name them in `--allow`.
- **Peer filter:** a joiner who uses `--allow host` only accepts messages from
  the name `host`, not `host-cursor`.

## Where each tool's config goes

| Slug | Tool | Config file ccbridge writes | Scope | Checked |
|---|---|---|---|---|
| `claude-code` | Claude Code | `<project>/.mcp.json` (`mcpServers`) | project | tested here |
| `cursor` | Cursor | `<project>/.cursor/mcp.json` (`mcpServers`) | project | server started from the written file; not opened in Cursor |
| `vscode` | VS Code (GitHub Copilot) | `<project>/.vscode/mcp.json` (`servers`, `"type": "stdio"`) | project | server started from the written file; not opened in VS Code |
| `kiro` | Kiro | `<project>/.kiro/settings/mcp.json` (`mcpServers`) | project | server started from the written file; not opened in Kiro |
| `opencode` | opencode | `<project>/opencode.json` (`mcp`, `"type": "local"`, command as one array, `environment`) | project | tested here (`opencode mcp list`) |
| `grok` | Grok Build | `<project>/.grok/config.toml` (`[mcp_servers.ccbridge]`) | project | tested here (`grok mcp list`) |
| `gemini` | Gemini CLI | `<project>/.gemini/settings.json` (`mcpServers`) | project | configured per docs, not tested here |
| `codex` | OpenAI Codex | `~/.codex/config.toml` (or `$CODEX_HOME`) | user-wide | configured per docs, not tested here |
| `antigravity` | Google Antigravity | `~/.gemini/antigravity/mcp_config.json` (`mcpServers`) | user-wide | configured per docs, not tested here |
| `windsurf` | Windsurf | `~/.codeium/windsurf/mcp_config.json` (`mcpServers`) | user-wide | configured per docs, not tested here |

These paths are the same on Windows, macOS and Linux. Each tool keeps its
config relative to the project or to your home folder. If one of them is
wrong, fix it in the `CLIENTS` table in
[`ccbridge/clients.py`](../ccbridge/clients.py): one line per tool.

"Tested here" means a project was set up with `host --local --client ... /
connect --client ...`, ccbridge's MCP server was started exactly as each
written file says, and the servers talked to each other over a real bridge. For
example, a Cursor→Grok DM arrived as from `sourabh-cursor`. For Grok and opencode,
the tool's own `mcp list` also showed the entry.

Cline, Zed, Amp, Continue and Roo aren't supported yet, because their formats
haven't been confirmed. Setting them up by hand works: copy the
command, the args and `CCBRIDGE_CONFIG` from any written file.

## What gets written

- **No secrets in any of them.** A tool's config holds only how to start the
  server (`python <ccbridge>/run_mcp_server.py`) and `CCBRIDGE_CONFIG`, the path
  of that tool's settings file in `<ccbridge>/.ccbridge_projects/`. On macOS and
  Linux, only you can read that file.
- **Project files are gitignored.** ccbridge adds `.cursor/mcp.json`,
  `.vscode/mcp.json`, `opencode.json` and the others it writes to the project's
  `.gitignore`, as it does for `.mcp.json`. Their entries point at files on this
  machine. If git already tracks one, leave the ccbridge change out of your
  commits.
- **Your other servers and settings are kept.** Only the `ccbridge` entry is
  added or replaced. Env keys you added to it yourself, such as
  `CCBRIDGE_CONFIRM_SENDS`, are kept.
- **A file ccbridge can't safely rewrite is left alone.** That covers JSON with
  comments (JSONC) and a TOML file that already has an `[mcp_servers.ccbridge]`
  table ccbridge didn't write. You get an error saying what to do.
- **User-wide configs** (Codex, Antigravity, Windsurf) are shared by all your
  projects:
  - Their entry is named after the project: `ccbridge-<project>-<hash>`. Each
    project gets its own entry and settings, and never depends on the folder
    the tool starts in.
  - The file is copied to `<file>.ccbridge-backup` before every change.
- **Codex:**
  - When the `codex` CLI is on PATH, ccbridge runs `codex mcp add`.
  - Otherwise it appends a block between `# >>> ccbridge <name> >>>` and
    `# <<< ccbridge <name> <<<` to `config.toml`. It never parses and rewrites the
    rest of the file. On a re-run it replaces only that block.
  - Grok's project file gets the same kind of block.

## Settings each tool's MCP server reads

Each tool's settings file holds the bridge settings: `CCBRIDGE_RELAY_URL`,
`CCBRIDGE_ROOM`, `CCBRIDGE_TOKEN`, `CCBRIDGE_NAME`, `CCBRIDGE_IDENTITY_KEY`, and
`CCBRIDGE_ALLOWED_PEERS` if set. It also holds:

- `CCBRIDGE_CLIENT`: the tool's slug.
- `CCBRIDGE_PROJECT_DIR`: the project folder, absolute.

Environment variables set in a tool's config still win over the file.

## Several tools on one computer

```bash
python <ccbridge>/bridge.py host sourabh --local --client all-installed
```

`--local` starts no tunnel: the bridge listens on 127.0.0.1 only, so only
programs on this computer can reach it. `--max-members N` caps how many members
the room lets in. It needs a relay version with member limits; until then,
host refuses the flag rather than ignoring it.

## Grok and Claude Code in one project

Grok also reads `.mcp.json`. When both files define `ccbridge`, Grok uses
the one in its own `.grok/config.toml`; that was checked here with `grok mcp list`.
So a project that ran `--client grok` keeps the two apart. Without it, Grok would
start Claude Code's entry and join as Claude Code's name. If you use Grok in a
project, give it its own entry with `connect --client grok`.
