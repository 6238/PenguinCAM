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
                                 NOT_IN_ASSEMBLY_MESSAGE, NOT_IN_PART_STUDIO_MESSAGE, NOT_SOLID_MESSAGE,
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
                    {'linkDocumentId': '../x'}, {'partId': 'a/../b' * 30}, {'partId': '..'},
                    {'partId': '.'}, {'partId': 'a/b'}, {'partId': 'a?b'}, {'partId': 'a#b'},
                    {'partId': 'a%2Fb'}, {'partId': 'a b'}, {'partId': 'JHD\n'}, {'documentId': DID + '\n'},
                    {'wvmId': 'zz?x=1'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PartSource.from_json(dict(data, **bad))
        self.assertEqual(len(PartSource.from_json(dict(data, configuration='x' * 2500)).configuration), 2000)
        self.assertEqual(source.key(), PartSource.from_json(data).key())


class ElementCacheTest(unittest.TestCase):
    def test_expired_entries_dropped_and_size_capped(self):
        now = [1000.0]
        cache = ElementCache(clock=lambda: now[0], max_entries=3)
        addresses = [OnshapeAddress(DID, 'w', WID, f'e{n:023d}') for n in range(5)]
        for address in addresses[:2]:
            cache._get('parts', address, lambda: [], False)
        self.assertEqual(len(cache), 2)
        now[0] += 601
        cache._get('parts', addresses[2], lambda: ['new'], False)
        self.assertEqual(len(cache), 1)                 # the two expired entries are gone
        for address in addresses[3:]:
            now[0] += 1
            cache._get('parts', address, lambda: [], False)
        now[0] += 1
        cache._get('parts', addresses[0], lambda: [], False)
        self.assertEqual(len(cache), 3)                 # the oldest went past the cap
        fetched = []
        cache._get('parts', addresses[2], lambda: fetched.append(1) or [], False)
        self.assertEqual(fetched, [1])                  # addresses[2] was the oldest

    def test_wrong_answer_shape_is_a_call_failure(self):
        from unittest import mock
        from print.onshape_parts import OnshapeCallFailed
        client = mock.Mock()
        for answer, method in (([1, 2], 'configuration_parameters'), ({'a': 1}, 'parts_list'),
                               ({'a': 1}, 'element_type'), ([], 'assembly_definition')):
            client._make_api_request.return_value = mock.Mock(status_code=200, json=lambda a=answer: a)
            with self.subTest(method=method), self.assertRaises(OnshapeCallFailed):
                getattr(ElementCache(), method)(client, PS)
        client._make_api_request.return_value = mock.Mock(status_code=200, json=mock.Mock(side_effect=ValueError))
        with self.assertRaises(OnshapeCallFailed):
            ElementCache().parts_list(client, PS)
        client._make_api_request.return_value = mock.Mock(status_code=200, json=lambda: ['x', {'partId': 'JHD'}])
        self.assertEqual(ElementCache().parts_list(client, PS), [{'partId': 'JHD'}])


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

    def test_partstudio_junk_part_id_refused_without_a_refetch(self):
        with replay_client('ps-one') as (client, sent):
            parts, errors = resolve_selections(client, PS, [{'partId': '..'}, {'selectionId': 'a/b'}],
                                               'selection', cache=ElementCache())
        self.assertEqual(parts, [])
        self.assertEqual(errors, [{'message': NOT_IN_PART_STUDIO_MESSAGE}] * 2)
        self.assertEqual(len(sent), 3)          # elements, configuration, parts: no refetch

    def test_part_id_is_quoted_in_the_export_path(self):
        from print.onshape_parts import _export_source
        from unittest import mock
        client = mock.Mock()
        with mock.patch('print.onshape_parts.fetch_following_redirects', return_value=(b'', [])) as fetch:
            _export_source(client, PartSource(DID, 'w', WID, EID_PS, 'a+b:c='), None)
        self.assertTrue(fetch.call_args.args[1].endswith('/partid/a%2Bb%3Ac%3D/stl'))

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

    def test_string_path_does_not_spend_the_refetch(self):
        selections = [{'occurrencePath': 'MboxInstance1aaaa'}, {'occurrencePath': ['MboxInstance1aaaa']}]
        with replay_client('asm-cache-miss') as (client, sent):
            parts, errors = resolve_selections(client, ASM, selections, 'selection', cache=ElementCache())
        self.assertEqual(errors, [{'message': NOT_IN_ASSEMBLY_MESSAGE}])
        self.assertEqual([p.name for p in parts], ['Box'])      # the list path still got the refetch
        self.assertEqual(len(sent), 3)

    def test_subassembly_matched_on_full_configuration(self):
        from print.onshape_parts import _walk_occurrence
        part = {'id': 'P', 'type': 'Part', 'partId': 'JKD', 'name': 'Pin <1>'}
        definition = {'rootAssembly': {'instances': [
            {'id': 'S', 'type': 'Assembly', 'documentId': DID, 'elementId': EID_ASM,
             'documentMicroversion': MID, 'configuration': '', 'fullConfiguration': 'Size=Large'}]},
            'subAssemblies': [
                {'documentId': DID, 'elementId': EID_ASM, 'documentMicroversion': MID,
                 'fullConfiguration': 'Size=Small', 'instances': [dict(part, partId='JSM')]},
                {'documentId': DID, 'elementId': EID_ASM, 'documentMicroversion': MID,
                 'fullConfiguration': 'Size=Large', 'instances': [part]}]}
        self.assertEqual(_walk_occurrence(definition, ['S', 'P'])['partId'], 'JKD')


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
