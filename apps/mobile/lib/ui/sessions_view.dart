import 'package:flutter/material.dart';

import '../data/app_store.dart';
import 'chat_view.dart';

class SessionsView extends StatelessWidget {
  const SessionsView({super.key, required this.store});
  final AppStore store;
  @override
  Widget build(BuildContext context) {
    final sessions = store.sessions.values.toList()
      ..sort(
        (a, b) =>
            (b['updatedAt'] as String).compareTo(a['updatedAt'] as String),
      );
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Row(
          children: [
            Expanded(
              child: Text(
                '${sessions.length} workers',
                style: Theme.of(context).textTheme.titleMedium,
              ),
            ),
            FilledButton.icon(
              onPressed: () => showDialog<void>(
                context: context,
                builder: (_) => NewWorkerDialog(store: store),
              ),
              icon: const Icon(Icons.add),
              label: const Text('New worker'),
            ),
          ],
        ),
        const SizedBox(height: 16),
        if (sessions.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 48, horizontal: 20),
            child: Column(
              children: [
                Icon(Icons.hub_outlined, size: 42, color: Color(0xff8eddeb)),
                SizedBox(height: 16),
                Text(
                  'Give your next task a home.',
                  style: TextStyle(fontSize: 20),
                ),
                SizedBox(height: 8),
                Text(
                  'Launch Pi, Codex, or Claude in a dedicated worker session.',
                  textAlign: TextAlign.center,
                ),
              ],
            ),
          ),
        for (final session in sessions)
          Card(
            margin: const EdgeInsets.only(bottom: 12),
            child: ListTile(
              contentPadding: const EdgeInsets.all(16),
              leading: const CircleAvatar(child: Icon(Icons.terminal)),
              title: Text(
                session['name'] as String,
                style: const TextStyle(fontWeight: FontWeight.w700),
              ),
              subtitle: Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '${session['agent']}  ·  ${session['status']}  ·  ${session['messaging']}',
                      style: const TextStyle(color: Color(0xff8eddeb)),
                    ),
                    const SizedBox(height: 6),
                    Text(
                      session['cwd'] as String,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ],
                ),
              ),
              trailing: const Icon(Icons.chevron_right),
              onTap: () => Navigator.of(context).push(
                MaterialPageRoute<void>(
                  builder: (_) => SessionThreadScreen(
                    store: store,
                    sessionId: session['id'] as String,
                  ),
                ),
              ),
            ),
          ),
        for (final item in store.outbox.values.where(
          (item) =>
              item['conversationId'] == 'workers' && item['state'] != 'done',
        ))
          OutboxCard(item: item, store: store),
      ],
    );
  }
}

class SessionThreadScreen extends StatelessWidget {
  const SessionThreadScreen({
    super.key,
    required this.store,
    required this.sessionId,
  });
  final AppStore store;
  final String sessionId;
  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: store,
    builder: (context, _) {
      final session = store.sessions[sessionId];
      return Scaffold(
        appBar: AppBar(
          title: Text(session?['name'] as String? ?? 'Worker thread'),
          actions: [
            IconButton(
              onPressed: store.reconnect,
              tooltip: 'Reconnect control',
              icon: const Icon(Icons.sync),
            ),
          ],
        ),
        body: Column(
          children: [
            Padding(
              padding: const EdgeInsets.all(12),
              child: Text(
                '${store.connection} · ${session?['agent']} · ${session?['status']} · ${session?['messaging']}',
              ),
            ),
            if (store.error != null)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: Text(
                  store.error!,
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ),
            Expanded(
              child: ChatView(store: store, sessionId: sessionId),
            ),
          ],
        ),
      );
    },
  );
}

class NewWorkerDialog extends StatefulWidget {
  const NewWorkerDialog({super.key, required this.store});
  final AppStore store;
  @override
  State<NewWorkerDialog> createState() => _NewWorkerDialogState();
}

class _NewWorkerDialogState extends State<NewWorkerDialog> {
  final _form = GlobalKey<FormState>();
  final _name = TextEditingController();
  final _cwd = TextEditingController();
  final _instructions = TextEditingController();
  String _agent = 'codex';
  bool _saving = false;
  String? _error;
  @override
  void dispose() {
    _name.dispose();
    _cwd.dispose();
    _instructions.dispose();
    super.dispose();
  }

  Future<void> _create() async {
    if (!_form.currentState!.validate()) return;
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      await widget.store.createWorker({
        'agent': _agent,
        'name': _name.text.trim(),
        'cwd': _cwd.text.trim(),
        'instructions': _instructions.text.trim(),
      });
      if (mounted) Navigator.pop(context);
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = '$e';
          _saving = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: const Text('Start a worker'),
    content: SizedBox(
      width: 440,
      child: SingleChildScrollView(
        child: Form(
          key: _form,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Choose an agent and a server-side project. Work is queued durably; a request is not proof of launch.',
              ),
              const SizedBox(height: 20),
              DropdownButtonFormField<String>(
                initialValue: _agent,
                decoration: const InputDecoration(labelText: 'Agent'),
                items: const [
                  DropdownMenuItem(value: 'pi', child: Text('Pi')),
                  DropdownMenuItem(value: 'codex', child: Text('Codex')),
                  DropdownMenuItem(value: 'claude', child: Text('Claude')),
                ],
                onChanged: _saving
                    ? null
                    : (value) => setState(() => _agent = value!),
              ),
              const SizedBox(height: 16),
              TextFormField(
                controller: _name,
                enabled: !_saving,
                decoration: const InputDecoration(labelText: 'Worker name'),
                validator: _required,
              ),
              const SizedBox(height: 16),
              TextFormField(
                controller: _cwd,
                enabled: !_saving,
                decoration: const InputDecoration(
                  labelText: 'Project directory',
                  hintText: '/path/on/control/server',
                ),
                validator: _required,
              ),
              const SizedBox(height: 16),
              TextFormField(
                controller: _instructions,
                enabled: !_saving,
                minLines: 3,
                maxLines: 6,
                decoration: const InputDecoration(labelText: 'Instructions'),
                validator: _required,
              ),
              if (_error != null)
                Text(
                  _error!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
            ],
          ),
        ),
      ),
    ),
    actions: [
      TextButton(
        onPressed: _saving ? null : () => Navigator.pop(context),
        child: const Text('Cancel'),
      ),
      FilledButton(
        onPressed: _saving ? null : _create,
        child: Text(_saving ? 'Saving…' : 'Create worker'),
      ),
    ],
  );
  String? _required(String? value) =>
      value == null || value.trim().isEmpty ? 'Required' : null;
}
