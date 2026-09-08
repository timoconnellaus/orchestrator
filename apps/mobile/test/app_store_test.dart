import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:orchestrator/data/app_store.dart';
import 'package:orchestrator/data/control_api.dart';

import 'fakes.dart';

void main() {
  late MemoryStore disk;
  late FakeApi api;
  late AppStore store;
  setUp(() {
    disk = MemoryStore();
    api = FakeApi();
    store = AppStore(disk, apiFactory: (_) => api);
  });
  tearDown(() async {
    store.dispose();
    await api.stream.close();
  });

  test('missing backend keeps text request durably and explicit retry preserves ID', () async {
    api.offline = true;
    await store.initialize();
    expect(store.connection, 'Offline');
    await store.send('Plan tomorrow');
    final item = store.outbox.values.single;
    expect(item['state'], 'retry');
    final first = Map<String, dynamic>.from(api.posts.single);
    expect(first['id'], isNotEmpty);
    api.offline = false;
    await store.reconnect();
    expect(api.posts.length, 1, reason: 'Reconnect never resubmits mutations');
    await store.retry(first['id']);
    expect(api.posts.last, first);
    expect(item['state'], 'accepted');
    expect(disk.values['snapshot:${store.url}'], contains('Plan tomorrow'));
  });

  test('history overlap and reconnect replay deduplicate; cursor survives restart with data', () async {
    api.histories['main'] = [message('m1', 'hello')];
    await store.initialize();
    expect(api.cursors, [0]);
    await store.applyEvent(event(1, message('m1', 'hello')));
    await store.applyEvent(event(1, message('m1', 'duplicate ignored')));
    await store.applyEvent(event(2, message('m2', 'arrived during snapshot')));
    expect(store.thread('main').length, 2);
    final saved = jsonDecode(disk.values['snapshot:${store.url}']!) as Json;
    expect(saved['cursor'], 2);
    expect(saved['messages'].length, 2);
    store.dispose();
    store = AppStore(disk, apiFactory: (_) => api);
    await store.initialize();
    expect(api.cursors.last, 2);
    expect(store.thread('main').map((m) => m['id']), containsAll(['m1', 'm2']));
  });

  test(
    'SSE delivers session status and worker history deduplicates replay',
    () async {
      api.sessions.add(session());
      api.histories['session:worker-1'] = [
        message(
          'wm',
          'Progress',
          conversation: 'session:worker-1',
          role: 'worker',
        ),
      ];
      await store.initialize();
      await store.loadThread('session:worker-1');
      api.stream.add(
        event(3, {...session(), 'status': 'blocked'}, type: 'session.updated'),
      );
      api.stream.add(event(4, api.histories['session:worker-1']!.single));
      await Future<void>.delayed(Duration.zero);
      expect(store.sessions['worker-1']!['status'], 'blocked');
      expect(store.thread('session:worker-1').length, 1);
      await store.send('Continue', sessionId: 'worker-1');
      expect(api.paths.last, '/v1/sessions/worker-1/messages');
      expect(api.posts.last.keys, unorderedEquals(['id', 'text']));
    },
  );

  test(
    'new worker mutations have stable ID and correct contract payload',
    () async {
      await store.initialize();
      api.failPost = true;
      await store.createWorker({
        'agent': 'pi',
        'name': 'Scout',
        'cwd': '/projects/demo',
        'instructions': 'Review',
      });
      final first = api.posts.single;
      expect(api.paths.single, '/v1/sessions');
      api.failPost = false;
      await store.retry(first['id']);
      expect(api.posts.last, first);
    },
  );

  test('operation event before POST acknowledgment is retained even with different operation ID', () async {
    await store.initialize();
    api.postHandler = (_, body) async {
      await store.applyEvent(
        event(1, {
          'id': 'op-1',
          'status': 'uncertain',
          'error': 'Worker outcome unknown',
          'updatedAt': '2026-07-17T00:00:01Z',
        }, type: 'operation.updated'),
      );
      return {'operationId': 'op-1'};
    };
    await store.send('Launch task');
    expect(store.outbox.values.single['state'], 'terminal');
    await store.retry(store.outbox.keys.single);
    expect(
      api.posts.length,
      1,
      reason: 'Uncertain server side effects are not repeated',
    );
  });

  for (final status in ['queued', 'running', 'succeeded', 'uncertain']) {
    test('lost POST acknowledgement preserves journal state $status', () async {
      await store.initialize();
      api.postHandler = (_, body) async {
        await store.applyEvent(
          event(1, {
            'id': body['id'],
            'status': status,
            'error': status == 'uncertain' ? 'Outcome unknown' : null,
            'updatedAt': '2026-09-08T00:00:01Z',
          }, type: 'operation.updated'),
        );
        throw const ControlError('POST acknowledgement lost');
      };
      await store.send('A durable request');
      final item = store.outbox.values.single;
      expect(item['state'], switch (status) {
        'succeeded' => 'done',
        'uncertain' => 'terminal',
        _ => 'accepted',
      });
      expect(item['error'], isNot('POST acknowledgement lost'));
      await store.retry(item['id']);
      expect(api.posts.length, 1);
    });
  }

  test(
    'late stale operation snapshot cannot overwrite terminal journal state',
    () async {
      await store.initialize();
      await store.applyEvent(
        event(1, {
          'id': 'op',
          'status': 'succeeded',
          'error': null,
          'updatedAt': '2026-09-08T00:00:01Z',
        }, type: 'operation.updated'),
      );
      await store.applyEvent(
        event(2, {
          'id': 'op',
          'status': 'running',
          'error': null,
          'updatedAt': '2026-09-08T00:00:01Z',
        }, type: 'operation.updated'),
      );
      expect(store.operations['op']!['status'], 'succeeded');
    },
  );

  test(
    'interrupted requests become explicitly retryable, not auto-resubmitted',
    () async {
      await store.initialize();
      final response = Completer<Json>();
      api.postHandler = (_, _) => response.future;
      final sending = store.send('Keep this');
      await Future<void>.delayed(Duration.zero);
      await store.reconnect();
      response.complete({'operationId': 'late'});
      await sending;
      expect(store.outbox.values.single['state'], 'retry');
      expect(api.posts.length, 1);
    },
  );

  test(
    'URL is validated, durable and isolates snapshots and requests',
    () async {
      await store.initialize();
      await store.send('Original server');
      await expectLater(
        store.configure('ftp://host'),
        throwsA(isA<ControlError>()),
      );
      await store.configure('http://100.64.0.2:8787/');
      expect(store.url, 'http://100.64.0.2:8787');
      expect(store.outbox, isEmpty);
      expect(disk.values['controlUrl'], store.url);
      await store.configure(AppStore.defaultUrl);
      expect(store.outbox.values.single['label'], 'Original server');
    },
  );

  test('local persistence failure prevents transmission', () async {
    await store.initialize();
    disk.fail = true;
    await expectLater(store.send('Do not lose'), throwsStateError);
    expect(api.posts, isEmpty);
    expect(store.outbox.values.single['label'], 'Do not lose');
  });
}
