import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:livekit_client/livekit_client.dart' show TranscriptionSegment;
import 'package:orchestrator/voice/voice_controller.dart';
import 'package:orchestrator/voice/voice_tuning.dart';

import 'fakes.dart';

void main() {
  late FakeVoice media;
  late FakeMicrophoneService native;
  late VoiceController voice;
  late FakeApi api;
  setUp(() {
    media = FakeVoice();
    native = FakeMicrophoneService();
    voice = VoiceController(media, native);
    api = FakeApi()..voiceConfigured = true;
  });
  tearDown(() async {
    voice.dispose();
    await api.stream.close();
  });

  test(
    'explicit join arms, mute releases capture and reconnect never re-arms',
    () async {
      expect(voice.armed, false);
      await voice.join(api);
      expect(native.starts, 1);
      expect(voice.armed, true);
      expect(api.paths.single, '/v1/voice/token');
      expect(api.posts.single, {
        'conversationId': 'main',
        'voiceTuning': VoiceTuning.defaults.toJson(),
      });
      await voice.toggleMute();
      expect(media.microphoneCalls, [true, false]);
      expect(voice.connected, true);
      expect(voice.armed, false);
      media.onState!('Connected');
      expect(voice.status, contains('muted'));
      expect(native.starts, 1);
      await voice.disconnect();
      media.onState!('Connected');
      expect(voice.armed, false);
      expect(voice.connected, false);
    },
  );

  test(
    'media controls mirror voice state and control mute or disconnect',
    () async {
      await voice.join(api);
      expect(native.mediaSessionStates, [(active: true, muted: false)]);

      native.onPause!();
      await Future<void>.delayed(Duration.zero);
      expect(voice.armed, false);
      expect(native.mediaSessionStates.last, (active: true, muted: true));

      native.onPlay!();
      await Future<void>.delayed(Duration.zero);
      expect(voice.armed, true);
      expect(native.mediaSessionStates.last, (active: true, muted: false));

      native.onStop!();
      await Future<void>.delayed(Duration.zero);
      expect(voice.connected, false);
      expect(native.mediaSessionStates.last, (active: false, muted: true));
    },
  );

  test(
    'notification stop disarms without allowing late media reconnect to arm',
    () async {
      await voice.join(api);
      native.onStop!();
      await Future<void>.delayed(Duration.zero);
      media.onState!('Connected');
      expect(voice.armed, false);
      expect(voice.connected, false);
      expect(media.disconnects, greaterThan(0));
      expect(native.stops, greaterThan(0));
      expect(media.microphoneCalls, [true]);
    },
  );

  test('disconnect during join cannot publish microphone later', () async {
    media.connecting = Completer<void>();
    final joining = voice.join(api);
    await Future<void>.delayed(Duration.zero);
    await voice.disconnect();
    media.connecting!.complete();
    await joining;
    expect(voice.armed, false);
    expect(media.microphoneCalls, isEmpty);
    expect(native.stops, greaterThan(0));
  });

  test('missing voice config releases native service and explains text remains available', () async {
    api.voiceConfigured = false;
    await voice.join(api);
    expect(voice.error, contains('not configured'));
    expect(voice.error, contains('Text chat still works'));
    expect(voice.armed, false);
    expect(native.stops, greaterThan(0));
    expect(media.connections, 0);
  });

  test('caption transport only admits the local transcribed speaker', () {
    final now = DateTime.utc(2026);
    final segments = [
      TranscriptionSegment(
        id: 's1',
        text: 'Hello',
        firstReceivedTime: now,
        lastReceivedTime: now,
        isFinal: false,
        language: 'en',
      ),
    ];
    expect(LiveKitVoice.userTranscripts('phone', 'agent', segments), isEmpty);
    expect(
      LiveKitVoice.userTranscripts('phone', 'another-phone', segments),
      isEmpty,
    );
    expect(LiveKitVoice.userTranscripts(null, 'phone', segments), isEmpty);
    final accepted = LiveKitVoice.userTranscripts(
      'phone',
      'phone',
      segments,
    ).single;
    expect(accepted.segmentId, 's1');
    expect(accepted.text, 'Hello');
    expect(accepted.isFinal, false);
  });

  test(
    'captions replace hypotheses; old finals cannot clear newer speech',
    () async {
      await voice.join(api);
      media.onUserTranscript!(const VoiceTranscript('one', 'Build'));
      media.onUserTranscript!(const VoiceTranscript('one', 'Build the app'));
      expect(voice.liveCaption!.text, 'Build the app');
      media.onUserTranscript!(const VoiceTranscript('two', 'Actually'));
      media.onUserTranscript!(
        const VoiceTranscript('one', 'Build the app', isFinal: true),
      );
      expect(voice.liveCaption!.text, 'Actually');
      media.onUserTranscript!(
        const VoiceTranscript('two', 'Actually stop', isFinal: true),
      );
      expect(voice.liveCaption, isNull);
      media.onUserTranscript!(const VoiceTranscript('two', 'Late interim'));
      expect(voice.liveCaption, isNull);
      expect(api.paths, [
        '/v1/voice/token',
      ], reason: 'Caption events never submit chat');
    },
  );

  test(
    'captions clear immediately on mute, reconnect, and disconnect',
    () async {
      await voice.join(api);
      media.onUserTranscript!(const VoiceTranscript('one', 'Before mute'));
      final muting = voice.toggleMute();
      expect(voice.liveCaption, isNull);
      await muting;
      media.onUserTranscript!(const VoiceTranscript('muted', 'Ignore me'));
      expect(voice.liveCaption, isNull);
      await voice.toggleMute();
      media.onUserTranscript!(
        const VoiceTranscript('muted', 'Late muted packet'),
      );
      expect(voice.liveCaption, isNull);
      media.onUserTranscript!(const VoiceTranscript('two', 'New speech'));
      media.onState!('Reconnecting audio');
      expect(voice.liveCaption, isNull);
      media.onUserTranscript!(
        const VoiceTranscript('offline', 'Late offline packet'),
      );
      expect(voice.liveCaption, isNull);
      media.onState!('Connected');
      media.onUserTranscript!(
        const VoiceTranscript('three', 'After reconnect'),
      );
      expect(voice.liveCaption!.text, 'After reconnect');
      final disconnecting = voice.disconnect();
      expect(voice.liveCaption, isNull);
      await disconnecting;
      media.onUserTranscript!(
        const VoiceTranscript('four', 'After disconnect'),
      );
      expect(voice.liveCaption, isNull);
    },
  );

  test('permission rejection never requests token or opens media', () async {
    native.deny = true;
    await voice.join(api);
    expect(voice.error, contains('Permission denied'));
    expect(api.posts, isEmpty);
    expect(media.microphoneCalls, isEmpty);
    expect(voice.armed, false);
  });
}
