import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import lapboard  # noqa: E402
import make_sample  # noqa: E402


def entry(kind, ts, **kw):
    e = {"type": kind, "timestamp": ts, "sessionId": "s1", "cwd": "/w", "gitBranch": "main", "version": "2.1.0"}
    e.update(kw)
    return e


class TimestampTests(unittest.TestCase):
    def test_variants(self):
        z = lapboard.parse_ts("2026-09-04T16:21:27.694Z")
        self.assertAlmostEqual(z % 1, 0.694, places=3)
        self.assertEqual(lapboard.parse_ts("2026-09-04T16:21:27Z"), lapboard.parse_ts("2026-09-04T18:21:27+02:00"))
        self.assertIsNone(lapboard.parse_ts(None))
        self.assertIsNone(lapboard.parse_ts("yesterday"))


class SyntheticSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries, cls.truth = make_sample.generate(hours=3.0, laps=20, seed=3)
        cls.d = lapboard.analyze(cls.entries, title="t", source="mem")

    def test_counts(self):
        s, tr = self.d["stats"], self.truth
        self.assertEqual(s["laps"], tr["laps"])
        self.assertEqual(s["counts"]["assistant"], tr["model_calls"])
        self.assertEqual(s["counts"]["tools"], tr["tool_calls"])
        self.assertEqual(s["counts"]["compactions"], tr["compactions"])
        self.assertGreater(tr["compactions"], 0)
        self.assertFalse(any(c.get("open") for c in self.d["calls"] if c["k"] == "t"))

    def test_tokens_exact(self):
        k, tr = self.d["stats"]["tokens"], self.truth
        self.assertEqual(k["prompt_computed"], tr["prompt_computed"])
        self.assertEqual(k["prompt_cached"], tr["prompt_cached"])
        self.assertEqual(k["completion"], tr["completion"])
        self.assertEqual(k["reasoning"], tr["reasoning_tokens"])
        self.assertEqual(self.d["meta"]["estimates"]["reasoning_tokens"], "reported")

    def test_wall_clock_is_additive(self):
        t = self.d["stats"]["time"]
        parts = t["prefill"] + t["reasoning"] + t["generation"] + t["tools"] + t["compaction"] + t["idle"]
        self.assertAlmostEqual(parts, t["wall"], places=2)
        self.assertAlmostEqual(t["decode"], t["reasoning"] + t["generation"], places=6)

    def test_phase_estimates_track_truth(self):
        t, tr = self.d["stats"]["time"], self.truth
        # prefill is the estimated quantity; decode and tools are measured
        self.assertLess(abs(t["prefill"] - tr["prefill"]) / tr["prefill"], 0.25, (t["prefill"], tr["prefill"]))
        self.assertLess(abs(t["reasoning"] - tr["reasoning"]) / tr["reasoning"], 0.25)
        self.assertLess(abs(t["tools_sum"] - tr["tool_sum"]) / tr["tool_sum"], 0.02)
        self.assertLess(abs(self.d["meta"]["estimates"]["decode_tok_s"] - 62.0) / 62.0, 0.1)

    def test_calls_are_ordered_and_in_range(self):
        end = self.d["end"]
        prev = -1
        for c in self.d["calls"]:
            if c["k"] == "m":
                self.assertGreaterEqual(c["t0"], prev)
                prev = c["t0"]
                self.assertLessEqual(c["t0"], c["p"][0])
                self.assertLessEqual(c["p"][0], c["p"][1])
                self.assertLessEqual(c["p"][1], c["t1"])
            self.assertGreaterEqual(c["t0"], 0)
            self.assertLessEqual(c["t1"], end + 1e-6)


