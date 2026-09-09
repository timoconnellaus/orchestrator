import 'dart:convert';

import 'voice_tuning.dart';

class VoiceDiagnostics {
  const VoiceDiagnostics({
    required this.level,
    required this.speech,
    required this.available,
    required this.fresh,
    required this.tuning,
  });
  final double level;
  final bool speech, available, fresh;
  final VoiceTuning tuning;
}

/// One validator per explicit join; nonce changes on capture/reconnect boundaries.
class VoiceDiagnosticsReceiver {
  VoiceDiagnosticsReceiver({
    required this.room,
    required this.speaker,
    required this.tuning,
  });
  final String room, speaker;
  final VoiceTuning tuning;
  String? nonce;
  String? _sender, _session;
  int _sequence = -1, _generation = -1;
  static const topic = 'orchestrator.voice.diagnostics.v1';
  static const probeTopic = 'orchestrator.voice.probe.v1';
  VoiceDiagnostics? receive(
    List<int> bytes, {
    required String sender,
    required String name,
    required bool isAgent,
    required String localSpeaker,
    required String currentRoom,
    DateTime? now,
  }) {
    if (bytes.length > 2048 ||
        nonce == null ||
        !isAgent ||
        name != 'orchestrator-voice' ||
        localSpeaker != speaker ||
        currentRoom != room) {
      return null;
    }
    try {
      final p = jsonDecode(utf8.decode(bytes));
      const fields = {
        'version',
        'room',
        'speaker',
        'sender',
        'session',
        'nonce',
        'sequence',
        'generation',
        'sentAtMs',
        'available',
        'fresh',
        'level',
        'speech',
        'voiceTuning',
      };
      if (p is! Map ||
          p.length != fields.length ||
          !fields.every(p.containsKey) ||
          p['version'] is! int ||
          p['version'] != 1 ||
          p['room'] != room ||
          p['speaker'] != speaker ||
          p['sender'] != sender ||
          p['nonce'] != nonce ||
          p['session'] is! String ||
          (p['session'] as String).isEmpty ||
          (p['session'] as String).length > 64 ||
          p['sequence'] is! int ||
          p['generation'] is! int ||
          p['sentAtMs'] is! int ||
          p['sequence'] < 0 ||
          p['generation'] < 0 ||
          p['sequence'] > 9007199254740991 ||
          p['generation'] > 9007199254740991 ||
          p['available'] is! bool ||
          p['fresh'] is! bool ||
          p['speech'] is! bool ||
          p['level'] is! num ||
          !(p['level'] as num).isFinite ||
          p['level'] < 0 ||
          p['level'] > 1) {
        return null;
      }
      final age =
          (now ?? DateTime.now()).millisecondsSinceEpoch -
          (p['sentAtMs'] as int);
      if (age < -1500 ||
          age > 1500 ||
          (_sender != null && _sender != sender) ||
          (_session != null && _session != p['session']) ||
          p['sequence'] <= _sequence ||
          p['generation'] < _generation ||
          VoiceTuning.fromJson(p['voiceTuning']) != tuning ||
          (!p['available'] && p['fresh']) ||
          ((!p['fresh'] || !p['available']) &&
              (p['level'] != 0 || p['speech'] != false))) {
        return null;
      }
      _sender = sender;
      _session = p['session'] as String;
      _sequence = p['sequence'] as int;
      _generation = p['generation'] as int;
      return VoiceDiagnostics(
        level: (p['level'] as num).toDouble(),
        speech: p['speech'] as bool,
        available: p['available'] as bool,
        fresh: p['fresh'] as bool,
        tuning: tuning,
      );
    } catch (_) {
      return null;
    }
  }
}

class VoiceJoinOptions {
  const VoiceJoinOptions(this.tuning, {this.room, this.speaker});
  final VoiceTuning tuning;
  final String? room, speaker;
}
