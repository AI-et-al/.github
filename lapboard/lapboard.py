#!/usr/bin/env python3
"""lapboard: telemetry and token accounting for long-horizon coding-agent sessions.

Reads Claude Code transcripts (~/.claude/projects/<project>/<session>.jsonl) and
pi sessions (~/.pi/agent/sessions/<cwd>/<stamp>_<id>.jsonl) and renders one page:

  stats      tg/s, pp/s, laps, an additive wall-clock split (prefill / reasoning /
             generation / tools / compaction / idle), token and cost accounting
  activity   a per-row timeline of every call: laps, assistant phases, each tool,
             compactions, idle stretches
  context    what is in the context window, request by request: system prompt,
             tool schemas, injected skill / MCP / agent listings, loaded skills,
             prompts, tool results by tool, images; what each category has cost
             over the session (computed vs. served from cache); cache rebuilds and
             their likely cause; deferred vs. direct tool loading

Only the Python standard library is required (3.8+).

    python3 lapboard.py list
    python3 lapboard.py build latest -o panel.html
    python3 lapboard.py serve latest --open
    python3 lapboard.py fleet --serve            # every session, herdr-style states
    python3 lapboard.py json  <session-id-or-path>

Measured vs. estimated: timestamps and usage counts are measured. The prefill/
decode split, token counts derived from characters, image tokens, and dollar cost
are estimates; the panel marks them with ~ and the footer states each method.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import hashlib
import http.server
import json
import math
import os
import re
import socket
import struct
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from pathlib import Path

__version__ = "0.2.0"
HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = HERE / "panel.html"
FLEET_TEMPLATE_PATH = HERE / "fleet.html"
DATA_MARKER = "__LAPBOARD_DATA__"
DEFAULT_WINDOW = 200_000
LARGE_WINDOW = 1_000_000
INTERRUPT_PREFIX = "[Request interrupted by user"
DEFAULT_CPT = 3.8          # characters per token when the session cannot be calibrated
PRICING_DATE = "2026-06"   # the table below is a snapshot; override with --price

# $/M input, $/M output, cache-read multiplier, cache-write 5m multiplier, cache-write 1h multiplier
PRICING = {
    "claude-fable-5-1": (10.0, 50.0, 0.025, 1.25, 2.0),
    "claude-mythos-5-1": (10.0, 50.0, 0.025, 1.25, 2.0),
    "claude-fable-5": (10.0, 50.0, 0.1, 1.25, 2.0),
    "claude-opus-5": (5.0, 25.0, 0.1, 1.25, 2.0),
    "claude-opus-4-8": (5.0, 25.0, 0.1, 1.25, 2.0),
    "claude-opus-4-7": (5.0, 25.0, 0.1, 1.25, 2.0),
    "claude-opus-4-6": (5.0, 25.0, 0.1, 1.25, 2.0),
    "claude-opus-4-5": (5.0, 25.0, 0.1, 1.25, 2.0),
    "claude-opus-4-1": (15.0, 75.0, 0.1, 1.25, 2.0),
    "claude-opus-4": (15.0, 75.0, 0.1, 1.25, 2.0),
    "claude-sonnet-5": (2.0, 10.0, 0.1, 1.25, 2.0),
    "claude-sonnet-4-6": (3.0, 15.0, 0.1, 1.25, 2.0),
    "claude-sonnet-4-5": (3.0, 15.0, 0.1, 1.25, 2.0),
    "claude-sonnet-4": (3.0, 15.0, 0.1, 1.25, 2.0),
    "claude-3-7-sonnet": (3.0, 15.0, 0.1, 1.25, 2.0),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25, 2.0),
    "claude-3-5-haiku": (0.8, 4.0, 0.1, 1.25, 2.0),
}

# --------------------------------------------------------------------------- utils

_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_ts(value):
    """ISO-8601 timestamp (or epoch milliseconds) -> epoch seconds, or None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
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


def pi_sessions_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or (Path.home() / ".pi" / "agent")) / "sessions"


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


def image_blocks(content):
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "image"]


def _b64_head(data: str, n: int = 6000) -> bytes:
    chunk = data[:n]
    chunk = chunk[: len(chunk) - len(chunk) % 4]
    try:
        return base64.b64decode(chunk, validate=False)
    except Exception:
        return b""


