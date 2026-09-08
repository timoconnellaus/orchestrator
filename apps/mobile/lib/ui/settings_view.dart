import 'package:flutter/material.dart';

import '../data/app_store.dart';
import '../voice/voice_controller.dart';

class SettingsView extends StatefulWidget {
  const SettingsView({super.key, required this.store, required this.voice});
  final AppStore store;
  final VoiceController voice;
  @override
  State<SettingsView> createState() => _SettingsViewState();
}

class _SettingsViewState extends State<SettingsView> {
  late final TextEditingController _url = TextEditingController(
    text: widget.store.url,
  );
  bool _saving = false;
  String? _error;
  @override
  void dispose() {
    _url.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      await widget.voice.disconnect();
      await widget.store.configure(_url.text);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) => ListView(
    padding: const EdgeInsets.all(20),
    children: [
      Text(
        'YOUR CONTROL SERVER',
        style: Theme.of(context).textTheme.labelLarge
            ?.copyWith(letterSpacing: 1.5, color: const Color(0xff8eddeb)),
      ),
      const SizedBox(height: 12),
      const Text(
        'Private by design',
        style: TextStyle(fontSize: 26, fontWeight: FontWeight.w700),
      ),
      const SizedBox(height: 8),
      const Text(
        'Connect directly to your orchestrator. No account screen, no provider keys on your phone.',
        style: TextStyle(color: Color(0xffaab6cc), height: 1.5),
      ),
      const SizedBox(height: 24),
      TextField(
        controller: _url,
        enabled: !_saving,
        keyboardType: TextInputType.url,
        autocorrect: false,
        decoration: const InputDecoration(
          labelText: 'Control URL',
          helperText: 'Emulator: http://10.0.2.2:8787',
        ),
      ),
      if (_error != null)
        Padding(
          padding: const EdgeInsets.only(top: 10),
          child: Text(
            _error!,
            style: TextStyle(color: Theme.of(context).colorScheme.error),
          ),
        ),
      const SizedBox(height: 16),
      Align(
        alignment: Alignment.centerLeft,
        child: FilledButton.icon(
          onPressed: _saving ? null : _save,
          icon: const Icon(Icons.link),
          label: Text(_saving ? 'Connecting…' : 'Save & reconnect'),
        ),
      ),
      const SizedBox(height: 28),
      const _InfoCard(
        icon: Icons.wifi_tethering,
        title: 'Reachable from your phone',
        text: 'Use an emulator address or your control server’s Tailscale address. Plain HTTP is supported for private networks; never expose this unauthenticated API publicly. Changing server keeps each server’s saved requests separate.',
      ),
      const _InfoCard(
        icon: Icons.mic_none,
        title: 'You control the microphone',
        text: 'Join voice explicitly to grant microphone permission and start an Android foreground notification. Mute releases capture while speaker playback stays on. Disconnect leaves the room. Reopening the app never arms the mic.',
      ),
      const _InfoCard(
        icon: Icons.shield_outlined,
        title: 'Screen-off listening is best effort',
        text: 'Android owns the foreground service, not a widget. The notification’s “Stop & close app” action disarms and closes this app to guarantee microphone release even if Flutter is unresponsive. Android may still kill the process; automatic background re-arming is never attempted.',
      ),
      const _InfoCard(
        icon: Icons.history,
        title: 'Durable, explicit retries',
        text: 'Accepted work is not automatically resubmitted. Unconfirmed requests remain saved with the same ID for explicit retry. Failed or uncertain server operations are shown, not silently repeated. Voice needs LiveKit and server-side speech configuration; typed chat does not.',
      ),
    ],
  );
}

class _InfoCard extends StatelessWidget {
  const _InfoCard({
    required this.icon,
    required this.title,
    required this.text,
  });
  final IconData icon;
  final String title;
  final String text;
  @override
  Widget build(BuildContext context) => Card(
    margin: const EdgeInsets.only(bottom: 12),
    child: Padding(
      padding: const EdgeInsets.all(18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, color: const Color(0xff8eddeb)),
          const SizedBox(height: 12),
          Text(
            title,
            style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 16),
          ),
          const SizedBox(height: 8),
          Text(
            text,
            style: const TextStyle(height: 1.5, color: Color(0xffbac6db)),
          ),
        ],
      ),
    ),
  );
}
