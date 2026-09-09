import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';

import 'control_api.dart';
import '../voice/voice_tuning.dart';

abstract class LocalStore {
  String? read(String key);
  Future<void> write(String key, String value);
}

class PreferencesStore implements LocalStore {
  PreferencesStore(this.preferences);
  final SharedPreferences preferences;
  @override
  String? read(String key) => preferences.getString(key);
  @override
  Future<void> write(String key, String value) async {
    if (!await preferences.setString(key, value)) {
      throw StateError('Could not save locally');
    }
  }
}

class AppStore extends ChangeNotifier {
  AppStore(this.storage, {ControlApi Function(String)? apiFactory})
    : _factory = apiFactory ?? HttpControlApi.new;
  VoiceTuning savedVoiceTuning = VoiceTuning.defaults;
  String? voiceTuningError;
  Future<void> saveVoiceTuning(VoiceTuning tuning) async {
    try {
      await storage.write('voiceTuning', jsonEncode(tuning.toJson()));
      savedVoiceTuning = tuning;
      voiceTuningError = null;
      _notify();
    } catch (e) {
      voiceTuningError = 'Voice settings were not saved: $e';
      _notify();
      rethrow;
    }
  }

  static const defaultUrl = 'http://10.0.2.2:8787';
  final LocalStore storage;
  final ControlApi Function(String) _factory;
  late ControlApi api;
  String url = defaultUrl;
  String connection = 'Connecting';
  String? error;
  bool voiceConfigured = false;
  int cursor = 0;
  int _generation = 0;
  bool _disposed = false;
  Timer? _retry;
  StreamSubscription<Json>? _events;
  Future<void> _writes = Future.value();
  final Map<String, Json> messages = {};
  final Map<String, Json> sessions = {};
  final Map<String, Json> outbox = {};
  final Map<String, Json> operations = {};

  Future<void> initialize() async {
    url = storage.read('controlUrl') ?? defaultUrl;
    try {
      final raw = storage.read('voiceTuning');
      if (raw != null) savedVoiceTuning = VoiceTuning.fromJson(jsonDecode(raw));
    } catch (_) {
      voiceTuningError =
          'Saved voice settings could not be read; using defaults.';
    }
    _restore();
    api = _factory(url);
    await reconnect();
  }

  void _restore() {
    messages.clear();
    sessions.clear();
    outbox.clear();
    operations.clear();
    cursor = 0;
    final raw = storage.read('snapshot:$url');
    if (raw == null) return;
    try {
      final saved = jsonDecode(raw) as Json;
      cursor = saved['cursor'] as int;
      for (final row in (saved['operations'] as List? ?? [])) {
        operations[row['id']] = Map<String, dynamic>.from(row);
      }
      for (final row in saved['messages'] as List) {
        messages[row['id']] = Map<String, dynamic>.from(row);
      }
      for (final row in saved['sessions'] as List) {
        sessions[row['id']] = Map<String, dynamic>.from(row);
      }
      for (final row in saved['outbox'] as List) {
        final item = Map<String, dynamic>.from(row);
        if (item['state'] == 'sending') {
          item['state'] = 'retry';
          item['error'] = 'Interrupted. Retry with the same ID.';
        }
        outbox[item['id']] = item;
      }
    } catch (_) {
      cursor = 0;
      messages.clear();
      sessions.clear();
      outbox.clear();
      error = 'Local history could not be read; replaying server history.';
    }
  }

  Future<void> _persist() {
    // Cursor and materialized data are one atomic preference value. Never save a
    // cursor separately: otherwise process death could permanently skip events.
    final key = 'snapshot:$url';
    final value = jsonEncode({
      'cursor': cursor,
      'messages': messages.values.toList(),
      'sessions': sessions.values.toList(),
      'outbox': outbox.values.toList(),
      'operations': operations.values.toList(),
    });
    final next = _writes
        .catchError((Object _) {})
        .then((_) => storage.write(key, value));
    _writes = next;
    return next;
  }

  Future<void> configure(String value) async {
    final uri = Uri.tryParse(value.trim());
    if (uri == null ||
        !['http', 'https'].contains(uri.scheme) ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        (uri.path.isNotEmpty && uri.path != '/')) {
      throw const ControlError(
        'Use an http(s) server address, without a path or credentials.',
      );
    }
    await storage.write(
      'controlUrl',
      value.trim().replaceFirst(RegExp(r'/$'), ''),
    );
    _generation++;
    _retry?.cancel();
    await _events?.cancel();
    api.close();
    url = value.trim().replaceFirst(RegExp(r'/$'), '');
    _restore();
    api = _factory(url);
    await reconnect();
  }

