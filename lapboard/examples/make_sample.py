#!/usr/bin/env python3
"""Generate a synthetic Claude Code transcript that looks like a long autonomous run.

The output uses the same entry shapes Claude Code writes (user prompts, per-block
assistant entries with usage, tool results, compaction boundaries), so it exercises
the parser end to end. A sidecar "truth" dict records the phase durations that were
simulated, which the tests compare against lapboard's estimates.

    python3 examples/make_sample.py --hours 16 --laps 99 --out /tmp/sample.jsonl
    python3 lapboard.py build /tmp/sample.jsonl --title "16h synthetic run" -o sample.html
"""
from __future__ import annotations

import argparse
import json
import math
import random
import uuid
from datetime import datetime, timedelta, timezone

MODEL = "claude-sonnet-4-5"
TOOLS = [  # name, weight, log-median seconds, sigma, result chars median
    ("Bash", 62, 1.6, 1.1, 900),
    ("Edit", 10, 0.08, 0.4, 120),
    ("Read", 8, 0.05, 0.3, 2400),
    ("Write", 4, 0.1, 0.4, 60),
    ("Grep", 5, 0.3, 0.6, 700),
    ("Glob", 3, 0.2, 0.5, 300),
    ("WebFetch", 2, 4.0, 0.7, 3000),
    ("mcp__jina__read_url", 1, 2.5, 0.6, 4000),
    ("mcp__vcc__search", 1, 1.2, 0.5, 1500),
    ("TodoWrite", 2, 0.05, 0.2, 80),
]
SNIPPETS = {
    "Bash": ["python -m pytest tests/test_port.py -q", "make build 2>&1 | tail -20", "git diff --stat",
             "python scripts/convert_weights.py --layer {n}", "grep -rn 'kernel' src/ | head", "ls -la build/"],
    "Edit": ["src/port/layer_{n}.py", "src/port/attention.py", "src/port/kernels.cu"],
    "Read": ["src/port/layer_{n}.py", "docs/porting-notes.md", "tests/test_port.py"],
    "Write": ["src/port/layer_{n}.py", "notes/lap-{n}.md"],
    "Grep": ["def forward", "rms_norm", "rope"],
    "Glob": ["src/**/*.py", "build/**/*.so"],
    "WebFetch": ["https://docs.example.com/api/{n}"],
    "mcp__jina__read_url": ["https://example.org/paper-{n}"],
    "mcp__vcc__search": ["attention kernel port"],
    "TodoWrite": ["update plan"],
}


def lognormal(rng, median, sigma):
    return median * math.exp(rng.gauss(0, sigma))


