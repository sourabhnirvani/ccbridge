# Using ccbridge from other AI coding tools

ccbridge's MCP server works in any tool that can start a local (stdio) MCP
server. `bridge.py` can write that tool's config for you.

```bash
python <ccbridge>/bridge.py clients                         # what is supported, and what is installed here
python <ccbridge>/bridge.py host sourabh --client claude-code,cursor   # prints a join link
python <ccbridge>/bridge.py join <join link> alex --client all-installed
python <ccbridge>/bridge.py connect --client codex,grok     # later, in a project that ran host or join
```

Everyone who joins with the link reaches everyone in the room: every person,
and every tool each of them connected. The longer invite string that host also
prints works wherever the link does.

`--client` takes a comma-separated list of the slugs below, or `all-installed`.
Without it, host and join set up Claude Code only, exactly as before.

## Every tool is a member of its own

Each tool in a project joins the room under **its own name**, with **its own
settings file**. With your name `sourabh`, Claude Code is `sourabh`, Cursor is
`sourabh-cursor`, Codex is `sourabh-codex`, and so on. Pass `connect --name` to
choose a name yourself. With several clients, `--name` replaces `sourabh` in
every name.

They can't share one name. Two tools under one name would share one place in
the room: each would take messages meant for the other, and each would hide
the other's messages as its own. Separate names also let them talk to each
other: Cursor can send Codex a message like any other member. All of a
machine's names are claimed with its one identity key, so nobody else can take
them. On relays that support it, each name is also registered with its tool
kind, so peers can see which tool each member is.

- **Locked rooms:** if the host started with `--allow`, only the names it listed
  can get in. Give host the same `--client` list, which lets those names in too,
  or name them in `--allow`. `connect` in a locked room says which name was refused.
- **Your own tools always hear each other.** With `--allow alex`, each tool in
  the project accepts messages from `alex` and from the project's other tools.
  A tool connected later is added to the earlier tools' lists too.
- **Peer filter:** a joiner who uses `--allow host` only accepts messages from
  the name `host`, not `host-cursor`. Name each tool in their `--allow`
  (`--allow host,host-cursor`).

## Where each tool's config goes

ccbridge writes each tool's **project** config wherever the tool has one. A
user-wide entry would start in every project's window, so two windows of one
tool would run two servers under one name.

| Slug | Tool | Config file ccbridge writes | Checked |
|---|---|---|---|
| `claude-code` | Claude Code | `.mcp.json` (`mcpServers`) | tested here |
| `cursor` | Cursor | `.cursor/mcp.json` (`mcpServers`, `"type": "stdio"`) | server started from the written file; not opened in Cursor |
| `vscode` | VS Code (GitHub Copilot) | `.vscode/mcp.json` (`servers`, `"type": "stdio"`) | server started from the written file; not opened in VS Code |
| `kiro` | Kiro | `.kiro/settings/mcp.json` (`mcpServers`) | server started from the written file; not opened in Kiro |
| `opencode` | opencode | `opencode.jsonc`, or `opencode.json` if that is the one the project has (`mcp.<name>`, `"type": "local"`, command as one array, `environment`, `enabled`) | tested here with opencode 1.17 (`opencode mcp list`) |
| `grok` | Grok Build | `.grok/config.toml` (`[mcp_servers.ccbridge]`) | tested here (`grok mcp list`) |
| `codex` | OpenAI Codex | `.codex/config.toml` (`[mcp_servers.ccbridge]`, `tool_timeout_sec = 120`) | per docs, not tested here. Codex reads it only in projects it trusts |
| `antigravity` | Google Antigravity | `.agents/mcp_config.json` (`mcpServers`) | per docs, not tested here |
| `devin` | Devin Desktop (formerly Windsurf) | `.devin/mcp_config.json` (`mcpServers`) | per docs, not tested here |
| `gemini` | Gemini CLI | `.gemini/settings.json` (`mcpServers`) | per docs, not tested here |
| `amp` | Amp | `.amp/settings.json` (`"amp.mcpServers"`) | per docs, not tested here |
| `junie` | Junie | `.junie/mcp/mcp.json` (`mcpServers`) | per docs, not tested here |
| `zed` | Zed | `.zed/settings.json` (`context_servers`) | per docs, not tested here |
| `windsurf` | Windsurf, older versions | `~/.codeium/windsurf/mcp_config.json`: user-wide, and only if that file already exists. Prefer `devin` | per docs, not tested here |

