# Field notes

What was learned building tokenograph, with the numbers. Written so that a reader who was
not in the room can check each claim against a session of their own. Everything here was
measured on the session that built the tool (Claude Code on Claude Fable 5.1, 4 September
2026) unless a source is named. Where a number is an estimate the method is stated.

## 1. Anatomy of a cache rebuild

The API renders a request as tools, then the system prompt, then messages, and prompt
caching is prefix matching over that sequence. Any changed byte invalidates everything
after it.

At 17:15:01 UTC one request reported `cache_read_input_tokens` of 38,641 and
`cache_creation_input_tokens` of 254,409, where the previous request had read 347,193 from
cache. The transcript records the system prompt in `prompt_snapshot` attachments, and the
two snapshots on either side of that request differ in exactly one section, "# Your
current remote execution environment", by 232 characters: the harness rewrote the
instruction for answering "which model are you". Nothing in the conversation caused it.

What survived, 38.6k tokens, is the tool block, because it precedes the system prompt.
That number is now used as the floor for the "tool schemas & unmeasured" ledger line,
since the transcript never records the built-in tool definitions.

Cost on Fable 5.1: cache writes at the one-hour TTL are billed at 2× the input price,
$20 per million tokens, against $0.25 per million for a cache read. 254k tokens times the
difference is about $5, one sixth of the session's bill at that point.

Avoidance, in order of leverage: keep the environment stable during long turns (adding
repositories, connecting MCP servers, changing permission modes are the events that
regenerate that section); compact deliberately before a known change; start a fresh
session after one instead of paying to rebuild a large window; watch the rebuild list in
the panel, which names the section and its size delta. The structural fix is upstream:
the API supports mid-conversation system messages that do not invalidate the prefix, and
volatile environment text belongs there.

The one-hour TTL also means an idle gap longer than an hour rebuilds the window on the
next request. The panel's rebuild list reports that cause when it applies.

## 2. Deferred versus direct tool loading, measured

Claude Code lists most tools by name only and loads a schema when the model calls
`ToolSearch`. Three quantities decide whether that is cheaper than loading directly, and
all three are in the transcript.

- The listing rides on every request. In the build session: 7.8k tokens on 53 requests,
  nearly all served from cache.
- A search that had a request to itself costs that request's output plus the newly
  computed input of the next request, and its latency: 2.2k tokens and 10.6 seconds for
  the first pair of tools loaded. A search batched with other tool calls costs only the
  search block and its result, tens of tokens.
- A loaded schema is re-sent on every later request, exactly as a directly loaded one
  would have been from request one.

With cache reads weighted at the model's multiplier, deferral cost slightly more for the
9 tools that were used and saved on the order of a million effective tokens by keeping
387 unused tools out of every prompt. Unloaded schemas are sized from the average of the
loaded ones because the transcript never sees them; that is the one estimate in the
comparison and the panel says so.

Practical reading: batch schema fetches with real work, and never load an MCP server's
whole tool set directly.

## 3. Re-sent history, and why the token count misleads

Every request in a Claude Code session replays the whole conversation since the last
compaction. The ledger reports the part of the window that entered more than 20 requests
ago and is still being sent.

| session that built the tool | value |
|---|---|
| tokens in the window | 726k |
| older than 20 requests, still re-sent | 570k, 79% |
| re-sent over the session, token-times | 28M |
| cost at cache-read prices | about $7 of a $126 session |

Replay is cheap while the cache holds, because a cache read on this model costs 2.5% of a
computed token. The costs of replay are exposure and headroom: a rebuild recomputes all
of it at once, and a full window forces a compaction that is itself lossy. That is the
economic case for distilled memory under caching, and it is not the case the memory
literature makes, which counts raw tokens.

The largest single category in the same window was tool inputs at 150k tokens, almost
all of it source files written through Bash heredocs. Every one of those files is
re-sent on every later request. The Write tool would cost the same; the lesson is to
write large files once and not to iterate on them inline.

## 4. Two papers, read against the ledger

### Selective Forgetting: A Graph-Based Memory Framework for Long-Term LLM Agents

Rusu, Khanzadeh and Alalfi, Toronto Metropolitan University, arXiv 2608.28978, 29 August
2026. Each conversational turn is extracted by GPT-4o-mini into typed nodes and edges;
retrieval takes the top-5 nodes by cosine and expands two hops; every 400 turns nodes
scoring low on recency, access frequency, degree and age are pruned. Evaluated on
LongMemEval, 500 questions.

| comparison | token F1 | judged correctness |
|---|---|---|
| graph vs flat vector store, overall | 0.417 vs 0.468, paired CI excludes zero | 0.454 vs 0.536 |
| recalling a prior assistant turn | 0.575 vs 0.774 | 0.607 vs 0.911 |
| temporal reasoning, the only win | 0.328 vs 0.334 | 0.293 vs 0.278 |
| forgetting vs none, 27k-node graph | +0.001, CI includes zero | −1.6 points, CI allows −3.8 |

