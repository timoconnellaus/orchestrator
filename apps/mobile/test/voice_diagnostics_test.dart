import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:orchestrator/voice/voice_diagnostics.dart';
import 'package:orchestrator/voice/voice_tuning.dart';

void main() {
  final now = DateTime.utc(2026);
  Map<String, dynamic> packet() => {
    'version': 1,
    'room': 'room',
    'speaker': 'phone',
    'sender': 'agent',
    'session': 'session',
    'nonce': 'one',
    'sequence': 1,
    'generation': 1,
    'sentAtMs': now.millisecondsSinceEpoch,
    'available': true,
    'fresh': true,
    'level': .5,
    'speech': true,
    'voiceTuning': VoiceTuning.defaults.toJson(),
  };
  VoiceDiagnosticsReceiver receiver() => VoiceDiagnosticsReceiver(
    room: 'room',
    speaker: 'phone',
    tuning: VoiceTuning.defaults,
  )..nonce = 'one';
  VoiceDiagnostics? receive(
    VoiceDiagnosticsReceiver r,
    Map<String, dynamic> p, {
    String sender = 'agent',
    String name = 'orchestrator-voice',
    bool agent = true,
    String local = 'phone',
    String room = 'room',
  }) => r.receive(
    utf8.encode(jsonEncode(p)),
    sender: sender,
    name: name,
    isAgent: agent,
    localSpeaker: local,
    currentRoom: room,
    now: now,
  );

  test('diagnostics validate exact bounded schema sender AGENT name room linked speaker and tuning', () {
    expect(receive(receiver(), packet())!.level, .5);
    for (final patch in [
      {'version': 2},
      {'version': true},
      {'room': 'old'},
      {'speaker': 'other'},
      {'sender': 'spoof'},
      {'session': ''},
      {'nonce': 'old'},
      {'sequence': -1},
      {'sequence': 1.1},
      {'generation': false},
      {'generation': -1},
      {'sentAtMs': now.millisecondsSinceEpoch - 1501},
      {'sentAtMs': now.millisecondsSinceEpoch + 1501},
      {'level': -1},
      {'level': 1.01},
      {'level': true},
      {'speech': 1},
      {'available': 'true'},
      {'text': 'not allowed'},
      {'voiceTuning': VoiceTuning.noisyRoom.toJson()},
      {'fresh': false},
      {'available': false},
    ]) {
      expect(
        receive(receiver(), {...packet(), ...patch}),
        isNull,
        reason: '$patch',
      );
    }
    expect(receive(receiver(), packet(), agent: false), isNull);
    expect(receive(receiver(), packet(), name: 'unknown'), isNull);
    expect(receive(receiver(), packet(), local: 'other'), isNull);
    expect(receive(receiver(), packet(), room: 'old'), isNull);
    expect(
      receiver().receive(
        List.filled(2049, 32),
        sender: 'agent',
        name: 'orchestrator-voice',
        isAgent: true,
        localSpeaker: 'phone',
        currentRoom: 'room',
      ),
      isNull,
    );
    expect(
      receiver().receive(
        utf8.encode('{"level":NaN}'),
        sender: 'agent',
        name: 'orchestrator-voice',
        isAgent: true,
        localSpeaker: 'phone',
        currentRoom: 'room',
      ),
      isNull,
    );
  });

  test('drops out of order wrong session sender generation and old capture nonce after reset', () {
    final r = receiver();
    expect(receive(r, packet()), isNotNull);
    expect(receive(r, packet()), isNull);
    expect(receive(r, {...packet(), 'sequence': 2, 'generation': 0}), isNull);
    expect(
      receive(r, {...packet(), 'sequence': 2, 'session': 'other'}),
      isNull,
    );
    expect(
      receive(r, {
        ...packet(),
        'sequence': 2,
        'sender': 'other',
      }, sender: 'other'),
      isNull,
    );
    r.nonce = null; // mute/disconnect boundary
    expect(receive(r, {...packet(), 'sequence': 2}), isNull);
    r.nonce = 'two'; // explicit unmute/reconnect diagnostics only
    expect(receive(r, {...packet(), 'sequence': 2}), isNull);
    expect(
      receive(r, {...packet(), 'nonce': 'two', 'sequence': 3, 'generation': 2}),
      isNotNull,
    );
    final fallback = receive(r, {
      ...packet(),
      'nonce': 'two',
      'sequence': 4,
      'generation': 2,
      'available': false,
      'fresh': false,
      'level': 0,
      'speech': false,
    });
    expect(fallback!.available, false);
    expect(fallback.tuning, VoiceTuning.defaults);
  });
}
