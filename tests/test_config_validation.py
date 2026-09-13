"""Tests for config_validation.validate_and_sanitize_config.

These cover the untrusted-input path used by the anonymous upload flow: size limits, YAML
safety, string sanitization (G-code comment safety), and numeric range checks."""

import os
import unittest

from config_validation import (
    ConfigValidationError,
    MAX_CONFIG_BYTES,
    validate_and_sanitize_config,
)
from team_config import TeamConfig

SHIPPED_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'static', 'docs', 'PenguinCAM-config-template.yaml')


def _shipped_template():
    with open(SHIPPED_TEMPLATE, encoding='utf-8') as handle:
        return handle.read()


class TestConfigValidation(unittest.TestCase):

    def test_valid_minimal_config(self):
        data, warnings = validate_and_sanitize_config('team:\n  number: 1234\n  name: "Test Team"\n')
        self.assertEqual(data['team']['number'], 1234)
        self.assertEqual(data['team']['name'], 'Test Team')
        self.assertEqual(warnings, [])
        # Sanity: the result is consumable by TeamConfig.
        self.assertEqual(TeamConfig(data).team_number, 1234)

    def test_empty_and_none_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('')
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config(None)

    def test_non_mapping_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('- just\n- a\n- list\n')
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('just a scalar')

    def test_oversized_rejected(self):
        huge = 'team:\n  name: "' + ('x' * (MAX_CONFIG_BYTES + 10)) + '"\n'
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config(huge)

    def test_malformed_yaml_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('team:\n  name: "unterminated\n')

    def test_unsupported_version_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('version: 99\nteam:\n  number: 1\n')

    def test_yaml_safe_load_blocks_object_construction(self):
        # A python object tag must not be instantiated; safe_load raises -> we reject.
        payload = "team: !!python/object/apply:os.system ['echo pwned']\n"
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config(payload)

    def test_v2_requires_a_machines_section(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('version: 2\nteam:\n  number: 1\n')
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('version: 2\nmachines: {}\n')

    def test_v2_missing_default_machine_is_repaired(self):
        # Left alone, a dangling/absent default_machine makes EVERY setting fall back to
        # the built-in defaults - which reads to a team as "my config is being ignored".
        yaml_text = ('version: 2\n'
                     'machines:\n'
                     '  router:\n'
                     '    name: Router\n'
                     '    machine:\n'
                     '      name: Router 4896\n'
                     '  plasma:\n'
                     '    name: Plasma\n'
                     '    machine:\n'
                     '      name: Plasma 510\n')
        data, warnings = validate_and_sanitize_config(yaml_text)
        self.assertEqual(data['default_machine'], 'router')
        self.assertTrue(any('default_machine' in w for w in warnings))
        # The repaired config resolves to a real machine instead of the built-in defaults.
        self.assertEqual(TeamConfig(data).machine_name, 'Router 4896')

    def test_v2_dangling_default_machine_is_repaired(self):
        yaml_text = ('version: 2\n'
                     'default_machine: ghost\n'
                     'machines:\n'
                     '  router:\n'
                     '    name: Router\n')
        data, warnings = validate_and_sanitize_config(yaml_text)
        self.assertEqual(data['default_machine'], 'router')
        self.assertTrue(any('ghost' in w for w in warnings))

    def test_v2_valid_default_machine_is_left_alone(self):
        yaml_text = ('version: 2\n'
                     'default_machine: plasma\n'
                     'machines:\n'
                     '  router:\n'
                     '    name: Router\n'
                     '  plasma:\n'
                     '    name: Plasma\n')
        data, warnings = validate_and_sanitize_config(yaml_text)
        self.assertEqual(data['default_machine'], 'plasma')
        self.assertEqual(warnings, [])

    def test_parens_stripped_from_strings(self):
        data, warnings = validate_and_sanitize_config('machine:\n  name: "Router (v2) (fast)"\n')
        self.assertNotIn('(', data['machine']['name'])
        self.assertNotIn(')', data['machine']['name'])
        self.assertTrue(warnings)

    def test_unicode_stripped_from_strings(self):
        # Curly quote + arrow + degree sign must not survive into a G-code comment.
        data, warnings = validate_and_sanitize_config('machine:\n  name: "Cut “deep” → 90°"\n')
        self.assertTrue(all(ord(c) < 127 for c in data['machine']['name']))
        self.assertTrue(warnings)

    def test_clean_string_produces_no_warning(self):
        data, warnings = validate_and_sanitize_config('machine:\n  name: "Avid Pro4896"\n')
        self.assertEqual(data['machine']['name'], 'Avid Pro4896')
        self.assertEqual(warnings, [])

    def test_numeric_out_of_range_rejected(self):
        # Absurd feed rate.
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('materials:\n  plywood:\n    feed_rate: 999999\n')
        # Stepover fraction must be <= 1.
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('materials:\n  plywood:\n    stepover_percentage: 5\n')

    def test_numeric_non_finite_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('materials:\n  plywood:\n    feed_rate: .inf\n')
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('materials:\n  plywood:\n    feed_rate: .nan\n')

    def test_numeric_in_range_accepted(self):
        data, warnings = validate_and_sanitize_config('materials:\n  plywood:\n    feed_rate: 80\n')
        self.assertEqual(data['materials']['plywood']['feed_rate'], 80)

    def test_length_unit_string_accepted(self):
        data, _ = validate_and_sanitize_config('machine:\n  dimensions:\n    x_max: "600mm"\n')
        self.assertEqual(data['machine']['dimensions']['x_max'], '600mm')

    def test_length_unparseable_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('machine:\n  dimensions:\n    x_max: "not-a-length"\n')

    def test_length_absurd_magnitude_rejected(self):
        with self.assertRaises(ConfigValidationError):
            validate_and_sanitize_config('machine:\n  dimensions:\n    x_max: 999999\n')

    def test_negative_length_allowed(self):
        # park_position.z is legitimately negative.
        data, _ = validate_and_sanitize_config('machine:\n  park_position:\n    x: 0.5\n    y: 23.5\n    z: -0.5\n')
        self.assertEqual(data['machine']['park_position']['z'], -0.5)

    # ------------------------------------------------------------------
    # Lenient mode (strict=False) -- the Onshape background-render policy.
    # ------------------------------------------------------------------

    def test_lenient_drops_out_of_range_value(self):
        data, warnings = validate_and_sanitize_config(
            'materials:\n  plywood:\n    feed_rate: 999999\n    spindle_speed: 18000\n',
            strict=False)
        # Bad key dropped (falls back to default), good key kept.
        self.assertNotIn('feed_rate', data['materials']['plywood'])
        self.assertEqual(data['materials']['plywood']['spindle_speed'], 18000)
        self.assertTrue(warnings)

    def test_lenient_structural_failure_returns_none(self):
        data, warnings = validate_and_sanitize_config('- a\n- b\n', strict=False)
        self.assertIsNone(data)
        self.assertTrue(warnings)

    def test_lenient_parse_failure_returns_none(self):
        data, warnings = validate_and_sanitize_config('name: "unterminated\n', strict=False)
        self.assertIsNone(data)
        self.assertTrue(warnings)

    def test_lenient_still_sanitizes_strings(self):
        data, warnings = validate_and_sanitize_config(
            'machine:\n  name: "Router (v2)"\n', strict=False)
        self.assertNotIn('(', data['machine']['name'])
        self.assertTrue(warnings)

    def test_full_template_is_valid(self):
        # The shipped CONFIG_TEMPLATE must pass validation unchanged.
        from team_config import CONFIG_TEMPLATE
        data, warnings = validate_and_sanitize_config(CONFIG_TEMPLATE)
        self.assertEqual(warnings, [])
        self.assertIn('materials', data)

    def test_shipped_yaml_template_is_valid(self):
        """The downloadable static/docs template is a separate file from CONFIG_TEMPLATE
        and is what a mentor actually edits, so validate it too."""
        data, warnings = validate_and_sanitize_config(_shipped_template(), strict=False)
        self.assertEqual(warnings, [])
        self.assertIn('machines', data)

    def test_shipped_yaml_template_printing_example_is_valid(self):
        """Uncommenting the example 'printing:' block must produce a usable pairing code."""
        text = _shipped_template().replace('# printing:', 'printing:').replace(
            '#   pairing_code:', '  pairing_code:')
        data, warnings = validate_and_sanitize_config(text, strict=False)
        self.assertEqual(warnings, [])
        self.assertEqual(data['printing']['pairing_code'], '7K3MQ9XW')


class TestPairingCode(unittest.TestCase):
    """`printing.pairing_code` is the printer relay's only identity, so it is normalised
    and shape-checked here rather than at every read site."""

    def test_quoted_code_is_accepted_and_normalised(self):
        data, warnings = validate_and_sanitize_config(
            'team:\n  number: 6238\nprinting:\n  pairing_code: "7k3m-q9xw"\n')
        self.assertEqual(data['printing']['pairing_code'], '7K3MQ9XW')
        self.assertEqual(warnings, [])

    def test_unquoted_all_digit_code_is_accepted(self):
        """YAML reads an unquoted 12345678 as an int; the mentor should not have to know."""
        data, _ = validate_and_sanitize_config(
            'team:\n  number: 6238\nprinting:\n  pairing_code: 12345678\n')
        self.assertEqual(data['printing']['pairing_code'], '12345678')

    def test_lookalike_letters_are_mapped(self):
        data, _ = validate_and_sanitize_config(
            'team:\n  number: 6238\nprinting:\n  pairing_code: "OIL23456"\n')
        self.assertEqual(data['printing']['pairing_code'], '01123456')

    def test_malformed_code_is_dropped_with_a_warning_in_lenient_mode(self):
        data, warnings = validate_and_sanitize_config(
            'team:\n  number: 6238\nprinting:\n  pairing_code: "nope"\n', strict=False)
        self.assertNotIn('pairing_code', data['printing'])
        self.assertTrue(any('printing.pairing_code' in w for w in warnings), warnings)

    def test_malformed_code_is_rejected_in_strict_mode(self):
        with self.assertRaises(ConfigValidationError) as caught:
            validate_and_sanitize_config(
                'team:\n  number: 6238\nprinting:\n  pairing_code: "nope"\n', strict=True)
        self.assertIn('printing.pairing_code must be the 8-character code shown by the Pi',
                      caught.exception.message)

    def test_a_printing_block_that_is_not_a_mapping_is_dropped_leniently(self):
        data, warnings = validate_and_sanitize_config(
            'team:\n  number: 6238\nprinting: "nonsense"\n', strict=False)
        self.assertNotIn('printing', data)
        self.assertTrue(any('printing' in w for w in warnings), warnings)

    def test_null_code_means_no_printer_paired(self):
        """`pairing_code:` with nothing after it is a mentor retiring a Pi, not an error."""
        for strict in (False, True):
            with self.subTest(strict=strict):
                data, warnings = validate_and_sanitize_config(
                    'team:\n  number: 6238\nprinting:\n  pairing_code:\n', strict=strict)
                self.assertNotIn('pairing_code', data['printing'])
                self.assertEqual(warnings, [])

    def test_empty_code_means_no_printer_paired(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                data, warnings = validate_and_sanitize_config(
                    'team:\n  number: 6238\nprinting:\n  pairing_code: "   "\n',
                    strict=strict)
                self.assertNotIn('pairing_code', data['printing'])
                self.assertEqual(warnings, [])

    def test_no_printing_block_is_fine(self):
        data, warnings = validate_and_sanitize_config('team:\n  number: 6238\n')
        self.assertNotIn('printing', data)
        self.assertEqual(warnings, [])

    def test_v2_printing_block_is_found_and_normalised(self):
        yaml_text = (
            'version: 2\n'
            'default_machine: omio\n'
            'team:\n  number: 6238\n'
            'printing:\n  pairing_code: "7K3M-Q9XW"\n'
            'machines:\n  omio:\n    name: "Omio"\n')
        data, _ = validate_and_sanitize_config(yaml_text)
        self.assertEqual(data['printing']['pairing_code'], '7K3MQ9XW')


class TestTeamConfigPairingCode(unittest.TestCase):
    def test_version_1_file(self):
        """A v1 file's top-level keys are moved under the 'default' machine internally."""
        config = TeamConfig({'team': {'number': 6238},
                             'printing': {'pairing_code': '7K3MQ9XW'}})
        self.assertEqual(config.pairing_code, '7K3MQ9XW')

    def test_version_2_file(self):
        """A v2 file keeps 'printing' at the root, beside 'team'."""
        config = TeamConfig({'version': 2, 'default_machine': 'omio',
                             'team': {'number': 6238},
                             'printing': {'pairing_code': '7K3MQ9XW'},
                             'machines': {'omio': {'name': 'Omio'}}})
        self.assertEqual(config.pairing_code, '7K3MQ9XW')

    def test_absent_block_gives_none(self):
        self.assertIsNone(TeamConfig({'team': {'number': 6238}}).pairing_code)
        self.assertIsNone(TeamConfig().pairing_code)

    def test_unnormalised_value_is_normalised_on_read(self):
        """Defence in depth: a config that reached TeamConfig without validation (an old
        session cookie, say) still yields a usable code."""
        config = TeamConfig({'team': {'number': 6238},
                             'printing': {'pairing_code': '7k3m-q9xw'}})
        self.assertEqual(config.pairing_code, '7K3MQ9XW')

    def test_garbage_value_gives_none(self):
        config = TeamConfig({'team': {'number': 6238},
                             'printing': {'pairing_code': ['not', 'a', 'code']}})
        self.assertIsNone(config.pairing_code)


if __name__ == '__main__':
    unittest.main()
