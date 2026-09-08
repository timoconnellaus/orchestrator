import 'package:flutter/material.dart';

import '../data/app_store.dart';
import '../voice/voice_controller.dart';
import 'chat_view.dart';
import 'sessions_view.dart';
import 'settings_view.dart';

class OrchestratorApp extends StatelessWidget {
  const OrchestratorApp({super.key, required this.store, required this.voice});
  final AppStore store;
  final VoiceController voice;
  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'Orchestrator',
    debugShowCheckedModeBanner: false,
    // Keep message geometry stable at list edges instead of Android's stretch
    // effect, which can look like warped text when the conversation already fits.
    scrollBehavior: const MaterialScrollBehavior().copyWith(overscroll: false),
    theme: ThemeData(
      brightness: Brightness.dark,
      useMaterial3: true,
      colorScheme:
          ColorScheme.fromSeed(
            seedColor: const Color(0xff7489f5),
            brightness: Brightness.dark,
          ).copyWith(
            primary: const Color(0xff8eddeb),
            surface: const Color(0xff111b2e),
          ),
      scaffoldBackgroundColor: const Color(0xff10182a),
      appBarTheme: const AppBarTheme(
        backgroundColor: Color(0xff10182a),
        centerTitle: false,
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: const Color(0xff1b263b),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: BorderSide.none,
        ),
      ),
      cardTheme: CardThemeData(
        color: const Color(0xff1b263b),
        elevation: 0,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      ),
    ),
    home: HomeScreen(store: store, voice: voice),
  );
}

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key, required this.store, required this.voice});
  final AppStore store;
  final VoiceController voice;
  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  int _tab = 0;
  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: Listenable.merge([widget.store, widget.voice]),
    builder: (context, _) {
      final store = widget.store;
      return Scaffold(
        appBar: AppBar(
          title: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'ORCHESTRATOR',
                style: TextStyle(
                  fontSize: 12,
                  letterSpacing: 2.4,
                  color: Color(0xff8eddeb),
                ),
              ),
              Text(
                [
                  'Your conversation',
                  'Worker sessions',
                  'Control center',
                ][_tab],
                style: const TextStyle(
                  fontWeight: FontWeight.w700,
                  fontSize: 24,
                ),
              ),
            ],
          ),
          toolbarHeight: 80,
          actions: [
            IconButton(
              onPressed: store.reconnect,
              tooltip: 'Reconnect control',
              icon: const Icon(Icons.sync),
            ),
            const SizedBox(width: 8),
          ],
        ),
        body: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 900),
            child: Column(
              children: [
                Padding(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 20,
                    vertical: 8,
                  ),
                  child: Row(
                    children: [
                      Icon(
                        Icons.circle,
                        size: 8,
                        color: store.connection == 'Live'
                            ? const Color(0xff8eddeb)
                            : Colors.amber,
                      ),
                      const SizedBox(width: 8),
                      Text(
                        store.connection,
                        style: const TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text(
                          store.url,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Color(0xffaab6cc),
                            fontSize: 12,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
                if (store.error != null)
                  Container(
                    width: double.infinity,
                    margin: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                    padding: const EdgeInsets.all(12),
                    decoration: BoxDecoration(
                      color: const Color(0xff392b31),
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: Text(
                      '${store.error}\nText requests stay saved. Reconnecting automatically.',
                      maxLines: 4,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontSize: 12),
                    ),
                  ),
                Expanded(
                  child: IndexedStack(
                    index: _tab,
                    children: [
                      Column(
                        children: [
                          Expanded(child: ChatView(store: store)),
                          VoiceBar(store: store, voice: widget.voice),
                        ],
                      ),
                      SessionsView(store: store),
                      SettingsView(store: store, voice: widget.voice),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
        bottomNavigationBar: NavigationBar(
          selectedIndex: _tab,
          onDestinationSelected: (value) => setState(() => _tab = value),
          destinations: const [
            NavigationDestination(
              icon: Icon(Icons.chat_bubble_outline),
              selectedIcon: Icon(Icons.chat_bubble),
              label: 'Chat',
            ),
            NavigationDestination(
              icon: Icon(Icons.view_agenda_outlined),
              selectedIcon: Icon(Icons.view_agenda),
              label: 'Sessions',
            ),
            NavigationDestination(icon: Icon(Icons.tune), label: 'Settings'),
          ],
        ),
      );
    },
  );
}

class VoiceBar extends StatelessWidget {
  const VoiceBar({super.key, required this.store, required this.voice});
  final AppStore store;
  final VoiceController voice;
  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Icon(
              voice.armed ? Icons.mic : Icons.mic_off_outlined,
              color: voice.armed
                  ? const Color(0xff8eddeb)
                  : const Color(0xffaab6cc),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Text(voice.status, style: const TextStyle(fontSize: 12)),
            ),
            if (!voice.connected && !voice.busy)
              TextButton(
                onPressed: () => voice.join(store.api),
                child: const Text('Join voice'),
              ),
            if (voice.connected)
              TextButton(
                onPressed: voice.busy ? null : voice.toggleMute,
                child: Text(voice.armed ? 'Mute' : 'Arm mic'),
              ),
            if (voice.connected || voice.busy)
              IconButton(
                onPressed: voice.disconnect,
                tooltip: 'Disconnect voice',
                icon: const Icon(Icons.close),
              ),
          ],
        ),
        if (voice.error != null)
          Text(
            voice.error!,
            style: TextStyle(
              fontSize: 12,
              color: Theme.of(context).colorScheme.error,
            ),
          ),
        if (!store.voiceConfigured && voice.error == null)
          const Text(
            'Voice needs server configuration. Text is always available.',
            style: TextStyle(fontSize: 11, color: Color(0xffaab6cc)),
          ),
      ],
    ),
  );
}
