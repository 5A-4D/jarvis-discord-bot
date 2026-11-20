import json
import tempfile
import unittest
from pathlib import Path

from jarvis_bot.identities import load_identity_reference


class IdentityReferenceTests(unittest.TestCase):
    def test_formats_name_equivalence_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identities.json"
            path.write_text(
                json.dumps(
                    {
                        "identity_groups": [
                            ["ExampleOwner", "Owner"],
                            ["MemberTwo", "member-two", "Member Two"],
                        ]
                    }
                ),
                encoding="utf-8",
            )

            reference = load_identity_reference(path)

        self.assertEqual(
            reference,
            "ExampleOwner = Owner\nMemberTwo = member-two = Member Two",
        )

    def test_preserves_configured_identity_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identities.json"
            path.write_text(
                json.dumps(
                    {
                        "identity_groups": [
                            ["ExampleMember", "memberthree", "Member Three"]
                        ]
                    }
                ),
                encoding="utf-8",
            )

            reference = load_identity_reference(path)

        self.assertEqual(reference, "ExampleMember = memberthree = Member Three")

    def test_missing_or_invalid_file_is_safely_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(load_identity_reference(root / "missing.json"), "")
            invalid = root / "invalid.json"
            invalid.write_text("not-json", encoding="utf-8")
            self.assertEqual(load_identity_reference(invalid), "")


if __name__ == "__main__":
    unittest.main()