class HandBuiltTests(unittest.TestCase):
    """A request whose tool result lands between two of its own blocks (streaming tool execution)."""

    def transcript(self):
        u = {"input_tokens": 10, "cache_creation_input_tokens": 990, "cache_read_input_tokens": 50000,
             "output_tokens": 300, "output_tokens_details": {"thinking_tokens": 200}}
        blk = lambda i, content: entry("assistant", None, requestId="r1", apiBlockIndex=i, message={
            "id": "m1", "role": "assistant", "model": "claude-x", "content": [content], "stop_reason": "tool_use", "usage": u})
        return [
            entry("user", "2026-01-01T00:00:00.000Z", message={"role": "user", "content": "do the thing"}),
            entry("attachment", "2026-01-01T00:00:00.100Z", attachment={"type": "date"}),
            entry("user", "2026-01-01T00:00:05.000Z", message={"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}),
            entry("user", "2026-01-01T00:00:10.000Z", message={"role": "user", "content": "do it anyway"}),
            dict(blk(0, {"type": "thinking", "thinking": "hmm" * 100}), timestamp="2026-01-01T00:00:16.000Z"),
            dict(blk(1, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls\npwd"}}), timestamp="2026-01-01T00:00:17.000Z"),
            entry("user", "2026-01-01T00:00:17.500Z", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a" * 400}]}),
            dict(blk(2, {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/x"}}), timestamp="2026-01-01T00:00:18.000Z"),
            entry("user", "2026-01-01T00:00:19.000Z", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "b" * 40, "is_error": True}]}),
            entry("system", "2026-01-01T00:00:30.000Z", subtype="compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 51000}),
            entry("user", "2026-01-01T00:00:30.100Z", isCompactSummary=True, message={"role": "user", "content": "This session is being continued..."}),
        ]

    def test_request_window_and_tools(self):
        d = lapboard.analyze(self.transcript(), source="mem")
        models = [c for c in d["calls"] if c["k"] == "m"]
        self.assertEqual(len(models), 1)
        m = models[0]
        self.assertAlmostEqual(m["t0"], 10.0, places=3)   # starts at the second prompt, not the tool result
        self.assertAlmostEqual(m["t1"], 18.0, places=3)   # ends at its last block
        self.assertEqual(m["tools"], ["Bash", "Read"])
        tools = {c["n"]: c for c in d["calls"] if c["k"] == "t"}
        self.assertAlmostEqual(tools["Bash"]["t1"] - tools["Bash"]["t0"], 0.5, places=3)
        self.assertEqual(tools["Bash"]["l"], "ls")
        self.assertEqual(tools["Read"]["err"], 1)
        self.assertEqual(d["stats"]["counts"]["tool_errors"], 1)

    def test_laps_interrupts_compaction_meta(self):
        d = lapboard.analyze(self.transcript(), source="mem")
        self.assertEqual(d["stats"]["laps"], 2)
        self.assertEqual(d["stats"]["counts"]["interrupts"], 1)
        self.assertEqual(d["stats"]["counts"]["compactions"], 1)
        comp = [c for c in d["calls"] if c["k"] == "c"][0]
        self.assertAlmostEqual(comp["t0"], 19.0, places=3)
        self.assertAlmostEqual(comp["t1"], 30.1, places=3)
        self.assertIn("51.0k tokens before", comp["l"])
        self.assertEqual(d["meta"]["cwd"], "/w")
        self.assertEqual(d["meta"]["git_branch"], "main")
        self.assertEqual(d["meta"]["cli_version"], "2.1.0")
        self.assertEqual(d["stats"]["context"]["used"], 51000)
        self.assertEqual(d["stats"]["context"]["window"], 200000)

    def test_no_decode_observation_falls_back(self):
        # a single request with only a thinking block: decode speed cannot be observed
        d = lapboard.analyze(self.transcript()[:5] + [entry("user", "2026-01-01T00:00:20.000Z", message={"role": "user", "content": "x"})], source="mem")
        self.assertIsNone(d["meta"]["estimates"]["decode_tok_s"])
        self.assertEqual(d["stats"]["time"]["prefill"], 0.0)
        self.assertGreater(d["stats"]["time"]["reasoning"], 0.0)


class RenderTests(unittest.TestCase):
    def test_fragment_and_escaping(self):
        entries, _ = make_sample.generate(hours=0.2, laps=2, seed=1)
        for e in entries:  # plant a script terminator inside a tool label
            blocks = (e.get("message") or {}).get("content")
            if e["type"] == "assistant" and isinstance(blocks, list) and blocks[0]["type"] == "tool_use":
                blocks[0]["input"] = {"command": "echo '</script> inside'"}
                break
        d = lapboard.analyze(entries, source="mem")
        page = lapboard.render_html(d)
        self.assertIn("<!doctype html>", page.lower())
        self.assertNotIn("</script> inside", page)
        self.assertIn("<\\/script> inside", page)
        frag = lapboard.render_html(d, fragment=True)
        self.assertNotIn("<html", frag)
        self.assertNotIn("<body", frag)
        self.assertTrue(frag.lstrip().startswith("<title>"))
        self.assertIn('id="lapboard-data"', frag)


if __name__ == "__main__":
    unittest.main()
