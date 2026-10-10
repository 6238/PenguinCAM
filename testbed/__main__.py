"""The test bed's command line: `uv run python -m testbed <command>`.

  build-docs [--rebuild NAME]  create the test documents in Onshape (live)
  record [SCENARIO...]         the live API check: record, then write the drift report
  record --ui [SCENARIO...]    the automated Onshape UI run for the panel scenarios
  drift                        compare the fresh recordings with the stored ones
  accept SCENARIO...           replace the stored recordings with the fresh ones
  ledger                       the call ledger's totals and its last ten lines

Exit codes: 1 for any other failure, 2 for a missing environment variable, 3 for a stop by
an Onshape check (a CAPTCHA or a verification step), 4 when the budget would be passed, the
ledger is unreadable, or Onshape answered 402.
"""
import argparse
import json
import os
import sys
from collections import deque

from testbed import settings
from testbed.ledger import AllowanceExhausted, BudgetExceeded, CallLedger, LedgerUnreadable
from testbed.scenarios import SCENARIOS

API_KEY_VARIABLES = ('ONSHAPE_ACCESS_KEY', 'ONSHAPE_SECRET_KEY')
EXIT_FAILED, EXIT_MISSING_VARIABLE, EXIT_ONSHAPE_CHECK, EXIT_BUDGET = 1, 2, 3, 4


class CommandFailed(Exception):
    """A command cannot go on; the message says why. Exit code 1."""


class _ArgumentParser(argparse.ArgumentParser):
    # argparse exits 2 on a usage error, which here means a missing environment variable.
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_FAILED, f'{self.prog}: error: {message}\n')


def _say(text):
    print(text, file=sys.stderr)


def _known_scenarios(names):
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        raise CommandFailed(f"unknown scenario: {', '.join(unknown)} "
                            f"(known: {', '.join(SCENARIOS)})")
    return names


def _enter_record_mode():
    """Switch this process to record mode: every OnshapeClient records and is counted."""
    os.environ[settings.MODE_VARIABLE] = 'record'
    settings.reset_testbed_settings_for_tests()


def _estimate(name):
    """Counted calls a recording of `name` will make: its last recording's, else its own."""
    from testbed.adapters import OWNER_LOOKUPS
    from testbed.cassette import Cassette
    if (settings.CASSETTE_DIR / f'{name}.json').is_file():
        return Cassette.load(settings.CASSETTE_DIR, name).counted_calls() + len(OWNER_LOOKUPS)
    return SCENARIOS[name].estimate


def _print_counted(ledger, before):
    """The line every live command ends with, whatever stopped it."""
    try:
        after = ledger.counted_in_cycle()
    except LedgerUnreadable as exc:
        _say(f'counted calls unknown: {exc}')
        return
    print(f'counted calls: {after - before}, remaining budget: {ledger.budget - after}')


def _write_drift_report(scenarios):
    from testbed.drift import diff_fresh_recordings, write_drift_report
    diffs = diff_fresh_recordings(scenarios)
    path = settings.drift_report_path()
    write_drift_report(diffs, path, compared=scenarios)
    print(f"drift report: {path} ({f'{len(diffs)} differences' if diffs else 'clean'})")
    return diffs


def _load_documents():
    if not settings.DOCUMENTS_FILE.is_file():
        raise CommandFailed(f'{settings.DOCUMENTS_FILE} does not exist; run build-docs first')
    return json.loads(settings.DOCUMENTS_FILE.read_text())


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def command_record(args):
    if args.ui:
        return _record_ui(args.scenarios)
    names = _known_scenarios(args.scenarios or [n for n, s in SCENARIOS.items() if not s.panel])
    panel = [name for name in names if SCENARIOS[name].panel]
    if panel:
        raise CommandFailed(f"{', '.join(panel)} has a panel part; record it with record --ui")
    settings.required_environment(*API_KEY_VARIABLES)
    _enter_record_mode()
    ledger = CallLedger()
    ledger.check_budget(sum(_estimate(name) for name in names))
    documents = _load_documents()

    from onshape_integration import OnshapeClient
    from testbed.adapters import (allowance_exhausted, begin_recording, discard_recording,
                                  finish_recording)
    from testbed.scenarios import run_scenario_api

    before = ledger.counted_in_cycle()
    recorded = []
    try:
        client = OnshapeClient.from_api_keys()
        for name in names:
            try:
                begin_recording(name, client)
                run_scenario_api(name, client, documents)
                # Raises after a 402 that a client method swallowed: the latch is still set.
                path = finish_recording()
            except Exception as exc:
                held = discard_recording()
                done = ', '.join(recorded) or 'none'
                _say(f'record stopped during {name}: {exc}')
                _say(f'{name}: {held} exchange{"" if held == 1 else "s"} recorded before the '
                     'stop were discarded; no fresh cassette was written for it')
                _say(f'scenarios recorded in full before it: {done}')
                if recorded:
                    _write_drift_report(recorded)
                if allowance_exhausted() or isinstance(exc, AllowanceExhausted):
                    raise AllowanceExhausted('Onshape answered 402 Payment Required: the annual '
                                             'API allowance is used up; every record command '
                                             'stops') from None
                return EXIT_FAILED
            print(f'recorded {name}: {path}')
            recorded.append(name)
        _write_drift_report(recorded)
    finally:
        _print_counted(ledger, before)
    return 0


