import 'package:flutter/material.dart';

import '../data/app_store.dart';
import '../voice/voice_controller.dart';
import '../voice/voice_tuning.dart';

class VoiceTuningPanel extends StatefulWidget {
  const VoiceTuningPanel({super.key, required this.store, required this.voice});
  final AppStore store;
  final VoiceController voice;
  @override
  State<VoiceTuningPanel> createState() => _VoiceTuningPanelState();
}

class _VoiceTuningPanelState extends State<VoiceTuningPanel> {
  late VoiceTuning _draft = widget.store.savedVoiceTuning;
  bool _saving = false;
  Future<void> _save() async {
    setState(() => _saving = true);
    try {
      await widget.store.saveVoiceTuning(_draft);
    } catch (_) {
      // Store exposes a visible, settings-specific error; draft stays editable.
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Widget _slider(
    String label,
    String key,
    double low,
    double high,
    int divisions,
  ) {
    final value = (_draft.toJson()[key] as num).toDouble();
    final fractional = key == 'activationThreshold' || key == 'elevenLabsSpeed';
    String format(double v) => key == 'elevenLabsSpeed'
        ? '${v.toStringAsFixed(2)}×'
        : fractional
        ? v.toStringAsFixed(2)
        : '${v.round()} ms';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('$label: ${format(value)}'),
        Slider(
          value: value,
          min: low,
          max: high,
          divisions: divisions,
          semanticFormatterCallback: format,
          onChanged: _saving
              ? null
              : (v) => setState(() {
                  _draft = _draft.withValue(
                    key,
                    fractional ? double.parse(v.toStringAsFixed(2)) : v.round(),
                  );
                }),
        ),
      ],
    );
  }

  Widget _switch(String label, String key) => SwitchListTile(
    contentPadding: EdgeInsets.zero,
    title: Text(label),
    value: _draft.toJson()[key] as bool,
    onChanged: _saving
        ? null
        : (v) => setState(() => _draft = _draft.withValue(key, v)),
  );

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: Listenable.merge([widget.store, widget.voice]),
    builder: (context, _) {
      final voice = widget.voice;
      final diagnostics = voice.diagnostics;
      return Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'Voice tuning',
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 8),
              const Text(
                'Edit a draft, then save for your next explicit join. Saving never changes the current microphone or utterance.',
              ),
              const SizedBox(height: 12),
              Wrap(
                spacing: 8,
                children: [
                  for (final preset in {
                    'Default': VoiceTuning.defaults,
                    'Noisy room': VoiceTuning.noisyRoom,
                    'Responsive': VoiceTuning.responsive,
                  }.entries)
                    ChoiceChip(
                      label: Text(preset.key),
                      selected: _draft == preset.value,
                      onSelected: _saving
                          ? null
                          : (_) => setState(() => _draft = preset.value),
                    ),
                ],
              ),
              const SizedBox(height: 16),
              _slider(
                'Activation threshold',
                'activationThreshold',
                .3,
                .8,
                50,
              ),
              _slider('Minimum speech', 'minSpeechMs', 50, 300, 25),
              _slider('Local end silence', 'endSilenceMs', 300, 1200, 90),
              _slider('Interruption duration', 'interruptionMs', 300, 1200, 90),
              const Text(
                'Local silence only tunes Mac speech detection. Transcription and endpointing also affect response time; SDK endpoint delay stays 0.8–3 seconds.',
              ),
              const SizedBox(height: 12),
              _slider(
                'ElevenLabs speaking speed',
                'elevenLabsSpeed',
                .8,
                1.2,
                8,
              ),
              const Text(
                'ElevenLabs only; OpenAI ignores this setting. 1.0× keeps the current voice defaults. Changes speaking rate, not the delay before audio starts.',
              ),
              const SizedBox(height: 12),
              const Text(
                'Phone processing requests (device behavior may vary)',
                style: TextStyle(fontWeight: FontWeight.bold),
              ),
              _switch('Echo cancellation (AEC)', 'echoCancellation'),
              _switch('Noise suppression (NS)', 'noiseSuppression'),
              _switch('Automatic gain control (AGC)', 'autoGainControl'),
              if (!_draft.echoCancellation)
                const Text(
                  'Warning: disabling echo cancellation on speaker can make the assistant hear itself.',
                ),
              Wrap(
                spacing: 8,
                children: [
                  FilledButton(
                    onPressed: _saving ? null : _save,
                    child: Text(_saving ? 'Saving…' : 'Save for next join'),
                  ),
                  TextButton(
                    onPressed: _saving
                        ? null
                        : () => setState(() => _draft = VoiceTuning.defaults),
                    child: const Text('Reset to defaults'),
                  ),
                ],
              ),
              if (widget.store.voiceTuningError != null)
                Text(
                  widget.store.voiceTuningError!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              if (_draft != widget.store.savedVoiceTuning)
                const Text('Unsaved draft'),
              const SizedBox(height: 12),
              Text(
                'Saved for next join: ${widget.store.savedVoiceTuning.summary}',
              ),
              const SizedBox(height: 8),
              Text(
                voice.activeTuning == null
                    ? 'Current join: none'
                    : 'Current join requests (frozen): ${voice.activeTuning!.summary}',
              ),
              if (voice.activeTuning != null &&
                  voice.activeTuning != widget.store.savedVoiceTuning)
                const Text('Saved settings differ from this join.'),
              const SizedBox(height: 8),
              Text(
                voice.confirmedTuning == null ? 'Mac tuning: not confirmed' : 'Mac detection tuning: confirmed for this join. Phone processing is requested, not hardware-verified.',
              ),
              const SizedBox(height: 8),
              Text(
                diagnostics == null || !diagnostics.available
                    ? 'Mac mic / speech: telemetry unavailable'
                    : !diagnostics.fresh
                    ? 'Mac mic / speech: waiting for fresh audio'
                    : 'Mac mic received · ${diagnostics.speech ? 'speech detected' : 'no speech detected'}',
              ),
              if (diagnostics != null &&
                  diagnostics.available &&
                  diagnostics.fresh)
                LinearProgressIndicator(
                  value: diagnostics.level,
                  semanticsLabel: 'Microphone level received by Mac',
                ),
              const Text(
                'Indicators describe received audio and local detection, not an accepted command. Nothing here is saved to chat history.',
              ),
            ],
          ),
        ),
      );
    },
  );
}
