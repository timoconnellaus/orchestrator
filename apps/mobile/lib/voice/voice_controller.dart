import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:livekit_client/livekit_client.dart';
import 'package:uuid/uuid.dart';

import '../data/control_api.dart';
import 'voice_diagnostics.dart';
import 'voice_tuning.dart';

@immutable
class VoiceTranscript {
  const VoiceTranscript(this.segmentId, this.text, {this.isFinal = false});
  final String segmentId;
  final String text;
  final bool isFinal;
}

abstract class VoiceBackend {
  Future<void> connect(String url, String token, {VoiceJoinOptions? options});
  Future<void> microphone(bool enabled);
  Future<void> disconnect();
  void Function(String state)? onState;
  void Function(VoiceTranscript transcript)? onUserTranscript;
  void Function(VoiceDiagnostics? diagnostics)? onDiagnostics;
}

class LiveKitVoice implements VoiceBackend {
  Room? _room;
  EventsListener<RoomEvent>? _listener;
  @override
  void Function(String state)? onState;
  @override
  void Function(VoiceTranscript transcript)? onUserTranscript;

  @override
  void Function(VoiceDiagnostics? diagnostics)? onDiagnostics;
  VoiceTuning _tuning = VoiceTuning.defaults;
  VoiceDiagnosticsReceiver? _diagnostics;
  Timer? _probeTimer;
  bool _captureEnabled = false;
  bool _probing = false;

  void _resetDiagnostics({bool probe = false}) {
    _probeTimer?.cancel();
    _diagnostics?.nonce = probe ? const Uuid().v4() : null;
    onDiagnostics?.call(null);
    if (probe) {
      unawaited(_probe());
      _probeTimer = Timer.periodic(
        const Duration(seconds: 1),
        (_) => unawaited(_probe()),
      );
    }
  }

  Future<void> _probe() async {
    final nonce = _diagnostics?.nonce;
    final participant = _room?.localParticipant;
    if (_probing || nonce == null || participant == null) return;
    _probing = true;
    try {
      await participant
          .publishData(
            utf8.encode(jsonEncode({'nonce': nonce})),
            reliable: true,
            topic: VoiceDiagnosticsReceiver.probeTopic,
          )
          // Future.timeout cannot cancel an SDK send. Keep the in-flight guard
          // until the underlying future completes so a blocked transport cannot
          // accumulate one abandoned send per timer tick.
          .whenComplete(() => _probing = false)
          .timeout(const Duration(milliseconds: 500));
    } catch (_) {
      // Diagnostics never interfere with microphone lifecycle.
    }
  }

  @visibleForTesting
  static Iterable<VoiceTranscript> userTranscripts(
    String? localIdentity,
    String transcribedIdentity,
    Iterable<TranscriptionSegment> segments,
  ) sync* {
    if (localIdentity == null ||
        localIdentity.isEmpty ||
        localIdentity != transcribedIdentity) {
      return;
    }
    for (final segment in segments) {
      yield VoiceTranscript(segment.id, segment.text, isFinal: segment.isFinal);
    }
  }

  @override
  Future<void> connect(
    String url,
    String token, {
    VoiceJoinOptions? options,
  }) async {
    _tuning = options?.tuning ?? VoiceTuning.defaults;
    _captureEnabled = false;
    _resetDiagnostics();
    _diagnostics = options?.room != null && options?.speaker != null
        ? VoiceDiagnosticsReceiver(
            room: options!.room!,
            speaker: options.speaker!,
            tuning: _tuning,
          )
        : null;
    // Full SDK reconnect recreates tracks from the ROOM defaults, not the
    // options supplied to the previous setMicrophoneEnabled call.
    final room = Room(roomOptions: _tuning.roomOptions);
    _room = room;
    void state(String value) {
      if (_room == room) onState?.call(value);
    }

    _listener = room.createListener()
      ..on<RoomReconnectingEvent>((_) {
        if (_room != room) return;
        _resetDiagnostics();
        state('Reconnecting audio');
      })
      ..on<RoomReconnectedEvent>((_) {
        if (_room != room) return;
        _resetDiagnostics(probe: _captureEnabled);
        state('Connected');
      })
      ..on<RoomDisconnectedEvent>((_) => state('Disconnected'))
      ..on<DataReceivedEvent>((event) {
        final participant = event.participant;
        if (_room != room ||
            event.topic != VoiceDiagnosticsReceiver.topic ||
            participant == null) {
          return;
        }
        final value = _diagnostics?.receive(
          event.data,
          sender: participant.identity,
          name: participant.name,
          isAgent: participant.kind == ParticipantKind.AGENT,
          localSpeaker: room.localParticipant?.identity ?? '',
          currentRoom: room.name ?? '',
        );
        if (value != null) onDiagnostics?.call(value);
      })
      ..on<TranscriptionEvent>((event) {
        if (_room != room) return;
        // The event identity is the transcribed speaker, not the forwarding agent.
        // Consume only this transport, not lk.transcription text streams as well.
        for (final transcript in userTranscripts(
          room.localParticipant?.identity,
          event.participant.identity,
          event.segments,
        )) {
          onUserTranscript?.call(transcript);
        }
      });
    await room.connect(url, token).timeout(const Duration(seconds: 25));
    if (_room != room) {
      await room.disconnect();
      await room.dispose();
      return;
    }
    // Subscribed remote audio is played by the official SDK; route to speaker.
    await AudioManager.instance.setSpeakerOutputPreferred(true);
  }

