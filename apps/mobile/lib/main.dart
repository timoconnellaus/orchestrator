import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'data/app_store.dart';
import 'ui/orchestrator_app.dart';
import 'voice/voice_controller.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final store = AppStore(
    PreferencesStore(await SharedPreferences.getInstance()),
  );
  final voice = VoiceController(LiveKitVoice(), AndroidMicrophoneService());
  runApp(OrchestratorApp(store: store, voice: voice));
  await store.initialize();
}
