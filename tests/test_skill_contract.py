from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "codex-pro-dispatch" / "SKILL.md"
NATIVE_PROTOCOL = ROOT / "skills" / "codex-pro-dispatch" / "references" / "native-protocol.md"
OPENAI_YAML = ROOT / "skills" / "codex-pro-dispatch" / "agents" / "openai.yaml"
PLUGIN_MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
VERSION = ROOT / "VERSION"
PACKAGE_INIT = ROOT / "src" / "codex_pro_dispatch" / "__init__.py"
README = ROOT / "README.md"
BUNDLED_HELPER = ROOT / "skills" / "codex-pro-dispatch" / "scripts" / "pro-dispatch"


class SkillContractTests(unittest.TestCase):
    def test_skill_has_valid_minimal_frontmatter(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        frontmatter = text.split("---\n", 2)[1]
        self.assertIn("name: codex-pro-dispatch", frontmatter)
        self.assertIn("description:", frontmatter)
        self.assertIn("## Contract", text)
        self.assertIn("`AT_MOST_ONCE_SAFETY`", text)
        self.assertNotIn("TODO", text)

    def test_public_release_version_is_consistent(self) -> None:
        version = VERSION.read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        skill = SKILL.read_text(encoding="utf-8")
        frontmatter = skill.split("---\n", 2)[1]
        self.assertIn(f'version: "{version}"', frontmatter)
        self.assertIn(f'__version__ = "{version}"', PACKAGE_INIT.read_text(encoding="utf-8"))
        self.assertIn(f"Version: v{version}", README.read_text(encoding="utf-8"))
        self.assertIn(f'"version": "{version}"', PLUGIN_MANIFEST.read_text(encoding="utf-8"))

    def test_skill_preserves_at_most_once_and_official_app_boundaries(self) -> None:
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
            "At most one native send attempt",
            "--native-controls-confirmed",
            "private temporary directory",
            "This plugin does not install or authenticate that connector",
            "local-only branches",
        ]:
            self.assertIn(phrase, text)
        self.assertNotIn("--reason '<exact", text)

    def test_skill_documents_the_exact_bounded_continuation_contract(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        continuation = (
            "[CODEX_PRO_DISPATCH_CONTINUE root_assignment_id=<root-assignment-id> "
            "next_index=<index>]\n\n"
            "Return only chunk <index> of the same deliverable.\n"
            "Continue from the last accepted boundary without repeating or summarizing accepted text.\n"
            "Use the required chunk envelope.\n"
            "Keep the entire response below 10,000 UTF-8 bytes.\n"
            "Set final=1 only when this chunk completes the deliverable.\n"
            "Otherwise set final=0."
        )
        self.assertIn(continuation, text)
        for phrase in [
            "result_protocol: \"bounded-footer-v1\"",
            "expected-root-assignment-id",
            "expected-chunk-index",
            "Parse the helper's completion JSON with a real JSON parser",
            "never use eval",
            "flush, fsync, and verify mode 0600",
            "including after a partial write",
            "At index 16 with final=0, stop",
            "exactly one replacement",
            "never resent",
            "legacy-active-assignment",
            "stable assistant item",
            "completed enclosing turn",
            "truncated: true",
            "For a repository-write assignment, first confirm the GitHub prerequisites",
            "independently verifies every reported branch, commit, file change, and CI result",
            "outbound_prompt_verified",
            "This recovery command verifies the already-existing message; it does not send anything",
            "single-trailing-newline artifact",
            "inspect the error payload exposed by the native control",
            "starts a fixed 30-minute cooldown",
            "pro-dispatch prepare` must remain blocked until the cooldown expires",
            "Wait using the worker conversation's native metadata or timestamp",
            "Do not repeatedly reopen the worker while it is generating",
            "open the worker by its exact conversation ID",
            "`worker reset --force` and `purge --yes --force` are break-glass operations",
            "## Same-worker continuation",
            "--continuation-of '<completed-assignment-id>'",
            "Collect when the user returns to ChatGPT/Codex, unless the user explicitly permits interruption",
            "Native desktop acceptance remains a release gate",
        ]:
            self.assertIn(phrase, normalized)
        for forbidden in [
            "pro-dispatch collect",
            "pro-dispatch result",
            "pro-dispatch artifact",
            "--result-mode",
            "--allow-empty-final",
            "chunk_body",
        ]:
            self.assertNotIn(forbidden, text)

    def test_native_protocol_preserves_raw_envelope_and_fail_stop_rules(self) -> None:
        text = NATIVE_PROTOCOL.read_text(encoding="utf-8")
        normalized = " ".join(text.split())
        for phrase in [
            "exact response bytes",
            "explicit native truncation metadata",
            "valid UTF-8",
            "contain no CR byte",
            "10,000 UTF-8 bytes",
            "Never normalize newlines, strip body text",
            "at most 16",
            "flushes and fsyncs before advancing",
            "exactly one operator-authorized replacement",
            "including after a partial write",
            "requires separately authorized fresh dispatch from the beginning",
        ]:
            self.assertIn(phrase, normalized)

    def test_readme_discloses_desktop_and_connector_prerequisites(self) -> None:
        text = README.read_text(encoding="utf-8")
        for phrase in [
            "**Desktop-only:**",
            "It does not run from ChatGPT on the web",
            "You do **not** need a connector",
            "write-capable GitHub connector",
            "Local and uncommitted files are invisible to the worker",
            "Common first-run problems",
            "OpenAI request ID when one is available",
        ]:
            self.assertIn(phrase, text)

    def test_openai_yaml_mentions_explicit_skill_name(self) -> None:
        text = OPENAI_YAML.read_text(encoding="utf-8")
        self.assertIn("$codex-pro-dispatch", text)
        self.assertIn("allow_implicit_invocation: false", text)

    def test_plugin_manifest_is_well_formed_and_bundles_the_skill(self) -> None:
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "codex-pro-dispatch")
        self.assertEqual(manifest["version"], VERSION.read_text(encoding="utf-8").strip())
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertLessEqual(len(manifest["interface"]["shortDescription"]), 30)
        self.assertEqual(manifest["license"], "MIT")
        self.assertTrue(os.access(BUNDLED_HELPER, os.X_OK))

    def test_repo_marketplace_exposes_the_root_plugin(self) -> None:
        marketplace = json.loads(
            (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(marketplace["name"], "codex-pro-dispatch")
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], "codex-pro-dispatch")
        self.assertEqual(entry["source"], {"source": "local", "path": "./"})
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertEqual(entry["policy"]["authentication"], "ON_INSTALL")
        self.assertEqual(entry["category"], "Developer Tools")


if __name__ == "__main__":
    unittest.main()
