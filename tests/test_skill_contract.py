from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "codex-pro-dispatch" / "SKILL.md"
OPENAI_YAML = ROOT / "skills" / "codex-pro-dispatch" / "agents" / "openai.yaml"


class SkillContractTests(unittest.TestCase):
    def test_skill_has_valid_minimal_frontmatter(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        frontmatter = text.split("---\n", 2)[1]
        self.assertIn("name: codex-pro-dispatch", frontmatter)
        self.assertIn("description:", frontmatter)
        self.assertNotIn("TODO", text)

    def test_skill_preserves_exactly_once_and_official_app_boundaries(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for phrase in [
            "Never resend automatically",
            "official combined ChatGPT/Codex desktop app",
            "Do not use ChatGPT Web",
            "Restore the exact parent Codex task",
            "Do not use the clipboard",
            "pro-dispatch arm '<assignment-id>'",
            "Do not call the native send unless arming succeeds",
            "--reason-file '<reason-file>'",
            "without interpolating it into a shell command",
            "pro-dispatch unusual-activity '<assignment-id>'",
            "HTTP 403",
            "30-minute cooldown",
            "Do not reduce this to a generic `systemError`",
        ]:
            self.assertIn(phrase, text)
        self.assertNotIn("--reason '<exact", text)

    def test_openai_yaml_mentions_explicit_skill_name(self) -> None:
        text = OPENAI_YAML.read_text(encoding="utf-8")
        self.assertIn("$codex-pro-dispatch", text)
        self.assertIn("allow_implicit_invocation: true", text)


if __name__ == "__main__":
    unittest.main()