def generate(hours=16.0, laps=99, seed=7, start=None, decode_tok_s=62.0, prefill_tok_s=2800.0,
             session_id=None, cwd="/home/dev/port", branch="port/qwen"):
    rng = random.Random(seed)
    start = start or datetime(2026, 9, 3, 19, 14, 0, tzinfo=timezone.utc)
    session_id = session_id or str(uuid.UUID(int=rng.getrandbits(128)))
    entries = []
    truth = {"prefill": 0.0, "reasoning": 0.0, "generation": 0.0, "tool_sum": 0.0, "laps": laps,
             "compactions": 0, "tool_calls": 0, "model_calls": 0, "prompt_computed": 0, "prompt_cached": 0,
             "completion": 0, "reasoning_tokens": 0}
    t = start
    parent = None

    def iso(dt_):
        return dt_.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt_.microsecond // 1000:03d}Z"

    def emit(entry, when):
        nonlocal parent
        entry.setdefault("uuid", str(uuid.UUID(int=rng.getrandbits(128))))
        entry.update({"parentUuid": parent, "isSidechain": False, "userType": "external", "cwd": cwd,
                      "sessionId": session_id, "version": "2.1.260", "gitBranch": branch, "timestamp": iso(when)})
        entries.append(entry)
        parent = entry["uuid"]

    total_budget = hours * 3600.0
    lap_target = total_budget / laps
    context = 18_000  # tokens currently in the conversation
    weights = [w for _, w, _, _, _ in TOOLS]

    for lap in range(1, laps + 1):
        prompt = ("Port the model to the new runtime layer by layer. Keep tests green, write notes after each "
                  "layer, and keep going until every layer passes." if lap == 1 else
                  f"continue (lap {lap}): pick up where you left off")
        emit({"type": "user", "message": {"role": "user", "content": prompt}}, t)
        context += 40
        lap_end = t + timedelta(seconds=lap_target * lognormal(rng, 1.0, 0.25))
        while True:
            # ---- one model call; the last call of a lap ends the turn without tools
            last_call = t >= lap_end
            req = "req_" + uuid.UUID(int=rng.getrandbits(128)).hex[:20]
            msg_id = "msg_" + uuid.UUID(int=rng.getrandbits(128)).hex[:20]
            computed = int(lognormal(rng, 900, 0.8))
            cached = context
            think = int(lognormal(rng, 350, 1.1)) if rng.random() < 0.85 else 0
            n_tools = 0 if last_call else rng.choice([1, 1, 1, 2, 2, 3])
            text_tok = int(lognormal(rng, 60, 0.9)) if (n_tools == 0 or rng.random() < 0.5) else 0
            tool_names = rng.choices([n for n, *_ in TOOLS], weights=weights, k=n_tools)
            tool_tok = 85 * n_tools
            out = think + text_tok + tool_tok
            prefill = 0.35 + computed / prefill_tok_s + rng.random() * 0.2
            t0 = t
            cursor = t0 + timedelta(seconds=prefill)
            blocks = []
            if think:
                cursor += timedelta(seconds=think / decode_tok_s)
                blocks.append(({"type": "thinking", "thinking": "…" * min(think, 400), "signature": "x"}, cursor))
            if text_tok:
                cursor += timedelta(seconds=text_tok / decode_tok_s)
                blocks.append(({"type": "text", "text": "Working on it. " * max(1, text_tok // 4)}, cursor))
            tool_uses = []
            for name in tool_names:
                cursor += timedelta(seconds=85 / decode_tok_s)
                tid = "toolu_" + uuid.UUID(int=rng.getrandbits(128)).hex[:22]
                snippet = rng.choice(SNIPPETS[name]).replace("{n}", str(rng.randint(1, 48)))
                key = "command" if name == "Bash" else ("pattern" if name in ("Grep", "Glob") else
                                                        "url" if "url" in name.lower() or name == "WebFetch" else
                                                        "query" if "search" in name else "file_path")
                blk = {"type": "tool_use", "id": tid, "name": name, "input": {key: snippet}}
                blocks.append((blk, cursor))
                tool_uses.append((tid, name, cursor))
            if not blocks:  # degenerate: emit an empty text block
                blocks.append(({"type": "text", "text": "done."}, cursor))
            usage = {"input_tokens": 3, "cache_creation_input_tokens": max(0, computed - 3),
                     "cache_read_input_tokens": cached, "output_tokens": out,
                     "output_tokens_details": {"thinking_tokens": think}, "service_tier": "standard"}
            stop = "tool_use" if tool_uses else "end_turn"
            for i, (blk, when) in enumerate(blocks):
                emit({"type": "assistant", "requestId": req, "apiBlockIndex": i,
                      "message": {"id": msg_id, "type": "message", "role": "assistant", "model": MODEL,
                                  "content": [blk], "stop_reason": stop, "stop_sequence": None, "usage": usage}}, when)
            truth["model_calls"] += 1
            truth["prefill"] += prefill
            truth["reasoning"] += think / decode_tok_s
            truth["generation"] += (text_tok + tool_tok + (0 if blocks[0][0]["type"] != "text" or text_tok else 0)) / decode_tok_s
            truth["prompt_computed"] += computed
            truth["prompt_cached"] += cached
            truth["completion"] += out
            truth["reasoning_tokens"] += think
            context += computed + out
            t = cursor
            # ---- tool results: each tool starts as soon as its block has streamed (like Claude Code),
            #      so calls in one message overlap each other and the tail of the stream
            end = t
            results = []
            for tid, name, started in tool_uses:
                _, _, med, sig, chars_med = next(x for x in TOOLS if x[0] == name)
                dur = lognormal(rng, med, sig)
                chars = int(lognormal(rng, chars_med, 0.7))
                when = started + timedelta(seconds=dur)
                end = max(end, when)
                results.append((when, tid, chars, dur))
            for when, tid, chars, dur in sorted(results):
                emit({"type": "user", "message": {"role": "user", "content": [
                    {"tool_use_id": tid, "type": "tool_result", "content": "x" * chars}]},
                      "toolUseResult": {"stdout": "", "stderr": "", "interrupted": False}}, when)
                truth["tool_sum"] += dur
                truth["tool_calls"] += 1
                context += chars // 4
            t = end + timedelta(seconds=0.02)
            # ---- compaction when the context is full
            if context > 165_000:
                dur = 8 + rng.random() * 12
                boundary = t + timedelta(seconds=dur)
                emit({"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
                      "compactMetadata": {"trigger": "auto", "preTokens": context}, "level": "info"}, boundary)
                emit({"type": "user", "isCompactSummary": True, "message": {"role": "user", "content":
                      "This session is being continued from a previous conversation that ran out of context. "
                      "Summary: porting layers, tests green so far."}}, boundary + timedelta(seconds=0.05))
                truth["compactions"] += 1
                context = 22_000
                t = boundary + timedelta(seconds=0.1)
            if last_call:
                break
        # the driver waits a moment before the next lap
        t += timedelta(seconds=0.5 + rng.random() * 2)
    truth["wall"] = (t - start).total_seconds()
    return entries, truth


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=float, default=16.0)
    ap.add_argument("--laps", type=int, default=99)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", required=True)
    ap.add_argument("--truth", help="write the simulated phase totals to this JSON file")
    a = ap.parse_args()
    entries, truth = generate(a.hours, a.laps, a.seed)
    with open(a.out, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, separators=(",", ":")) + "\n")
    if a.truth:
        with open(a.truth, "w", encoding="utf-8") as fh:
            json.dump(truth, fh, indent=1)
    print(f"wrote {a.out}: {len(entries)} entries, {truth['model_calls']} model calls, "
          f"{truth['tool_calls']} tool calls, {truth['compactions']} compactions, wall {truth['wall'] / 3600:.1f}h")


if __name__ == "__main__":
    main()
