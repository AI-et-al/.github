#!/usr/bin/env python3
"""lapboard: a telemetry panel for long-horizon Claude Code sessions.

Reads Claude Code session transcripts (~/.claude/projects/<project>/<session>.jsonl)
and renders one self-contained page with

  throughput   tg/s (completion tokens per decode second) and pp/s (computed
               prompt tokens per prefill second)
  laps         one lap per user prompt, with the average lap duration
  time         wall clock split into prefill / reasoning / generation / tools /
               compaction / idle; the split is additive and sums to wall clock
  tokens       prompt tokens (computed vs. served from cache), completion,
               reasoning, tool results
  activity     a per-row timeline of every call: laps, assistant phases, each
               tool by name, compactions, idle stretches

Only the Python standard library is required (3.8+).

    python3 lapboard.py list
    python3 lapboard.py build latest -o panel.html
    python3 lapboard.py serve latest --port 8787
    python3 lapboard.py json  <session-id-or-path>

What is measured and what is estimated: the transcript records a wall-clock
timestamp for every content block the model streams back and exact token
usage per request, but not the API's time to first token. Everything except
the prefill/decode split is measured. That split is estimated from the decode
speed observed on visible text that follows a thinking block (see README).
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.server
import json
import os
import re
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from pathlib import Path

__version__ = "0.1.0"
HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "panel.html"
DATA_MARKER = "__LAPBOARD_DATA__"
DEFAULT_WINDOW = 200_000
LARGE_WINDOW = 1_000_000
INTERRUPT_PREFIX = "[Request interrupted by user"

# --------------------------------------------------------------------------- utils

_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_ts(value):
    """ISO-8601 timestamp -> epoch seconds (float), or None when absent/invalid."""
    if not value or not isinstance(value, str):
        return None
    m = _TS_RE.match(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    try:
        base = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), micro, tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    offset = 0
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset = sign * (int(digits[:2]) * 3600 + int(digits[2:4]) * 60)
    return base.timestamp() - offset


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def projects_dir() -> Path:
    return claude_dir() / "projects"


def text_of(content) -> str:
    """Flatten message content (string or block list) to the text it carries."""
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict):
                kind = block.get("type")
                if kind == "text":
                    out.append(block.get("text") or "")
                elif kind == "tool_result":
                    out.append(text_of(block.get("content")))
    return "\n".join(out)


_TAG_RE = re.compile(r"<[^>]{1,80}>")
_WS_RE = re.compile(r"\s+")


def clean_prompt(text: str, limit: int = 140) -> str:
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


_LABEL_KEYS = ("command", "file_path", "pattern", "query", "url", "path", "notebook_path",
               "skill", "subagent_type", "prompt", "description", "message", "text")


def tool_label(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in _LABEL_KEYS:
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:140]
    return ""


# --------------------------------------------------------------------------- sessions

def iter_sessions():
    root = projects_dir()
    if not root.is_dir():
        return []
    found = []
    for proj in sorted(root.iterdir()):
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            found.append({"path": f, "project": proj.name, "session_id": f.stem,
                          "size": st.st_size, "mtime": st.st_mtime})
    found.sort(key=lambda s: s["mtime"], reverse=True)
    return found


def first_prompt(path: Path, max_lines: int = 400) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary"):
                    continue
                content = (e.get("message") or {}).get("content")
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    continue
                txt = clean_prompt(text_of(content), 90)
                if txt and not txt.startswith(INTERRUPT_PREFIX):
                    return txt
    except OSError:
        pass
    return ""


def resolve_session(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.is_file():
        return p
    if p.is_dir():
        files = sorted(p.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if files:
            return files[0]
        raise SystemExit(f"no .jsonl transcripts in {p}")
    sessions = iter_sessions()
    if arg == "latest":
        if not sessions:
            raise SystemExit(f"no sessions found under {projects_dir()}")
        return sessions[0]["path"]
    matches = [s for s in sessions if s["session_id"].startswith(arg)]
    if len(matches) == 1:
        return matches[0]["path"]
    if not matches:
        raise SystemExit(f"no session matching {arg!r} under {projects_dir()} (try: lapboard.py list)")
    raise SystemExit("ambiguous session id, matches:\n  " + "\n  ".join(str(m["path"]) for m in matches))


def subagent_files(session_path: Path):
    """Transcripts of subagents spawned by this session (newer Claude Code layouts)."""
    side = session_path.parent / session_path.stem
    if not side.is_dir():
        return []
    return sorted(f for f in side.rglob("*.jsonl") if f.is_file())


# --------------------------------------------------------------------------- reader

class TranscriptReader:
    """Incremental JSONL reader that tolerates a file still being appended to."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.buf = b""
        self.entries = []
        self.size = 0
        self.mtime = 0.0

    def refresh(self) -> bool:
        try:
            st = self.path.stat()
        except OSError:
            return False
        if st.st_size < self.offset:  # truncated or replaced: start over
            self.offset, self.buf, self.entries = 0, b"", []
        if st.st_size == self.offset:
            return False
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        data = self.buf + chunk
        lines = data.split(b"\n")
        self.buf = lines.pop()  # possibly partial last line
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                self.entries.append(json.loads(raw))
            except ValueError:
                continue
        self.size, self.mtime = st.st_size, st.st_mtime
        return True


