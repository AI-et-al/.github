import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import lapboard  # noqa: E402
import make_sample  # noqa: E402


def iso(sec):
    import datetime as dt
    return dt.datetime.fromtimestamp(sec, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(round((sec % 1) * 1000)):03d}Z"


class PiSessionTests(unittest.TestCase):
    """A hand-built pi session in the shape of pi-mono's fixtures."""

    def entries(self):
        t = 1_765_000_000.0
        usage = lambda inp, out, cr, cw, cost: {"input": inp, "output": out, "cacheRead": cr, "cacheWrite": cw,
                                                 "totalTokens": inp + out + cr + cw, "reasoning": 40,
                                                 "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": cost}}
        return [
            {"type": "session", "version": 3, "id": "pi-1", "timestamp": iso(t), "cwd": "/w/proj"},
            {"type": "model_change", "id": "e0", "parentId": None, "timestamp": iso(t), "provider": "anthropic", "modelId": "claude-sonnet-4-5"},
            {"type": "message", "id": "e1", "parentId": "e0", "timestamp": iso(t + 1), "message": {"role": "user", "content": [{"type": "text", "text": "port the kernel"}], "timestamp": (t + 1) * 1000}},
            {"type": "message", "id": "e2", "parentId": "e1", "timestamp": iso(t + 9), "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "plan"}, {"type": "text", "text": "Reading."},
                {"type": "toolCall", "id": "c1", "name": "read", "arguments": {"path": "/w/proj/a.py"}},
                {"type": "toolCall", "id": "c2", "name": "bash", "arguments": {"command": "make test"}}],
                "api": "anthropic-messages", "provider": "anthropic", "model": "claude-sonnet-4-5",
                "usage": usage(1000, 300, 20000, 500, 0.05), "stopReason": "toolUse", "timestamp": (t + 1) * 1000}},
            {"type": "message", "id": "e3", "parentId": "e2", "timestamp": iso(t + 9.3), "message": {"role": "toolResult", "toolCallId": "c1", "toolName": "read", "content": [{"type": "text", "text": "x" * 4000}], "isError": False, "timestamp": (t + 9.3) * 1000}},
            {"type": "message", "id": "e4", "parentId": "e3", "timestamp": iso(t + 14), "message": {"role": "toolResult", "toolCallId": "c2", "toolName": "bash", "content": [{"type": "text", "text": "FAILED"}], "isError": True, "timestamp": (t + 14) * 1000}},
            {"type": "message", "id": "e5", "parentId": "e4", "timestamp": iso(t + 20), "message": {"role": "assistant", "content": [{"type": "text", "text": "Fixed it."}],
                "api": "anthropic-messages", "provider": "anthropic", "model": "claude-sonnet-4-5",
                "usage": usage(50, 120, 21500, 1200, 0.03), "stopReason": "stop", "timestamp": (t + 14) * 1000}},
            {"type": "compaction", "id": "e6", "parentId": "e5", "timestamp": iso(t + 30), "summary": "S" * 800, "firstKeptEntryId": "e5", "tokensBefore": 23000},
            {"type": "message", "id": "e7", "parentId": "e6", "timestamp": iso(t + 31), "message": {"role": "bashExecution", "command": "ls", "output": "a.py", "exitCode": 0, "timestamp": (t + 31) * 1000}},
        ]

    def test_pi_parse(self):
        d = lapboard.analyze(self.entries(), source="/home/x/.pi/agent/sessions/w/2025_pi-1.jsonl")
        self.assertEqual(d["meta"]["fmt"], "pi")
        self.assertEqual(d["meta"]["cwd"], "/w/proj")
        self.assertEqual(d["meta"]["models"], ["claude-sonnet-4-5"])
        s = d["stats"]
        self.assertEqual(s["laps"], 1)
        self.assertEqual(s["counts"]["assistant"], 2)
        self.assertEqual(s["counts"]["tools"], 2)
        self.assertEqual(s["counts"]["tool_errors"], 1)
        self.assertEqual(s["counts"]["compactions"], 1)
        models = [c for c in d["calls"] if c["k"] == "m"]
        self.assertAlmostEqual(models[0]["t1"] - models[0]["t0"], 8.0, places=2)   # message ts -> entry ts
        self.assertEqual(models[0]["stop"], "tool_use")
        self.assertEqual(models[1]["stop"], "end_turn")
        tools = {c["n"]: c for c in d["calls"] if c["k"] == "t"}
        self.assertAlmostEqual(tools["read"]["t1"] - tools["read"]["t0"], 0.3, places=2)  # starts when the message ends
        self.assertAlmostEqual(tools["bash"]["t0"], tools["read"]["t1"], places=3)       # sequential
        self.assertEqual(tools["bash"]["err"], 1)
        self.assertEqual(s["tokens"]["prompt_computed"], 1000 + 500 + 50 + 1200)
        self.assertEqual(s["tokens"]["prompt_cached"], 41500)
        self.assertEqual(s["tokens"]["reasoning"], 80)
        self.assertAlmostEqual(s["cost_reported"], 0.08, places=6)
        self.assertIsNotNone(s["cost"])
        self.assertEqual(s["cost"]["pricing"]["model"], "claude-sonnet-4-5")
        comp = [c for c in d["calls"] if c["k"] == "c"][0]
        self.assertIn("23.0k tokens before", comp["l"])
        self.assertEqual(s["context"]["used"], 50 + 1200 + 21500)
        self.assertEqual(d["ledger"]["cum"]["requests"], 2)

    def test_format_detection(self):
        self.assertEqual(lapboard.detect_format(self.entries()), "pi")
        self.assertEqual(lapboard.detect_format([{"type": "user", "message": {"role": "user", "content": "x"}}]), "claude")


