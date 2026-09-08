import 'package:flutter/material.dart';

import '../data/app_store.dart';
import '../data/control_api.dart';

class ChatView extends StatefulWidget {
  const ChatView({super.key, required this.store, this.sessionId});
  final AppStore store;
  final String? sessionId;
  @override
  State<ChatView> createState() => _ChatViewState();
}

class _ChatViewState extends State<ChatView> {
  final _text = TextEditingController();
  bool _sending = false;
  String get _conversation =>
      widget.sessionId == null ? 'main' : 'session:${widget.sessionId}';
  @override
  void initState() {
    super.initState();
    if (widget.sessionId != null) widget.store.loadThread(_conversation);
  }

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  Future<void> _send() async {
    final text = _text.text.trim();
    if (text.isEmpty || _sending) return;
    setState(() => _sending = true);
    try {
      await widget.store.send(text, sessionId: widget.sessionId);
      if (mounted) _text.clear();
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('Not saved: $e')));
      }
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.store,
    builder: (context, _) {
      final rows = widget.store.thread(_conversation);
      final pending = widget.store.outbox.values
          .where(
            (item) =>
                item['conversationId'] == _conversation &&
                item['state'] != 'done',
          )
          .toList();
      return Column(
        children: [
          Expanded(
            child: rows.isEmpty && pending.isEmpty
                ? Center(
                    child: SingleChildScrollView(
                      child: Padding(
                        padding: const EdgeInsets.all(32),
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(
                              widget.sessionId == null
                                  ? Icons.auto_awesome_outlined
                                  : Icons.terminal,
                              size: 42,
                              color: Theme.of(context).colorScheme.primary,
                            ),
                            const SizedBox(height: 20),
                            Text(
                              widget.sessionId == null
                                  ? 'A calm place to orchestrate.'
                                  : 'Talk to this worker',
                              style: Theme.of(context).textTheme.titleLarge,
                              textAlign: TextAlign.center,
                            ),
                            const SizedBox(height: 10),
                            Text(
                              widget.sessionId == null
                                  ? 'Plan a task, check progress, or start a worker.\nYour conversation stays here.'
                                  : 'Messages queue safely until the worker is ready.',
                              textAlign: TextAlign.center,
                              style: const TextStyle(
                                color: Color(0xffaab6cc),
                                height: 1.5,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  )
                : ListView.builder(
                    reverse: true,
                    padding: const EdgeInsets.fromLTRB(16, 20, 16, 12),
                    itemCount: rows.length + pending.length,
                    itemBuilder: (context, reverseIndex) {
                      final index =
                          rows.length + pending.length - reverseIndex - 1;
                      if (index >= rows.length) {
                        return OutboxCard(
                          item: pending[index - rows.length],
                          store: widget.store,
                        );
                      }
                      return MessageBubble(message: rows[index]);
                    },
                  ),
          ),
          Container(
            padding: const EdgeInsets.fromLTRB(16, 10, 16, 12),
            decoration: const BoxDecoration(
              border: Border(top: BorderSide(color: Color(0xff263149))),
            ),
            child: SafeArea(
              top: false,
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Expanded(
                    child: TextField(
                      controller: _text,
                      minLines: 1,
                      maxLines: 5,
                      enabled: !_sending,
                      textCapitalization: TextCapitalization.sentences,
                      decoration: InputDecoration(
                        hintText: widget.sessionId == null
                            ? 'Message your orchestrator…'
                            : 'Message worker…',
                        labelText: 'Message',
                      ),
                    ),
                  ),
                  const SizedBox(width: 10),
                  IconButton.filled(
                    onPressed: _sending ? null : _send,
                    tooltip: 'Send message',
                    icon: _sending
                        ? const SizedBox(
                            width: 20,
                            height: 20,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.arrow_upward),
                  ),
                ],
              ),
            ),
          ),
        ],
      );
    },
  );
}

class MessageBubble extends StatelessWidget {
  const MessageBubble({super.key, required this.message});
  final Json message;
  @override
  Widget build(BuildContext context) {
    final user = message['role'] == 'user';
    return Align(
      alignment: user ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        constraints: BoxConstraints(
          maxWidth: MediaQuery.sizeOf(context).width.clamp(0, 800) * .85,
        ),
        margin: const EdgeInsets.only(bottom: 14),
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: user ? const Color(0xff303b71) : const Color(0xff1b263b),
          borderRadius: BorderRadius.circular(18),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              user ? 'YOU' : (message['role'] as String).toUpperCase(),
              style: const TextStyle(
                fontSize: 10,
                letterSpacing: 1.3,
                color: Color(0xff8eddeb),
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 7),
            SelectableText(
              message['text'] as String,
              style: const TextStyle(fontSize: 16, height: 1.5),
            ),
          ],
        ),
      ),
    );
  }
}

class OutboxCard extends StatelessWidget {
  const OutboxCard({super.key, required this.item, required this.store});
  final Json item;
  final AppStore store;
  @override
  Widget build(BuildContext context) => Card(
    child: Padding(
      padding: const EdgeInsets.all(14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            item['label'] as String,
            style: const TextStyle(fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 6),
          Text(switch (item['state']) {
            'sending' => 'Sending · saved on this phone',
            'retry' => 'Not confirmed · saved on this phone',
            'terminal' =>
              'Operation ${item['status']} · not automatically retried',
            _ =>
              'Accepted · ${item['status'] ?? 'queued'} (not proof of completion)',
          }, style: const TextStyle(fontSize: 12, color: Color(0xff8eddeb))),
          if (item['error'] != null)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                item['error'].toString(),
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
            ),
          if (item['state'] == 'retry')
            TextButton.icon(
              onPressed: () async {
                try {
                  await store.retry(item['id'] as String);
                } catch (e) {
                  if (context.mounted) {
                    ScaffoldMessenger.of(context)
                        .showSnackBar(SnackBar(content: Text('$e')));
                  }
                }
              },
              icon: const Icon(Icons.refresh),
              label: const Text('Retry same request'),
            ),
        ],
      ),
    ),
  );
}
