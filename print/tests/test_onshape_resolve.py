"""Resolving Onshape selections to part sources and exporting them, over synthetic cassettes.

Each test names its cassette (testbed/tests/fixtures/cassettes/synthetic-print-<flow>.json,
written by testbed/tests/fixtures/make_print_fixtures.py): replay answers from the one
current cassette, so each holds the whole call sequence of one flow.
"""
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from print.limits import MAX_PART_BYTES  # noqa: E402
from print.onshape_parts import (CONFIGURED_MESSAGE, DUPLICATE_NAME_MESSAGE,  # noqa: E402
                                 NOT_IN_ASSEMBLY_MESSAGE, NOT_SOLID_MESSAGE,
                                 STANDARD_CONTENT_MESSAGE, ElementCache, OnshapeAddress,
                                 OnshapeLimitReached, PartRefused, PartSource, ResolvedPart,
                                 export_part_mesh, resolve_selections)
from print.tests.onshape_replay import replay_client as _replay_client  # noqa: E402

DID = 'd00000000000000000000001'
WID = 'a00000000000000000000001'
MID = 'c00000000000000000000001'
EID_PS = 'e00000000000000000000001'
EID_ASM = 'e00000000000000000000002'
DID2 = 'd00000000000000000000002'
VID2 = 'b00000000000000000000002'
EID2 = 'e00000000000000000000003'
PS = OnshapeAddress(DID, 'w', WID, EID_PS)
ASM = OnshapeAddress(DID, 'w', WID, EID_ASM)
BOX_BYTES = 84 + 12 * 50


def replay_client(flow):
    return _replay_client(f'synthetic-print-{flow}')


def dialog_item(**overrides):
    item = {'documentId': DID2, 'versionId': VID2, 'elementId': EID2, 'elementType': 'PARTSTUDIO',
            'elementConfiguration': '', 'itemType': 'PART', 'partName': 'Box', 'idTag': 'idTagBox1',
            'isSurface': False, 'isComposite': False, 'isSketch': False, 'isFlattenedBody': False}
    item.update(overrides)
    return item


