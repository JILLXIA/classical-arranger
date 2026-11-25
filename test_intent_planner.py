"""Offline contract tests. An in-memory HTTP response substitutes for an actual model."""
from io import BytesIO
import json
import unittest
from unittest.mock import patch

from intent_planner import plan_request


class PlannerTests(unittest.TestCase):
    def run_plan(self, text, proposal):
        wrapper = {"choices": [{"message": {"content": json.dumps(proposal)}}]}
        with patch("intent_planner.request.urlopen", return_value=BytesIO(json.dumps(wrapper).encode())) as post:
            result = plan_request(text, base_url="http://localhost:11434/v1", model="mock")
            req = post.call_args.args[0]
        self.assertEqual(req.full_url, "http://localhost:11434/v1/chat/completions")
        self.assertNotIn("Authorization", req.headers)
        self.assertEqual(json.loads(req.data)["messages"][-1], {"role": "user", "content": text})
        return result

    def test_midi_preview(self):
        plan = self.run_plan("Make a funk MIDI arrangement and a matching WAV preview", {
            "midi_style": "funk", "style_quote": "funk", "outputs": ["preview", "midi"], "reference_style": "",
        })
        self.assertEqual(plan, {"midi_style": "funk", "outputs": ["midi", "preview"], "reference_style": ""})

    def test_independent_audio(self):
        ref = self.run_plan("Generate a separate neo soul audio reference", {
            "midi_style": None, "style_quote": "", "outputs": ["reference"], "reference_style": "neo soul",
        })
        self.assertEqual(ref["reference_style"], "neo soul")

    def test_neo_soul(self):
        for name in ("Neo Soul", "Neo-Soul", "neo soul"):
            text = f"Use {name} as the R&B style and export MIDI and a preview."
            with self.subTest(text=text):
                plan = self.run_plan(text, {"midi_style": "neo_soul", "style_quote": name,
                                           "outputs": ["midi", "preview"], "reference_style": ""})
                self.assertEqual(plan["midi_style"], "neo_soul")

    def test_generic_rnb_does_not_select_neo_soul(self):
        for quote in ("R&B", "Neo Soul"):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                self.run_plan("Give me an R&B MIDI arrangement", {"midi_style": "neo_soul", "style_quote": quote,
                                              "outputs": ["midi"], "reference_style": ""})

    def test_two_engines(self):
        proposal = {"midi_style": "swing", "style_quote": "Swing", "outputs": ["reference", "midi"],
                    "reference_style": "electro swing"}
        self.assertEqual(self.run_plan("Swing MIDI and a separate electro swing reference", proposal)["outputs"], ["midi", "reference"])

    def test_unsupported(self):
        with self.assertRaisesRegex(ValueError, "Which"):
            self.run_plan("Arrange the MIDI as R&B", {"clarification": "Which MIDI preset should I use: funk, swing, or Neo Soul?"})

    def test_invented_or_partial_styles(self):
        for text, quote in (("R&B MIDI", "swing"), ("electro swing MIDI", "swing"), ("swingset MIDI", "swing"),
                            ("swing-house MIDI", "swing"), ("electro swing MIDI", "electro swing")):
            with self.subTest(text=text, quote=quote), self.assertRaises(ValueError):
                self.run_plan(text, {"midi_style": "swing", "style_quote": quote, "outputs": ["midi"], "reference_style": ""})

    def test_reject_extended_schema(self):
        with self.assertRaises(ValueError):
            self.run_plan("Give me funk MIDI", {"midi_style": "funk", "style_quote": "funk", "outputs": ["midi"],
                                           "reference_style": "", "extra": "injected"})

    def test_reject_wrong_types(self):
        for outputs in ([], ["midi", "midi"], ["stems"], "midi", [[]]):
            with self.subTest(outputs=outputs), self.assertRaises(ValueError):
                self.run_plan("Make a funk MIDI arrangement", {"midi_style": "funk", "style_quote": "funk", "outputs": outputs, "reference_style": ""})


if __name__ == "__main__":
    unittest.main()
