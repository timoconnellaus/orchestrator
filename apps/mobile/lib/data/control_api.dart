import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

typedef Json = Map<String, dynamic>;

abstract class ControlApi {
  Future<Json> get(String path);
  Future<Json> post(String path, Json body);
  Stream<Json> events(int after);
  void close();
}

class HttpControlApi implements ControlApi {
  HttpControlApi(this.base, {http.Client Function()? clientFactory})
    : _factory = clientFactory ?? http.Client.new;
  final String base;
  final http.Client Function() _factory;
  final Set<http.Client> _clients = {};
  bool _closed = false;

  Uri _uri(String path) => Uri.parse('$base$path');
  http.Client _client() {
    if (_closed) throw StateError('Connection closed');
    final client = _factory();
    _clients.add(client);
    return client;
  }

  Future<Json> _request(String method, String path, Json? body) async {
    final client = _client();
    try {
      final response =
          await (method == 'GET'
                  ? client.get(_uri(path))
                  : client.post(
                      _uri(path),
                      headers: {'Content-Type': 'application/json'},
                      body: jsonEncode(body),
                    ))
              .timeout(const Duration(seconds: 15));
      final data = jsonDecode(response.body) as Json;
      if (response.statusCode < 200 || response.statusCode >= 300) {
        throw ControlError(
          data['error']?['message']?.toString() ??
              'HTTP ${response.statusCode}',
        );
      }
      return data;
    } on TimeoutException {
      throw const ControlError(
        'Control request timed out. Retry explicitly; the same ID will be reused.',
      );
    } finally {
      client.close();
      _clients.remove(client);
    }
  }

  @override
  Future<Json> get(String path) => _request('GET', path, null);
  @override
  Future<Json> post(String path, Json body) => _request('POST', path, body);

  @override
  Stream<Json> events(int after) async* {
    final client = _client();
    try {
      final request = http.Request('GET', _uri('/v1/events?after=$after'));
      request.headers['Accept'] = 'text/event-stream';
      final response = await client
          .send(request)
          .timeout(const Duration(seconds: 15));
      if (response.statusCode != 200) {
        throw ControlError('Event stream HTTP ${response.statusCode}');
      }
      var data = <String>[];
      await for (final line
          in response.stream
              .timeout(const Duration(seconds: 45))
              .transform(utf8.decoder)
              .transform(const LineSplitter())) {
        if (line.isEmpty) {
          if (data.isNotEmpty) yield jsonDecode(data.join('\n')) as Json;
          data = [];
        } else if (line.startsWith('data:')) {
          data.add(line.substring(5).trimLeft());
        }
      }
    } finally {
      client.close();
      _clients.remove(client);
    }
  }

  @override
  void close() {
    _closed = true;
    for (final client in _clients.toList()) {
      client.close();
    }
    _clients.clear();
  }
}

class ControlError implements Exception {
  const ControlError(this.message);
  final String message;
  @override
  String toString() => message;
}