class PartSourceTest(unittest.TestCase):
    def test_json_round_trip_and_validation(self):
        source = PartSource(DID, 'v', VID2, EID2, 'JHD', 'A=1', DID)
        data = source.to_json()
        self.assertEqual(data, {'documentId': DID, 'wvm': 'v', 'wvmId': VID2, 'elementId': EID2,
                                'partId': 'JHD', 'configuration': 'A=1', 'linkDocumentId': DID})
        self.assertEqual(PartSource.from_json(data), source)
        self.assertNotIn('configuration', PartSource(DID, 'w', WID, EID_PS, 'JHD').to_json())
        for bad in ({'documentId': 'nothex'}, {'wvm': 'x'}, {'partId': ''}, {'elementId': None},
                    {'linkDocumentId': '../x'}, {'partId': 'a/../b' * 30}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PartSource.from_json(dict(data, **bad))
        self.assertEqual(len(PartSource.from_json(dict(data, configuration='x' * 2500)).configuration), 2000)
        self.assertEqual(source.key(), PartSource.from_json(data).key())


class ResolvePartStudioTest(unittest.TestCase):
    def test_partstudio_part_id_used_directly_and_named_from_parts_list(self):
        with replay_client('ps-one') as (client, sent):
            parts, errors = resolve_selections(client, PS, [{'partId': 'JHD'}], 'selection',
                                               cache=ElementCache())
        self.assertEqual(errors, [])
        self.assertEqual([(p.name, p.source) for p in parts],
                         [('Box', PartSource(DID, 'w', WID, EID_PS, 'JHD'))])
        self.assertEqual(len(sent), 3)          # elements, configuration, parts

    def test_partstudio_selection_id_matched_through_parts_list(self):
        with replay_client('ps-one') as (client, _sent):
            parts, errors = resolve_selections(client, PS, [{'selectionId': 'JKD'}], 'selection',
                                               cache=ElementCache())
        self.assertEqual(errors, [])
        self.assertEqual([(p.name, p.source.part_id) for p in parts], [('Cylinder', 'JKD')])

    def test_partstudio_sheet_body_refused(self):
        with replay_client('ps-one') as (client, _sent):
            parts, errors = resolve_selections(client, PS, [{'partId': 'JSD'}], 'selection',
                                               cache=ElementCache())
        self.assertEqual(parts, [])
        self.assertEqual(errors, [{'name': 'Lid sketch',
                                   'message': NOT_SOLID_MESSAGE.format(name='Lid sketch')}])

    def test_configured_partstudio_without_configuration_refused(self):
        with replay_client('ps-configured') as (client, sent):
            parts, errors = resolve_selections(client, PS, [{'partId': 'JHD'}, {'partId': 'JKD'}],
                                               'selection', cache=ElementCache())
        self.assertEqual(parts, [])
        self.assertEqual(errors, [{'message': CONFIGURED_MESSAGE}] * 2)
        self.assertEqual(len(sent), 2)          # elements, configuration

    def test_configured_partstudio_with_panel_configuration_passes_it_through(self):
        address = OnshapeAddress(DID, 'w', WID, EID_PS, 'Size=20+mm')
        with replay_client('ps-configured-passed') as (client, _sent):
            parts, errors = resolve_selections(client, address, [{'partId': 'JHD'}], 'selection',
                                               cache=ElementCache())
            self.assertEqual(errors, [])
            self.assertEqual(parts[0].source.configuration, 'Size=20+mm')
            result = export_part_mesh(client, parts[0], byte_cap=MAX_PART_BYTES)
        self.assertEqual(len(result.stl), BOX_BYTES)

    def test_element_type_cached_for_ten_minutes(self):
        now = [1000.0]
        cache = ElementCache(clock=lambda: now[0])
        with replay_client('ps-resolve-twice') as (client, sent):
            resolve_selections(client, PS, [{'partId': 'JHD'}], 'selection', cache=cache)
            self.assertEqual(len(sent), 3)
            now[0] += 599
            parts, _ = resolve_selections(client, PS, [{'partId': 'JHD'}], 'selection', cache=cache)
            self.assertEqual(len(sent), 3)      # all three answers from the cache
            self.assertEqual(parts[0].name, 'Box')
            now[0] += 2                         # 601 s after the first fetch
            resolve_selections(client, PS, [{'partId': 'JHD'}], 'selection', cache=cache)
            self.assertEqual(len(sent), 6)

    def test_partstudio_cache_miss_refetches_once(self):
        with replay_client('ps-cache-miss') as (client, sent):
            parts, errors = resolve_selections(client, PS, [{'partId': 'JND'}], 'selection',
                                               cache=ElementCache())
            self.assertEqual(errors, [])
            self.assertEqual([(p.name, p.source.part_id) for p in parts], [('Spacer', 'JND')])
            self.assertEqual(len(sent), 4)      # elements, configuration, parts, parts again
            result = export_part_mesh(client, parts[0], byte_cap=MAX_PART_BYTES)
        self.assertEqual(len(result.stl), BOX_BYTES)


class ResolveAssemblyTest(unittest.TestCase):
    def test_assembly_occurrence_maps_to_source_with_link_document(self):
        with replay_client('asm-one') as (client, _sent):
            parts, errors = resolve_selections(client, ASM, [{'occurrencePath': ['MboxInstance1aaaa']}],
                                               'selection', cache=ElementCache())
            self.assertEqual(errors, [])
            self.assertEqual([(p.name, p.source) for p in parts],
                             [('Box', PartSource(DID2, 'v', VID2, EID2, 'JHD', None, DID))])
            result = export_part_mesh(client, parts[0], byte_cap=MAX_PART_BYTES)
        self.assertEqual(len(result.stl), BOX_BYTES)

    def test_subassembly_occurrence_followed(self):
        path = ['MsubInstance1aaaa', 'McylInstance1aaaa']
        with replay_client('asm-sub') as (client, _sent):
            parts, errors = resolve_selections(client, ASM, [{'occurrencePath': path}], 'selection',
                                               cache=ElementCache())
            self.assertEqual(errors, [])
            # In the open document with no version: the open address's workspace, no link.
            self.assertEqual([(p.name, p.source) for p in parts],
                             [('Cylinder', PartSource(DID, 'w', WID, EID_PS, 'JKD'))])
            result = export_part_mesh(client, parts[0], byte_cap=MAX_PART_BYTES)
        self.assertEqual(len(result.stl), 84 + 256 * 50)

    def test_standard_content_refused(self):
        with replay_client('asm-standard') as (client, _sent):
            parts, errors = resolve_selections(client, ASM, [{'occurrencePath': ['MscrewInstance1aa']}],
                                               'selection', cache=ElementCache())
        self.assertEqual(parts, [])
        name = 'Socket Head Cap Screw'
        self.assertEqual(errors, [{'name': name, 'message': STANDARD_CONTENT_MESSAGE.format(name=name)}])

    def test_assembly_cache_miss_refetches_once(self):
        with replay_client('asm-cache-miss') as (client, sent):
            parts, errors = resolve_selections(client, ASM, [{'occurrencePath': ['MboxInstance1aaaa']}],
                                               'selection', cache=ElementCache())
            self.assertEqual(errors, [])
            self.assertEqual(len(sent), 3)      # elements, definition, definition again
            result = export_part_mesh(client, parts[0], byte_cap=MAX_PART_BYTES)
        self.assertEqual(result.source.document_id, DID2)

    def test_still_missing_after_refetch_refused(self):
        selections = [{'occurrencePath': ['Munknown1']}, {'occurrencePath': ['Munknown2']}, {}]
        with replay_client('asm-cache-miss') as (client, sent):
            parts, errors = resolve_selections(client, ASM, selections, 'selection', cache=ElementCache())
        self.assertEqual(parts, [])
        self.assertEqual(errors, [{'message': NOT_IN_ASSEMBLY_MESSAGE}] * 3)
        self.assertEqual(len(sent), 3)          # one refetch, however many selections miss


class ResolveDialogTest(unittest.TestCase):
    def test_dialog_idtag_used_as_part_id(self):
        with replay_client('dialog-one') as (client, sent):
            parts, errors = resolve_selections(client, PS, [dialog_item()], 'dialog', cache=ElementCache())
            self.assertEqual(sent, [])          # resolving a dialog pick makes no call
            self.assertEqual(errors, [])
            self.assertEqual(parts, [ResolvedPart('Box', PartSource(DID2, 'v', VID2, EID2, 'idTagBox1',
                                                                    None, DID), 'Box')])
            result = export_part_mesh(client, parts[0], byte_cap=MAX_PART_BYTES)
        self.assertEqual((len(result.stl), result.calls, result.source.part_id), (BOX_BYTES, 2, 'idTagBox1'))

    def test_dialog_idtag_fallback_matches_part_name(self):
        with replay_client('dialog-fallback') as (client, _sent):
            parts, _ = resolve_selections(client, PS, [dialog_item()], 'dialog', cache=ElementCache())
            result = export_part_mesh(client, parts[0], byte_cap=MAX_PART_BYTES)
        self.assertEqual(result.source, PartSource(DID2, 'v', VID2, EID2, 'JHD', None, DID))
        self.assertEqual(len(result.stl), BOX_BYTES)
        self.assertEqual(result.calls, 4)       # 404, parts, 307, 200

    def test_dialog_duplicate_names_refused(self):
        with replay_client('dialog-duplicate') as (client, _sent):
            parts, _ = resolve_selections(client, PS, [dialog_item()], 'dialog', cache=ElementCache())
            with self.assertRaises(PartRefused) as caught:
                export_part_mesh(client, parts[0], byte_cap=MAX_PART_BYTES)
        self.assertEqual(caught.exception.name, 'Box')
        self.assertEqual(caught.exception.message, DUPLICATE_NAME_MESSAGE.format(name='Box'))

    def test_dialog_surface_refused(self):
        with replay_client('none') as (client, sent):
            parts, errors = resolve_selections(client, PS, [dialog_item(isSurface=True),
                                                            dialog_item(partName='Sk', isSketch=True)],
                                               'dialog', cache=ElementCache())
        self.assertEqual(parts, [])
        self.assertEqual(errors, [{'name': 'Box', 'message': NOT_SOLID_MESSAGE.format(name='Box')},
                                  {'name': 'Sk', 'message': NOT_SOLID_MESSAGE.format(name='Sk')}])
        self.assertEqual(sent, [])

    def test_dialog_configuration_cut_to_2000(self):
        with replay_client('none') as (client, _sent):
            parts, errors = resolve_selections(client, PS, [dialog_item(elementConfiguration='x' * 2500)],
                                               'dialog', cache=ElementCache())
        self.assertEqual(errors, [])
        self.assertEqual(len(parts[0].source.configuration), 2000)

    def test_dialog_workspace_in_open_document_has_no_link(self):
        item = dialog_item(documentId=DID, versionId=None, workspaceId=WID, elementId=EID_PS)
        with replay_client('none') as (client, _sent):
            parts, _ = resolve_selections(client, PS, [item], 'dialog', cache=ElementCache())
        self.assertEqual(parts[0].source, PartSource(DID, 'w', WID, EID_PS, 'idTagBox1'))


class ResolveRefreshTest(unittest.TestCase):
    def test_refresh_reads_sources_and_refuses_bad_ones(self):
        good = PartSource(DID, 'w', WID, EID_PS, 'JHD').to_json()
        with replay_client('ps-refresh') as (client, sent):
            parts, errors = resolve_selections(
                client, PS, [{'source': good, 'name': 'Box'},
                             {'source': dict(good, documentId='x'), 'name': 'Bad'}],
                'refresh', cache=ElementCache())
            self.assertEqual(sent, [])
            self.assertEqual([(p.name, p.source.to_json()) for p in parts], [('Box', good)])
            self.assertEqual([e['name'] for e in errors], ['Bad'])
            result = export_part_mesh(client, parts[0], byte_cap=MAX_PART_BYTES)
        self.assertEqual(len(result.stl), BOX_BYTES)


class ExportTest(unittest.TestCase):
    def resolved_box(self, client):
        parts, _ = resolve_selections(client, PS, [{'partId': 'JHD'}], 'selection', cache=ElementCache())
        return parts[0]

    def test_export_402_raises_limit_reached(self):
        with replay_client('ps-402') as (client, _sent):
            part = self.resolved_box(client)
            with self.assertRaises(OnshapeLimitReached) as caught:
                export_part_mesh(client, part, byte_cap=MAX_PART_BYTES)
        self.assertEqual(caught.exception.name, 'Box')

    def test_export_counts_calls_including_redirect(self):
        with replay_client('ps-one') as (client, _sent):
            result = export_part_mesh(client, self.resolved_box(client), byte_cap=MAX_PART_BYTES)
        self.assertEqual(result.calls, 2)
        self.assertEqual(len(result.stl), BOX_BYTES)
        self.assertEqual(len(result.hops), 1)
        self.assertGreaterEqual(result.ms, 0)

    def test_export_asks_for_print_tessellation(self):
        with replay_client('ps-one') as (client, sent):
            export_part_mesh(client, self.resolved_box(client), byte_cap=MAX_PART_BYTES)
        exports = [url for url, _headers in sent if urlsplit(url).path.endswith('/stl')]
        self.assertEqual(len(exports), 1)
        query = parse_qs(urlsplit(exports[0]).query)
        self.assertEqual(query, {'mode': ['binary'], 'units': ['millimeter'],
                                 'chordTolerance': ['0.00005'], 'angleTolerance': ['0.1309']})

    def test_export_too_large_raises_part_too_large(self):
        from print.onshape_parts import PartTooLarge
        with replay_client('ps-one') as (client, _sent):
            with self.assertRaises(PartTooLarge) as caught:
                export_part_mesh(client, self.resolved_box(client), byte_cap=100)
        self.assertEqual(caught.exception.name, 'Box')


if __name__ == '__main__':
    unittest.main()
