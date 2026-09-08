import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:orchestrator/data/app_store.dart';
import 'package:orchestrator/ui/orchestrator_app.dart';
import 'package:orchestrator/voice/voice_controller.dart';

import 'fakes.dart';

void main() {
  late FakeApi api;
  late AppStore store;
  late VoiceController voice;
  void initializeFakes() {
    api = FakeApi();
    store = AppStore(MemoryStore(), apiFactory: (_) => api);
    voice = VoiceController(FakeVoice(), FakeMicrophoneService());
  }

  tearDown(() async {
    store.dispose();
    voice.dispose();
    await api.stream.close();
  });

  testWidgets('missing backend shows reconnect and saved retry; no login', (
    tester,
  ) async {
    initializeFakes();
    api.offline = true;
    await store.initialize();
    await tester.pumpWidget(OrchestratorApp(store: store, voice: voice));
    expect(find.text('Offline'), findsOneWidget);
    expect(find.text('Login'), findsNothing);
    await tester.enterText(
      find.widgetWithText(TextField, 'Message'),
      'Help me plan',
    );
    await tester.tap(find.byTooltip('Send message'));
    await tester.pumpAndSettle();
    expect(find.text('Help me plan'), findsOneWidget);
    expect(find.text('Retry same request'), findsOneWidget);
    expect(api.posts.single['text'], 'Help me plan');
    api.offline = false;
    await store.reconnect();
  });

  testWidgets('Android chat edges never stretch message contents', (
    tester,
  ) async {
    initializeFakes();
    api.histories['main'] = [message('short', 'A short conversation')];
    await store.initialize();
    await tester.pumpWidget(OrchestratorApp(store: store, voice: voice));
    expect(
      Theme.of(tester.element(find.byType(ListView))).platform,
      TargetPlatform.android,
    );
    expect(find.byType(StretchingOverscrollIndicator), findsNothing);
    final list = find.byType(ListView);
    final scroll = tester.state<ScrollableState>(
      find.descendant(of: list, matching: find.byType(Scrollable)).first,
    );
    expect(scroll.position.maxScrollExtent, 0);
    await tester.drag(list, const Offset(0, -150));
    await tester.pumpAndSettle();
    expect(scroll.position.pixels, 0);
    expect(tester.takeException(), isNull);
  });

  for (final worker in [false, true]) {
    testWidgets(
      'long ${worker ? 'worker' : 'main'} history scrolls when dragging message text',
      (tester) async {
        initializeFakes();
        final conversation = worker ? 'session:worker-1' : 'main';
        api.sessions.add(session());
        api.histories[conversation] = List.generate(
          30,
          (i) => message(
            'history-$i',
            'History message $i\\nSecond line\\nThird line',
            conversation: conversation,
          ),
        );
        await store.initialize();
        await tester.pumpWidget(OrchestratorApp(store: store, voice: voice));
        if (worker) {
          await tester.tap(find.text('Sessions'));
          await tester.pumpAndSettle();
          await tester.tap(find.text('Atlas'));
          await tester.pumpAndSettle();
        }
        final scroll = tester.state<ScrollableState>(
          find
              .descendant(
                of: find.byType(ListView),
                matching: find.byType(Scrollable),
              )
              .first,
        );
        expect(scroll.position.maxScrollExtent, greaterThan(0));
        expect(scroll.position.pixels, 0);
        await tester.drag(
          find.byType(SelectableText).hitTestable().first,
          const Offset(0, 160),
        );
        await tester.pumpAndSettle();
        expect(scroll.position.pixels, greaterThan(0));
        expect(tester.takeException(), isNull);
      },
    );
  }

  testWidgets(
    'live captions are transient and leave drafts and durable chat untouched',
    (tester) async {
      initializeFakes();
      api.voiceConfigured = true;
      await store.initialize();
      await tester.pumpWidget(OrchestratorApp(store: store, voice: voice));
      await voice.join(api);
      await tester.pumpAndSettle();
      await tester.enterText(
        find.widgetWithText(TextField, 'Message'),
        'Keep my draft',
      );
      final media = voice.backend as FakeVoice;
      media.onUserTranscript!(const VoiceTranscript('caption', 'Check'));
      await tester.pump();
      expect(find.text('Hearing… · not yet sent'), findsOneWidget);
      expect(find.text('Check'), findsOneWidget);
      media.onUserTranscript!(
        const VoiceTranscript('caption', 'Check the sessions'),
      );
      await tester.pump();
      expect(find.text('Check'), findsNothing);
      expect(find.text('Check the sessions'), findsOneWidget);
      expect(find.text('Keep my draft'), findsOneWidget);
      expect(store.messages, isEmpty);
      expect(store.outbox, isEmpty);
      expect(api.paths, ['/v1/voice/token']);
      media.onUserTranscript!(
        const VoiceTranscript('caption', 'Check the sessions', isFinal: true),
      );
      await tester.pump();
      expect(find.byKey(const ValueKey('live-voice-caption')), findsNothing);
      expect(store.messages, isEmpty);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('history bubbles, session status, worker thread and composer', (
    tester,
  ) async {
    initializeFakes();
    api.sessions.add(session());
    api.histories['main'] = [message('main-1', 'Ready to help')];
    api.histories['session:worker-1'] = [
      message(
        'thread-1',
        'Worker progress',
        conversation: 'session:worker-1',
        role: 'worker',
      ),
    ];
    await store.initialize();
    await tester.pumpWidget(OrchestratorApp(store: store, voice: voice));
    expect(find.text('Ready to help'), findsOneWidget);
    await tester.tap(find.text('Sessions'));
    await tester.pumpAndSettle();
    expect(find.text('Atlas'), findsOneWidget);
    expect(find.text('codex  ·  working  ·  connected'), findsOneWidget);
    await tester.tap(find.text('Atlas'));
    await tester.pumpAndSettle();
    expect(find.text('Worker progress'), findsOneWidget);
    await tester.enterText(
      find.widgetWithText(TextField, 'Message'),
      'Next step',
    );
    await tester.tap(find.byTooltip('Send message'));
    await tester.pumpAndSettle();
    expect(api.paths.last, '/v1/sessions/worker-1/messages');
  });

  testWidgets('new worker dialog validates and submits chosen agent', (
    tester,
  ) async {
    initializeFakes();
    await store.initialize();
    await tester.pumpWidget(OrchestratorApp(store: store, voice: voice));
    await tester.tap(find.text('Sessions'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('New worker'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Create worker'));
    await tester.pumpAndSettle();
    expect(find.text('Required'), findsNWidgets(3));
    await tester.enterText(
      find.widgetWithText(TextFormField, 'Worker name'),
      'Scout',
    );
    await tester.enterText(
      find.widgetWithText(TextFormField, 'Project directory'),
      '/projects/demo',
    );
    await tester.enterText(
      find.widgetWithText(TextFormField, 'Instructions'),
      'Review',
    );
    await tester.ensureVisible(find.byType(DropdownButtonFormField<String>));
    await tester.tap(find.byType(DropdownButtonFormField<String>));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Claude').last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Create worker'));
    await tester.pumpAndSettle();
    expect(api.posts.single['agent'], 'claude');
    expect(api.posts.single['name'], 'Scout');
  });

  testWidgets('voice displays armed, mute and disconnect with fake media', (
    tester,
  ) async {
    initializeFakes();
    api.voiceConfigured = true;
    await store.initialize();
    await tester.pumpWidget(OrchestratorApp(store: store, voice: voice));
    await tester.tap(find.text('Join voice'));
    await tester.pumpAndSettle();
    expect(find.text('Listening · mic armed'), findsOneWidget);
    await tester.tap(find.text('Mute'));
    await tester.pumpAndSettle();
    expect(find.text('Muted · speaker on'), findsOneWidget);
    await tester.tap(find.byTooltip('Disconnect voice'));
    await tester.pumpAndSettle();
    expect(find.text('Voice off'), findsOneWidget);
  });

  testWidgets('compact layout and settings URL are usable without overflow', (
    tester,
  ) async {
    initializeFakes();
    tester.view.physicalSize = const Size(360, 740);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await store.initialize();
    await tester.pumpWidget(OrchestratorApp(store: store, voice: voice));
    await tester.tap(find.text('Settings'));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.widgetWithText(TextField, 'Control URL'),
      'http://100.64.0.4:8787',
    );
    await tester.tap(find.text('Save & reconnect'));
    await tester.runAsync(() async {
      await Future<void>.delayed(Duration.zero);
    });
    await tester.pumpAndSettle();
    expect(store.url, 'http://100.64.0.4:8787');
    expect(tester.takeException(), isNull);
  });
}