class LedgerTests(unittest.TestCase):
    def transcript(self):
        t = 1_700_000_000.0
        usage = lambda inp, cc, cr, out, th: {"input_tokens": inp, "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
                                               "output_tokens": out, "output_tokens_details": {"thinking_tokens": th},
                                               "cache_creation": {"ephemeral_1h_input_tokens": cc, "ephemeral_5m_input_tokens": 0}}
        base = {"sessionId": "s", "cwd": "/w", "gitBranch": "main", "version": "2.1.0"}
        def asst(i, ts, rid, blocks, u):
            return [dict(base, type="assistant", timestamp=iso(ts + j * 0.5), requestId=rid, apiBlockIndex=j,
                         message={"id": "m" + rid, "role": "assistant", "model": "claude-sonnet-4-6", "content": [b], "stop_reason": "tool_use", "usage": u})
                    for j, b in enumerate(blocks)]
        sp = ["You are an agent." * 40, "# Environment\n" + "e" * 400]
        e = []
        e.append(dict(base, type="user", timestamp=iso(t), message={"role": "user", "content": "start the port"}))
        e.append(dict(base, type="attachment", timestamp=iso(t), attachment={"type": "skill_listing", "content": "- a: b" * 200}, rendered=[{"content": "<system-reminder>" + "- a: b" * 200 + "</system-reminder>"}]))
        e.append(dict(base, type="attachment", timestamp=iso(t), attachment={"type": "deferred_tools_delta", "addedNames": ["T%d" % i for i in range(30)], "removedNames": []}, rendered=[{"content": "tools: " + ", ".join("T%d" % i for i in range(30))}]))
        e.append(dict(base, type="attachment", timestamp=iso(t), attachment={"type": "prompt_snapshot", "systemPrompt": sp, "hostPrompt": "h"}))
        e += asst(0, t + 5, "r1", [{"type": "thinking", "thinking": "..."}, {"type": "tool_use", "id": "u1", "name": "ToolSearch", "input": {"query": "select:T3"}}], usage(2, 3000, 8000, 300, 200))
        e.append(dict(base, type="user", timestamp=iso(t + 6.5), message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u1", "content": "[{\"type\":\"tool_reference\",\"tool_name\":\"T3\"}]"}]}))
        e.append(dict(base, type="attachment", timestamp=iso(t + 6.5), attachment={"type": "deferred_tools_record", "entries": [{"name": "T3", "description": "d" * 600, "input_schema": {"type": "object"}}]}))
        e += asst(0, t + 8, "r2", [{"type": "thinking", "thinking": "..."}, {"type": "tool_use", "id": "u2", "name": "T3", "input": {"path": "/x"}}], usage(30, 900, 11000, 200, 100))
        e.append(dict(base, type="user", timestamp=iso(t + 9), message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u2", "content": "y" * 12000}]}))
        e += asst(0, t + 12, "r3", [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "done"}], usage(30, 4000, 11900, 50, 20))
        # a system-prompt change forces a rebuild: everything after the tool block is recomputed
        sp2 = [sp[0], sp[1] + " changed"]
        e.append(dict(base, type="user", timestamp=iso(t + 700), message={"role": "user", "content": "continue"}))
        e.append(dict(base, type="attachment", timestamp=iso(t + 700), attachment={"type": "prompt_snapshot", "systemPrompt": sp2, "hostPrompt": "h"}))
        e += asst(0, t + 705, "r4", [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "ok"}], usage(2, 12000, 3900, 40, 10))
        return e

    def test_ledger_shape_events_and_tools(self):
        d = lapboard.analyze(self.transcript(), source="mem")
        L = d["ledger"]
        self.assertNotIn("error", L)
        self.assertEqual(L["cum"]["requests"], 4)
        self.assertEqual(len(L["series"]), 4)
        keys = {r["key"] for r in L["now"]["rows"]}
        self.assertIn("system", keys)
        self.assertIn("attach", keys)
        self.assertIn("tool_schema", keys)
        self.assertEqual(len(L["events"]), 1)
        ev = L["events"][0]
        self.assertEqual(ev["k"], 3)
        self.assertIn("system prompt changed", ev["cause"])
        self.assertIn("# Environment", ev["cause"])
        self.assertEqual(L["tool_block_hint"], 3900)
        T = L["tools"]
        self.assertEqual(T["deferred_count"], 30)
        self.assertEqual(T["unloaded_count"], 29)
        self.assertEqual(T["loaded"][0]["name"], "T3")
        self.assertTrue(T["loaded"][0]["solo"])
        self.assertEqual(T["loaded"][0]["loaded_at"], 1)
        self.assertEqual(T["loaded"][0]["requests_after"], 3)
        self.assertGreater(T["unloaded_direct_eff"], T["unloaded_deferred_eff"])
        for s in L["series"]:  # estimates never exceed the measured window
            self.assertLessEqual(sum(s["g"].values()), s["m"] + 1)
        cost = d["stats"]["cost"]
        self.assertEqual(cost["pricing"]["model"], "claude-sonnet-4-6")
        self.assertAlmostEqual(cost["cache_write"], (3000 + 900 + 4000 + 12000) * 2.0 * 3 / 1e6, places=9)
        self.assertAlmostEqual(cost["output"], (300 + 200 + 50 + 40) * 15 / 1e6, places=9)
        self.assertTrue(all(r["cost"] is not None for r in L["cum"]["rows"]))
        self.assertAlmostEqual(sum(r["cost"] for r in L["cum"]["rows"]), cost["input"] + cost["cache_write"] + cost["cache_read"], places=6)

    def test_price_override_and_unknown_model(self):
        e = self.transcript()
        for x in e:
            if x.get("type") == "assistant":
                x["message"]["model"] = "mystery-model"
        d = lapboard.analyze(e, source="mem")
        self.assertIsNone(d["stats"]["cost"])
        d = lapboard.analyze(e, source="mem", price=lapboard.parse_price("4,20"))
        self.assertEqual(d["stats"]["cost"]["pricing"]["source"], "--price")
        self.assertAlmostEqual(d["stats"]["cost"]["output"], 590 * 20 / 1e6, places=9)