# --------------------------------------------------------------------------- analysis

def _new_ctx():
    return {"models": [], "tools": [], "prompts": [], "interrupts": [], "errors": [],
            "compact_boundaries": [], "compact_summaries": [], "meta": {}, "open_tools": [],
            "tool_chars": 0}


def _scan(entries, sub: bool, ctx: dict, stream: str = "main"):
    """One pass over a transcript stream, in file order."""
    last_input_ts = None   # running max timestamp of everything that is not an assistant block
    last_any_ts = None     # running max timestamp of everything
    requests = {}
    pending_tools = {}
    meta = ctx["meta"]

    for idx, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        kind = e.get("type")
        ts = parse_ts(e.get("timestamp"))
        if not sub and len(meta) < 4:
            for key, src in (("session_id", "sessionId"), ("cwd", "cwd"), ("git_branch", "gitBranch"),
                             ("cli_version", "version")):
                if key not in meta and e.get(src):
                    meta[key] = e[src]

        if kind == "assistant":
            msg = e.get("message") or {}
            if e.get("isApiErrorMessage"):
                ctx["errors"].append({"k": "e", "t0": ts, "t1": ts, "sub": int(sub),
                                      "l": clean_prompt(text_of(msg.get("content")), 160)})
                if ts is not None:
                    last_any_ts = ts if last_any_ts is None else max(last_any_ts, ts)
                continue
            rid = e.get("requestId") or msg.get("id") or e.get("uuid") or f"{stream}:{idx}"
            call = requests.get(rid)
            if call is None:
                call = {"k": "m", "rid": rid, "t0": last_input_ts if last_input_ts is not None else ts,
                        "t1": ts, "blocks": [], "usage": None, "model": msg.get("model"),
                        "sub": int(sub), "err": 0, "stop": None, "think_chars": 0, "text_chars": 0,
                        "tools": [], "stream": stream}
                requests[rid] = call
                ctx["models"].append(call)
            if ts is not None:
                call["t1"] = ts if call["t1"] is None else max(call["t1"], ts)
                last_any_ts = ts if last_any_ts is None else max(last_any_ts, ts)
            if msg.get("stop_reason"):
                call["stop"] = msg["stop_reason"]
            if isinstance(msg.get("usage"), dict):
                call["usage"] = msg["usage"]
            if msg.get("model"):
                call["model"] = msg["model"]
            content = msg.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt in ("thinking", "redacted_thinking"):
                    call["think_chars"] += len(b.get("thinking") or "")
                    call["blocks"].append((ts, "thinking"))
                elif bt == "text":
                    call["text_chars"] += len(b.get("text") or "")
                    call["blocks"].append((ts, "text"))
                elif bt == "tool_use":
                    name = b.get("name") or "tool"
                    call["blocks"].append((ts, "tool_use"))
                    call["tools"].append(name)
                    tc = {"k": "t", "n": name, "t0": ts, "t1": None, "l": tool_label(b.get("input")),
                          "sub": int(sub), "err": 0, "ch": 0, "id": b.get("id"), "open": 0}
                    if b.get("id"):
                        pending_tools[b["id"]] = tc
                    ctx["tools"].append(tc)
            continue

        prev_any_ts = last_any_ts
        if ts is not None:
            last_input_ts = ts if last_input_ts is None else max(last_input_ts, ts)
            last_any_ts = ts if last_any_ts is None else max(last_any_ts, ts)

        if kind == "user":
            msg = e.get("message") or {}
            content = msg.get("content")
            results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            if results:
                for b in results:
                    tc = pending_tools.pop(b.get("tool_use_id"), None)
                    chars = len(text_of(b.get("content")))
                    ctx["tool_chars"] += chars
                    if tc is None:
                        continue
                    tc["t1"] = ts
                    tc["err"] = int(bool(b.get("is_error")))
                    tc["ch"] = chars
                continue
            if e.get("isCompactSummary"):
                ctx["compact_summaries"].append({"ts": ts, "idx": idx, "stream": stream})
                continue
            if e.get("isMeta"):
                continue
            txt = text_of(content).strip()
            if not txt:
                continue
            if txt.startswith(INTERRUPT_PREFIX):
                ctx["interrupts"].append({"k": "x", "t0": ts, "t1": ts, "sub": int(sub), "l": txt[:80]})
                continue
            if not sub:
                ctx["prompts"].append({"k": "p", "t0": ts, "t1": ts, "l": clean_prompt(txt), "ch": len(txt)})
        elif kind == "system" and e.get("subtype") == "compact_boundary":
            cm = e.get("compactMetadata") or {}
            ctx["compact_boundaries"].append({"ts": ts, "idx": idx, "prev": prev_any_ts, "stream": stream,
                                              "pre": cm.get("preTokens"), "trigger": cm.get("trigger")})

    ctx["open_tools"].extend(pending_tools.values())


