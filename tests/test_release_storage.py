"""Isolated tests for the reduced release's storage foundation."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from codex_pro_dispatch.native_storage import (
    Directory, IntegrityError, canonical, decode, read_evidence,
)


class ReleaseStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pro-release-storage-")
        self.addCleanup(temporary.cleanup)
        # Resolve only our freshly created fixture, not caller authority paths.
        self.root = Path(temporary.name).resolve(strict=True)
        self.root.chmod(0o700)

    def artifact(self, raw=b'{"text":"fixture"}\n'):
        with Directory(self.root) as directory:
            directory.write("read.json", raw)
        return self.root / "read.json"

    def test_exact_evidence_and_size_boundary(self):
        raw = '{"text":"café","space": "kept"}\n'.encode("utf-8")
        path = self.artifact(raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(read_evidence(path), raw)
        self.assertEqual(read_evidence(path, limit=len(raw)), raw)
        with self.assertRaises(IntegrityError):
            read_evidence(path, limit=len(raw) - 1)
        self.assertEqual(path.read_bytes(), raw)

    def test_rejects_symlink_ancestors_leaves_and_hardlinks(self):
        path = self.artifact()
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        leaf = self.root / "leaf.json"
        leaf.symlink_to(path)
        for unsafe in (alias / "read.json", leaf):
            with self.subTest(path=unsafe):
                with self.assertRaises((IntegrityError, OSError)):
                    read_evidence(unsafe)
        os.link(path, self.root / "hard.json")
        with self.assertRaises(IntegrityError):
            read_evidence(path)

    def test_evidence_modes_are_exact(self):
        path = self.artifact()
        for mode in (0o400, 0o640):
            path.chmod(mode)
            with self.subTest(mode=mode):
                with self.assertRaises(IntegrityError):
                    read_evidence(path)
        path.chmod(0o600)
        self.root.chmod(0o750)
        with self.assertRaises(IntegrityError):
            read_evidence(path)
        self.root.chmod(0o700)
        self.assertEqual(read_evidence(path), b'{"text":"fixture"}\n')

    def test_pinned_directory_replacement_is_rejected(self):
        original = self.root / "pinned"
        original.mkdir(mode=0o700)
        with Directory(original) as directory:
            directory.write("value.json", b"original")
            original.rename(self.root / "displaced")
            original.mkdir(mode=0o700)
            with self.assertRaises(IntegrityError):
                directory.read("value.json")
            with self.assertRaises(IntegrityError):
                directory.write("other.json", b"must not be written")
        self.assertEqual(
            (self.root / "displaced" / "value.json").read_bytes(), b"original"
        )
        self.assertEqual(list(original.iterdir()), [])

    def test_immutable_write_and_stale_temp_fail_closed(self):
        with Directory(self.root) as directory:
            directory.write("value.json", b"original", immutable=True)
            directory.write("value.json", b"original", immutable=True)
            with self.assertRaises(IntegrityError):
                directory.write("value.json", b"different", immutable=True)
            name = ".value.json.unit.tmp"
            descriptor = directory.open(name, os.O_WRONLY, create=True)
            try:
                os.write(descriptor, b"incomplete")
            finally:
                os.close(descriptor)
            with self.assertRaises(IntegrityError):
                directory.read("value.json")
            with self.assertRaises(IntegrityError):
                directory.write("value.json", b"replacement")
        self.assertEqual((self.root / "value.json").read_bytes(), b"original")
        self.assertEqual((self.root / name).read_bytes(), b"incomplete")

    def test_canonical_encoding_and_strict_decoding(self):
        value = {"z": [None, True, 9007199254740991], "a": "Ω"}
        encoded = canonical(value)
        self.assertEqual(
            encoded, b'{"a":"\\u03a9","z":[null,true,9007199254740991]}'
        )
        self.assertEqual(decode(encoded + b"\n", frozen=True), value)
        pretty = json.dumps(value, indent=2).encode("utf-8")
        self.assertEqual(decode(pretty), value)
        for raw in (
            pretty,
            encoded,
            b'{"a":1,"a":2}\n',
            b'{"a":-1}\n',
            b'{"a":1.5}\n',
            b'{"a":"\\ud800"}\n',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(IntegrityError):
                    decode(raw, frozen=True)


if __name__ == "__main__":
    unittest.main()
