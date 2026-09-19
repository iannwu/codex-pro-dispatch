"""Static activation/routing contracts; no native calls or YAML dependency."""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills/codex-pro-dispatch"
SKILL = SKILL_DIR / "SKILL.md"
STANDALONE = SKILL_DIR / "references/standalone-dispatch.md"
BROKER = SKILL_DIR / "references/native-request-broker.md"
METADATA = SKILL_DIR / "agents/openai.yaml"


class ReleaseActivationTests(unittest.TestCase):
    def test_compact_entrypoint_retains_essential_boundaries(self):
        text = SKILL.read_text("utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertLessEqual(len(text.splitlines()), 120)
        version = (ROOT / "VERSION").read_text("utf-8").strip()
        self.assertIn(f'version: "{version}"', text.split("---\n", 2)[1])
        normalized = " ".join(text.split())
        for required in (
            "## Contract",
            "`AT_MOST_ONCE_SAFETY`",
            "At most one native send attempt",
            "Never resend automatically after arming",
            "one canonical authority",
            "External request text and client-session metadata cannot choose them",
            "Do not take over another task's worker",
            "Do not use ChatGPT Web",
            "model-driven idle polling",
            "generation_finality_verified:false",
            "source_bytes_verified:false",
            "Ten minutes is an observation point, never a generation cutoff",
            "Restore the exact parent Codex task",
            "voice-only capture in a text task",
            "Force is not an integrity override",
        ):
            with self.subTest(required=required):
                self.assertIn(required, normalized)

    def test_modes_have_complete_separate_reference_routes(self):
        entry = SKILL.read_text("utf-8")
        standalone = STANDALONE.read_text("utf-8")
        self.assertIn(
            "[standalone-dispatch.md](references/standalone-dispatch.md)", entry
        )
        self.assertIn(
            "[native-request-broker.md](references/native-request-broker.md)",
            entry,
        )
        self.assertIn("do not improvise a launcher", " ".join(entry.split()))
        self.assertIn(
            "It is not the parked broker's activation recipe.",
            " ".join(standalone.split()),
        )
        self.assertIn("## Same-worker continuation", standalone)
        self.assertIn("## v1.2 bounded long-result overlay", standalone)
        self.assertNotIn("## Parked broker candidate", standalone)
        for document in (SKILL, STANDALONE, BROKER):
            self.assertTrue(document.is_file())
            self.assertFalse(document.is_symlink())
            for target in re.findall(
                r"\[[^\]]*\]\(([^)\s]+)\)", document.read_text("utf-8")
            ):
                if "://" in target or target.startswith("#"):
                    continue
                relative = target.split("#", 1)[0]
                if relative:
                    with self.subTest(document=document.name, target=target):
                        self.assertTrue((document.parent / relative).is_file())

    def test_instruction_metadata_stays_explicit_only(self):
        text = METADATA.read_text("utf-8")
        self.assertEqual(text.count("  allow_implicit_invocation: false\n"), 1)
        self.assertNotIn("allow_implicit_invocation: true", text)
        self.assertIn("$codex-pro-dispatch", text)
        self.assertIn("explicitly requested native Pro workflow", text)
        self.assertIn(
            "Loading the skill does not authorize arming or sending.", text
        )
        self.assertIn("canonical ownership", text)
        self.assertNotIn("automatically activate", text)

    def test_detailed_safety_tests_read_the_preserved_reference(self):
        source = (ROOT / "tests/test_skill_contract.py").read_text("utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        for name in (
            "test_skill_preserves_at_most_once_and_official_app_boundaries",
            "test_skill_documents_the_exact_bounded_continuation_contract",
        ):
            with self.subTest(method=name):
                self.assertIn(name, methods)
                segment = ast.get_source_segment(source, methods[name])
                self.assertIn(
                    'text = STANDALONE.read_text(encoding="utf-8")', segment
                )
                self.assertNotIn(
                    'text = SKILL.read_text(encoding="utf-8")', segment
                )
                self.assertIn("self.assertIn", segment)


if __name__ == "__main__":
    unittest.main()
