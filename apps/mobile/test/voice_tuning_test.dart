import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:orchestrator/data/app_store.dart';
import 'package:orchestrator/ui/voice_tuning_panel.dart';
import 'package:orchestrator/voice/voice_controller.dart';
import 'package:orchestrator/voice/voice_diagnostics.dart';
import 'package:orchestrator/voice/voice_tuning.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'fakes.dart';

class DelayedMicrophone extends FakeVoice {
  final enabling = Completer<void>();
  @override
  Future<void> microphone(bool enabled) async {
    await super.microphone(enabled);
    if (enabled) await enabling.future;
  }
}

const diagnostic = VoiceDiagnostics(
  level: .25,
  speech: true,
  available: true,
  fresh: true,
  tuning: VoiceTuning.defaults,
);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('voice tuning cross-language defaults and strict validation', () {
    final fixture = jsonDecode(
      File('../../docs/fixtures/voice-tuning-v1.json').readAsStringSync(),
    ) as Map;
    expect(VoiceTuning.defaults.toJson(), fixture['defaults']);
    for (final patch in fixture['validOverrides'] as List) {
      final value = {
        ...VoiceTuning.defaults.toJson(),
        ...(patch as Map).cast<String, dynamic>(),
      };
      expect(VoiceTuning.fromJson(value).toJson(), value);
    }
    for (final patch in [
      ...fixture['invalidOverrides'] as List,
      {'activationThreshold': double.nan},
      {'activationThreshold': double.infinity},
      {'elevenLabsSpeed': double.nan},
      {'elevenLabsSpeed': double.infinity},
    ]) {
      expect(
        () => VoiceTuning.fromJson({
          ...VoiceTuning.defaults.toJson(),
          ...(patch as Map).cast<String, dynamic>(),
        }),
        throwsFormatException,
      );
    }
    expect(() => VoiceTuning.fromJson({}), throwsFormatException);
  });

  test(
    'all eight capture combinations map to supported pinned SDK options',
    () {
      for (final aec in [false, true]) {
        for (final ns in [false, true]) {
          for (final agc in [false, true]) {
            final tuning = VoiceTuning.defaults
                .withValue('echoCancellation', aec)
                .withValue('noiseSuppression', ns)
                .withValue('autoGainControl', agc);
            for (final options in [
              tuning.captureOptions,
              tuning.roomOptions.defaultAudioCaptureOptions,
            ]) {
              expect(options.echoCancellation, aec);
              expect(options.noiseSuppression, ns);
              expect(options.autoGainControl, agc);
              expect(options.stopAudioCaptureOnMute, true);
            }
          }
        }
      }
    },
  );

  test(
    'preferences persist tuning independently of history and show save errors',
    () async {
      SharedPreferences.setMockInitialValues({});
      final preferences = PreferencesStore(
        await SharedPreferences.getInstance(),
      );
      final api = FakeApi();
      final store = AppStore(preferences, apiFactory: (_) => api);
      await store.initialize();
      final saved = VoiceTuning.noisyRoom.withValue('elevenLabsSpeed', 1.15);
      await store.saveVoiceTuning(saved);
      final restored = AppStore(preferences, apiFactory: (_) => api);
      await restored.initialize();
      expect(restored.savedVoiceTuning, saved);
      expect(
        preferences.read('snapshot:${store.url}'),
        isNot(contains('voiceTuning')),
      );
      expect(
        preferences.read('snapshot:${store.url}'),
        isNot(contains('diagnostics')),
      );
      final memory = MemoryStore()..fail = true;
      final failing = AppStore(memory, apiFactory: (_) => api)..api = api;
      await expectLater(
        failing.saveVoiceTuning(VoiceTuning.responsive),
        throwsStateError,
      );
      expect(failing.savedVoiceTuning, VoiceTuning.defaults);
      expect(failing.voiceTuningError, contains('not saved'));
      failing.dispose();
      restored.dispose();
      store.dispose();
      await api.stream.close();
    },
  );

  test(
    'join freezes before awaits despite saving, mute and SDK reconnect',
    () async {
      final api = FakeApi()..voiceConfigured = true;
      final memory = MemoryStore();
      final store = AppStore(memory, apiFactory: (_) => api);
      await store.initialize();
      final media = FakeVoice()..connecting = Completer<void>();
      final voice = VoiceController(media, FakeMicrophoneService());
      final joining = voice.join(api, tuning: store.savedVoiceTuning);
      await Future<void>.delayed(Duration.zero);
      final next = VoiceTuning.noisyRoom.withValue('elevenLabsSpeed', 1.2);
      await store.saveVoiceTuning(next);
      expect(voice.activeTuning, VoiceTuning.defaults);
      expect(media.microphoneCalls, isEmpty);
      media.connecting!.complete();
      await joining;
      await voice.toggleMute();
      await voice.toggleMute();
      media.onState!('Reconnecting audio');
      media.onState!('Connected');
      expect(media.joinOptions!.tuning, VoiceTuning.defaults);
      expect(media.connections, 1);
      expect(
        voice.confirmedTuning,
        isNull,
      ); // token echo isn't Mac confirmation
      await voice.disconnect();
      await voice.join(api, tuning: store.savedVoiceTuning);
      expect(media.joinOptions!.tuning, next);
      voice.dispose();
      store.dispose();
      await api.stream.close();
    },
  );

  test('old server cannot silently activate custom settings; defaults remain unconfirmed', () async {
    final api = FakeApi()
      ..postHandler = (_, _) async => {'url': 'ws://mock', 'token': 'mock'};
    final media = FakeVoice();
    final voice = VoiceController(media, FakeMicrophoneService());
    await voice.join(api, tuning: VoiceTuning.noisyRoom);
    expect(voice.connected, false);
    expect(voice.error, contains('did not acknowledge'));
    expect(media.connections, 0);
    await voice.join(api);
    expect(voice.connected, true);
    expect(voice.confirmedTuning, isNull);
    voice.dispose();
    await api.stream.close();
  });

  testWidgets(
    'join acknowledgement buffered during enable expires without rearming',
    (tester) async {
      final api = FakeApi()..voiceConfigured = true;
      final media = DelayedMicrophone();
      final voice = VoiceController(media, FakeMicrophoneService());
      final joining = voice.join(api);
      await tester.pump();
      media.onDiagnostics!(diagnostic);
      expect(voice.diagnostics, isNull);
      media.enabling.complete();
      await joining;
      expect(voice.diagnostics!.speech, true);
      await tester.pump(const Duration(milliseconds: 1501));
      expect(voice.diagnostics, isNull);
      media.onDiagnostics!(diagnostic);
      await voice.toggleMute();
      expect(voice.diagnostics, isNull);
      media.onDiagnostics!(diagnostic);
      expect(voice.diagnostics, isNull);
      media.onState!('Connected');
      expect(voice.armed, false);
      await voice.toggleMute();
      media.onDiagnostics!(diagnostic);
      media.onState!('Reconnecting audio');
      expect(voice.diagnostics, isNull);
      voice.dispose();
      expect(media.onDiagnostics, isNull);
      await tester.pump();
      await api.stream.close();
    },
  );

  testWidgets(
    'expired acknowledgement cannot be revived by delayed enable or canceled join',
    (tester) async {
      final api = FakeApi()..voiceConfigured = true;
      final media = DelayedMicrophone();
      final voice = VoiceController(media, FakeMicrophoneService());
      final joining = voice.join(api);
      await tester.pump();
      media.onDiagnostics!(diagnostic);
      await tester.pump(const Duration(seconds: 2));
      await voice.disconnect();
      media.enabling.complete();
      await joining;
      expect(voice.diagnostics, isNull);
      expect(voice.activeTuning, isNull);
      expect(voice.armed, false);
      voice.dispose();
      await tester.pump();
      await api.stream.close();
    },
  );

  testWidgets(
    'expired buffered acknowledgement stays expired when join succeeds',
    (tester) async {
      final api = FakeApi()..voiceConfigured = true;
      final media = DelayedMicrophone();
      final voice = VoiceController(media, FakeMicrophoneService());
      final joining = voice.join(api);
      await tester.pump();
      media.onDiagnostics!(diagnostic);
      await tester.pump(const Duration(seconds: 2));
      media.enabling.complete();
      await joining;
      expect(voice.armed, true);
      expect(voice.diagnostics, isNull);
      expect(voice.confirmedTuning, isNull);
      voice.dispose();
      await tester.pump();
      await api.stream.close();
    },
  );

  testWidgets(
    'compact tuning panel sliders presets reset saved-active indication and errors',
    (tester) async {
      tester.view.physicalSize = const Size(320, 640);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final api = FakeApi()..voiceConfigured = true;
      final memory = MemoryStore();
      final store = AppStore(memory, apiFactory: (_) => api);
      await store.initialize();
      final media = FakeVoice();
      final voice = VoiceController(media, FakeMicrophoneService());
      await voice.join(api);
      await tester.pumpWidget(
        MaterialApp(
          theme: ThemeData.dark(useMaterial3: true),
          home: Scaffold(
            body: SingleChildScrollView(
              child: VoiceTuningPanel(store: store, voice: voice),
            ),
          ),
        ),
      );
      expect(find.byType(Slider), findsNWidgets(5));
      await tester.ensureVisible(find.byType(Slider).first);
      await tester.drag(find.byType(Slider).first, const Offset(30, 0));
      await tester.pumpAndSettle();
      expect(find.text('Unsaved draft'), findsOneWidget);
      await tester.ensureVisible(find.text('Responsive'));
      await tester.tap(find.text('Responsive'));
      await tester.pumpAndSettle();
      expect(tester.widget<Slider>(find.byType(Slider).at(2)).value, 350);
      await tester.ensureVisible(find.text('Noisy room'));
      await tester.tap(find.text('Noisy room'));
      await tester.pumpAndSettle();
      expect(find.text('Unsaved draft'), findsOneWidget);
      final speed = tester.widget<Slider>(find.byType(Slider).at(4));
      expect(speed.min, .8);
      expect(speed.max, 1.2);
      speed.onChanged!(1.2);
      await tester.pumpAndSettle();
      expect(find.text('ElevenLabs speaking speed: 1.20×'), findsOneWidget);
      final expected = VoiceTuning.noisyRoom.withValue('elevenLabsSpeed', 1.2);
      final save = find.text('Save for next join');
      await tester.ensureVisible(save);
      await tester.tap(save);
      await tester.pumpAndSettle();
      expect(store.savedVoiceTuning, expected);
      expect(voice.activeTuning, VoiceTuning.defaults);
      expect(
        find.text('Saved settings differ from this join.'),
        findsOneWidget,
      );
      expect(media.microphoneCalls, [true]);
      final reset = find.text('Reset to defaults');
      await tester.ensureVisible(reset);
      await tester.tap(reset);
      await tester.pumpAndSettle();
      expect(store.savedVoiceTuning, expected);
      memory.fail = true;
      await tester.ensureVisible(save);
      await tester.tap(save);
      await tester.pumpAndSettle();
      expect(
        find.textContaining('Voice settings were not saved'),
        findsOneWidget,
      );
      await tester.ensureVisible(find.text('Echo cancellation (AEC)'));
      await tester.tap(find.text('Echo cancellation (AEC)'));
      await tester.pumpAndSettle();
      expect(find.textContaining('assistant hear itself'), findsOneWidget);
      expect(store.savedVoiceTuning, expected);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      voice.dispose();
      store.dispose();
      await api.stream.close();
    },
  );
}