  Future<void> reconnect() async {
    final generation = ++_generation;
    _retry?.cancel();
    await _events?.cancel();
    if (!_current(generation)) return;
    api.close();
    for (final item in outbox.values) {
      if (item['state'] == 'sending') {
        item['state'] = 'retry';
        item['error'] = 'Connection interrupted. Retry with the same ID.';
      }
    }
    api = _factory(url);
    final activeApi = api;
    connection = 'Connecting';
    error = null;
    _notify();
    try {
      final health = await activeApi.get('/health');
      final listing = await activeApi.get('/v1/sessions');
      final history = await activeApi.get('/v1/messages?conversationId=main');
      if (!_current(generation)) return;
      voiceConfigured = health['voiceConfigured'] == true;
      for (final row in listing['sessions'] as List) {
        sessions[row['id']] = Map<String, dynamic>.from(row);
      }
      _mergeMessages(history);
      await _persist();
      if (!_current(generation)) return;
      connection = 'Live';
      _notify();
      // History comes first, then durable journal replay from the saved cursor.
      // IDs deduplicate their overlap; no 'current time' cursor can skip a gap.
      _events = activeApi
          .events(cursor)
          .listen(
            (event) async {
              if (!_current(generation)) return;
              try {
                await applyEvent(event);
              } catch (e) {
                if (_current(generation)) _offline(e, generation);
              }
            },
            onError: (Object e) => _offline(e, generation),
            onDone: () => _offline('Event stream closed', generation),
          );
      for (final item in outbox.values.toList()) {
        if (item['state'] == 'accepted' && item['operationId'] != null) {
          try {
            final result = await activeApi.get(
              '/v1/operations/${Uri.encodeComponent(item['operationId'] as String)}',
            );
            if (!_current(generation)) return;
            _operation(result['operation'] as Json);
            await _persist();
            _notify();
          } catch (_) {
            /* Journal replay remains authoritative. */
          }
        }
      }
    } catch (e) {
      _offline(e, generation);
    }
  }

  bool _current(int generation) => !_disposed && generation == _generation;
  void _offline(Object e, int generation) {
    if (!_current(generation)) return;
    connection = 'Offline';
    error = e.toString();
    _notify();
    _retry?.cancel();
    _retry = Timer(const Duration(seconds: 4), reconnect);
  }

  void _mergeMessages(Json data) {
    for (final row in data['messages'] as List) {
      messages[row['id']] = Map<String, dynamic>.from(row);
    }
  }

  Future<void> loadThread(String conversationId) async {
    final generation = _generation;
    try {
      final result = await api.get(
        '/v1/messages?conversationId=${Uri.encodeQueryComponent(conversationId)}',
      );
      if (!_current(generation)) return;
      _mergeMessages(result);
      await _persist();
      _notify();
    } catch (e) {
      error = e.toString();
      _notify();
    }
  }

  Future<void> applyEvent(Json event) async {
    final seq = event['seq'] as int;
    if (seq <= cursor) return;
    final data = event['data'] as Json;
    switch (event['type']) {
      case 'message.created':
        messages[data['id']] = data;
      case 'session.updated':
        sessions[data['id']] = data;
      case 'operation.updated':
        _operation(data);
    }
    cursor = seq;
    await _persist();
    _notify();
  }

  void _operation(Json operation) {
    final previous = operations[operation['id']];
    const terminal = {'succeeded', 'failed', 'uncertain'};
    if (previous != null &&
        terminal.contains(previous['status']) &&
        !terminal.contains(operation['status'])) {
      return;
    }
    if (previous != null &&
        (previous['updatedAt'] as String).compareTo(
              operation['updatedAt'] as String,
            ) >
            0) {
      return;
    }
    operations[operation['id']] = operation;
    for (final item in outbox.values) {
      if (item['operationId'] == operation['id'] ||
          item['id'] == operation['id']) {
        item['operationId'] = operation['id'];
        item['state'] = switch (operation['status']) {
          'failed' || 'uncertain' => 'terminal',
          'succeeded' => 'done',
          _ => 'accepted',
        };
        item['status'] = operation['status'];
        item['error'] = operation['error'];
      }
    }
  }

  List<Json> thread(String id) =>
      messages.values.where((m) => m['conversationId'] == id).toList()..sort(
        (a, b) =>
            (a['createdAt'] as String).compareTo(b['createdAt'] as String),
      );

  Future<void> send(String text, {String? sessionId}) async {
    await _enqueue(
      sessionId == null
          ? '/v1/chat'
          : '/v1/sessions/${Uri.encodeComponent(sessionId)}/messages',
      {
        'text': text,
        if (sessionId == null) ...{'conversationId': 'main', 'source': 'text'},
      },
      sessionId == null ? 'main' : 'session:$sessionId',
      text,
    );
  }

  Future<void> createWorker(Json fields) =>
      _enqueue('/v1/sessions', fields, 'workers', 'Create ${fields['name']}');

  Future<void> _enqueue(
    String path,
    Json payload,
    String conversationId,
    String label,
  ) async {
    final id = const Uuid().v4();
    outbox[id] = {
      'id': id,
      'path': path,
      'body': {'id': id, ...payload},
      'conversationId': conversationId,
      'label': label,
      'state': 'retry',
    };
    // Do not transmit if durable local persistence fails. Composer stays intact.
    await _persist();
    _notify();
    await retry(id);
  }

  Future<void> retry(String id) async {
    final item = outbox[id];
    if (item == null || item['state'] != 'retry') return;
    final generation = _generation;
    item['state'] = 'sending';
    item['error'] = null;
    _notify();
    try {
      await _persist();
      if (!_current(generation)) return;
      final response = await api.post(
        item['path'] as String,
        Map<String, dynamic>.from(item['body'] as Map),
      );
      if (!_current(generation)) return;
      item['operationId'] = response['operationId'];
      if (item['state'] == 'sending') item['state'] = 'accepted';
      final operation = operations[item['operationId']];
      if (operation != null) _operation(operation);
    } catch (e) {
      if (!_current(generation)) return;
      // Journal delivery can beat a lost POST acknowledgement. A transport error
      // must not regress an operation the server has already accepted/completed.
      final known = operations[item['operationId'] ?? item['id']];
      if (known != null) {
        _operation(known);
      } else if (item['state'] == 'sending') {
        item['state'] = 'retry';
        item['error'] = e.toString();
      }
    }
    if (_current(generation)) {
      await _persist();
      _notify();
    }
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _generation++;
    _retry?.cancel();
    _events?.cancel();
    api.close();
    super.dispose();
  }
}