class ImageTests(unittest.TestCase):
    def test_png_dims_and_tokens(self):
        import base64, struct, zlib
        w, h = 1440, 900
        raw = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0) + b"\0\0\0\0"
        data = base64.b64encode(raw).decode()
        self.assertEqual(lapboard.image_dims("image/png", data), (w, h))
        self.assertEqual(lapboard.image_tokens("image/png", data), 1534)   # 1.296 MP scaled to the 1.15 MP cap
        self.assertEqual(lapboard.image_tokens("image/png", data, None, "claude-opus-4-8"), 1728)  # hi-res: no downscale
        big = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 4000, 3000, 8, 6, 0, 0, 0) + b"\0\0\0\0"
        bd = base64.b64encode(big).decode()
        self.assertLess(lapboard.image_tokens("image/png", bd), lapboard.image_tokens("image/png", bd, None, "claude-fable-5-1"))
        self.assertEqual(lapboard.image_tokens("image/png", "notbase64!!"), 1000)


class FleetTests(unittest.TestCase):
    def test_derive_state(self):
        now = 1_000_000.0
        entries, _ = make_sample.generate(hours=0.3, laps=3, seed=2)
        p = lapboard.analyze(entries, source="mem")
        self.assertEqual(lapboard.derive_state(p, now - 30, now), "done")
        self.assertEqual(lapboard.derive_state(p, now - 7200, now), "idle")
        # an unanswered tool call: working while fresh, blocked once it has waited
        p2 = json.loads(json.dumps(p))
        p2["calls"].append({"k": "t", "t0": p2["end"], "t1": p2["end"], "n": "AskUserQuestion", "open": 1, "l": "", "lap": 3, "sub": 0, "err": 0, "ch": 0})
        self.assertEqual(lapboard.derive_state(p2, now - 5, now), "working")
        self.assertEqual(lapboard.derive_state(p2, now - 60, now), "blocked")

    def test_fleet_payload_with_fake_herdr(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("no unix sockets")
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "claude" / "projects" / "-w-proj"
            proj.mkdir(parents=True)
            entries, _ = make_sample.generate(hours=0.2, laps=2, seed=5, session_id="abc-session", cwd="/w/proj")
            path = proj / "abc-session.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            sock_dir = Path(tmp) / "herdr"
            sock_dir.mkdir()
            sock_path = sock_dir / "herdr.sock"
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(sock_path))
            srv.listen(2)
            seen = {}

            def serve():
                conn, _ = srv.accept()
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                req = json.loads(data.split(b"\n")[0])
                seen["req"] = req
                resp = {"id": req["id"], "result": {"agents": [
                    {"pane_id": "p1", "tab_id": "t1", "workspace_id": "w1", "agent": "claude", "display_agent": "Claude Code",
                     "agent_status": "blocked", "cwd": "/w/proj", "name": "port",
                     "agent_session": {"source": "herdr:claude", "agent": "claude", "kind": "id", "value": "abc-session"}}]}}
                conn.sendall((json.dumps(resp) + "\n").encode())
                conn.close()

            th = threading.Thread(target=serve, daemon=True)
            th.start()
            old = dict(os.environ)
            os.environ["CLAUDE_CONFIG_DIR"] = str(Path(tmp) / "claude")
            os.environ["PI_CODING_AGENT_DIR"] = str(Path(tmp) / "nope")
            os.environ["HERDR_SOCKET_PATH"] = str(sock_path)
            os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "xdg")
            try:
                fp = lapboard.fleet_payload(limit=5)
            finally:
                os.environ.clear()
                os.environ.update(old)
                srv.close()
            self.assertEqual(seen["req"]["method"], "agent.list")
            self.assertEqual(len(fp["rows"]), 1)
            row = fp["rows"][0]
            self.assertEqual(row["session_id"], "abc-session")
            self.assertEqual(row["herdr"]["status"], "blocked")
            self.assertEqual(row["herdr"]["pane_id"], "p1")
            self.assertEqual(fp["herdr_servers"], [str(sock_path)])
            html = lapboard.render_fleet_html(fp)
            self.assertIn("lapboard-data", html)


if __name__ == "__main__":
    unittest.main()