def _record_ui(names):
    from testbed import ui_run
    names = _known_scenarios(names or ui_run.default_ui_scenarios())
    settings.required_environment(*ui_run.UI_VARIABLES)
    ledger = CallLedger()
    ledger.check_budget(sum(_estimate(name) for name in names
                            if not ui_run.owner_steps(SCENARIOS[name])))
    before = ledger.counted_in_cycle()
    try:
        outcomes = ui_run.run_onshape_ui(names)
        for outcome in outcomes:
            if outcome.outcome != ui_run.NEEDS_OWNER:
                print(outcome.line())
        waiting = [o for o in outcomes if o.outcome == ui_run.NEEDS_OWNER]
        if waiting:
            print('Needs the owner in an Onshape checkpoint:')
            for outcome in waiting:
                print(f'  {outcome.line()}')
        recorded = [o.name for o in outcomes if o.outcome == ui_run.RECORDED]
        if recorded:
            _write_drift_report(recorded)
        failed = [o for o in outcomes if o.outcome == ui_run.FAILED]
        if failed:
            _say(f'{len(failed)} of {len(outcomes)} scenarios failed')
            return EXIT_FAILED
    except ui_run.OnshapeCheckStopped as exc:
        _say(f'stopped by an Onshape check: {exc}')
        return EXIT_ONSHAPE_CHECK
    except (ui_run.OnshapeStepFailed, ui_run.UiRunRefused) as exc:
        raise CommandFailed(str(exc)) from None
    finally:
        if ui_run.panel_url_file().is_file():
            print(f'panel URL the development app opens: '
                  f'{ui_run.panel_url_file().read_text().strip()}')
        _print_counted(ledger, before)
    return 0


def command_build_docs(args):
    from testbed import documents
    settings.required_environment(*API_KEY_VARIABLES)
    _enter_record_mode()
    ledger = CallLedger()
    ledger.check_budget(documents.BUILD_DOCS_ESTIMATE)
    from onshape_integration import OnshapeClient
    from testbed.adapters import set_current_scenario
    before = ledger.counted_in_cycle()
    try:
        set_current_scenario('build-docs')
        try:
            built = documents.build_test_documents(
                OnshapeClient.from_api_keys(), settings.testbed_folder_id(),
                rebuild=args.rebuild)
        except (documents.NotATestDocument, RuntimeError, ValueError) as exc:
            raise CommandFailed(f'build-docs stopped: {exc}') from None
    finally:
        _print_counted(ledger, before)
    print(f'documents: {settings.DOCUMENTS_FILE}')
    pending = documents.pending_documents(built)
    for name, why in pending.items():
        if why == documents.NOT_IN_FOLDER:
            print(f'{name} was created outside the test folder (document id '
                  f"{built[name]['did']}): move it into the test folder in Onshape, then run "
                  'build-docs again to build it')
        else:
            print(f'{name} was not built: {why}; run build-docs again')
    return EXIT_FAILED if pending else 0


def command_drift(args):
    from testbed.drift import fresh_scenarios
    scenarios = fresh_scenarios()
    if not scenarios:
        raise CommandFailed(f'nothing fresh to compare in {settings.FRESH_DIR}; record first')
    _write_drift_report(scenarios)
    return 0


def command_accept(args):
    from testbed.drift import RecordingNotClean, accept_fresh_recording, check_fresh_recording
    names = _known_scenarios(args.scenarios)
    try:
        for name in names:      # scan them all first: one dirty scenario moves nothing
            check_fresh_recording(name)
    except (FileNotFoundError, RecordingNotClean) as exc:
        raise CommandFailed(str(exc)) from None
    for name in names:
        try:
            moved = accept_fresh_recording(name)
        except (FileNotFoundError, RecordingNotClean) as exc:
            raise CommandFailed(str(exc)) from None
        for path in moved:
            print(f'accepted {name}: {path}')
    print('Commit them with a message that says why the recording changed.')
    return 0


def command_ledger(args):
    ledger = CallLedger()
    counted = ledger.counted_in_cycle()
    print(f'ledger: {ledger.path}')
    print(f'counted calls since {ledger.cycle_start}: {counted}')
    print(f'budget: {ledger.budget} (per-run cap {ledger.per_run_cap})')
    print(f'remaining: {ledger.budget - counted}')
    if ledger.path.exists():
        with ledger.path.open() as f:
            last = deque((line.rstrip('\n') for line in f if line.strip()), maxlen=10)
        print(f'last {len(last)} lines:')
        for line in last:
            print(f'  {line}')
    return 0


def build_parser():
    parser = _ArgumentParser(prog='python -m testbed',
                             description='The Onshape test bed for PenguinCAM.')
    commands = parser.add_subparsers(dest='command', required=True, parser_class=_ArgumentParser)
    build = commands.add_parser('build-docs', help='create the test documents in Onshape')
    build.add_argument('--rebuild', metavar='NAME', help='delete and rebuild one test document')
    build.set_defaults(handler=command_build_docs)
    record = commands.add_parser('record', help='record scenarios against real Onshape')
    record.add_argument('--ui', action='store_true', help='the automated Onshape UI run')
    record.add_argument('scenarios', nargs='*', metavar='SCENARIO')
    record.set_defaults(handler=command_record)
    commands.add_parser('drift', help='write the drift report').set_defaults(handler=command_drift)
    accept = commands.add_parser('accept', help='store the fresh recordings of scenarios')
    accept.add_argument('scenarios', nargs='+', metavar='SCENARIO')
    accept.set_defaults(handler=command_accept)
    commands.add_parser('ledger', help='the call ledger and the budget').set_defaults(
        handler=command_ledger)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except settings.MissingEnvironmentVariable as exc:
        _say(str(exc))
        return EXIT_MISSING_VARIABLE
    except (BudgetExceeded, LedgerUnreadable, AllowanceExhausted) as exc:
        _say(str(exc))
        return EXIT_BUDGET
    except CommandFailed as exc:
        _say(str(exc))
        return EXIT_FAILED


if __name__ == '__main__':
    sys.exit(main())