  @override
  Future<void> microphone(bool enabled) async {
    final participant = _room?.localParticipant;
    if (participant == null && enabled) {
      throw StateError('Voice is not connected');
    }
    _captureEnabled = enabled;
    _resetDiagnostics(probe: enabled);
    await participant?.setMicrophoneEnabled(
      enabled,
      audioCaptureOptions: _tuning.captureOptions,
    );
  }

  @override
  Future<void> disconnect() async {
    _captureEnabled = false;
    _resetDiagnostics();
    _diagnostics = null;
    final room = _room;
    _room = null;
    await _listener?.dispose();
    _listener = null;
    if (room != null) {
      try {
        await room.disconnect();
      } finally {
        await room.dispose();
      }
    }
  }
}

abstract class MicrophoneService {
  Future<void> start();
  Future<void> stop();
  void Function()? onStop;
}

class AndroidMicrophoneService implements MicrophoneService {
  AndroidMicrophoneService() {
    _channel.setMethodCallHandler((call) async {
      if (call.method == 'stopRequested') onStop?.call();
    });
  }
  static const _channel = MethodChannel('dev.tim.orchestrator/microphone');
  @override
  void Function()? onStop;
  @override
  Future<void> start() async => _channel.invokeMethod<void>('arm');
  @override
  Future<void> stop() async => _channel.invokeMethod<void>('disarm');
}

class VoiceController extends ChangeNotifier {
  VoiceController(this.backend, this.service) {
    service.onStop = () {
      unawaited(disconnect());
    };
    backend.onUserTranscript = _receiveUserTranscript;
    backend.onDiagnostics = _receiveDiagnostics;
    backend.onState = (value) {
      if (value == 'Disconnected') {
        unawaited(disconnect());
      } else if (connected) {
        _captionsReady = value == 'Connected';
        if (!_captionsReady) {
          _clearCaption();
          _clearDiagnostics();
        }
        status = '$value · ${armed ? 'mic armed' : 'muted'}';
        _notify();
      }
    };
  }
  final VoiceBackend backend;
  final MicrophoneService service;
  bool armed = false;
  bool connected = false;
  bool busy = false;
  bool _disposed = false;
  int _epoch = 0;
  String status = 'Voice off';
  String? error;
  VoiceTranscript? liveCaption;
  bool _captionsReady = false;
  final Set<String> _retiredCaptionIds = {};
  VoiceTuning? activeTuning;
  VoiceTuning? confirmedTuning;
  VoiceDiagnostics? diagnostics;
  VoiceDiagnostics? _pendingDiagnostics;
  Timer? _diagnosticsExpiry;
  bool _joiningMicrophone = false;

  void _clearDiagnostics() {
    _diagnosticsExpiry?.cancel();
    diagnostics = null;
    confirmedTuning = null;
    _pendingDiagnostics = null;
  }

  void _receiveDiagnostics(VoiceDiagnostics? value) {
    if (_disposed) return;
    if (value == null) {
      _clearDiagnostics();
      _notify();
      return;
    }
    if (value.tuning != activeTuning ||
        (!_joiningMicrophone && (!connected || !armed || !_captionsReady))) {
      return;
    }
    _diagnosticsExpiry?.cancel();
    _diagnosticsExpiry = Timer(const Duration(milliseconds: 1500), () {
      _clearDiagnostics();
      _notify();
    });
    if (_joiningMicrophone) {
      _pendingDiagnostics = value;
      return;
    }
    diagnostics = value;
    confirmedTuning = value.tuning;
    _notify();
  }

  void _retireCaption(String id) {
    _retiredCaptionIds.add(id);
    if (_retiredCaptionIds.length > 128) {
      _retiredCaptionIds.remove(_retiredCaptionIds.first);
    }
  }

  void _clearCaption() {
    final caption = liveCaption;
    if (caption != null) _retireCaption(caption.segmentId);
    liveCaption = null;
  }

