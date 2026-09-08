import 'dart:async';

import 'package:orchestrator/data/app_store.dart';
import 'package:orchestrator/data/control_api.dart';
import 'package:orchestrator/voice/voice_controller.dart';

class MemoryStore implements LocalStore {
  final values = <String, String>{};
  bool fail = false;
  @override
  String? read(String key) => values[key];
  @override
  Future<void> write(String key, String value) async {
    if (fail) throw StateError('disk full');
    values[key] = value;
  }
}

class FakeApi implements ControlApi {
  bool offline = false;
  bool failPost = false;
  bool voiceConfigured = false;
  final histories = <String, List<Json>>{'main': []};
  final sessions = <Json>[];
  final posts = <Json>[];
  final paths = <String>[];
  final cursors = <int>[];
  final stream = StreamController<Json>.broadcast();
  Future<Json> Function(String, Json)? postHandler;
  @override
  Future<Json> get(String path) async {
    if (offline) throw const ControlError('Control server unreachable');
    if (path == '/health') {
      return {'status': 'ok', 'voiceConfigured': voiceConfigured};
    }
    if (path == '/v1/sessions') return {'sessions': sessions};
    if (path.startsWith('/v1/messages')) {
      return {
        'messages':
            histories[Uri.parse(path).queryParameters['conversationId']] ?? [],
      };
    }
    throw const ControlError('Not found');
  }

  @override
  Future<Json> post(String path, Json body) async {
    paths.add(path);
    posts.add(Map.of(body));
    if (postHandler != null) return postHandler!(path, body);
    if (offline || failPost) throw const ControlError('Request not confirmed');
    if (path == '/v1/voice/token') {
      if (!voiceConfigured) {
        throw const ControlError('Voice is not configured on the server');
      }
      return {'url': 'ws://mock', 'token': 'mock', 'room': 'orchestrator-main'};
    }
    return {'operationId': body['id']};
  }

  @override
  Stream<Json> events(int after) {
    cursors.add(after);
    return stream.stream;
  }

  @override
  void close() {}
}

Json message(
  String id,
  String text, {
  String conversation = 'main',
  String role = 'assistant',
}) => {
  'id': id,
  'text': text,
  'conversationId': conversation,
  'sessionId': null,
  'role': role,
  'replyTo': null,
  'createdAt': '2026-07-17T00:00:00Z',
};
Json session() => {
  'id': 'worker-1',
  'name': 'Atlas',
  'agent': 'codex',
  'cwd': '/projects/demo',
  'status': 'working',
  'messaging': 'connected',
  'paneId': null,
  'workspaceId': null,
  'createdAt': '2026-07-17T00:00:00Z',
  'updatedAt': '2026-07-17T00:00:00Z',
};
Json event(int seq, Json data, {String type = 'message.created'}) => {
  'seq': seq,
  'type': type,
  'data': data,
  'createdAt': '2026-07-17T00:00:00Z',
};

class FakeVoice implements VoiceBackend {
  int connections = 0;
  int disconnects = 0;
  final microphoneCalls = <bool>[];
  Completer<void>? connecting;
  @override
  void Function(String)? onState;
  @override
  Future<void> connect(String url, String token) async {
    connections++;
    await connecting?.future;
  }

  @override
  Future<void> microphone(bool enabled) async {
    microphoneCalls.add(enabled);
  }

  @override
  Future<void> disconnect() async {
    disconnects++;
  }
}

class FakeMicrophoneService implements MicrophoneService {
  int starts = 0;
  int stops = 0;
  bool deny = false;
  @override
  void Function()? onStop;
  @override
  Future<void> start() async {
    starts++;
    if (deny) throw StateError('Permission denied');
  }

  @override
  Future<void> stop() async {
    stops++;
  }
}
