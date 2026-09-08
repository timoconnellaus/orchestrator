import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:orchestrator/data/control_api.dart';

import 'fakes.dart';

void main() {
  testWidgets(
    'HTTP deadline bounds unresponsive mutations without automatic retry',
    (tester) async {
      final pending = Completer<http.Response>();
      var requests = 0;
      final api = HttpControlApi(
        'http://mock',
        clientFactory: () => MockClient((_) {
          requests++;
          return pending.future;
        }),
      );
      final result = expectLater(
        api.post('/v1/chat', {'id': 'stable', 'text': 'hello'}),
        throwsA(
          isA<ControlError>().having(
            (e) => e.message,
            'message',
            contains('timed out'),
          ),
        ),
      );
      await tester.pump(const Duration(seconds: 16));
      await result;
      expect(requests, 1);
      pending.complete(http.Response('{"operationId":"stable"}', 202));
      api.close();
    },
  );

  test('HTTP uses direct response objects and stable JSON payload', () async {
    final api = HttpControlApi(
      'http://mock:8787',
      clientFactory: () => MockClient((request) async {
        expect(request.url.path, '/v1/chat');
        expect(jsonDecode(request.body), {
          'id': 'stable',
          'text': 'hello',
          'conversationId': 'main',
        });
        return http.Response('{"operationId":"op"}', 202);
      }),
    );
    expect(
      await api.post('/v1/chat', {
        'id': 'stable',
        'text': 'hello',
        'conversationId': 'main',
      }),
      {'operationId': 'op'},
    );
    api.close();
  });

  test('structured server errors surface informative message', () async {
    final api = HttpControlApi(
      'http://mock',
      clientFactory: () => MockClient(
        (_) async => http.Response(
          '{"error":{"code":"voice_not_configured","message":"Configure LiveKit on control"}}',
          503,
        ),
      ),
    );
    await expectLater(
      api.post('/v1/voice/token', {'conversationId': 'main'}),
      throwsA(
        isA<ControlError>().having(
          (e) => e.message,
          'message',
          contains('Configure LiveKit'),
        ),
      ),
    );
    api.close();
  });

  test('SSE parses comment heartbeats, CRLF, multiline JSON and resume query', () async {
    final api = HttpControlApi(
      'http://mock',
      clientFactory: () => MockClient((request) async {
        expect(request.url.queryParameters['after'], '7');
        expect(request.headers['Accept'], 'text/event-stream');
        final data = jsonEncode(event(8, message('m8', 'Replay')));
        return http.Response(
          ': heartbeat\r\n\r\nid: 8\r\nevent: message.created\r\ndata: ${data.substring(0, 1)}\r\ndata: ${data.substring(1)}\r\n\r\n',
          200,
        );
      }),
    );
    final events = await api.events(7).toList();
    expect(events.single['seq'], 8);
    expect(events.single['data']['text'], 'Replay');
    api.close();
  });
}
