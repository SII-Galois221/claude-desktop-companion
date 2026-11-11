import unittest

from flows import FlowInspector, RESIDENTIAL, classify, parse_lsof
from watchdog_core import Process, ROOT, BUNDLE, CODE_BASE

UID = 501
MAIN = Process(100, 1, UID, (1, 0), ROOT)
HELPER = Process(101, 100, UID, (2, 0), BUNDLE + 'Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper')
CODE_PATH = CODE_BASE + '/1.0/hash/claude.app/Contents/MacOS/claude'
CODE = Process(102, 100, UID, (3, 0), CODE_PATH)
TOOL = Process(103, 102, UID, (4, 0), '/usr/bin/ssh')
CLI = Process(200, 1, UID, (5, 0), '/opt/homebrew/bin/claude')


def record(name='127.0.0.1:50123->127.0.0.1:7897', transport='TCP', state='ESTABLISHED'):
    return {'pid': 101, 'name': name, 'transport': transport, 'state': state}


def connection(chain=None, port='50123', source='127.0.0.1', network='tcp', inbound=None):
    meta = {'sourceIP': source, 'sourcePort': port, 'network': network,
            'host': 'api.anthropic.com', 'destinationPort': '443'}
    if inbound is not None:
        meta['inboundPort'] = inbound
    return {'metadata': meta, 'chains': [RESIDENTIAL, 'chain'] if chain is None else chain}


class FlowTests(unittest.TestCase):
    def check(self, sock=None, connections=None, available=True):
        return classify(sock or record(), HELPER, 'helper',
                        [connection()] if connections is None else connections, available)

    def test_lsof_nul_newline_records(self):
        data = (b'p101\0cClaude Helper\0\nf12\0PTCP\0n127.0.0.1:50123->127.0.0.1:7897\0'
                b'TST=ESTABLISHED\0TQR=0\0\nf13\0PUDP\0n*:5555\0\n'
                b'p103\0cssh\0\nf5\0PTCP\0n192.0.2.2:1000->192.0.2.3:22\0TST=ESTABLISHED\0\n')
        values = parse_lsof(data)
        self.assertEqual([x['pid'] for x in values], [101, 101, 103])
        self.assertEqual(values[0]['state'], 'ESTABLISHED')
        self.assertEqual(values[1]['transport'], 'UDP')

    def test_residential_requires_exact_source_and_protocol(self):
        self.assertEqual(self.check()['verdict'], 'residential')
        for different in (connection(port='50124'), connection(source='127.0.0.2'), connection(network='udp')):
            self.assertEqual(self.check(connections=[different])['verdict'], 'unverified')

    def test_ambiguous_match_is_not_guessed(self):
        self.assertEqual(self.check(connections=[connection(), connection()])['verdict'], 'unverified')

    def test_optional_inbound_port_filters(self):
        self.assertEqual(self.check(connections=[connection(inbound='17897')])['verdict'], 'unverified')

    def test_house_name_in_nonfinal_position_not_green(self):
        self.assertEqual(self.check(connections=[connection(['DIRECT', RESIDENTIAL])])['verdict'], 'direct')
        self.assertEqual(self.check(connections=[connection(['another-proxy', RESIDENTIAL])])['verdict'], 'other_proxy')

    def test_unknown_chain_not_green(self):
        self.assertEqual(self.check(connections=[connection([])])['verdict'], 'unverified')
        self.assertEqual(self.check(connections=[connection(['REJECT'])])['verdict'], 'unverified')

    def test_controller_failure_never_assumes_proxy_route(self):
        self.assertEqual(self.check(available=False)['verdict'], 'unverified')

    def test_ipv6_and_mapped_ipv4(self):
        sock = record('[::ffff:127.0.0.1]:50123->[::1]:17897')
        self.assertEqual(self.check(sock)['verdict'], 'residential')
        sock = record('[::1]:50123->[::1]:17897')
        self.assertEqual(self.check(sock, [connection(source='::1')])['verdict'], 'residential')

    def test_direct_socket_and_internal_socket(self):
        self.assertEqual(self.check(record('192.0.2.1:40000->203.0.113.2:443'))['verdict'], 'direct')
        self.assertEqual(self.check(record('127.0.0.1:40000->127.0.0.1:1234'))['verdict'], 'local')

    def test_unconnected_udp_not_assumed_local_safe(self):
        self.assertEqual(self.check(record('*:54321', 'UDP', ''))['verdict'], 'unverified')
        self.assertEqual(self.check(record('[::1]:54321', 'UDP', ''))['verdict'], 'unverified')

    def test_listen_is_not_outbound(self):
        self.assertEqual(self.check(record('*:54321', 'TCP', 'LISTEN'))['verdict'], 'local')

    def test_scope_includes_tools_excludes_independent_cli(self):
        scope = FlowInspector(uid=UID)
        selected, kinds, excluded = scope.select([MAIN, HELPER, CODE, TOOL, CLI])
        self.assertEqual(set(selected), {100, 101, 102, 103})
        self.assertEqual(kinds[103], 'tool_descendant')
        self.assertEqual(excluded[0]['pid'], 200)

    def test_orphan_requires_prior_proven_identity(self):
        orphan = Process(CODE.pid, 1, CODE.uid, CODE.started, CODE.path)
        fresh = FlowInspector(uid=UID)
        self.assertNotIn(CODE.pid, fresh.select([orphan])[0])
        prior = FlowInspector(uid=UID)
        prior.select([MAIN, CODE])
        self.assertIn(CODE.pid, prior.select([orphan])[0])
        reused = Process(CODE.pid, 1, CODE.uid, (33, 0), CODE.path)
        self.assertNotIn(CODE.pid, prior.select([reused])[0])

    def test_no_sockets_is_not_a_safety_claim(self):
        result = FlowInspector(uid=UID).inspect(processes=[MAIN],
            socket_reader=lambda pids: ([], True), connection_reader=lambda: ([], True))
        self.assertEqual(result['counts']['total'], 0)
        self.assertIn('不代表全部流量安全', result['coverage_note'])

    def test_lsof_failure_is_explicit(self):
        result = FlowInspector(uid=UID).inspect(processes=[MAIN],
            socket_reader=lambda pids: ([], False), connection_reader=lambda: ([], False))
        self.assertFalse(result['socket_inspection_available'])
        self.assertFalse(result['controller_available'])
        self.assertTrue(result['partial'])

    def test_reader_truncation_diagnostic_is_preserved(self):
        result = FlowInspector(uid=UID).inspect(processes=[MAIN],
            socket_reader=lambda pids: ([], True),
            connection_reader=lambda: ([], False, {'partial': True, 'truncated': True,
                                                   'reason': 'controller_response_too_large'}))
        self.assertTrue(result['partial'])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['diagnostics']['controller']['reason'], 'controller_response_too_large')


if __name__ == '__main__':
    unittest.main()