def image_dims(media_type, data) -> tuple:
    """(width, height) from the first bytes of a base64 PNG/JPEG/GIF/WebP, or (0, 0)."""
    raw = _b64_head(data or "")
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            return struct.unpack(">II", raw[16:24])
        if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
            return struct.unpack("<HH", raw[6:10])
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and len(raw) >= 30:
            if raw[12:16] == b"VP8 ":
                return struct.unpack("<HH", raw[26:30])[0] & 0x3FFF, struct.unpack("<HH", raw[26:30])[1] & 0x3FFF
            if raw[12:16] == b"VP8L":
                b = raw[21:25]
                return 1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        if raw[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(raw):
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = struct.unpack(">H", raw[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    return w, h
                i += 2 + seg
    except Exception:
        pass
    return 0, 0


HI_RES_MODELS = re.compile(r"opus-4-[78]|opus-5|fable|mythos|sonnet-5")


def image_tokens(media_type, data, dims=None, model=None) -> int:
    """Vision token estimate: the image is scaled to the model's limit (1568 px on the long
    edge and ~1.15 MP; 2576 px and ~3.6 MP on hi-res models), then costs about (w*h)/750
    tokens. Unknown dimensions -> 1000."""
    w, h = dims or image_dims(media_type, data)
    if not w or not h:
        return 1000
    hi = bool(model and HI_RES_MODELS.search(str(model)))
    edge, mp = (2576.0, 3_590_000.0) if hi else (1568.0, 1_150_000.0)
    scale = min(1.0, edge / max(w, h), math.sqrt(mp / (w * h)))
    return int(math.ceil((w * scale) * (h * scale) / 750.0))


_TAG_RE = re.compile(r"<[^>]{1,80}>")
_WS_RE = re.compile(r"\s+")


def clean_prompt(text: str, limit: int = 140) -> str:
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


_LABEL_KEYS = ("command", "file_path", "path", "pattern", "query", "url", "notebook_path",
               "skill", "subagent_type", "prompt", "description", "message", "text", "content")


def tool_label(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in _LABEL_KEYS:
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:140]
    return ""


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


def _title_from(text: str, limit: int = 64) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return (cut if len(cut) >= limit // 2 else text[:limit].rstrip()) + "…"


# --------------------------------------------------------------------------- pricing

def pricing_for(model, override=None):
    """(in $/M, out $/M, read mult, write-5m mult, write-1h mult, label) or None."""
    if override:
        return tuple(override) + ("--price",)
    if not model:
        return None
    key = re.sub(r"\[.*?\]", "", str(model)).strip().lower()
    key = re.sub(r"-\d{8}$", "", key)
    best = None
    for name, row in PRICING.items():
        if key.startswith(name) and (best is None or len(name) > len(best[0])):
            best = (name, row)
    if not best:
        return None
    return best[1] + (f"table {PRICING_DATE}",)


def parse_price(text):
    if not text:
        return None
    parts = [float(x) for x in text.split(",")]
    if len(parts) < 2:
        raise SystemExit("--price expects IN,OUT[,READ_MULT,WRITE5M_MULT,WRITE1H_MULT] in $/M tokens")
    defaults = [0.1, 1.25, 2.0]
    return tuple(parts[:2] + parts[2:5] + defaults[len(parts) - 2:])


# --------------------------------------------------------------------------- discovery

def iter_sessions():
    """Claude Code and pi sessions on this machine, newest first."""
    found = []
    root = projects_dir()
    if root.is_dir():
        for proj in sorted(root.iterdir()):
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                try:
                    st = f.stat()
                except OSError:
                    continue
                found.append({"path": f, "fmt": "claude", "project": proj.name, "session_id": f.stem,
                              "size": st.st_size, "mtime": st.st_mtime})
    proot = pi_sessions_dir()
    if proot.is_dir():
        for f in proot.rglob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            sid = f.stem.split("_", 1)[1] if "_" in f.stem else f.stem
            found.append({"path": f, "fmt": "pi", "project": f.parent.name, "session_id": sid,
                          "size": st.st_size, "mtime": st.st_mtime})
    found.sort(key=lambda s: s["mtime"], reverse=True)
    return found


def detect_format(entries, path=None) -> str:
    if path and ".pi" in str(path).split(os.sep):
        return "pi"
    for e in entries[:5]:
        if isinstance(e, dict) and e.get("type") == "session" and "cwd" in e and "id" in e:
            return "pi"
    for e in entries[:50]:
        if isinstance(e, dict) and e.get("type") == "message" and isinstance(e.get("message"), dict) \
                and e["message"].get("role") in ("user", "assistant", "toolResult"):
            return "pi"
    return "claude"


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
                if e.get("type") == "message" and isinstance(e.get("message"), dict) and e["message"].get("role") == "user":
                    txt = clean_prompt(text_of(e["message"].get("content")), 90)
                    if txt:
                        return txt
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
        files = sorted(p.rglob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if files:
            return files[0]
        raise SystemExit(f"no .jsonl transcripts in {p}")
    sessions = iter_sessions()
    if arg == "latest":
        if not sessions:
            raise SystemExit(f"no sessions found under {projects_dir()} or {pi_sessions_dir()}")
        return sessions[0]["path"]
    matches = [s for s in sessions if s["session_id"].startswith(arg)]
    if len(matches) == 1:
        return matches[0]["path"]
    if not matches:
        raise SystemExit(f"no session matching {arg!r} (try: lapboard.py list)")
    raise SystemExit("ambiguous session id, matches:\n  " + "\n  ".join(str(m["path"]) for m in matches))


def subagent_files(session_path: Path):
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
        if st.st_size < self.offset:
            self.offset, self.buf, self.entries = 0, b"", []
        if st.st_size == self.offset:
            return False
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        data = self.buf + chunk
        lines = data.split(b"\n")
        self.buf = lines.pop()
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


# --------------------------------------------------------------------------- scanning

def _new_ctx():
    return {"models": [], "tools": [], "prompts": [], "interrupts": [], "errors": [],
            "compact_boundaries": [], "compact_summaries": [], "compact_events": [], "meta": {},
            "open_tools": [], "tool_chars": 0, "items": [], "snapshots": [], "tool_records": [],
            "deferred_listing": [], "model_changes": [], "reported_cost": 0.0, "n_entries": 0}


def _item(ctx, idx, ts, group, sub, chars=0, img=0, extra=None):
    it = {"i": idx, "t": ts, "g": group, "s": sub, "ch": int(chars), "img": int(img)}
    if extra:
        it.update(extra)
    ctx["items"].append(it)
    return it


def _bump(ctx, key, ts):
    cur = ctx.get(key)
    if ts is not None:
        ctx[key] = ts if cur is None else max(cur, ts)


def _scan_claude(entries, sub: bool, ctx: dict, stream: str = "main"):
    """One pass over a Claude Code transcript stream, in file order."""
    last_input_ts = None
    last_any_ts = None
    requests = {}
    pending_tools = {}
    meta = ctx["meta"]
    last_skill = None
    cur_model = None
    ctx["n_entries"] += len(entries)

    for idx, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        kind = e.get("type")
        ts = parse_ts(e.get("timestamp"))
        if not sub and len(meta) < 5:
            for key, src in (("session_id", "sessionId"), ("cwd", "cwd"), ("git_branch", "gitBranch"),
                             ("cli_version", "version")):
                if key not in meta and e.get(src):
                    meta[key] = e[src]
            meta.setdefault("fmt", "claude")

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
                        "tools": [], "stream": stream, "idx": idx, "fmt": "claude"}
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
                cur_model = msg["model"]
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
                    n = len(b.get("text") or "")
                    call["text_chars"] += n
                    call["blocks"].append((ts, "text"))
                    if not sub and n:
                        _item(ctx, idx, ts, "assistant", None, n)
                elif bt == "tool_use":
                    name = b.get("name") or "tool"
                    call["blocks"].append((ts, "tool_use"))
                    call["tools"].append(name)
                    inp = b.get("input")
                    tc = {"k": "t", "n": name, "t0": ts, "t1": None, "l": tool_label(inp),
                          "sub": int(sub), "err": 0, "ch": 0, "id": b.get("id"), "open": 0, "idx": idx,
                          "in_ch": len(json.dumps(inp, ensure_ascii=False)) if inp is not None else 0}
                    if name == "Skill" and isinstance(inp, dict):
                        last_skill = inp.get("skill") or "skill"
                    if b.get("id"):
                        pending_tools[b["id"]] = tc
                    ctx["tools"].append(tc)
                    if not sub:
                        _item(ctx, idx, ts, "tool_input", name, tc["in_ch"])
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
                    imgs = image_blocks(b.get("content"))
                    img_tok = 0
                    for im in imgs:
                        src = im.get("source") or {}
                        dims = None
                        tr = e.get("toolUseResult")
                        if isinstance(tr, dict) and isinstance(tr.get("file"), dict):
                            d = tr["file"].get("dimensions") or {}
                            if d.get("displayWidth") and d.get("displayHeight"):
                                dims = (d["displayWidth"], d["displayHeight"])
                        img_tok += image_tokens(src.get("media_type"), src.get("data"), dims, cur_model)
                    ctx["tool_chars"] += chars
                    name = tc["n"] if tc else "tool"
                    if not sub:
                        _item(ctx, idx, ts, "tool_result", name, chars, 0)
                        if img_tok:
                            _item(ctx, idx, ts, "image", name, 0, img_tok)
                    if tc is None:
                        continue
                    tc["t1"] = ts
                    tc["err"] = int(bool(b.get("is_error")))
                    tc["ch"] = chars
                    tc["img"] = img_tok
                continue
            if e.get("isCompactSummary"):
                ctx["compact_summaries"].append({"ts": ts, "idx": idx, "stream": stream})
                if not sub:
                    _item(ctx, idx, ts, "compaction", None, len(text_of(content)))
                continue
            txt = text_of(content)
            if e.get("isMeta"):
                if not sub and txt.strip():
                    _item(ctx, idx, ts, "skill", last_skill or "meta", len(txt))
                continue
            stripped = txt.strip()
            if stripped.startswith(INTERRUPT_PREFIX):
                ctx["interrupts"].append({"k": "x", "t0": ts, "t1": ts, "sub": int(sub), "l": stripped[:80]})
                continue
            imgs = image_blocks(content)
            if not stripped and not imgs:
                continue
            if not sub:
                if stripped:
                    ctx["prompts"].append({"k": "p", "t0": ts, "t1": ts, "l": clean_prompt(stripped), "ch": len(txt)})
                    _item(ctx, idx, ts, "prompt", None, len(txt))
                for im in imgs:
                    src = im.get("source") or {}
                    _item(ctx, idx, ts, "image", "user", 0, image_tokens(src.get("media_type"), src.get("data"), None, cur_model))
        elif kind == "attachment":
            if sub:
                continue
            att = e.get("attachment") or {}
            atype = att.get("type") or "attachment"
            rendered = e.get("rendered")
            chars = 0
            if isinstance(rendered, list):
                for r in rendered:
                    chars += len(r.get("content") or "") if isinstance(r, dict) else len(str(r))
            elif isinstance(rendered, str):
                chars = len(rendered)
            if atype == "prompt_snapshot":
                sections = []
                sp = att.get("systemPrompt")
                if isinstance(sp, list):
                    for s in sp:
                        s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
                        first = (s.strip().splitlines() or [""])[0].strip()
                        sections.append((clean_prompt(first, 60) or "(section)", len(s)))
                elif isinstance(sp, str):
                    sections.append(("system prompt", len(sp)))
                digest = hashlib.sha1(json.dumps(sections).encode()).hexdigest()[:12]
                ctx["snapshots"].append({"idx": idx, "ts": ts, "sections": sections, "hash": digest})
                continue
            if atype == "deferred_tools_record":
                # the ToolSearch result is a tool_reference block; the API expands it in place to the
                # tool's definition, so the schema lands in the messages (and in cache_creation)
                for ent in att.get("entries") or []:
                    if isinstance(ent, dict) and ent.get("name"):
                        n = len(json.dumps(ent, ensure_ascii=False))
                        ctx["tool_records"].append({"idx": idx, "ts": ts, "name": ent["name"], "ch": n})
                        _item(ctx, idx, ts, "tool_schema", ent["name"], n)
                continue
            if not chars:
                for key in ("text", "content"):
                    if isinstance(att.get(key), str):
                        chars = len(att[key])
                        break
            if atype == "deferred_tools_delta":
                ctx["deferred_listing"].append({"idx": idx, "ts": ts, "ch": chars,
                                                "added": list(att.get("addedNames") or []),
                                                "removed": list(att.get("removedNames") or [])})
            if chars:
                _item(ctx, idx, ts, "attach", atype, chars)
        elif kind == "system" and e.get("subtype") == "compact_boundary":
            cm = e.get("compactMetadata") or {}
            ctx["compact_boundaries"].append({"ts": ts, "idx": idx, "prev": prev_any_ts, "stream": stream,
                                              "pre": cm.get("preTokens"), "trigger": cm.get("trigger")})

    ctx["open_tools"].extend(pending_tools.values())


def _scan_pi(entries, sub: bool, ctx: dict, stream: str = "main"):
    """One pass over a pi session file (github.com/badlogic/pi-mono), in file order."""
    meta = ctx["meta"]
    last_any_ts = None
    last_assistant = None
    model = None
    ctx["n_entries"] += len(entries)
    for idx, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        kind = e.get("type")
        ts = parse_ts(e.get("timestamp"))
        if kind == "session":
            if not sub:
                meta.setdefault("fmt", "pi")
                meta.setdefault("session_id", e.get("id"))
                meta.setdefault("cwd", e.get("cwd"))
                if e.get("modelId"):
                    model = e["modelId"]
            continue
        if kind == "session_info" and e.get("name") and not sub:
            meta["name"] = e["name"]
            continue
        if kind == "model_change":
            model = e.get("modelId") or model
            ctx["model_changes"].append({"ts": ts, "idx": idx, "model": model, "provider": e.get("provider")})
            continue
        if kind == "compaction":
            summary = e.get("summary") or ""
            t0 = last_any_ts if last_any_ts is not None else ts
            ctx["compact_events"].append({"t0": t0, "t1": ts, "idx": idx, "pre": e.get("tokensBefore"),
                                          "label": "compaction" + (f" · {fmt_tokens(e['tokensBefore'])} tokens before" if e.get("tokensBefore") else "")})
            if not sub:
                _item(ctx, idx, ts, "compaction", None, len(summary))
            _bump_last = ts
            if ts is not None:
                last_any_ts = ts if last_any_ts is None else max(last_any_ts, ts)
            continue
        if kind != "message" or not isinstance(e.get("message"), dict):
            if ts is not None:
                last_any_ts = ts if last_any_ts is None else max(last_any_ts, ts)
            continue
        msg = e["message"]
        role = msg.get("role")
        mts = parse_ts(msg.get("timestamp"))
        if role == "user":
            content = msg.get("content")
            txt = text_of(content)
            if not sub:
                if txt.strip():
                    ctx["prompts"].append({"k": "p", "t0": ts, "t1": ts, "l": clean_prompt(txt.strip()), "ch": len(txt)})
                    _item(ctx, idx, ts, "prompt", None, len(txt))
                for im in image_blocks(content):
                    _item(ctx, idx, ts, "image", "user", 0, image_tokens(im.get("mimeType"), im.get("data")))
        elif role == "assistant":
            usage = msg.get("usage") or {}
            t0 = mts if mts is not None else (last_any_ts if last_any_ts is not None else ts)
            if ts is not None and t0 is not None and t0 > ts:
                t0 = ts
            call = {"k": "m", "rid": e.get("id") or f"{stream}:{idx}", "t0": t0, "t1": ts, "blocks": [],
                    "usage": usage, "model": msg.get("model") or model, "sub": int(sub),
                    "err": int(msg.get("stopReason") == "error"),
                    "stop": {"stop": "end_turn", "toolUse": "tool_use"}.get(msg.get("stopReason"), msg.get("stopReason")),
                    "think_chars": 0, "text_chars": 0, "tools": [], "stream": stream, "idx": idx, "fmt": "pi",
                    "cost": ((usage.get("cost") or {}).get("total") or 0.0)}
            ctx["reported_cost"] += call["cost"] or 0.0
            if msg.get("stopReason") == "error" or msg.get("errorMessage"):
                ctx["errors"].append({"k": "e", "t0": ts, "t1": ts, "sub": int(sub),
                                      "l": clean_prompt(str(msg.get("errorMessage") or "error"), 160)})
            pending = []
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking":
                    call["think_chars"] += len(b.get("thinking") or "")
                    call["blocks"].append((None, "thinking"))
                elif bt == "text":
                    n = len(b.get("text") or "")
                    call["text_chars"] += n
                    call["blocks"].append((None, "text"))
                    if not sub and n:
                        _item(ctx, idx, ts, "assistant", None, n)
                elif bt == "toolCall":
                    name = b.get("name") or "tool"
                    args = b.get("arguments")
                    call["tools"].append(name)
                    call["blocks"].append((None, "tool_use"))
                    tc = {"k": "t", "n": name, "t0": ts, "t1": None, "l": tool_label(args), "sub": int(sub),
                          "err": 0, "ch": 0, "id": b.get("id"), "open": 0, "idx": idx,
                          "in_ch": len(json.dumps(args, ensure_ascii=False)) if args is not None else 0}
                    pending.append(tc)
                    ctx["tools"].append(tc)
                    if not sub:
                        _item(ctx, idx, ts, "tool_input", name, tc["in_ch"])
            ctx["models"].append(call)
            last_assistant = {"call": call, "pending": {t["id"]: t for t in pending if t.get("id")},
                              "cursor": ts}
        elif role == "toolResult":
            tc = None
            if last_assistant:
                tc = last_assistant["pending"].pop(msg.get("toolCallId"), None)
            content = msg.get("content")
            chars = len(text_of(content))
            img_tok = sum(image_tokens(im.get("mimeType"), im.get("data")) for im in image_blocks(content))
            ctx["tool_chars"] += chars
            name = tc["n"] if tc else (msg.get("toolName") or "tool")
            if not sub:
                _item(ctx, idx, ts, "tool_result", name, chars)
                if img_tok:
                    _item(ctx, idx, ts, "image", name, 0, img_tok)
            if tc is not None:
                start = last_assistant["cursor"] if last_assistant else ts
                end = mts if mts is not None else ts
                if start is not None and end is not None and end < start:
                    start = end
                tc["t0"], tc["t1"] = start, end
                tc["err"] = int(bool(msg.get("isError")))
                tc["ch"], tc["img"] = chars, img_tok
                if last_assistant:
                    last_assistant["cursor"] = end
        elif role == "bashExecution":
            n = len(msg.get("command") or "") + len(msg.get("output") or "")
            if not sub:
                _item(ctx, idx, ts, "bash_user", None, n)
                ctx["interrupts"].append({"k": "b", "t0": ts, "t1": ts, "sub": 0,
                                          "l": "bash: " + (msg.get("command") or "")[:70]})
        elif role in ("custom", "branchSummary", "compactionSummary"):
            n = len(text_of(msg.get("content"))) if msg.get("content") is not None else 0
            if not sub and n:
                _item(ctx, idx, ts, "custom" if role == "custom" else "compaction", msg.get("customType") if role == "custom" else None, n)
        if ts is not None:
            last_any_ts = ts if last_any_ts is None else max(last_any_ts, ts)
    if last_assistant:
        ctx["open_tools"].extend(last_assistant["pending"].values())


def _usage_numbers(call):
    u = call.get("usage") or {}
    if call.get("fmt") == "pi":
        inp = int(u.get("input") or 0)
        cw = int(u.get("cacheWrite") or 0)
        cr = int(u.get("cacheRead") or 0)
        out = int(u.get("output") or 0)
        think = u.get("reasoning")
        think_reported = isinstance(think, (int, float))
        think = int(think) if think_reported else 0
        cw1h = int(u.get("cacheWrite1h") or 0)
        return inp, cw, cr, out, max(0, min(out, think)), think_reported, cw1h
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
    cc1h = int(((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens")) or 0)
    return inp, cc, cr, out, max(0, min(out, think)), think_reported, cc1h


# --------------------------------------------------------------------------- phases

def _phase_split(models):
    """Assign each model call (prefill, reasoning, generation) durations.

    With per-block timestamps (Claude Code): decode speed is observed on visible text
    that follows a thinking block, then separates prefill from thinking decode.
    Without them (pi): a least-squares fit latency ~ a + b*computed + c*output over the
    session's requests estimates prefill; decode is the remainder, split by tokens.
    Returns (decode tok/s or None, method).
    """
    vis_tok = vis_dur = 0.0
    timed = [c for c in models if c["_think_end"] is not None]
    for c in timed:
        if c["t1"] > c["_think_end"] + 0.05 and c["_out"] > c["_think"]:
            vis_tok += c["_out"] - c["_think"]
            vis_dur += c["t1"] - c["_think_end"]
    tg = vis_tok / vis_dur if (vis_dur >= 2.0 and vis_tok >= 50) else None
    method = "blocks" if tg else None

    if not tg:
        fit = _fit_latency(models)
        if fit:
            a, b, c_ = fit
            for c in models:
                lat = max(0.0, c["t1"] - c["t0"])
                prefill = min(lat, max(0.0, a + b * (c["_in"] + c["_cc"])))
                dec = lat - prefill
                share = (c["_think"] / c["_out"]) if c["_out"] else 0.0
                c["ph"] = (prefill, dec * share, dec * (1 - share))
            return (1.0 / c_ if c_ > 0 else None), "regression"
        for c in models:
            lat = max(0.0, c["t1"] - c["t0"])
            think_end = c["_think_end"]
            if think_end is not None:
                first = min(lat, max(0.0, think_end - c["t0"]))
                c["ph"] = (0.0, first, max(0.0, lat - first))
            else:
                share = (c["_think"] / c["_out"]) if c["_out"] else 0.0
                c["ph"] = (0.0, lat * share, lat * (1 - share))
        return None, "none"

    for c in models:
        lat = max(0.0, c["t1"] - c["t0"])
        think_end = c["_think_end"]
        if think_end is not None:
            first = min(lat, max(0.0, think_end - c["t0"]))
            tokens = c["_think"] if c["_think"] > 0 else max(0, c["_out"] - c["text_chars"] // 4)
            think_dec = min(first, tokens / tg)
            c["ph"] = (first - think_dec, think_dec, max(0.0, lat - first))
        else:
            dec = min(lat, c["_out"] / tg)
            c["ph"] = (lat - dec, 0.0, dec)
    return tg, method


def _fit_latency(models):
    """Least squares for latency = a + b*computed_in + c*out, coefficients clamped >= 0."""
    rows = [(1.0, float(c["_in"] + c["_cc"]), float(c["_out"]), max(0.0, c["t1"] - c["t0"]))
            for c in models if c["t1"] > c["t0"] and c["_out"] > 0]
    if len(rows) < 8:
        return None

    def solve(cols):
        n = len(cols)
        A = [[sum(r[i] * r[j] for r in rows) for j in cols] for i in cols]
        y = [sum(r[i] * r[3] for r in rows) for i in cols]
        for i in range(n):  # gaussian elimination with partial pivoting
            p = max(range(i, n), key=lambda k: abs(A[k][i]))
            if abs(A[p][i]) < 1e-12:
                return None
            A[i], A[p] = A[p], A[i]
            y[i], y[p] = y[p], y[i]
            for k in range(i + 1, n):
                f = A[k][i] / A[i][i]
                for j in range(i, n):
                    A[k][j] -= f * A[i][j]
                y[k] -= f * y[i]
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            x[i] = (y[i] - sum(A[i][j] * x[j] for j in range(i + 1, n))) / A[i][i]
        return dict(zip(cols, x))

    sol = solve([0, 1, 2])
    if sol is None:
        return None
    if sol[2] <= 0:
        return None
    if sol[1] < 0:
        sol = solve([0, 2]) or {}
        sol[1] = 0.0
    if sol.get(0, 0) < 0:
        sol[0] = 0.0
    return sol.get(0, 0.0), sol.get(1, 0.0), sol[2]


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


def auto_window(models, override):
    if override:
        return int(override)
    mx = 0
    for c in models:
        mx = max(mx, c["_in"] + c["_cc"] + c["_cr"])
        if "[1m]" in (c.get("model") or ""):
            return LARGE_WINDOW
    return LARGE_WINDOW if mx > DEFAULT_WINDOW else DEFAULT_WINDOW


# --------------------------------------------------------------------------- ledger

ATTACH_LABELS = {
    "skill_listing": "skills listing", "mcp_instructions_delta": "MCP instructions",
    "deferred_tools_delta": "deferred tool names", "agent_listing_delta": "agent listing",
    "environment": "environment", "session_context": "session context", "task_reminder": "task reminders",
    "total_tokens_reminder": "budget reminders", "batching_reminder_sent": "batching reminders",
    "silent_turn_reminder": "silent-turn reminders", "queued_command": "queued commands",
    "remote_session_change": "remote session notes", "inlined_image_paths": "image path notes",
}
GROUP_LABELS = {
    "system": "system prompt", "residual": "tool schemas & unmeasured", "attach": "injected reminders",
    "thinking": "re-sent thinking (this turn)", "tool_schema": "tool schemas loaded on demand",
    "skill": "loaded skills", "prompt": "user prompts", "image": "images", "assistant": "assistant text",
    "tool_input": "tool inputs", "tool_result": "tool results", "compaction": "compaction summaries",
    "bash_user": "user shell output", "custom": "extension messages",
}


def _label_for(group, sub):
    if group == "attach":
        return ATTACH_LABELS.get(sub, (sub or "attachment").replace("_", " "))
    if group in ("tool_result", "tool_input", "image", "skill", "custom", "tool_schema") and sub:
        return f"{GROUP_LABELS[group]} · {sub}"
    return GROUP_LABELS.get(group, group)


def build_ledger(ctx, models, price, window, base):
    """Reconstruct the context window at every main-agent request and cost it out."""
    main = sorted([c for c in models if not c["sub"]], key=lambda c: c["idx"])
    items = sorted(ctx["items"], key=lambda it: it["i"])
    snaps = sorted(ctx["snapshots"], key=lambda s: s["idx"])
    comp_idx = sorted(set([b["idx"] for b in ctx["compact_boundaries"]] +
                          [s["idx"] for s in ctx["compact_summaries"]] +
                          [c["idx"] for c in ctx["compact_events"]]))
    if not main:
        return None

    def measured(c):
        return c["_in"] + c["_cc"] + c["_cr"]

    # ---- the tool block: when a system-prompt change rebuilds the cache, what stays cached
    # is the prefix before the system prompt, i.e. the tool definitions
    def is_rebuild(prev, cur):
        pm = measured(prev)
        return cur["_cr"] < 0.85 * pm and (cur["_in"] + cur["_cc"]) > max(2_000, 0.25 * pm)

    hint = 0.0
    snap_at = {}
    for c in main:
        snap_at[c["idx"]] = next((s["hash"] for s in reversed(snaps) if s["idx"] < c["idx"]), None)
    for k in range(1, len(main)):
        prev, cur = main[k - 1], main[k]
        if snap_at[cur["idx"]] != snap_at[prev["idx"]] and is_rebuild(prev, cur) and cur["_cr"] > 1000 \
                and not any(prev["idx"] < ci < cur["idx"] for ci in comp_idx):
            hint = max(hint, float(cur["_cr"]))
    reconciled = 0

    # ---- calibrate characters per token on requests served from a warm cache. Two rates:
    # tool traffic (shell output, JSON, code) and prose (prompts, listings, assistant text).
    TOOLISH = ("tool_result", "tool_input", "tool_schema", "bash_user")
    pairs = []
    for k in range(1, len(main)):
        prev, cur = main[k - 1], main[k]
        if cur["_cr"] < 0.9 * measured(prev) or (cur["_in"] + cur["_cc"]) < 200:
            continue
        if any(prev["idx"] < ci < cur["idx"] for ci in comp_idx):
            continue
        new = [it for it in items if prev["idx"] < it["i"] < cur["idx"]]
        if any(it["g"] == "image" for it in new):
            continue  # image token costs are themselves estimates; keep them out of the fit
        exact = sum(it["img"] for it in new)  # re-sent thinking carries an exact token count
        ct = sum(it["ch"] for it in new if it["g"] in TOOLISH)
        cp = sum(it["ch"] for it in new if it["g"] not in TOOLISH)
        toks = cur["_in"] + cur["_cc"] - exact
        if ct + cp >= 800 and toks > 50:
            pairs.append((ct, cp, toks))
    # the first request is nearly all prose (system prompt, listings, the prompt); when the
    # tool block is known from cache retention (below), it measures the prose rate directly
    prose_prior = None
    if hint and main:
        first = main[0]
        first_items = [it for it in items if it["i"] < first["idx"]]
        first_snap = next((s for s in reversed(snaps) if s["idx"] < first["idx"]), None)
        pchars = sum(it["ch"] for it in first_items if it["g"] not in TOOLISH) + \
            (sum(n for _, n in first_snap["sections"]) if first_snap else 0)
        tchars = sum(it["ch"] for it in first_items if it["g"] in TOOLISH)
        exact = sum(it["img"] for it in first_items)
        left = measured(first) - hint - exact - tchars / DEFAULT_CPT
        if pchars > 5000 and left > 500 and tchars < 0.2 * pchars:
            prose_prior = min(6.0, max(2.0, pchars / left))
    cpt_tool, cpt_prose = _fit_cpt(pairs, prose_prior)
    calibrated = bool(pairs)
    cpt = cpt_tool

    def tok(it):
        return it["img"] + it["ch"] / (cpt_tool if it["g"] in TOOLISH else cpt_prose)

    read_mult = price[2] if price else 0.1
    p_in = price[0] / 1e6 if price else 0.0
    p_out = price[1] / 1e6 if price else 0.0


    # ---- walk the requests
    cum = defaultdict(lambda: {"computed": 0.0, "cached": 0.0, "present": 0, "cost": 0.0, "peak": 0.0})
    series = []
    events = []
    prev = None
    prev_snap = None
    last_detail = None
    for k, c in enumerate(main):
        snap = None
        for s in snaps:
            if s["idx"] < c["idx"]:
                snap = s
        last_comp = max([ci for ci in comp_idx if ci < c["idx"]], default=-1)
        last_prompt = max([it["i"] for it in items if it["g"] == "prompt" and it["i"] < c["idx"]], default=-1)
        present = [it for it in items if (last_comp < it["i"] < c["idx"] or (it["g"] == "compaction" and last_comp <= it["i"] < c["idx"]))
                   and (not it.get("transient") or it["i"] > last_prompt)]
        sys_tokens = sum(n for _, n in snap["sections"]) / cpt_prose if snap else 0.0
        est_raw = sys_tokens + sum(tok(it) for it in present)
        meas = measured(c)
        # a rebuild caused by a system-prompt change leaves exactly the tool block cached:
        # that retained size is a floor for what the estimates cannot see
        floor = hint if hint and meas > hint else 0.0
        f = min(1.0, (meas - floor) / est_raw) if est_raw > 0 else 1.0
        if f < 1.0:
            reconciled += 1
        est = est_raw * f
        residual = max(0.0, meas - est)

        # composition by key
        comp = defaultdict(float)
        detail = defaultdict(lambda: defaultdict(float))
        if snap:
            comp["system"] += sys_tokens * f
            for label, n in snap["sections"]:
                detail["system"][label] += n / cpt_prose * f
        comp["residual"] += residual
        for it in present:
            key = it["g"]
            comp[key] += tok(it) * f
            detail[key][it["s"] or ""] += tok(it) * f

        # cost attribution: new content is computed first, the rest is served from cache
        computed_budget = float(c["_in"] + c["_cc"])
        new_ids = set()
        if prev is None:
            new_ids = {id(it) for it in present}
            sys_new = True
            res_new = True
        else:
            new_ids = {id(it) for it in present if it["i"] > prev["idx"]}
            sys_new = bool(snap and prev_snap and snap["hash"] != prev_snap["hash"]) or (snap is not None and prev_snap is None)
            res_new = False
        parts = []  # (key, sub, tokens, is_new)
        if snap:
            for label, n in snap["sections"]:
                parts.append(("system", label, n / cpt_prose * f, sys_new))
        parts.append(("residual", "", residual, res_new))
        for it in present:
            parts.append((it["g"], it["s"] or "", tok(it) * f, id(it) in new_ids))
        new_total = sum(p[2] for p in parts if p[3])
        old_total = sum(p[2] for p in parts if not p[3])
        if computed_budget >= new_total:
            new_share, old_share = 1.0, ((computed_budget - new_total) / old_total if old_total > 0 else 0.0)
        else:
            new_share, old_share = (computed_budget / new_total if new_total > 0 else 0.0), 0.0
        old_share = min(1.0, old_share)
        # actual $ for this request's input, split by computed vs cached at the API's own prices
        cc1h = c["_cc1h"]
        cc5m = max(0, c["_cc"] - cc1h)
        write_cost = (c["_in"] * 1.0 + cc5m * (price[3] if price else 1.25) + cc1h * (price[4] if price else 2.0)) * p_in
        read_cost = c["_cr"] * read_mult * p_in
        comp_rate = write_cost / computed_budget if computed_budget > 0 else 0.0
        cache_rate = read_cost / c["_cr"] if c["_cr"] > 0 else 0.0
        for key, sub_, t, is_new in parts:
            share = new_share if is_new else old_share
            comp_part = t * share
            cache_part = max(0.0, t - comp_part)
            ck = (key, sub_)
            d = cum[ck]
            d["computed"] += comp_part
            d["cached"] += cache_part
            d["present"] += 1 if t > 0 else 0
            d["cost"] += comp_part * comp_rate + cache_part * cache_rate
            d["peak"] = max(d["peak"], t)

        # cache rebuilds
        if prev is not None:
            prev_meas = measured(prev)
            recomputed = c["_in"] + c["_cc"]
            if is_rebuild(prev, c):
                cause = "unknown (prefix changed: tool set, system prompt, or history edit)"
                if any(prev["idx"] < ci < c["idx"] for ci in comp_idx):
                    cause = "compaction (expected)"
                elif sys_new and snap and prev_snap:
                    changed = []
                    a = dict(prev_snap["sections"])
                    for label, n in snap["sections"]:
                        if a.get(label) != n:
                            changed.append(f"{label} ({n - a.get(label, 0):+d} chars)")
                    for label in a:
                        if label not in dict(snap["sections"]):
                            changed.append(f"{label} (removed)")
                    cause = "system prompt changed: " + ("; ".join(changed[:3]) if changed else "sections differ")
                else:
                    gap = c["t0"] - prev["t1"]
                    ttl = 3600.0 if prev["_cc1h"] > 0 else 300.0
                    if gap > ttl:
                        cause = f"cache expired after {fmt_duration(gap)} idle (TTL {fmt_duration(ttl)})"
                    elif (c.get("model") or "") != (prev.get("model") or ""):
                        cause = f"model changed to {c.get('model')}"
                extra = recomputed * (comp_rate - cache_rate) if price else None
                events.append({"t": round(c["t0"] - base, 3), "recomputed": int(recomputed),
                               "expected": int(prev_meas), "cause": cause,
                               "extra_cost": extra, "k": k})
        series.append({"t": round(c["t0"] - base, 3), "m": int(meas), "in": c["_in"], "cc": c["_cc"],
                       "cr": c["_cr"], "g": {key: int(round(v)) for key, v in comp.items() if v >= 1}})
        last_detail = (c, comp, detail, present, snap, meas)
        prev, prev_snap = c, (snap or prev_snap)

    # ---- table for the latest request
    c, comp, detail, present, snap, meas = last_detail
    now_rows = []
    for key in sorted(comp, key=lambda k: -comp[k]):
        subs = sorted(detail.get(key, {}).items(), key=lambda kv: -kv[1])
        now_rows.append({"key": key, "label": GROUP_LABELS.get(key, key), "tokens": int(round(comp[key])),
                         "pct": (comp[key] / window) if window else None,
                         "subs": [{"label": (s or "(unnamed)") if key != "system" else s, "tokens": int(round(v))}
                                  for s, v in subs[:12] if v >= 1]})

    # ---- cumulative table (finer keys)
    cum_rows = []
    for (key, sub_), d in cum.items():
        cum_rows.append({"key": key, "sub": sub_, "label": _label_for(key, sub_) if key != "system" else f"system prompt · {sub_}",
                         "computed": int(round(d["computed"])), "cached": int(round(d["cached"])),
                         "present": d["present"], "cost": d["cost"] if price else None, "peak": int(round(d["peak"]))})
    # collapse system sections and reminders into one row each, keep the big categories per sub
    def collapse(rows, key, label):
        grp = [r for r in rows if r["key"] == key]
        if len(grp) <= 1:
            return rows
        merged = {"key": key, "sub": "", "label": label, "computed": sum(r["computed"] for r in grp),
                  "cached": sum(r["cached"] for r in grp), "present": max(r["present"] for r in grp),
                  "cost": (sum(r["cost"] for r in grp) if price else None), "peak": sum(r["peak"] for r in grp),
                  "subs": sorted([{"label": r["sub"] or r["label"], "computed": r["computed"], "cached": r["cached"],
                                   "cost": r["cost"], "peak": r["peak"]} for r in grp],
                                 key=lambda r: -(r["computed"] + 0.1 * r["cached"]))[:12]}
        return [r for r in rows if r["key"] != key] + [merged]
    for key in sorted({r["key"] for r in cum_rows}):
        cum_rows = collapse(cum_rows, key, "injected reminders & listings" if key == "attach" else GROUP_LABELS.get(key, key))
    for r in cum_rows:
        if not r.get("subs"):
            r["label"] = GROUP_LABELS.get(r["key"], r["key"]) if r["key"] != "attach" else "injected reminders & listings"
            if r["key"] in ("tool_result", "tool_input", "image", "skill", "custom", "tool_schema") and r.get("sub"):
                r["label"] = f"{GROUP_LABELS[r['key']]} · {r['sub']}"
    cum_rows.sort(key=lambda r: -(r["computed"] + read_mult * r["cached"]))
    total_computed = sum(r["computed"] for r in cum_rows)
    total_cached = sum(r["cached"] for r in cum_rows)

    return {
        "cpt": round(cpt_tool, 2), "cpt_prose": round(cpt_prose, 2), "calibrated": calibrated, "window": window,
        "tool_block_hint": int(hint), "reconciled": reconciled,
        "read_mult": read_mult, "series": series, "events": events,
        "now": {"k": len(main) - 1, "measured": int(meas), "estimated": int(round(sum(comp.values()) - comp["residual"])),
                "rows": now_rows},
        "cum": {"rows": cum_rows, "computed": int(total_computed), "cached": int(total_cached),
                "requests": len(main)},
        "tools": _tool_loading(ctx, main, cpt, read_mult, base),
    }


PROSE_CPT = 3.5   # prior for prompts, listings and assistant text (current Claude tokenizers)


def _fit_cpt(pairs, prose_prior=None):
    """Characters per token for tool traffic and for prose, from warm-cache request pairs.

    tokens_k = a * tool_chars_k + b * prose_chars_k. Prose is usually a small share of the
    new content, so b is only fitted when prose carries enough weight; otherwise it stays
    at the prior (measured on the first request when possible) and a is fitted alone.
    """
    prose = prose_prior or PROSE_CPT
    if not pairs:
        return DEFAULT_CPT, prose
    tot_t = sum(ct for ct, _, _ in pairs)
    tot_p = sum(cp for _, cp, _ in pairs)
    b = 1.0 / prose
    if prose_prior is None and tot_p >= 30_000 and tot_p >= 0.25 * (tot_t + tot_p):
        sxx = sum(ct * ct for ct, _, _ in pairs)
        syy = sum(cp * cp for _, cp, _ in pairs)
        sxy = sum(ct * cp for ct, cp, _ in pairs)
        sxt = sum(ct * t for ct, _, t in pairs)
        syt = sum(cp * t for _, cp, t in pairs)
        det = sxx * syy - sxy * sxy
        if det > 1e-9:
            bb = (syt * sxx - sxt * sxy) / det
            if bb > 0:
                b = min(1 / 2.8, max(1 / 6.0, bb))
    num = sum(ct * (t - b * cp) for ct, cp, t in pairs)
    den = sum(ct * ct for ct, _, _ in pairs)
    a = num / den if den > 0 else 1.0 / DEFAULT_CPT
    cpt_tool = min(6.5, max(1.6, 1.0 / a)) if a > 0 else DEFAULT_CPT
    return cpt_tool, 1.0 / b


def _tool_loading(ctx, main, cpt, read_mult, base):
    """Deferred (ToolSearch) vs direct tool loading, from what the transcript shows."""
    if not ctx["deferred_listing"] and not ctx["tool_records"]:
        return None
    R = len(main)
    listing = ctx["deferred_listing"]
    listing_tokens = (listing[-1]["ch"] / cpt) if listing else 0.0
    deferred_names = set()
    for d in listing:
        deferred_names.update(d["added"])
        deferred_names.difference_update(d["removed"])
    seen = {}
    for rec in sorted(ctx["tool_records"], key=lambda r: r["idx"]):
        if rec["name"] not in seen:
            seen[rec["name"]] = rec
    searches = [t for t in ctx["tools"] if t["n"] == "ToolSearch" and not t["sub"]]
    by_idx = {c["idx"]: k for k, c in enumerate(main)}

    def request_index_after(idx):
        return sum(1 for c in main if c["idx"] > idx)

    def request_of(idx):
        ks = [k for k, c in enumerate(main) if c["idx"] <= idx]
        return ks[-1] if ks else 0

    loaded = []
    per_tool_listing = listing_tokens / len(deferred_names) if deferred_names else 0.0
    for name, rec in seen.items():
        schema = rec["ch"] / cpt
        after = request_index_after(rec["idx"])
        k = request_of(rec["idx"])
        # the search that loaded it: the nearest preceding ToolSearch call
        srch = max([t for t in searches if t["idx"] <= rec["idx"]], key=lambda t: t["idx"], default=None)
        overhead_tok = overhead_s = 0.0
        solo = False
        if srch is not None:
            req = main[request_of(srch["idx"])]
            solo = all(t == "ToolSearch" for t in req["tools"]) and req["stop"] == "tool_use"
            if solo:  # the whole request existed only to fetch schemas: one extra round trip
                k_req = request_of(srch["idx"])
                nxt = main[k_req + 1] if k_req + 1 < len(main) else None
                overhead_tok = req["_out"] + ((nxt["_in"] + nxt["_cc"]) if nxt else 0)
                overhead_s = max(0.0, req["t1"] - req["t0"]) + max(0.0, (srch["t1"] or srch["t0"]) - srch["t0"])
            else:     # batched with real work: only the search block and its result were extra
                overhead_tok = 40 + srch["ch"] / cpt
                overhead_s = max(0.0, (srch["t1"] or srch["t0"]) - srch["t0"])
        deferred_eff = per_tool_listing * (1 + read_mult * (R - 1)) + schema * (1 + read_mult * max(0, after - 1))
        direct_eff = schema * (1 + read_mult * (R - 1))
        loaded.append({"name": name, "schema": int(round(schema)), "loaded_at": k + 1, "requests_after": after,
                       "overhead_tokens": int(overhead_tok), "overhead_s": round(overhead_s, 1), "solo": solo,
                       "deferred_eff": int(round(deferred_eff + overhead_tok)), "direct_eff": int(round(direct_eff)),
                       "t": round(rec["ts"] - base, 3) if rec["ts"] else None})
    loaded.sort(key=lambda d: d["loaded_at"])
    n_unloaded = max(0, len(deferred_names) - len(seen))
    avg_schema = (sum(d["schema"] for d in loaded) / len(loaded)) if loaded else 600.0
    unloaded_direct = n_unloaded * avg_schema * (1 + read_mult * (R - 1))
    unloaded_deferred = per_tool_listing * n_unloaded * (1 + read_mult * (R - 1))
    return {
        "requests": R, "listing_tokens": int(round(listing_tokens)), "deferred_count": len(deferred_names),
        "loaded": loaded, "searches": len(searches), "avg_schema": int(round(avg_schema)),
        "unloaded_count": n_unloaded, "unloaded_direct_eff": int(round(unloaded_direct)),
        "unloaded_deferred_eff": int(round(unloaded_deferred)),
        "loaded_deferred_eff": int(sum(d["deferred_eff"] for d in loaded)),
        "loaded_direct_eff": int(sum(d["direct_eff"] for d in loaded)),
    }


# --------------------------------------------------------------------------- analysis

def analyze(main_entries, sub_streams=(), *, title=None, context_window=None, live=False,
            source="", now=None, poll_ms=2500, fmt=None, price=None):
    now = now or time.time()
    fmt = fmt or detect_format(main_entries, source)
    scan = _scan_pi if fmt == "pi" else _scan_claude
    ctx = _new_ctx()
    scan(main_entries, False, ctx, "main")
    for name, entries in sub_streams:
        scan(entries, True, ctx, name)

    models = [c for c in ctx["models"] if c["t1"] is not None]
    for c in models:
        if c["t0"] is None or c["t0"] > c["t1"]:
            c["t0"] = c["t1"]
        c["_in"], c["_cc"], c["_cr"], c["_out"], c["_think"], c["_think_rep"], c["_cc1h"] = _usage_numbers(c)
        tks = [b[0] for b in c["blocks"] if b[1] == "thinking" and b[0] is not None]
        c["_think_end"] = max(tks) if tks else None
    for c in models:
        if not c["sub"] and c["_think"] > 0:
            _item(ctx, c["idx"], c["t1"], "thinking", None, 0, c["_think"], {"transient": True})
    tools = [t for t in ctx["tools"] if t["t0"] is not None]
    prompts = sorted((p for p in ctx["prompts"] if p["t0"] is not None), key=lambda p: p["t0"])
    interrupts = [x for x in ctx["interrupts"] if x["t0"] is not None]
    errors = [x for x in ctx["errors"] if x["t0"] is not None]

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
        raise SystemExit("no timestamped entries found; is this a Claude Code or pi transcript?")
    t_start = min(stamps)
    t_end = max(stamps)
    if live:
        t_end = max(t_end, now)

    for t in tools:
        if t["t1"] is None:
            t["open"] = 1
            t["t1"] = t_end if live else t["t0"]

    compactions = []
    used = set()
    for b in ctx["compact_boundaries"]:
        t0 = b["prev"] if b["prev"] is not None else b["ts"]
        t1 = b["ts"]
        for i, s in enumerate(ctx["compact_summaries"]):
            if s["stream"] == b["stream"] and s["idx"] > b["idx"] and s["idx"] - b["idx"] <= 12 and s["ts"]:
                t1 = max(t1 or s["ts"], s["ts"])
                used.add(i)
                break
        if t0 is None or t1 is None:
            continue
        label = (b.get("trigger") or "compaction")
        if b.get("pre"):
            label += f" · {fmt_tokens(b['pre'])} tokens before"
        compactions.append({"k": "c", "t0": min(t0, t1), "t1": max(t0, t1), "l": label, "sub": 0})
    for i, s in enumerate(ctx["compact_summaries"]):
        if i in used or s["ts"] is None:
            continue
        prev = max([t for t in stamps if t < s["ts"]] or [s["ts"]])
        compactions.append({"k": "c", "t0": prev, "t1": s["ts"], "l": "compaction", "sub": 0})
    for ce in ctx["compact_events"]:
        if ce["t1"] is None:
            continue
        t0 = ce["t0"] if ce["t0"] is not None else ce["t1"]
        compactions.append({"k": "c", "t0": min(t0, ce["t1"]), "t1": ce["t1"], "l": ce["label"], "sub": 0})
    compactions.sort(key=lambda c: c["t0"])

    tg, phase_method = _phase_split(models)

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

    laps = []
    for i, p in enumerate(prompts):
        t1 = prompts[i + 1]["t0"] if i + 1 < len(prompts) else t_end
        n_calls = sum(1 for c in models if not c["sub"] and p["t0"] <= c["t0"] < t1)
        n_tools = sum(1 for t in tools if not t["sub"] and p["t0"] <= t["t0"] < t1)
        laps.append({"n": i + 1, "t0": p["t0"], "t1": max(t1, p["t0"]), "calls": n_calls, "tools": n_tools, "l": p["l"]})
    lap_of = lambda t: next((lp["n"] for lp in reversed(laps) if lp["t0"] <= t), None)

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
    window = auto_window(models, context_window)
    ctx_used = (last["_in"] + last["_cc"] + last["_cr"]) if last else 0
    model_names = sorted({c["model"] for c in models if c.get("model")})
    wall = max(0.0, t_end - t_start)

    # ---- cost
    primary_model = None
    if models:
        counts = defaultdict(int)
        for c in models:
            counts[c.get("model") or ""] += c["_in"] + c["_cc"] + c["_cr"] + c["_out"]
        primary_model = max(counts, key=counts.get)
    pricing = pricing_for(primary_model, price)
    cost = None
    if pricing:
        p_in, p_out, rd, w5, w1 = pricing[:5]
        c_in = c_out = c_read = c_write = 0.0
        for c in models:
            pr_ = pricing_for(c.get("model"), price) or pricing
            pi_, po_, rd_, w5_, w1_ = pr_[:5]
            cc1h = c["_cc1h"]
            cc5m = max(0, c["_cc"] - cc1h)
            c_in += c["_in"] * pi_ / 1e6
            c_write += (cc5m * w5_ + cc1h * w1_) * pi_ / 1e6
            c_read += c["_cr"] * rd_ * pi_ / 1e6
            c_out += c["_out"] * po_ / 1e6
        cost = {"total": c_in + c_out + c_read + c_write, "input": c_in, "output": c_out,
                "cache_read": c_read, "cache_write": c_write,
                "pricing": {"model": primary_model, "in": p_in, "out": p_out, "read_mult": rd,
                            "write_5m_mult": w5, "write_1h_mult": w1, "source": pricing[5]}}
    reported = ctx["reported_cost"] if fmt == "pi" and ctx["reported_cost"] else None

    stats = {
        "tg_s": (tok_out / decode_time) if decode_time > 0 else None,
        "pp_s": (tok_computed / prefill_time) if prefill_time > 0 else None,
        "laps": len(laps),
        "avg_lap_s": (sum(lp["t1"] - lp["t0"] for lp in laps) / len(laps)) if laps else None,
        "time": {"wall": wall, "prefill": prefill_time, "reasoning": totals["reasoning"],
                 "generation": totals["generation"], "decode": decode_time, "tools": totals["tools"],
                 "compaction": totals["compaction"], "idle": totals["idle"], "idle_wait": idle_wait,
                 "idle_overhead": idle_over, "tools_sum": sum(t["t1"] - t["t0"] for t in tools)},
        "tokens": {"total": tok_computed + tok_cached + tok_out, "prompt_computed": tok_computed,
                   "prompt_cache_write": tok_cache_write, "prompt_cached": tok_cached,
                   "completion": tok_out, "reasoning": tok_think, "tool_results_est": tok_tool_est,
                   "cache_hit_ratio": (tok_cached / (tok_cached + tok_computed)) if (tok_cached + tok_computed) else None},
        "context": {"used": ctx_used, "window": window, "pct": (ctx_used / window) if window else None},
        "counts": {"calls": len(models) + len(tools) + len(compactions), "assistant": len(models),
                   "assistant_sub": sum(1 for c in models if c["sub"]), "tools": len(tools),
                   "tools_sub": sum(1 for t in tools if t["sub"]), "tool_errors": sum(t["err"] for t in tools),
                   "compactions": len(compactions), "interrupts": len([x for x in interrupts if x["k"] == "x"]),
                   "errors": len(errors), "prompts": len(prompts)},
        "cost": cost, "cost_reported": reported,
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
        calls.append({"k": x["k"], "t0": rel(x["t0"]), "t1": rel(x["t0"]), "l": x["l"], "lap": lap_of(x["t0"])})
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

    ledger = None
    try:
        ledger = build_ledger(ctx, models, pricing, window, base)
    except Exception as exc:  # the ledger is an add-on: never take the panel down with it
        ledger = {"error": f"{type(exc).__name__}: {exc}"}

    default_title = ctx["meta"].get("name") or (_title_from(prompts[0]["l"]) if prompts else (ctx["meta"].get("session_id") or "session"))
    meta = {
        "title": title or default_title,
        "source": source,
        "fmt": fmt,
        "agent": "pi" if fmt == "pi" else "claude-code",
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
            "phase_method": phase_method,
            "prefill": {"blocks": "estimated from the visible-text decode speed", "regression": "estimated by a latency fit",
                        "none": "not separable"}[phase_method],
            "reasoning_tokens": ("reported" if think_rep and all(think_rep) else
                                 "estimated" if not any(think_rep) else "mixed"),
        },
        "lapboard": __version__,
    }
    return {"meta": meta, "base": base, "end": rel(t_end), "stats": stats, "calls": calls,
            "laps": [{**lp, "t0": rel(lp["t0"]), "t1": rel(lp["t1"])} for lp in laps],
            "tools": tools_out, "idle": [[rel(a), rel(b), w] for a, b, w in idle_out],
            "ledger": ledger}


# --------------------------------------------------------------------------- state (fleet)

STATE_ORDER = {"blocked": 0, "done": 1, "working": 2, "idle": 3, "unknown": 4}


def derive_state(payload, mtime, now=None):
    """herdr-style state from the transcript tail: working / blocked / done / idle."""
    now = now or time.time()
    age = now - mtime
    if age > 6 * 3600:
        return "idle"
    calls = payload["calls"]
    open_tools = [c for c in calls if c["k"] == "t" and c.get("open")]
    models = [c for c in calls if c["k"] == "m"]
    laps = payload["laps"]
    if open_tools:
        return "blocked" if age > 20 else "working"
    last_model = max(models, key=lambda c: c["t1"]) if models else None
    last_lap = laps[-1] if laps else None
    if last_lap and (not last_model or last_model["t0"] < last_lap["t0"]):
        return "working" if age < 600 else "idle"
    if last_model and last_model.get("stop") in ("end_turn", "stop", None):
        return "done" if age < 1800 else "idle"
    if age < 120:
        return "working"
    return "idle"


# --------------------------------------------------------------------------- herdr

def herdr_config_dir() -> Path:
    if os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"]) / "herdr"
    return Path.home() / ".config" / "herdr"


def herdr_socket_paths():
    out = []
    env = os.environ.get("HERDR_SOCKET_PATH")
    if env:
        out.append(Path(env))
    root = herdr_config_dir()
    out.append(root / "herdr.sock")
    out.extend(Path(p) for p in glob.glob(str(root / "sessions" / "*" / "herdr.sock")))
    seen, uniq = set(), []
    for p in out:
        if p not in seen and p.exists():
            seen.add(p)
            uniq.append(p)
    return uniq


def herdr_request(sock_path, method, params=None, timeout=1.5):
    """One call on herdr's newline-delimited JSON socket API."""
    if not hasattr(socket, "AF_UNIX"):
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        s.sendall((json.dumps({"id": "lapboard-1", "method": method, "params": params or {}}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0]
        return json.loads(line) if line else None
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def herdr_agents():
    """Agents known to running herdr servers, keyed by agent session id / path and by cwd."""
    by_session, by_cwd, servers = {}, defaultdict(list), []
    for sp in herdr_socket_paths():
        resp = herdr_request(sp, "agent.list")
        if not resp or "result" not in resp:
            continue
        res = resp["result"]
        agents = res.get("agents") if isinstance(res, dict) else res
        if not isinstance(agents, list):
            continue
        servers.append(str(sp))
        for a in agents:
            if not isinstance(a, dict):
                continue
            rec = {"status": a.get("agent_status") or "unknown", "agent": a.get("display_agent") or a.get("agent"),
                   "pane_id": a.get("pane_id"), "tab_id": a.get("tab_id"), "workspace_id": a.get("workspace_id"),
                   "name": a.get("name") or a.get("title"), "cwd": a.get("cwd") or a.get("foreground_cwd"),
                   "server": sp.parent.name if sp.parent.name != "herdr" else "default"}
            sess = a.get("agent_session") or {}
            if isinstance(sess, dict) and sess.get("value"):
                by_session[str(sess["value"])] = rec
            if rec["cwd"]:
                by_cwd[rec["cwd"]].append(rec)
    return {"by_session": by_session, "by_cwd": by_cwd, "servers": servers}


def fleet_payload(limit=30, price=None, context_window=None, now=None, cache=None):
    now = now or time.time()
    sessions = iter_sessions()[:limit]
    herdr = herdr_agents()
    rows = []
    for s in sessions:
        key = str(s["path"])
        entry = cache.get(key) if cache is not None else None
        if entry is None or entry["mtime"] != s["mtime"]:
            reader = TranscriptReader(s["path"])
            reader.refresh()
            try:
                p = analyze(reader.entries, [], source=key, fmt=s["fmt"], price=price,
                            context_window=context_window, live=False, now=now)
            except SystemExit:
                continue
            entry = {"mtime": s["mtime"], "payload": p}
            if cache is not None:
                cache[key] = entry
        p = entry["payload"]
        st = p["stats"]
        h = herdr["by_session"].get(p["meta"].get("session_id") or "") or herdr["by_session"].get(key)
        if h is None and p["meta"].get("cwd") and len(herdr["by_cwd"].get(p["meta"]["cwd"], [])) == 1:
            h = herdr["by_cwd"][p["meta"]["cwd"]][0]
        state = derive_state(p, s["mtime"], now)
        rows.append({
            "path": key, "fmt": s["fmt"], "session_id": p["meta"].get("session_id") or s["session_id"],
            "title": p["meta"]["title"], "cwd": p["meta"].get("cwd"), "branch": p["meta"].get("git_branch"),
            "models": p["meta"].get("models"), "state": state, "herdr": h,
            "last_activity": s["mtime"], "age_s": now - s["mtime"], "wall_s": st["time"]["wall"],
            "laps": st["laps"], "calls": st["counts"]["calls"], "tools": st["counts"]["tools"],
            "computed": st["tokens"]["prompt_computed"], "cached": st["tokens"]["prompt_cached"],
            "completion": st["tokens"]["completion"], "context_pct": st["context"]["pct"],
            "context_used": st["context"]["used"], "cost": (st["cost"] or {}).get("total") if st.get("cost") else None,
            "cost_reported": st.get("cost_reported"), "idle_wait_s": st["time"]["idle_wait"],
            "blocked_hint": next((c["n"] for c in p["calls"] if c["k"] == "t" and c.get("open")), None),
        })
    rows.sort(key=lambda r: (STATE_ORDER.get(r["herdr"]["status"] if r["herdr"] else r["state"], 9), -r["last_activity"]))
    return {"generated_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="seconds"),
            "now": now, "rows": rows, "herdr_servers": herdr["servers"], "lapboard": __version__,
            "sources": {"claude": str(projects_dir()), "pi": str(pi_sessions_dir())}}


# --------------------------------------------------------------------------- rendering

def _inline(template_path, payload, fragment=False, title=None):
    if not template_path.is_file():
        raise SystemExit(f"template not found: {template_path}")
    tpl = template_path.read_text(encoding="utf-8")
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    title = title or "lapboard"
    safe = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    page = tpl.replace(DATA_MARKER, data).replace("<title>lapboard</title>", f"<title>{safe}</title>", 1)
    if not fragment:
        return page
    head = re.search(r"<!--head-->(.*?)<!--/head-->", page, re.S)
    body = re.search(r"<!--body-->(.*?)<!--/body-->", page, re.S)
    return (head.group(1) if head else "") + (body.group(1) if body else "")


def render_html(payload, fragment=False):
    return _inline(TEMPLATE_PATH, payload, fragment, (payload.get("meta") or {}).get("title"))


def render_fleet_html(payload, fragment=False):
    return _inline(FLEET_TEMPLATE_PATH, payload, fragment, "lapboard fleet")


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
                   source=str(reader.path), now=now, poll_ms=int(getattr(args, "interval", 2.5) * 1000),
                   fmt=(None if getattr(args, "format", "auto") == "auto" else args.format),
                   price=parse_price(getattr(args, "price", None)))


# --------------------------------------------------------------------------- commands

def cmd_list(args):
    sessions = iter_sessions()
    if not sessions:
        print(f"no sessions under {projects_dir()} or {pi_sessions_dir()}")
        return 1
    print(f"{len(sessions)} session(s), newest first\n")
    for s in sessions[: args.n]:
        when = dt.datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")
        size = f"{s['size'] / 1e6:5.1f} MB"
        print(f"  {when}  {size}  {s['fmt']:<6}  {s['session_id'][:8]}  {s['project']:<28.28}  {first_prompt(s['path'])}")
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
    cost = f" · ~${st['cost']['total']:.2f}" if st.get("cost") else ""
    print(f"wrote {out}  ({out.stat().st_size / 1e3:.0f} kB)  {payload['meta']['agent']} · "
          f"wall {fmt_duration(st['time']['wall'])} · {st['counts']['assistant']} assistant calls · "
          f"{st['counts']['tools']} tool calls · {st['laps']} laps{cost}")
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


def _serve(handler_fn, host, port, banner, open_browser):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            route = self.path.split("?", 1)[0]
            out = handler_fn(route)
            if out is None:
                self.send_response(404)
                self.end_headers()
                return
            body, ctype = out
            body = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            pass

    server = http.server.ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"{banner}\n          live at {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


def cmd_serve(args):
    path = resolve_session(args.session)
    reader, subs = load_streams(path, not args.no_subagents)
    state = _LiveState(reader, subs, args)
    state.snapshot()

    def handle(route):
        if route in ("/", "/index.html"):
            return render_html(state.snapshot()), "text/html; charset=utf-8"
        if route == "/data.json":
            return json.dumps(state.snapshot(), separators=(",", ":")), "application/json"
        return None

    _serve(handle, args.host, args.port, f"lapboard: {path}", args.open)
    return 0


def cmd_fleet(args):
    price = parse_price(args.price)
    if not args.serve:
        payload = fleet_payload(args.limit, price, args.context_window)
        out = Path(args.output)
        out.write_text(render_fleet_html(payload), encoding="utf-8")
        print(f"wrote {out}: {len(payload['rows'])} sessions" +
              (f" · herdr: {', '.join(payload['herdr_servers'])}" if payload["herdr_servers"] else " · herdr: not reachable"))
        return 0
    cache = {}
    lock = threading.Lock()
    holder = {"payload": None, "at": 0.0, "sessions": {}}

    def fleet():
        with lock:
            now = time.time()
            if holder["payload"] is None or now - holder["at"] >= 3.0:
                holder["payload"] = fleet_payload(args.limit, price, args.context_window, now, cache)
                holder["payload"]["live"] = True
                holder["payload"]["poll_ms"] = 5000
                holder["at"] = now
            return holder["payload"]

    def session_state(i):
        rows = fleet()["rows"]
        if i < 0 or i >= len(rows):
            return None
        path = rows[i]["path"]
        st = holder["sessions"].get(path)
        if st is None:
            ns = argparse.Namespace(title=None, context_window=args.context_window, interval=2.5,
                                    format=rows[i]["fmt"], price=args.price)
            reader, subs = load_streams(Path(path), True)
            st = _LiveState(reader, subs, ns)
            holder["sessions"][path] = st
        return st

    def handle(route):
        if route in ("/", "/index.html"):
            return render_fleet_html(fleet()), "text/html; charset=utf-8"
        if route == "/fleet.json":
            return json.dumps(fleet(), separators=(",", ":")), "application/json"
        m = re.match(r"^/s/(\d+)/(index\.html|data\.json)?$", route)
        if m:
            st = session_state(int(m.group(1)))
            if st is None:
                return None
            if m.group(2) == "data.json":
                return json.dumps(st.snapshot(), separators=(",", ":")), "application/json"
            return render_html(st.snapshot()), "text/html; charset=utf-8"
        return None

    _serve(handle, args.host, args.port, "lapboard fleet", args.open)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lapboard", description="telemetry and token accounting for coding-agent sessions")
    ap.add_argument("--version", action="version", version=f"lapboard {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("session", help="transcript path, session id prefix, directory, or 'latest'")
    common.add_argument("--title", help="panel title (default: the first prompt, or pi's session name)")
    common.add_argument("--context-window", type=int, default=None,
                        help="context window in tokens for the fill ring (default: auto, 200k or 1M)")
    common.add_argument("--no-subagents", action="store_true", help="ignore subagent transcripts")
    common.add_argument("--format", choices=["auto", "claude", "pi"], default="auto")
    common.add_argument("--price", help="IN,OUT[,READ_MULT,WRITE5M_MULT,WRITE1H_MULT] in $/M tokens; default: built-in table")

    p = sub.add_parser("list", help="list Claude Code and pi sessions, newest first")
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
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("json", parents=[common], help="dump the computed panel data as JSON")
    p.add_argument("-o", "--output")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(fn=cmd_json, interval=2.5)

    p = sub.add_parser("fleet", help="every session with herdr-style states, tokens and cost")
    p.add_argument("--serve", action="store_true", help="live page instead of a static file")
    p.add_argument("-o", "--output", default="fleet.html")
    p.add_argument("--limit", type=int, default=30, help="most recent sessions to include")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8788)
    p.add_argument("--open", action="store_true")
    p.add_argument("--context-window", type=int, default=None)
    p.add_argument("--price", default=None)
    p.set_defaults(fn=cmd_fleet)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
