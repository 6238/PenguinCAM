"""The daemon's test suite must run even where bambulabs_api is not installed.

printer_daemon.printer is the ONLY module that touches the library, and it imports it
lazily inside connect(), so importing the package costs nothing and `make test` does not
depend on the library being present."""
import unittest

import printer_daemon


class TestPackage(unittest.TestCase):
    def test_version_and_user_agent(self):
        self.assertTrue(printer_daemon.VERSION)
        self.assertEqual(printer_daemon.USER_AGENT,
                         f"penguincam-printer/{printer_daemon.VERSION}")


if __name__ == '__main__':
    unittest.main()