  void _receiveUserTranscript(VoiceTranscript transcript) {
    if (_disposed) return;
    if (!connected || !armed || !_captionsReady) {
      _retireCaption(transcript.segmentId);
      return;
    }
    if (_retiredCaptionIds.contains(transcript.segmentId)) return;
    if (transcript.isFinal || transcript.text.trim().isEmpty) {
      if (transcript.isFinal) _retireCaption(transcript.segmentId);
      if (liveCaption?.segmentId == transcript.segmentId) liveCaption = null;
    } else {
      if (liveCaption?.segmentId != transcript.segmentId) _clearCaption();
      liveCaption = transcript;
    }
    // Captions never enter AppStore, the typed composer, or the durable outbox.
    _notify();
  }

  Future<void> join(
    ControlApi api, {
    VoiceTuning tuning = VoiceTuning.defaults,
  }) async {
    if (busy || connected) return;
    final epoch = ++_epoch;
    activeTuning = tuning; // Freeze BEFORE permission/token/media awaits.
    _clearDiagnostics();
    _clearCaption();
    _retiredCaptionIds.clear();
    _captionsReady = false;
    busy = true;
    error = null;
    status = 'Requesting microphone';
    _notify();
    try {
      // This bridge only succeeds while Activity is RESUMED, after permission.
      await service.start();
      if (epoch != _epoch) return;
      status = 'Joining voice';
      _notify();
      final credentials = await api.post('/v1/voice/token', {
        'conversationId': 'main',
        'voiceTuning': tuning.toJson(),
      });
      if (epoch != _epoch) return;
      final effective = credentials['voiceTuning'];
      if (effective == null
          ? tuning != VoiceTuning.defaults
          : VoiceTuning.fromJson(effective) != tuning) {
        throw StateError(
          'Server did not acknowledge requested voice tuning. Update server or use defaults.',
        );
      }
      await backend.connect(
        credentials['url'] as String,
        credentials['token'] as String,
        options: VoiceJoinOptions(
          tuning,
          room: effective == null ? null : credentials['room'] as String?,
          speaker: effective == null ? null : credentials['speaker'] as String?,
        ),
      );
      if (epoch != _epoch) {
        await backend.disconnect();
        return;
      }
      _joiningMicrophone = true;
      await backend.microphone(true);
      _joiningMicrophone = false;
      if (epoch != _epoch) {
        await backend.disconnect();
        return;
      }
      connected = true;
      armed = true;
      _captionsReady = true;
      status = 'Listening · mic armed';
      final pending = _pendingDiagnostics;
      _pendingDiagnostics = null;
      if (pending != null) {
        // Preserve the original receipt expiry while microphone enable awaited.
        diagnostics = pending;
        confirmedTuning = pending.tuning;
      }
    } catch (e) {
      if (epoch != _epoch) return;
      error = 'Voice unavailable: $e. Text chat still works.';
      await _release();
      status = 'Voice off';
    } finally {
      _joiningMicrophone = false;
      if (epoch != _epoch || !armed) await service.stop();
      busy = false;
      _notify();
    }
  }

  Future<void> toggleMute() async {
    if (!connected || busy) return;
    final epoch = ++_epoch;
    busy = true;
    error = null;
    try {
      if (armed) {
        // Disarm intent is set before any await. SDK reconnect never calls arm.
        armed = false;
        _clearDiagnostics();
        _clearCaption();
        _notify();
        await backend.microphone(false);
        await service.stop();
        status = 'Muted · speaker on';
      } else {
        await service.start();
        if (epoch != _epoch) return;
        await backend.microphone(true);
        if (epoch != _epoch) {
          await backend.disconnect();
          return;
        }
        armed = true;
        status = 'Listening · mic armed';
      }
    } catch (e) {
      error = 'Microphone unavailable: $e';
      await _release();
      status = 'Voice off';
    } finally {
      busy = false;
      _notify();
    }
  }

  Future<void> _release() async {
    activeTuning = null;
    _clearDiagnostics();
    armed = false;
    connected = false;
    _captionsReady = false;
    _clearCaption();
    // Disconnect releases local tracks before removing the foreground service.
    try {
      await backend.disconnect();
    } finally {
      await service.stop();
    }
  }

  Future<void> disconnect() async {
    final alreadyBusy = busy;
    busy = true;
    ++_epoch;
    _joiningMicrophone = false;
    activeTuning = null;
    _clearDiagnostics();
    armed = false;
    connected = false;
    _captionsReady = false;
    _clearCaption();
    status = 'Voice off';
    _notify();
    try {
      await _release();
    } catch (e) {
      error = 'Could not finish voice cleanup: $e';
    } finally {
      if (!alreadyBusy) busy = false;
    }
    _notify();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    service.onStop = null;
    backend.onState = null;
    backend.onUserTranscript = null;
    backend.onDiagnostics = null;
    unawaited(disconnect());
    super.dispose();
  }
}
