import 'package:flutter/foundation.dart';
import 'package:livekit_client/livekit_client.dart';

@immutable
class VoiceTuning {
  const VoiceTuning._({
    this.activationThreshold = .5,
    this.minSpeechMs = 50,
    this.endSilenceMs = 550,
    this.interruptionMs = 500,
    this.echoCancellation = true,
    this.noiseSuppression = true,
    this.autoGainControl = true,
  });
  static const defaults = VoiceTuning._();
  static const noisyRoom = VoiceTuning._(
    activationThreshold: .65,
    minSpeechMs: 150,
    endSilenceMs: 700,
    interruptionMs: 700,
  );
  static const responsive = VoiceTuning._(
    endSilenceMs: 350,
    interruptionMs: 300,
  );
  final double activationThreshold;
  final int minSpeechMs, endSilenceMs, interruptionMs;
  final bool echoCancellation, noiseSuppression, autoGainControl;

  factory VoiceTuning.fromJson(Object? value) {
    if (value is! Map ||
        value.length != defaults.toJson().length ||
        !defaults.toJson().keys.every(value.containsKey) ||
        value['version'] is! int ||
        value['version'] != 1) {
      throw const FormatException('Unsupported voice tuning fields/version');
    }
    num number(String key, num low, num high, {bool integer = true}) {
      final n = value[key];
      if (n is! num ||
          !n.isFinite ||
          (integer && n is! int) ||
          n < low ||
          n > high) {
        throw FormatException('Invalid $key');
      }
      return n;
    }

    bool flag(String key) {
      if (value[key] is! bool) throw FormatException('Invalid $key');
      return value[key] as bool;
    }

    return VoiceTuning._(
      activationThreshold: number(
        'activationThreshold',
        .3,
        .8,
        integer: false,
      ).toDouble(),
      minSpeechMs: number('minSpeechMs', 50, 300).toInt(),
      endSilenceMs: number('endSilenceMs', 300, 1200).toInt(),
      interruptionMs: number('interruptionMs', 300, 1200).toInt(),
      echoCancellation: flag('echoCancellation'),
      noiseSuppression: flag('noiseSuppression'),
      autoGainControl: flag('autoGainControl'),
    );
  }
  Map<String, dynamic> toJson() => {
    'version': 1,
    'activationThreshold': activationThreshold,
    'minSpeechMs': minSpeechMs,
    'endSilenceMs': endSilenceMs,
    'interruptionMs': interruptionMs,
    'echoCancellation': echoCancellation,
    'noiseSuppression': noiseSuppression,
    'autoGainControl': autoGainControl,
  };
  VoiceTuning withValue(String key, Object value) =>
      VoiceTuning.fromJson({...toJson(), key: value});
  RoomOptions get roomOptions =>
      RoomOptions(defaultAudioCaptureOptions: captureOptions);
  AudioCaptureOptions get captureOptions => AudioCaptureOptions(
    echoCancellation: echoCancellation,
    noiseSuppression: noiseSuppression,
    autoGainControl: autoGainControl,
    stopAudioCaptureOnMute: true,
  );
  String get summary =>
      'Threshold ${activationThreshold.toStringAsFixed(2)} · speech $minSpeechMs ms · silence $endSilenceMs ms · interrupt $interruptionMs ms · AEC ${echoCancellation ? "on" : "off"} / NS ${noiseSuppression ? "on" : "off"} / AGC ${autoGainControl ? "on" : "off"}';
  @override
  bool operator ==(Object other) =>
      other is VoiceTuning && mapEquals(toJson(), other.toJson());
  @override
  int get hashCode => Object.hashAll(toJson().values);
}
