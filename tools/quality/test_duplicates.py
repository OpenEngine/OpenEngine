"""Policy tests use real parsers and whole temporary source trees."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import duplicates as detector


POLICY = tomllib.loads(Path(__file__).with_name("duplicates.toml").read_text())


def block(name="work", offset=0):
    return (f"def {name}(value):\n" + "".join(
        f"    value = transform(value, {i + offset})\n" for i in range(30))
        + "    return value\n")


class DuplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "base"
        self.head = self.root / "head"
        self.base.mkdir()
        self.head.mkdir()
        self.config = {**POLICY, "parse_exceptions": {}}

    def write(self, root, path, content):
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)

    def copies(self, root, count, content=None):
        for i in range(count):
            self.write(root, f"copy{i}.py", content or block())

    def scan(self, root):
        report = detector.scan(root, self.config)
        self.assertEqual(report["errors"], [])
        return report

    def findings(self):
        return detector.findings(self.scan(self.head), self.scan(self.base))

    def test_third_file_and_fourth_file_report_every_location(self):
        for before in (2, 3):
            with self.subTest(before=before):
                self.copies(self.base, before)
                self.copies(self.head, before + 1)
                found = self.findings()
                self.assertTrue(found)
                for group in found.values():
                    self.assertEqual(len(detector.scope(group)), before + 1)

    def test_multiple_copies_in_one_file_do_not_count_as_three_files(self):
        self.write(self.head, "one.py", block() * 3)
        self.write(self.head, "two.py", block())
        self.assertFalse(self.findings())

    def test_pairwise_chain_is_not_a_three_file_clone(self):
        first, second = block("one"), block("two", 100)
        self.write(self.head, "a.py", first)
        self.write(self.head, "b.py", first + second)
        self.write(self.head, "c.py", second)
        self.assertFalse(self.findings())

    def test_baseline_debt_shifts_renames_and_relocations(self):
        self.copies(self.base, 3)
        self.copies(self.head, 3)
        self.assertFalse(self.findings())
        (self.head / "copy0.py").unlink()
        self.write(self.head, "moved/named.py", "# heading\n\n" + block())
        self.write(self.head, "copy1.py", block().replace(" = ", "="))
        self.write(self.head, "unrelated.py", "answer = 42\n")
        self.assertFalse(self.findings())

    def test_changed_content_and_expansion_within_qualifying_group(self):
        self.copies(self.base, 3)
        self.copies(self.head, 3, block(offset=500))
        self.assertTrue(self.findings())
        self.copies(self.head, 3)
        self.write(self.head, "copy0.py", block() * 2)
        self.assertTrue(self.findings())
        # Changing just one occurrence to unrelated code reduces existing debt.
        self.write(self.head, "copy0.py", block(offset=500))
        self.assertFalse(self.findings())

    def test_small_boilerplate_and_configurable_threshold(self):
        self.copies(self.head, 3, "def identity(value):\n    return value\n")
        self.assertFalse(self.findings())
        self.config["min_tokens"] = 3
        self.assertTrue(self.findings())

    def test_exclusions_and_unsupported_files_are_explicit(self):
        self.copies(self.head, 2)
        for path in ("node_modules/third.py", "dist/third.py", "build/third.py",
                     "tests/fixtures/third.py", "tools/quality/fixtures/third.py",
                     "site/static/v2/third.py"):
            self.write(self.head, path, block())
        self.write(self.head, "unknown.xyz", block())
        report = self.scan(self.head)
        self.assertEqual(report["files"], 2)
        self.assertEqual(report["unsupported"], ["unknown.xyz"])
        self.assertFalse(self.findings())

    def test_normalization_preserves_identifiers_and_literals(self):
        def keys(content):
            return {key for key, _, _ in detector.fingerprints(content.encode(), "python", 100)}

        original = block()
        self.assertEqual(keys(original), keys(original.replace(" = ", "=") + "# comment\n"))
        self.assertFalse(keys(original) & keys(block(offset=100)))
        self.assertFalse(keys(original) & keys(original.replace("value", "renamed")))
        self.assertNotEqual(keys(original + 'label = "a b"\n'),
                            keys(original + 'label = "a  b"\n'))

    def test_all_supported_grammars(self):
        samples = {
            "python": "x = 1", "javascript": "const x = 1;",
            "typescript": "const x: number = 1;", "tsx": "const x = <div/>;",
            "bash": "echo hello", "rust": "fn main() {}", "go": "package main\nfunc main() {}",
        }
        self.assertEqual(set(samples), set(detector.LANGUAGES.values()))
        for language, source in samples.items():
            with self.subTest(language=language):
                self.assertTrue(list(detector.fingerprints(source.encode(), language, 1)))

    def test_python_line_continuations_normalize_without_changing_literals(self):
        original = block()
        continued = original.replace("transform(value, 15)", "transform(\\\n        value, 15)")
        self.copies(self.base, 2)
        self.copies(self.head, 2)
        self.write(self.head, "third.py", continued)
        found = self.findings()
        self.assertTrue(found)
        self.assertTrue(all(len(detector.scope(group)) == 3 for group in found.values()))
        self.write(self.base, "third.py", original)
        self.assertFalse(self.findings())
        # A backslash newline inside a literal remains meaningful content.
        def keys(suffix):
            return {key for key, _, _ in detector.fingerprints(
                (original + suffix).encode(), "python", 100)}

        self.assertNotEqual(keys('label = "a\\\nb"\n'), keys('label = "ab"\n'))

    def test_reporting_reads_only_each_context_byte_range(self):
        self.copies(self.head, 3, block("first") + block("second", 100))
        base, current = self.scan(self.base), self.scan(self.head)
        found = detector.findings(current, base)
        self.assertGreater(len(found), 1)
        sources = {p: (self.head / p).read_bytes() for p in ("copy0.py", "copy1.py", "copy2.py")}
        reads = []

        class Source(io.BytesIO):
            def read(self, size=-1):
                reads.append((self.tell(), size))
                return super().read(size)

        original_open = Path.open

        def open_source(path, mode="r", **kwargs):
            if mode != "rb":
                return original_open(path, mode, **kwargs)
            self.assertEqual(mode, "rb")
            return Source(sources[path.name])

        stdout = io.StringIO()
        args = ["duplicates", "--root", str(self.head), "--baseline-root", str(self.base)]
        with patch.object(sys, "argv", args), patch.object(detector, "scan", side_effect=[base, current]), \
                patch.object(detector.tomllib, "loads", return_value=self.config), \
                patch.object(Path, "open", autospec=True, side_effect=open_source), \
                redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            self.assertEqual(detector.main(), 1)
        expected = []
        for key, group in sorted(found.items()):
            loc = group["locations"][0]
            expected.append((loc["start_byte"], loc["end_byte"] - loc["start_byte"]))
            self.assertEqual(json.loads(stdout.getvalue())["findings"][key]["context"],
                             sources[loc["path"]][loc["start_byte"]:loc["end_byte"]].decode())
        self.assertEqual(reads, expected)

    def test_parse_error_never_silently_skips_and_waiver_is_content_scoped(self):
        import hashlib

        bad = b"def broken(\n"
        self.write(self.head, "bad.py", bad.decode())
        self.assertTrue(detector.scan(self.head, self.config)["errors"])
        self.config["parse_exceptions"] = {"bad.py": {
            "sha256": hashlib.sha256(bad).hexdigest(), "rationale": "test grammar failure"}}
        self.assertTrue(self.scan(self.head)["parse_exceptions"])
        self.write(self.head, "bad.py", bad.decode() + "# edit\n")
        self.assertTrue(detector.scan(self.head, self.config)["errors"])

    def test_missing_source_tree_fails_closed(self):
        config = {**self.config, "roots": ["missing-source"]}
        self.assertTrue(detector.scan(self.head, config)["errors"])

    def test_exception_scope_growth_and_rationale(self):
        self.copies(self.head, 3)
        report = self.scan(self.head)
        found = self.findings()
        exceptions = [{"fingerprint": key, "files": detector.scope(group),
                       "rationale": "Independent protocol fixtures deliberately share syntax"}
                      for key, group in found.items()]
        self.assertFalse(detector.apply_exceptions(found, report, exceptions))
        self.assertFalse(found)
        self.copies(self.head, 4)
        grown = self.scan(self.head)
        found = self.findings()
        self.assertTrue(detector.apply_exceptions(found, grown, exceptions))
        self.assertTrue(found)
        exceptions[0]["rationale"] = " "
        self.assertTrue(detector.apply_exceptions(found, report, exceptions))
        exceptions[0]["files"] = {"*.py": 3}
        self.assertTrue(detector.apply_exceptions(found, report, exceptions))

    def test_fixing_waived_parse_error_does_not_break_baseline_scan(self):
        import hashlib

        bad = "def broken(\n"
        self.write(self.base, "bad.py", bad)
        self.write(self.base, "tools/quality/duplicates.toml",
                   '[parse_exceptions."bad.py"]\n'
                   f'sha256 = "{hashlib.sha256(bad.encode()).hexdigest()}"\n'
                   'rationale = "existing grammar failure"\n')
        self.write(self.head, "bad.py", "def repaired():\n    pass\n")
        policy = detector.baseline_policy(self.base, self.config)
        self.assertFalse(detector.scan(self.base, policy)["errors"])
        self.assertFalse(self.scan(self.head)["parse_exceptions"])

    def test_cli_json_diagnostics_and_exit_codes(self):
        config_path = self.root / "policy.toml"
        config_path.write_text(Path(__file__).with_name("duplicates.toml").read_text().split(
            "[parse_exceptions.")[0])
        self.copies(self.head, 3)
        command = [sys.executable, str(Path(detector.__file__)), "--root", str(self.head),
                   "--baseline-root", str(self.base), "--config", str(config_path)]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("copy2.py:", result.stderr)
        self.assertIn("transform", result.stderr)
        self.assertTrue(json.loads(result.stdout)["findings"])
        self.copies(self.base, 3)
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.write(self.head, "broken.py", "def broken(")
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(json.loads(result.stdout)["errors"])

    def test_large_file_is_scanned_without_recursion(self):
        large = block() + "".join(f"variable_{i} = {i}\n" for i in range(20000))
        self.copies(self.head, 3, large)
        self.assertGreater(self.scan(self.head)["bytes"], 1_000_000)
        self.assertTrue(self.findings())


if __name__ == "__main__":
    unittest.main()