def _usage_numbers(call):
    u = call.get("usage") or {}
    inp = int(u.get("input_tokens") or 0)
    cc = int(u.get("cache_creation_input_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    details = u.get("output_tokens_details") or {}
    think = details.get("thinking_tokens")
    think_reported = isinstance(think, (int, float))
    if think_reported:
        think = int(think)
    else:
        total_chars = call["think_chars"] + call["text_chars"]
        think = int(round(out * call["think_chars"] / total_chars)) if total_chars else 0
    think = max(0, min(out, think))
    return inp, cc, cr, out, think, think_reported


def _phase_split(models):
    """Assign each model call a (prefill, reasoning, generation) duration.

    Measured: request latency (trigger -> last block), the end of thinking (thinking
    block timestamp) and the visible-text tail after it. Estimated: decode speed
    from those visible tails, which then separates prefill from thinking decode.
    Returns the decode speed used, or None when it could not be observed.
    """
    vis_tok = vis_dur = 0.0
    for c in models:
        think_end = c["_think_end"]
        if think_end is not None and c["t1"] > think_end + 0.05 and c["_out"] > c["_think"]:
            vis_tok += c["_out"] - c["_think"]
            vis_dur += c["t1"] - think_end
    tg = vis_tok / vis_dur if (vis_dur >= 2.0 and vis_tok >= 50) else None

    for c in models:
        lat = max(0.0, c["t1"] - c["t0"])
        think_end = c["_think_end"]
        if tg:
            if think_end is not None:
                first = min(lat, max(0.0, think_end - c["t0"]))
                tokens = c["_think"] if c["_think"] > 0 else max(0, c["_out"] - c["text_chars"] // 4)
                think_dec = min(first, tokens / tg)
                prefill = first - think_dec
                reasoning = think_dec
                generation = max(0.0, lat - first)
            else:
                dec = min(lat, c["_out"] / tg)
                prefill, reasoning, generation = lat - dec, 0.0, dec
        else:
            prefill = 0.0
            if think_end is not None:
                first = min(lat, max(0.0, think_end - c["t0"]))
                reasoning, generation = first, max(0.0, lat - first)
            else:
                reasoning, generation = 0.0, lat
        c["ph"] = (prefill, reasoning, generation)
    return tg


def _partition(intervals, t_start, t_end):
    """Additive wall-clock split. intervals: (a, b, kind, priority)."""
    events = []
    for a, b, kind, pr in intervals:
        if a is None or b is None or b <= a:
            continue
        events.append((max(a, t_start), 1, pr, kind))
        events.append((min(b, t_end), -1, pr, kind))
    events.sort(key=lambda x: (x[0], x[1]))
    active = {}
    totals = defaultdict(float)
    idle = []
    cur = t_start
    for t, d, pr, kind in events:
        if t > cur:
            if active:
                totals[max(active)[1]] += t - cur
            else:
                idle.append([cur, t])
                totals["idle"] += t - cur
            cur = t
        key = (pr, kind)
        active[key] = active.get(key, 0) + d
        if active[key] <= 0:
            del active[key]
    if t_end > cur:
        idle.append([cur, t_end])
        totals["idle"] += t_end - cur
    return totals, idle


def fmt_window(models, override):
    if override:
        return int(override)
    mx = 0
    for c in models:
        mx = max(mx, c["_in"] + c["_cc"] + c["_cr"])
        if "[1m]" in (c.get("model") or ""):
            return LARGE_WINDOW
    return LARGE_WINDOW if mx > DEFAULT_WINDOW else DEFAULT_WINDOW


def analyze(main_entries, sub_streams=(), *, title=None, context_window=None, live=False,
            source="", now=None, poll_ms=2500):
    now = now or time.time()
    ctx = _new_ctx()
    _scan(main_entries, False, ctx, "main")
    for name, entries in sub_streams:
        _scan(entries, True, ctx, name)

    models = [c for c in ctx["models"] if c["t1"] is not None]
    for c in models:
        if c["t0"] is None or c["t0"] > c["t1"]:
            c["t0"] = c["t1"]
        c["_in"], c["_cc"], c["_cr"], c["_out"], c["_think"], c["_think_rep"] = _usage_numbers(c)
        tks = [b[0] for b in c["blocks"] if b[1] == "thinking" and b[0] is not None]
        c["_think_end"] = max(tks) if tks else None
    tools = [t for t in ctx["tools"] if t["t0"] is not None]
    prompts = sorted((p for p in ctx["prompts"] if p["t0"] is not None), key=lambda p: p["t0"])
    interrupts = [x for x in ctx["interrupts"] if x["t0"] is not None]
    errors = [x for x in ctx["errors"] if x["t0"] is not None]

    # session bounds
    stamps = [c["t0"] for c in models] + [c["t1"] for c in models] + [p["t0"] for p in prompts] + \
             [t["t0"] for t in tools] + [t["t1"] for t in tools if t["t1"] is not None] + \
             [x["t0"] for x in interrupts + errors]
    for e in main_entries:
        if isinstance(e, dict):
            ts = parse_ts(e.get("timestamp"))
            if ts is not None:
                stamps.append(ts)
                break
    if not stamps:
        raise SystemExit("no timestamped entries found; is this a Claude Code transcript?")
    t_start = min(stamps)
    t_end = max(stamps)
    if live:
        t_end = max(t_end, now)

    for t in tools:
        if t["t1"] is None:
            t["open"] = 1
            t["t1"] = t_end if live else t["t0"]

    # compactions: boundary marker (newer CLI) and/or the summary message it produces
    compactions = []
    used_summaries = set()
    for b in ctx["compact_boundaries"]:
        t0 = b["prev"] if b["prev"] is not None else b["ts"]
        t1 = b["ts"]
        for i, s in enumerate(ctx["compact_summaries"]):
            if s["stream"] == b["stream"] and s["idx"] > b["idx"] and s["idx"] - b["idx"] <= 12 and s["ts"]:
                t1 = max(t1 or s["ts"], s["ts"])
                used_summaries.add(i)
                break
        if t0 is None or t1 is None:
            continue
        label = (b.get("trigger") or "compaction")
        if b.get("pre"):
            label += f" · {fmt_tokens(b['pre'])} tokens before"
        compactions.append({"k": "c", "t0": min(t0, t1), "t1": max(t0, t1), "l": label, "sub": 0})
    for i, s in enumerate(ctx["compact_summaries"]):
        if i in used_summaries or s["ts"] is None:
            continue
        prev = max([t for t in stamps if t < s["ts"]] or [s["ts"]])
        compactions.append({"k": "c", "t0": prev, "t1": s["ts"], "l": "compaction", "sub": 0})
    compactions.sort(key=lambda c: c["t0"])

    tg = _phase_split(models)

    # wall-clock partition (main-agent model phases win over subagents, over compaction, over tools)
    intervals = []
    for c in models:
        pf, rs, gn = c["ph"]
        pr = 6 if not c["sub"] else 5
        a = c["t0"]
        intervals.append((a, a + pf, "prefill", pr))
        intervals.append((a + pf, a + pf + rs, "reasoning", pr))
        intervals.append((a + pf + rs, c["t1"], "generation", pr))
    for c in compactions:
        intervals.append((c["t0"], c["t1"], "compaction", 4))
    for t in tools:
        intervals.append((t["t0"], t["t1"], "tools", 3))
    totals, idle_spans = _partition(intervals, t_start, t_end)

    prompt_ts = sorted(p["t0"] for p in prompts) + sorted(x["t0"] for x in interrupts)
    idle_out = []
    idle_wait = idle_over = 0.0
    for a, b in idle_spans:
        waiting = any(abs(b - pt) <= 1.0 for pt in prompt_ts) or (live and b >= t_end - 0.001)
        if waiting:
            idle_wait += b - a
        else:
            idle_over += b - a
        if b - a >= 0.5:
            idle_out.append([a, b, "w" if waiting else "o"])

    # laps
    laps = []
    for i, p in enumerate(prompts):
        t1 = prompts[i + 1]["t0"] if i + 1 < len(prompts) else t_end
        n_calls = sum(1 for c in models if not c["sub"] and p["t0"] <= c["t0"] < t1)
        n_tools = sum(1 for t in tools if not t["sub"] and p["t0"] <= t["t0"] < t1)
        laps.append({"n": i + 1, "t0": p["t0"], "t1": max(t1, p["t0"]), "calls": n_calls, "tools": n_tools,
                     "l": p["l"]})
    lap_of = lambda t: next((lp["n"] for lp in reversed(laps) if lp["t0"] <= t), None)

    # tokens
    tok_computed = sum(c["_in"] + c["_cc"] for c in models)
    tok_cache_write = sum(c["_cc"] for c in models)
    tok_cached = sum(c["_cr"] for c in models)
    tok_out = sum(c["_out"] for c in models)
    tok_think = sum(c["_think"] for c in models)
    think_rep = [c["_think_rep"] for c in models]
    tok_tool_est = int(round(ctx["tool_chars"] / 4.0))

    decode_time = totals["reasoning"] + totals["generation"]
    prefill_time = totals["prefill"]
    main_models = [c for c in models if not c["sub"]]
    last = max(main_models, key=lambda c: c["t1"]) if main_models else None
    window = fmt_window(models, context_window)
    ctx_used = (last["_in"] + last["_cc"] + last["_cr"]) if last else 0
    model_names = sorted({c["model"] for c in models if c.get("model")})
    wall = max(0.0, t_end - t_start)

    stats = {
        "tg_s": (tok_out / decode_time) if decode_time > 0 else None,
        "pp_s": (tok_computed / prefill_time) if prefill_time > 0 else None,
        "laps": len(laps),
        "avg_lap_s": (sum(lp["t1"] - lp["t0"] for lp in laps) / len(laps)) if laps else None,
        "time": {"wall": wall, "prefill": prefill_time, "reasoning": totals["reasoning"],
                 "generation": totals["generation"], "decode": decode_time, "tools": totals["tools"],
                 "compaction": totals["compaction"], "idle": totals["idle"], "idle_wait": idle_wait,
                 "idle_overhead": idle_over,
                 "tools_sum": sum(t["t1"] - t["t0"] for t in tools)},
        "tokens": {"total": tok_computed + tok_cached + tok_out, "prompt_computed": tok_computed,
                   "prompt_cache_write": tok_cache_write, "prompt_cached": tok_cached,
                   "completion": tok_out, "reasoning": tok_think, "tool_results_est": tok_tool_est,
                   "cache_hit_ratio": (tok_cached / (tok_cached + tok_computed)) if (tok_cached + tok_computed) else None},
        "context": {"used": ctx_used, "window": window, "pct": (ctx_used / window) if window else None},
        "counts": {"calls": len(models) + len(tools) + len(compactions), "assistant": len(models),
                   "assistant_sub": sum(1 for c in models if c["sub"]), "tools": len(tools),
                   "tools_sub": sum(1 for t in tools if t["sub"]), "tool_errors": sum(t["err"] for t in tools),
                   "compactions": len(compactions), "interrupts": len(interrupts), "errors": len(errors),
                   "prompts": len(prompts)},
    }

    base = t_start
    rel = lambda t: round(t - base, 3)
    calls = []
    for c in sorted(models, key=lambda c: c["t0"]):
        pf, rs, _ = c["ph"]
        calls.append({"k": "m", "t0": rel(c["t0"]), "t1": rel(c["t1"]),
                      "p": [rel(c["t0"] + pf), rel(c["t0"] + pf + rs)],
                      "tok": [c["_in"] + c["_cc"], c["_cr"], c["_out"], c["_think"]],
                      "lap": lap_of(c["t0"]), "sub": c["sub"], "err": c["err"], "stop": c["stop"],
                      "tools": c["tools"][:8], "m": c.get("model")})
    for t in sorted(tools, key=lambda t: t["t0"]):
        calls.append({"k": "t", "t0": rel(t["t0"]), "t1": rel(t["t1"]), "n": t["n"], "l": t["l"],
                      "lap": lap_of(t["t0"]), "sub": t["sub"], "err": t["err"], "ch": t["ch"], "open": t["open"]})
    for c in compactions:
        calls.append({"k": "c", "t0": rel(c["t0"]), "t1": rel(c["t1"]), "l": c["l"], "lap": lap_of(c["t0"])})
    for x in interrupts:
        calls.append({"k": "x", "t0": rel(x["t0"]), "t1": rel(x["t0"]), "l": x["l"], "lap": lap_of(x["t0"])})
    for x in errors:
        calls.append({"k": "e", "t0": rel(x["t0"]), "t1": rel(x["t0"]), "l": x["l"], "lap": lap_of(x["t0"])})

    per_tool = defaultdict(lambda: {"count": 0, "total_s": 0.0, "errors": 0})
    for t in tools:
        d = per_tool[t["n"]]
        d["count"] += 1
        d["total_s"] += t["t1"] - t["t0"]
        d["errors"] += t["err"]
    tools_out = [{"name": k, **v} for k, v in per_tool.items()]
    tools_out.sort(key=lambda d: (-d["count"], d["name"]))

    default_title = _title_from(prompts[0]["l"]) if prompts else (ctx["meta"].get("session_id") or "session")
    meta = {
        "title": title or default_title,
        "source": source,
        "session_id": ctx["meta"].get("session_id"),
        "cwd": ctx["meta"].get("cwd"),
        "git_branch": ctx["meta"].get("git_branch"),
        "cli_version": ctx["meta"].get("cli_version"),
        "models": model_names,
        "generated_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds"),
        "live": bool(live),
        "poll_ms": int(poll_ms),
        "subagent_streams": len(sub_streams),
        "estimates": {
            "decode_tok_s": tg,
            "prefill": "estimated" if tg else "not separable (no visible-text tail after thinking)",
            "reasoning_tokens": ("reported" if think_rep and all(think_rep) else
                                 "estimated" if not any(think_rep) else "mixed"),
        },
        "lapboard": __version__,
    }
    return {"meta": meta, "base": base, "end": rel(t_end), "stats": stats, "calls": calls,
            "laps": [{**lp, "t0": rel(lp["t0"]), "t1": rel(lp["t1"])} for lp in laps],
            "tools": tools_out, "idle": [[rel(a), rel(b), w] for a, b, w in idle_out]}


def _title_from(text: str, limit: int = 64) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return (cut if len(cut) >= limit // 2 else text[:limit].rstrip()) + "…"


# --------------------------------------------------------------------------- formatting

def fmt_tokens(n):
    n = float(n or 0)
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e5:
        return f"{n / 1e3:.0f}k"
    if n >= 1e3:
        return f"{n / 1e3:.1f}k"
    return str(int(n))


def fmt_duration(s):
    s = float(s or 0)
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    return f"{int(s // 3600)}h{int((s % 3600) // 60):02d}m"


# --------------------------------------------------------------------------- rendering

def render_html(payload, fragment=False):
    if not TEMPLATE_PATH.is_file():
        raise SystemExit(f"template not found: {TEMPLATE_PATH}")
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    title = (payload.get("meta") or {}).get("title") or "lapboard"
    safe = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    page = tpl.replace(DATA_MARKER, data).replace("<title>lapboard</title>", f"<title>{safe}</title>", 1)
    if not fragment:
        return page
    head = re.search(r"<!--head-->(.*?)<!--/head-->", page, re.S)
    body = re.search(r"<!--body-->(.*?)<!--/body-->", page, re.S)
    return (head.group(1) if head else "") + (body.group(1) if body else "")


def load_streams(session_path: Path, include_subagents: bool):
    reader = TranscriptReader(session_path)
    reader.refresh()
    subs = []
    if include_subagents:
        for f in subagent_files(session_path):
            r = TranscriptReader(f)
            r.refresh()
            subs.append((f.stem, r))
    return reader, subs


def build_payload(reader, subs, args, live=False, now=None):
    return analyze(reader.entries, [(name, r.entries) for name, r in subs],
                   title=args.title, context_window=args.context_window, live=live,
                   source=str(reader.path), now=now, poll_ms=int(getattr(args, "interval", 2.5) * 1000))


# --------------------------------------------------------------------------- commands

def cmd_list(args):
    sessions = iter_sessions()
    if not sessions:
        print(f"no sessions under {projects_dir()}")
        return 1
    print(f"{len(sessions)} session(s) under {projects_dir()}, newest first\n")
    for s in sessions[: args.n]:
        when = dt.datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")
        size = f"{s['size'] / 1e6:5.1f} MB"
        snippet = first_prompt(s["path"])
        print(f"  {when}  {size}  {s['session_id'][:8]}  {s['project']:<32.32}  {snippet}")
    print("\nuse a session id prefix, 'latest', or a path with build / serve / json")
    return 0


def cmd_json(args):
    path = resolve_session(args.session)
    reader, subs = load_streams(path, not args.no_subagents)
    payload = build_payload(reader, subs, args)
    text = json.dumps(payload, indent=1 if args.pretty else None, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(text)
    return 0


def cmd_build(args):
    path = resolve_session(args.session)
    reader, subs = load_streams(path, not args.no_subagents)
    payload = build_payload(reader, subs, args)
    out = Path(args.output)
    out.write_text(render_html(payload, fragment=args.fragment), encoding="utf-8")
    st = payload["stats"]
    print(f"wrote {out}  ({out.stat().st_size / 1e3:.0f} kB)  "
          f"wall {fmt_duration(st['time']['wall'])} · {st['counts']['assistant']} assistant calls · "
          f"{st['counts']['tools']} tool calls · {st['laps']} laps")
    return 0


class _LiveState:
    def __init__(self, reader, subs, args):
        self.reader, self.subs, self.args = reader, subs, args
        self.lock = threading.Lock()
        self.payload = None
        self.last = 0.0

    def snapshot(self):
        with self.lock:
            now = time.time()
            if self.payload is None or now - self.last >= 1.0:
                changed = self.reader.refresh()
                for _, r in self.subs:
                    changed = r.refresh() or changed
                if self.payload is None or changed or now - self.last >= 10.0:
                    self.payload = build_payload(self.reader, self.subs, self.args, live=True, now=now)
                self.last = now
            return self.payload


def cmd_serve(args):
    path = resolve_session(args.session)
    reader, subs = load_streams(path, not args.no_subagents)
    state = _LiveState(reader, subs, args)
    state.snapshot()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            route = self.path.split("?", 1)[0]
            if route in ("/", "/index.html"):
                body = render_html(state.snapshot()).encode("utf-8")
                ctype = "text/html; charset=utf-8"
            elif route == "/data.json":
                body = json.dumps(state.snapshot(), separators=(",", ":")).encode("utf-8")
                ctype = "application/json"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):  # keep the terminal quiet
            if args.verbose:
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"lapboard: {path}\n          live at {url}  (polling every {args.interval:g}s, Ctrl-C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lapboard", description="telemetry panel for long-horizon Claude Code sessions")
    ap.add_argument("--version", action="version", version=f"lapboard {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("session", help="transcript path, session id prefix, project directory, or 'latest'")
    common.add_argument("--title", help="panel title (default: the first prompt)")
    common.add_argument("--context-window", type=int, default=None,
                        help="context window in tokens for the fill ring (default: auto, 200k or 1M)")
    common.add_argument("--no-subagents", action="store_true", help="ignore subagent transcripts")

    p = sub.add_parser("list", help="list Claude Code sessions, newest first")
    p.add_argument("-n", type=int, default=20, help="how many to show")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("build", parents=[common], help="write a self-contained HTML panel")
    p.add_argument("-o", "--output", default="lapboard.html")
    p.add_argument("--fragment", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_build, interval=2.5)

    p = sub.add_parser("serve", parents=[common], help="serve a live panel that follows the transcript")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--interval", type=float, default=2.5, help="browser poll interval in seconds")
    p.add_argument("--open", action="store_true", help="open the browser")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("json", parents=[common], help="dump the computed panel data as JSON")
    p.add_argument("-o", "--output")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(fn=cmd_json, interval=2.5)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
