# tokenograph: brief for agents and humans starting here

Read this before touching the code. It is the distilled version of the conversation
that built the tool, kept so that the next session, human or model, does not relearn it.
The long form with evidence is in `docs/field-notes.md`.

## What this is

Tokenometrics for coding-agent sessions. It reads Claude Code transcripts and pi session
files and renders one page: throughput, an additive wall-clock split, token and cost
accounting, a per-call activity timeline, a ledger of what is in the context window and
what each part has cost, cache rebuilds with causes, a deferred-vs-direct tool-loading
comparison, a re-sent-history measure, a property-graph export, and a fleet view with
herdr's agent states. Python standard library only. No build step.

```
python3 -m tokenograph list
python3 -m tokenograph build latest -o panel.html
python3 -m tokenograph serve latest --open
python3 -m tokenograph fleet --serve --open
python3 -m tokenograph graph latest -o s.graphml
python3 -m tokenograph json latest --pretty
python3 -m unittest discover -s tests          # must pass before any push
```

## Layout

```
tokenograph/__init__.py   everything: adapters, phases, ledger, graph, fleet, CLI
tokenograph/panel.html    session page; __TOKENOGRAPH_DATA__ is replaced with the JSON payload
tokenograph/fleet.html    fleet page
examples/make_sample.py   synthetic transcript generator with ground-truth timings (the test oracle)
tests/                    unittest; hand-built transcripts for both formats, a fake herdr socket
docs/field-notes.md       evidence, method, paper notes, the numbers behind the claims
```

## Invariants: do not break these

- **Standard library only.** The value of the tool is that it runs anywhere with `python3`.
- **Measured and estimated stay distinct.** Timestamps, usage counters, cache reads and
  writes, thinking tokens and pi's cost are measured. The prefill/decode split, token
  counts derived from characters, image tokens and dollar cost are estimates and carry a
  `~` in the panel and an explanation in the footer. New numbers follow the same rule.
- **The wall-clock split is additive.** prefill + reasoning + generation + tools +
  compaction + idle equals wall clock, always. A test checks it.
- **Ledger estimates never exceed the measured window.** Per request they are scaled to
  fit; the residual is reported as "tool schemas & unmeasured", floored by the tool block
  inferred from cache retention.
- **Nothing from a real transcript is committed.** Tests use synthetic or hand-built
  data. Transcripts contain commands, file contents and sometimes secrets.
- **Adapters are auto-detected**, format forced with `--format`. Adding an adapter means a
  `_scan_<name>` that fills the same `ctx` structure; everything downstream is shared.

## Facts about the inputs that took effort to learn

Claude Code transcripts, `~/.claude/projects/<project>/<session>.jsonl`:

- One entry per streamed content block, each with its own timestamp and the request's
  usage. Blocks of one response share `requestId`. Grouping by it is how requests are
  rebuilt; the thinking block's timestamp is the end of thinking.
- Tools start as soon as their block has streamed, so a tool result can appear in the file
  between two blocks of the same request. A request starts at the running maximum
  timestamp of non-assistant entries before its first block.
- `prompt_snapshot` attachments carry the full system prompt as a list of sections. Every
  injected reminder or listing (`skill_listing`, `mcp_instructions_delta`,
  `deferred_tools_delta`, task and budget reminders) arrives as an `attachment` with a
  `rendered` field that is the exact text sent. `deferred_tools_record` entries are the
  tool definitions the API expanded from a `ToolSearch` result; they are not rendered but
  they are in the prompt.
- A `Skill` tool result is tiny; the skill's text arrives as an `isMeta` user entry.
- `output_tokens_details.thinking_tokens` is reported. Thinking is re-sent while the turn
  continues and counted; the ledger keeps it until the next user prompt.
- Compaction: `system` entries with `subtype: compact_boundary`, then a user entry with
  `isCompactSummary`. Interrupts are user entries starting with `[Request interrupted`.
- `cache_creation.ephemeral_1h_input_tokens` tells you the TTL the writes used.
- Image tokens are not reported anywhere; they are estimated from pixel dimensions parsed
  out of the base64 header, roughly width times height over 750 after the model's downscale.

pi sessions, `~/.pi/agent/sessions/<cwd>/<stamp>_<id>.jsonl`:

- One entry per message. For assistant messages `message.timestamp` is the request start
  and the entry's `timestamp` is the end. Tools run sequentially after the message.
- No system prompt and no per-block timing, so prefill is a latency fit and the system
  prompt shows up under "tool schemas & unmeasured". `usage.cost.total` is pi's own cost
  and matches this tool's estimate to the cent on pi's fixtures.

herdr: newline-delimited JSON over `~/.config/herdr/herdr.sock` (or `$HERDR_SOCKET_PATH`);
`{"id":..,"method":"agent.list","params":{}}` returns each pane's agent, status
(`working|blocked|done|idle|unknown`) and `agent_session.value`, the agent's own session
id that herdr's integrations report at session start. The fleet joins on that.

## Lessons that change how you should work

1. **Prompt caching is prefix matching**: tools, then system prompt, then messages. One
   changed byte invalidates everything after it. A system-prompt section that the harness
   rewrote mid-session recomputed 254k tokens of a 347k window in one request. On Fable
   5.1 that is a five-dollar event, one sixth of the session's bill at the time. Avoid
   environment changes mid-session (adding repos, connecting servers, changing modes),
   compact before a known change, and treat a large window as capital at risk. The panel
   names the changed section when it happens.
2. **Under caching, replayed history is cheap until it is not.** Cache reads on Fable 5.1
   cost 2.5% of computed tokens. In the session that built this tool, 79% of a 726k
   window was content older than 20 requests, re-sent 28 million token-times for about
   $7 of a $126 session. The costs of replay are rebuild exposure, window headroom and
   attention, not the per-token bill. Optimize for those.
3. **Deferred tool loading wins by keeping unused tools out**, not by making used tools
   cheaper. A solo `ToolSearch` round trip costs a request; a search batched with real
   calls costs almost nothing. With 396 deferred tools and 9 used, deferral saved on the
   order of a million effective tokens. The panel computes this per session.
4. **Heredoc file writes live in the window.** Writing source files through `cat <<EOF`
   in Bash puts the whole file into tool inputs that are re-sent every request. In the
   build session tool inputs were 150k of the window. Prefer writing large files once,
   and know that Write and Edit carry the same content.
5. **Structure is compression.** Extraction-based graph memory lost to flat retrieval on
   verbatim recall (LongMemEval, Rusu et al. 2026); state-delta memory cut GUI-agent
   prompt tokens threefold and its quality moved success by nine points (MGA, Cheng et
   al. 2026). If you build memory for agents, measure the verbatim-recall regression and
   the replay cost. This tool measures the second; the first needs an eval harness.

## Working conventions

- Run the tests before every push. Build the panel on a real session and look at it once
  in a browser or headless Chromium when the page changes.
- Rebuild `examples/sample-panel.png` when the panel's look changes:
  `python3 examples/make_sample.py --hours 16 --laps 99 --out /tmp/s.jsonl` then
  `python3 -m tokenograph build /tmp/s.jsonl -o /tmp/s.html` and screenshot it.
- Keep the pricing table dated (`PRICING_DATE`) and let `--price` override it.
- The panel is deliberately single-theme dark, matching the reference it was built from.
- When in doubt about what the transcript contains, print entries; the format is not
  documented anywhere but the files themselves, and it changes between CLI versions.
