import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:livekit_client/livekit_client.dart';

import '../data/control_api.dart';

abstract class VoiceBackend {
  Future<void> connect(String url, String token);
  Future<void> microphone(bool enabled);
  Future<void> disconnect();
  void Function(String state)? onState;
}

class LiveKitVoice implements VoiceBackend {
  Room? _room;
  EventsListener<RoomEvent>? _listener;
  @override
  void Function(String state)? onState;
  @override
  Future<void> connect(String url, String token) async {
    final room = Room();
    _room = room;
    _listener = room.createListener()
      ..on<RoomReconnectingEvent>((_) => onState?.call('Reconnecting audio'))
      ..on<RoomReconnectedEvent>((_) => onState?.call('Connected'))
      ..on<RoomDisconnectedEvent>((_) => onState?.call('Disconnected'));
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
    await participant?.setMicrophoneEnabled(
      enabled,
      audioCaptureOptions: const AudioCaptureOptions(
        stopAudioCaptureOnMute: true,
      ),
    );
  }

  @override
  Future<void> disconnect() async {
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
    backend.onState = (value) {
      if (value == 'Disconnected') {
        unawaited(disconnect());
      } else if (connected) {
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

  Future<void> join(ControlApi api) async {
    if (busy || connected) return;
    final epoch = ++_epoch;
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
      });
      if (epoch != _epoch) return;
      await backend.connect(
        credentials['url'] as String,
        credentials['token'] as String,
      );
      if (epoch != _epoch) {
        await backend.disconnect();
        return;
      }
      await backend.microphone(true);
      if (epoch != _epoch) {
        await backend.disconnect();
        return;
      }
      connected = true;
      armed = true;
      status = 'Listening · mic armed';
    } catch (e) {
      error = 'Voice unavailable: $e. Text chat still works.';
      await _release();
      status = 'Voice off';
    } finally {
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
    armed = false;
    connected = false;
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
    armed = false;
    connected = false;
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
    unawaited(disconnect());
    super.dispose();
  }
}
