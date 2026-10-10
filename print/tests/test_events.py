"""Print event lines: the quoting rule, the line format and the guards."""
import unittest
from unittest import mock

from print import events
from print.events import format_print_line, print_event, quote_value, valid_page_id


class QuoteValueTest(unittest.TestCase):
    def test_bare_values_stay_bare(self):
        self.assertEqual(quote_value("Bracket_2.stl"), "Bracket_2.stl")
        self.assertEqual(quote_value("a@b+c:d/e-f"), "a@b+c:d/e-f")
        self.assertEqual(quote_value(84), "84")
        self.assertEqual(quote_value(-101), "-101")
        self.assertEqual(quote_value(1.5), "1.5")

    def test_unsafe_values_are_json_encoded(self):
        self.assertEqual(quote_value("two words"), '"two words"')
        forged = quote_value("x\n[PRINT] page")
        self.assertEqual(forged, '"x\\n[PRINT] page"')
        self.assertNotIn("\n", forged)
        self.assertEqual(quote_value("a=b"), '"a=b"')
        self.assertEqual(quote_value("Grüße"), '"Gr\\u00fc\\u00dfe"')
        self.assertEqual(quote_value(""), '""')

    def test_trailing_newline_is_never_bare(self):
        self.assertEqual(quote_value("x\n"), '"x\\n"')
        self.assertEqual(quote_value(["a", "b\n"]), '"a,b\\n"')
        self.assertIsNone(valid_page_id("abcdef012345\n"))
        for job in ("abcdefg\n", "abc\n"):
            with self.assertRaises(ValueError):
                format_print_line("deliver", None, None, job, {})
        with self.assertRaises(ValueError):
            format_print_line("deliver", None, None, None, {"key\n": 1})

    def test_none_bool_list_and_dict(self):
        self.assertEqual(quote_value(None), "-")
        self.assertEqual(quote_value(True), "yes")
        self.assertEqual(quote_value(False), "no")
        self.assertEqual(quote_value(["A", "B C"]), '"A,B C"')
        self.assertEqual(quote_value(("A", "B")), '"A,B"')     # a comma is not bare
        with self.assertRaises(TypeError):
            quote_value({})


class LineTest(unittest.TestCase):
    def test_line_format(self):
        self.assertEqual(format_print_line("export", "abcdef012345", 1234, None, {"bytes": 84}),
                         "[PRINT] export pid=abcdef012345 team=1234 job=- bytes=84")
        self.assertEqual(format_print_line("slice_done", None, None, "AbC-_123", {"a": 1, "b": "x y"}),
                         '[PRINT] slice_done pid=- team=- job=AbC-_123 a=1 b="x y"')

    def test_full_job_id_refused(self):
        with self.assertRaises(ValueError):
            format_print_line("slice_done", None, None, "a" * 43, {})
        with self.assertRaises(ValueError):
            format_print_line("slice_done", None, None, "ab cd", {})

    def test_bad_page_id_refused_and_unknown_event_refused(self):
        for bad in ("ABCDEF012345", "abcdef01234", "abcdef0123456", "abcdef01234g", ""):
            with self.subTest(pid=bad):
                self.assertIsNone(valid_page_id(bad))
                with self.assertRaises(ValueError):
                    format_print_line("page", bad, None, None, {})
        self.assertIsNone(valid_page_id(None))
        self.assertEqual(valid_page_id("abcdef012345"), "abcdef012345")
        with self.assertRaises(ValueError):
            format_print_line("print_job", None, None, None, {})


class PrintEventTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(events.configure_print_events, events._log, events._metrics)
        self.lines = []
        self.metrics = mock.Mock()
        events.configure_print_events(self.lines.append, self.metrics)

    def test_request_writes_no_metrics_row(self):
        line = print_event("request", pid="abcdef012345", team=6238, method="GET", status=200)
        self.assertEqual(self.lines, [line])
        self.assertEqual(line, "[PRINT] request pid=abcdef012345 team=6238 job=- method=GET status=200")
        self.metrics.log_event.assert_not_called()
        print_event("page", pid="abcdef012345", team=6238, source="onshape")
        self.metrics.log_event.assert_called_once_with(
            "print_page", team_number=6238,
            metadata={"pid": "abcdef012345", "job": None, "source": "onshape"})
        self.assertEqual(len(self.lines), 2)


if __name__ == "__main__":
    unittest.main()
