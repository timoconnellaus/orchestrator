import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:orchestrator/voice/voice_controller.dart';

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
      expect(api.posts.single, {'conversationId': 'main'});
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

  test('permission rejection never requests token or opens media', () async {
    native.deny = true;
    await voice.join(api);
    expect(voice.error, contains('Permission denied'));
    expect(api.posts, isEmpty);
    expect(media.microphoneCalls, isEmpty);
    expect(voice.armed, false);
  });
}
