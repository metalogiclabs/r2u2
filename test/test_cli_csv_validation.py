"""End-to-end fail-closed CSV parsing tests for the C reference CLI.

Build the debug C monitor with `make -C monitors/c debug` before running.
The compiler and monitor are both real subprocesses, not mocks.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MONITOR = ROOT / "monitors/c/build/r2u2_debug"
COMPILER = ROOT / "compiler/c2po.py"


class CSVInputValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MONITOR.is_file():
            raise RuntimeError("Run `make -C monitors/c debug` before this test")
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.specs = {}
        preprocessed = subprocess.run(
            [os.environ.get("CC", "cc"), "-E", "-P", "-x", "c", "-"],
            input="#include <stdio.h>\nBUFSIZ\n", text=True,
            capture_output=True, check=True,
        )
        cls.c_bufsiz = int(preprocessed.stdout.splitlines()[-1])
        full_signals = ",".join(f"s{i}" for i in range(256))
        full_trace = "# " + full_signals + "\n" + ",".join(["0"] * 256) + "\n"
        for kind, source, booleanizer, protected in (
            ("atomic", "INPUT\n    s: bool;\nFTSPEC\n    s;\n", False, False),
            ("numeric", "INPUT\n    s: int;\nFTSPEC\n    s > 0;\n", True, False),
            ("two_atomic", "INPUT\n    BOOST,COAST: bool;\nFTSPEC\n    COAST;\n", False, False),
            ("full_atomic", f"INPUT\n    {full_signals}: bool;\nFTSPEC\n    s255;\n", False, False),
            ("full_numeric", f"INPUT\n    {full_signals}: int;\nFTSPEC\n    s255 > 0;\n", True, False),
            ("protected_two_atomic", "INPUT\n    BOOST,COAST: bool;\nFTSPEC\n    COAST;\n", False, True),
            ("protected_numeric", "INPUT\n    s: int;\nFTSPEC\n    s > 0;\n", True, True),
            ("protected_float", "INPUT\n    s: float;\nFTSPEC\n    s > 0.5;\n", True, True),
            ("protected_full_atomic", f"INPUT\n    {full_signals}: bool;\nFTSPEC\n    s255;\n", False, True),
        ):
            spec = tmp / f"{kind}.c2po"
            trace = tmp / f"{kind}.csv"
            binary = tmp / f"{kind}.bin"
            spec.write_text(source)
            trace.write_text(
                full_trace if kind.startswith("full_") or kind.endswith("full_atomic")
                else "# BOOST,COAST\n0,1\n" if kind.endswith("two_atomic")
                else "# s\n0\n"
            )
            command = [sys.executable, str(COMPILER), "--spec", str(spec),
                       "--trace", str(trace), "--output", str(binary)]
            if booleanizer:
                command.append("--booleanizer")
            if protected:
                command.append("--input-contract")
            compiled = subprocess.run(command, cwd=ROOT, text=True,
                                      capture_output=True, check=False)
            assert compiled.returncode == 0, compiled.stderr
            cls.specs[kind] = binary

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_monitor(self, kind: str, csv: str):
        env = {**os.environ,
               "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
               "UBSAN_OPTIONS": "halt_on_error=1"}
        return subprocess.run([str(MONITOR), str(self.specs[kind])],
                              input=csv, text=True, capture_output=True,
                              check=False, env=env)

    def assert_csv_rejected_safely(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Invalid CSV", result.stderr)
        self.assertNotIn("AddressSanitizer", result.stderr)
        self.assertNotIn("runtime error:", result.stderr)

    def test_atomic_column_257_is_rejected_before_write(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("atomic", ",".join(["0"] * 257) + "\n"))

    def test_numeric_column_257_is_rejected_before_write(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("numeric", ",".join(["0"] * 257) + "\n"))

    def test_column_256_is_within_array_capacity(self):
        for kind in ("atomic", "numeric"):
            with self.subTest(kind=kind):
                result = self.run_monitor("full_" + kind, ",".join(["0"] * 256) + "\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "0:0,F")

    def test_timestamp_without_signal_is_rejected(self):
        self.assert_csv_rejected_safely(self.run_monitor("atomic", "@0\n"))

    def test_bare_timestamp_marker_is_rejected(self):
        self.assert_csv_rejected_safely(self.run_monitor("atomic", "@\n"))

    def test_invalid_timestamp_does_not_look_like_eof(self):
        result = self.run_monitor("atomic", "0\n@invalid\n0\n")
        self.assertEqual(result.stdout.strip(), "0:0,F")
        self.assert_csv_rejected_safely(result)

    def test_valid_timestamp_still_runs(self):
        result = self.run_monitor("atomic", "@0 0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0:0,F")

    def test_timestamp_with_suffix_is_rejected(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("atomic", "@1garbage 0\n"))

    def test_negative_timestamp_is_rejected(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("atomic", "@-1 0\n"))

    def test_timestamp_integer_overflow_is_rejected(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("atomic", "@4294967296 0\n"))

    def test_timestamp_cannot_exceed_verdict_time_field(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("atomic", "@2147483648 0\n"))

    def test_timestamp_max_verdict_time_is_valid(self):
        result = self.run_monitor("atomic", "@2147483647 0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0:2147483647,F")

    def test_overlong_data_record_is_rejected_as_one_record(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("atomic", "0" * 9000 + "\n"))

    def test_overlong_header_is_rejected_before_data(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("atomic", "# " + "x" * 9000 + "\n0\n"))

    def test_complete_final_record_at_buffer_boundary_is_allowed(self):
        # A BUFSIZ-1-byte record is complete even if only its newline, or
        # EOF, remains outside the initial fgets buffer.
        for terminator in ("", "\n"):
            with self.subTest(terminator=repr(terminator)):
                result = self.run_monitor(
                    "atomic", "0" * (self.c_bufsiz - 1) + terminator,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "0:0,T")

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "requires Linux directory stream read semantics")
    def test_trace_read_failure_is_not_silent_end_of_trace(self):
        # On Linux fopen(directory, "r") succeeds but reading it fails.
        result = subprocess.run(
            [str(MONITOR), str(self.specs["atomic"]), self.tmp.name],
            text=True, capture_output=True, check=False,
            env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
                 "UBSAN_OPTIONS": "halt_on_error=1"},
        )
        self.assert_csv_rejected_safely(result)

    def test_boolean_zero_has_same_meaning_with_crlf(self):
        result = self.run_monitor("atomic", "0\r\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0:0,F")

    def test_embedded_carriage_return_cannot_split_signal_columns(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("atomic", "0\r1\n"))

    def test_header_declared_two_columns_accepts_complete_rows(self):
        result = self.run_monitor("two_atomic", "# BOOST,COAST\n0,1\n0,0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0:0,T\n0:1,F")

    def test_header_declared_missing_column_rejects_before_next_verdict(self):
        result = self.run_monitor("two_atomic", "# BOOST,COAST\n0,1\n0\n")
        self.assertEqual(result.stdout.strip(), "0:0,T")
        self.assert_csv_rejected_safely(result)

    def test_header_declared_trailing_empty_column_is_rejected(self):
        result = self.run_monitor("two_atomic", "# BOOST,COAST\n0,1\n0,\n")
        self.assertEqual(result.stdout.strip(), "0:0,T")
        self.assert_csv_rejected_safely(result)

    def test_header_declared_leading_empty_column_is_rejected(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("two_atomic", "# BOOST,COAST\n,1\n"))

    def test_header_declared_interior_empty_column_is_rejected(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("two_atomic", "# BOOST,COAST\n0,,1\n"))

    def test_header_declared_whitespace_only_column_is_rejected(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("two_atomic", "# BOOST,COAST\n0, \t\n"))

    def test_header_declared_extra_column_is_rejected(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("two_atomic", "# BOOST,COAST\n0,1,0\n"))

    def test_empty_header_column_is_rejected(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("two_atomic", "# BOOST,\n0,1\n"))

    def test_whitespace_only_header_column_is_rejected(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("two_atomic", "# BOOST, \t\n0,1\n"))

    def test_header_declaring_more_than_capacity_is_rejected(self):
        header = "# " + ",".join(f"s{i}" for i in range(257))
        self.assert_csv_rejected_safely(
            self.run_monitor("full_atomic", header + "\n0\n"))

    def test_header_declared_timestamped_rows_are_accepted(self):
        result = self.run_monitor("two_atomic", "# BOOST,COAST\n@0 0,1\n@1 0,0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0:0,T\n0:1,F")

    def test_header_declared_crlf_rows_are_accepted(self):
        result = self.run_monitor("two_atomic", "# BOOST,COAST\r\n0,1\r\n0,0\r\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0:0,T\n0:1,F")

    def test_headerless_sparse_rows_remain_accepted_for_compatibility(self):
        result = self.run_monitor("two_atomic", "0,1\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0:0,T\n0:1,T")

    def test_protected_binary_distinguishes_reordered_source_signals(self):
        # A compiler that discards signal names cannot distinguish these
        # programs even though the first signal in each CSV means something else.
        with tempfile.TemporaryDirectory() as dirname:
            outputs = []
            for label, first, second in (
                ("rocket", "BOOST", "COAST"),
                ("renamed", "IGNITION", "SEPARATION"),
            ):
                base = Path(dirname) / label
                spec = base.with_suffix(".c2po")
                trace = base.with_suffix(".csv")
                binary = base.with_suffix(".bin")
                spec.write_text(
                    f"INPUT\n    {first},{second}: bool;\nFTSPEC\n    {second};\n"
                )
                trace.write_text(f"# {first},{second}\n0,1\n")
                result = subprocess.run(
                    [sys.executable, str(COMPILER), "--spec", str(spec),
                     "--trace", str(trace), "--output", str(binary),
                     "--input-contract"],
                    cwd=ROOT, text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs.append(binary.read_bytes())
            self.assertNotEqual(outputs[0], outputs[1])

    def test_protected_binary_works_without_auxiliary_formula_names(self):
        with tempfile.TemporaryDirectory() as dirname:
            source = Path(dirname) / "input.c2po"
            trace = Path(dirname) / "input.csv"
            binary = Path(dirname) / "input.bin"
            source.write_text("INPUT\n    s: bool;\nFTSPEC\n    s;\n")
            trace.write_text("# s\n1\n")
            compiled = subprocess.run(
                [sys.executable, str(COMPILER), "--spec", str(source),
                 "--trace", str(trace), "--input-contract", "--no-aux",
                 "--output", str(binary)],
                cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run(
                [str(MONITOR), str(binary)], input=trace.read_text(),
                text=True, capture_output=True, check=False,
                env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
                     "UBSAN_OPTIONS": "halt_on_error=1"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "0:0,T")

    def test_protected_binary_accepts_exact_signal_header(self):
        result = self.run_monitor("protected_two_atomic", "# BOOST,COAST\n0,1\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0:0,T")

    def test_protected_binary_rejects_reordered_signal_header(self):
        result = self.run_monitor("protected_two_atomic", "# COAST,BOOST\n0,1\n")
        self.assertEqual(result.stdout, "")
        self.assert_csv_rejected_safely(result)

    def test_protected_binary_rejects_headerless_trace(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("protected_two_atomic", "0,1\n"))

    def test_protected_binary_rejects_extra_declared_signal(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("protected_two_atomic", "# BOOST,COAST,EXTRA\n0,1,0\n"))

    def test_protected_binary_accepts_maximum_256_mapped_signals(self):
        header = "# " + ",".join(f"s{i}" for i in range(256))
        result = self.run_monitor(
            "protected_full_atomic", header + "\n" + ",".join(["0"] * 255 + ["1"]) + "\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0:0,T")

    def test_protected_boolean_rejects_non_boolean_lexeme(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("protected_two_atomic", "# BOOST,COAST\n0,banana\n"))

    def test_protected_integer_rejects_invalid_lexeme(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("protected_numeric", "# s\ngarbage\n"))

    def test_protected_integer_rejects_overflow(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("protected_numeric", "# s\n2147483648\n"))

    def test_protected_float_rejects_nan(self):
        self.assert_csv_rejected_safely(
            self.run_monitor("protected_float", "# s\nnan\n"))

    def test_protected_trace_rejects_embedded_nul_before_verdict(self):
        # fgets permits embedded NUL; string parsers may otherwise accept a
        # valid prefix and silently discard the rest of the physical record.
        result = self.run_monitor("protected_numeric", "# s\n1\x00garbage\n")
        self.assertEqual(result.stdout, "")
        self.assert_csv_rejected_safely(result)

    def test_protected_binary_rejects_corrupt_required_footer(self):
        raw = self.specs["protected_two_atomic"].read_bytes()
        self.assertTrue(raw.endswith(b"R2U2SIG1"))
        length = int.from_bytes(raw[-12:-8], "big")
        payload_start = len(raw) - 12 - length
        wrong_type = bytearray(raw)
        wrong_type[payload_start + 2] = ord('X')
        cases = (
            raw[:-3],
            raw[:-12] + b"\xff\xff\xff\xff" + raw[-8:],
            bytes(wrong_type),
        )
        with tempfile.TemporaryDirectory() as dirname:
            corrupted = Path(dirname) / "bad.bin"
            for case in cases:
                with self.subTest(size=len(case), tail=case[-8:]):
                    corrupted.write_bytes(case)
                    result = subprocess.run(
                        [str(MONITOR), str(corrupted)], input="# BOOST,COAST\n0,1\n",
                        text=True, capture_output=True, check=False,
                        env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
                             "UBSAN_OPTIONS": "halt_on_error=1"},
                    )
                    self.assertEqual(result.stdout, "")
                    self.assert_csv_rejected_safely(result)

    def test_protected_binary_rejects_missing_legacy_aux_terminator(self):
        raw = self.specs["protected_two_atomic"].read_bytes()
        length = int.from_bytes(raw[-12:-8], "big")
        payload_start = len(raw) - 12 - length
        self.assertEqual(raw[payload_start - 2:payload_start], b"\x00\x00")
        with tempfile.TemporaryDirectory() as dirname:
            corrupted = Path(dirname) / "missing_terminator.bin"
            corrupted.write_bytes(raw[:payload_start - 1] + raw[payload_start:])
            result = subprocess.run(
                [str(MONITOR), str(corrupted)], input="# BOOST,COAST\n0,1\n",
                text=True, capture_output=True, check=False,
                env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
                     "UBSAN_OPTIONS": "halt_on_error=1"},
            )
            self.assertEqual(result.stdout, "")
            self.assert_csv_rejected_safely(result)

    def test_protected_binary_rejects_damaged_sig1_header_marker(self):
        # Mutating only the declaration must not silently turn a protected
        # binary into legacy mode while leaving its SIG1 footer intact.
        raw = self.specs["protected_two_atomic"].read_bytes()
        self.assertEqual(raw.count(b" SIG1"), 1)
        corrupted_binary = raw.replace(b" SIG1", b" SIG0", 1)
        self.assertTrue(corrupted_binary.endswith(b"R2U2SIG1"))
        with tempfile.TemporaryDirectory() as dirname:
            corrupted = Path(dirname) / "damaged_header.bin"
            corrupted.write_bytes(corrupted_binary)
            result = subprocess.run(
                [str(MONITOR), str(corrupted)], input="# COAST,BOOST\n0,1\n",
                text=True, capture_output=True, check=False,
                env={**os.environ, "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1",
                     "UBSAN_OPTIONS": "halt_on_error=1"},
            )
            self.assertEqual(result.stdout, "")
            self.assert_csv_rejected_safely(result)

    def test_protected_numeric_accepts_full_lexemes(self):
        for kind, value, want in (
            ("protected_numeric", "1", "0:0,T"),
            ("protected_float", "0.75", "0:0,T"),
        ):
            with self.subTest(kind=kind):
                result = self.run_monitor(kind, f"# s\n{value}\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), want)

    def test_existing_playground_compiler_entry_point_remains_compatible(self):
        # A new optional compiler feature must not break the existing Rust and
        # playground wrapper, which calls main_rs with its historical arguments.
        with tempfile.TemporaryDirectory() as dirname:
            spec = Path(dirname) / "input.c2po"
            trace = Path(dirname) / "input.csv"
            output = Path(dirname) / "spec.bin"
            spec.write_text("INPUT\n    s: bool;\nFTSPEC\n    s;\n")
            trace.write_text("# s\n0\n")
            code = (
                "import sys; sys.path.insert(0, 'compiler'); "
                "from c2po import main; "
                "sys.exit(main.main_rs(sys.argv[1], sys.argv[2], '', sys.argv[3], '', "
                "True, False, False, False, False, 3600).value)"
            )
            result = subprocess.run([sys.executable, "-c", code, str(spec), str(trace), str(output)],
                                    cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
