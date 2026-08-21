from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SwiftContractTests(unittest.TestCase):
    def test_waiter_requires_user_and_assistant_groups(self) -> None:
        source = (ROOT / "bin" / "cgpt-wait-idle").read_text(encoding="utf-8")
        self.assertIn("current.groupCount >= options.baselineGroups + 2", source)
        self.assertIn("hasAssistantGroup", source)

    def test_sender_verifies_the_complete_input_before_pressing_send(self) -> None:
        source = (ROOT / "bin" / "cgpt-send").read_text(encoding="utf-8")
        verification = source.index("if populatedValue != options.message")
        press = source.index("AXUIElementPerformAction(button, kAXPressAction as CFString)")
        self.assertLess(verification, press)
        self.assertIn("refusing to send", source)

    def test_sender_restores_the_pasteboard(self) -> None:
        source = (ROOT / "bin" / "cgpt-send").read_text(encoding="utf-8")
        self.assertIn("PasteboardSnapshot.capture", source)
        self.assertIn("snapshot.restore(pasteboard)", source)

    def test_send_button_matching_accepts_descriptive_labels(self) -> None:
        for name in ["cgpt-send", "cgpt-wait-idle"]:
            source = (ROOT / "bin" / name).read_text(encoding="utf-8")
            self.assertIn('text.hasPrefix("\\(prefix) ")', source)


if __name__ == "__main__":
    unittest.main()