All paths except Windsurf's are inside the project, so they are the same on
Windows, macOS and Linux. If one of them is wrong, fix it in the `CLIENTS` table
in [`ccbridge/clients.py`](../ccbridge/clients.py): one line per tool.

"Tested here" means a project was set up with `host --local --client ...` and
`connect --client ...`, ccbridge's MCP server was started exactly as each
written file says, and the servers talked to each other over a real bridge. For
example, a Cursor→Grok DM arrived as from `sourabh-cursor`. For Grok and
opencode, the tool's own `mcp list` also showed the entry.

`all-installed` picks `devin` over `windsurf` when both are found. Cline isn't
supported: it only has a user-wide config. Roo Code is no longer maintained.
For any tool not listed, copy the command, the args and `CCBRIDGE_CONFIG` from
a written file into its MCP settings.

## What gets written

- **No secrets in any of them.** A tool's config holds only how to start the
  server (`python <ccbridge>/run_mcp_server.py`) and `CCBRIDGE_CONFIG`, the path
  of that tool's settings file in `<ccbridge>/.ccbridge_projects/`. On macOS and
  Linux, only you can read that file.
- **Nothing is pre-approved.** ccbridge never writes `autoApprove`,
  `alwaysAllow`, `trust` or anything like them. Its send and share tools stay
  behind each tool's own approval prompt.
- **Project files are gitignored.** ccbridge adds `.cursor/mcp.json`,
  `.vscode/mcp.json`, `opencode.jsonc` and the others it writes to the
  project's `.gitignore`, as it does for `.mcp.json`. Their entries point at
  files on this machine. If git already tracks one, ccbridge points that out:
  leave the ccbridge change out of your commits.
- **Your other servers and settings are kept.** Only the `ccbridge` entry is
  added or replaced. Env keys you added to it yourself, such as
  `CCBRIDGE_CONFIRM_SENDS`, are kept.
- **A file ccbridge can't safely rewrite is left alone.** That covers JSON with
  comments (JSONC), and a TOML file that already has an `[mcp_servers.ccbridge]`
  table ccbridge didn't write. You get an error saying what to do.
- **Codex and Grok:** ccbridge adds a block between `# >>> ccbridge <name> >>>`
  and `# <<< ccbridge <name> <<<` to the TOML file. It never parses and
  rewrites the rest. A re-run replaces only that block.
- **Old Windsurf:** its user-wide file is shared by all your projects, so the
  entry is named `ccbridge_<project>_<hash>` (lowercase letters, digits and
  single underscores, which every tool accepts). The file is copied to
  `<file>.ccbridge-backup` before every change.

## Limits to know about

- **One window per project.** Two windows of one tool on the same project run
  two copies of its server under one name.
- **Old Windsurf** starts every connected project's entry in every window. Use
  `devin` where you can.
- **Moving a project:** Claude Code's settings follow the project. Run
  `connect` again for every other tool after a move, so they get the new
  folder.
- **Grok and Claude Code in one project:** Grok also reads `.mcp.json` and
  `.cursor/mcp.json`. When those and `.grok/config.toml` all define
  `ccbridge`, Grok uses its own file. That was checked here with `grok mcp list`.
  So a project that ran `--client grok` keeps them apart. Without it, Grok would
  start Claude Code's (or Cursor's) entry and join under that tool's name. If
  you use Grok in a project, run `connect --client grok`.

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
programs on this computer can reach it. The join link is then
`http://127.0.0.1:<port>/j/<code>`. `--max-members N` caps how many members the
room lets in. Every tool counts as a member. It needs a relay version with
member limits; until then, host refuses the flag rather than ignoring it.