The mechanism is that extraction is lossy compression: decomposing a turn into entities
discards the surface form that verbatim questions depend on. Things to hold against the
abstract: the "matched budget" is five retrieval roots on each side, not matched tokens,
and neither side's context size is reported; the forgetting experiment has no
random-pruning control, so a 10% prune with no loss is also consistent with 10% of the
graph being junk; the forgetting run sits in a regime where absolute scores have halved
from interference. The authors state most of this themselves. The result fits a pattern
across GraphRAG, HippoRAG and Mem0 comparisons: structure helps relational and temporal
questions and hurts verbatim recall. Moderately established, not settled.

### MGA: Memory-Driven GUI Agent for Observation-Centric Interaction

Cheng, Liu, Sun, Shi, Chen and Wang, ShanghaiTech, Tongji, ECUST and Shanghai AI
Laboratory, arXiv 2510.24168v3, April 2026. An Observer reads the screen with intent
inference forbidden; a Memory agent verifies each action against before and after
screenshots and appends a validated state delta to an append-only chain; a Planner decides
from instruction, observation and chain; a Grounder executes. Evaluated on OSWorld.

| result | number |
|---|---|
| OSWorld overall, 50 steps, GPT-5 planner | 64.7% vs OS-Symphony 63.6% |
| ablation, Workflow domain | full 56.3%, no memory 39.0%, no observer 46.0% |
| memory model Qwen3-8B to GPT-5 | 47.7% to 56.3% |
| tokens per task vs OS-Symphony | 226k vs 599k, with more steps, 19.3 vs 15.2 |
| memory module's share of tokens | 42.6% |

The strong parts are the ablation and the swap, not the 1.1-point lead, which has no
confidence interval on 369 tasks. Prompt tokens fall threefold because history is
distilled, and memory quality alone moved success by nine points. Caveats: the Observer
is fine-tuned on GUICourse and the baselines are not; comparisons mix backbones and step
budgets; the cost figures ignore prompt caching entirely, which section 3 shows is the
dominant term.

### What both miss

Both treat replayed history as tokens to eliminate. Under caching it is exposure to
eliminate. The measurement that decides whether distilled memory pays for itself in a
given harness is the re-sent-history line plus the rebuild list, and tokenograph reports
both.

## 5. How the estimates are made

- Characters to tokens: two rates, one for tool traffic and one for prose, fitted on
  requests served from a warm cache where the API's computed tokens equal the new content
  since the previous request. Pairs containing images are excluded from the fit because
  image tokens are themselves estimated. The prose rate is measured on the first request
  when the tool block is known from a rebuild, otherwise it is a prior of 3.5.
- Prefill versus decode: the visible-text tail after a thinking block is pure decode of a
  known number of tokens; pooled, it gives the decode speed; each request's thinking phase
  is then split into decode time and prefill. Without per-block timing, a least-squares
  latency fit over the session's requests. Both are marked `~`.
- Images: dimensions from the PNG, JPEG, GIF or WebP header inside the base64, scaled to
  the model's limit, then width times height over 750. Hi-res models get the larger cap.
- Cost: a dated table of list prices with cache read and write multipliers; the API's own
  split between 5-minute and 1-hour writes is used. pi's recorded cost is shown alongside
  and agrees to the cent on pi's fixtures for the models both cover.
- Attribution: each request's input bill is split across categories by the tokens they
  contributed, new content first. It is the only split the usage counters support.

## 6. Avenues worth exploring

Ordered by how much they change what the tool can tell you.

1. **Exact token counts.** The Messages API has a token-counting endpoint. A `calibrate`
   command that counts a sample of items would replace the character model for Claude
   sessions and settle the image-token question. Needs an API key; optional.
2. **Adapters with real timing.** llama.cpp and SGLang report prompt and generation
   timings per request, which is what the original panel's tg/s and pp/s came from. A
   server-log adapter would make those measured rather than estimated for local models.
   Codex, OpenCode and Gemini CLI sessions are the obvious next transcript formats.
3. **Advice, not only accounting.** A command that reads a session and emits the three
   or four actions with the highest expected saving: which tool results dominate the
   window, whether a compaction is due before a planned environment change, which tools
   should be deferred given their usage across the fleet.
4. **Harness hooks.** A Claude Code Stop hook that appends the session's cost line and
   rebuild count to the terminal; a PostToolUse hook that warns when a single result
   exceeds a token threshold; herdr's `notification.show` for rebuild alerts in the pane.
5. **Fleet economics over time.** Cost per repository, branch and day; time-to-compaction
   prediction from the window's growth rate; a deferral list recommended from tool usage
   frequency across all sessions.
6. **Experiments the graph export enables.** Simulate a forgetting policy on your own
   sessions and see what it would evict and how much window it frees; find which tool
   results are still being paid for twenty requests later; score compaction fidelity by
   comparing what a summary kept against what was later re-read.
7. **Shareable, scrubbed sessions.** A `scrub` command that keeps sizes, timestamps and
   tool names but blanks content, so panels and bug reports can be shared without leaking
   commands, and so a corpus of real session shapes can back regression tests.
8. **Redaction in the panel.** Tool labels in the panel today are the first line of the
   command or path. A `--redact` flag before sharing a built page.
