import { randomUUID } from 'node:crypto';
import { AccessToken, RoomAgentDispatch, RoomConfiguration } from 'livekit-server-sdk';
import { defaultVoiceTuning, voiceTuningSchema, type VoiceTuning, type Config } from './config.js';
import { HttpError } from './types.js';
export function voiceConfigured(config: Config): boolean { return Boolean(config.livekitUrl && config.livekitKey && config.livekitSecret); }
export async function voiceToken(config: Config, requested: VoiceTuning = defaultVoiceTuning): Promise<{ url: string; token: string; room: string; speaker: string; voiceTuning: VoiceTuning }> {
  if (!voiceConfigured(config)) throw new HttpError(503, 'voice_not_configured', 'LiveKit URL and credentials are not configured');
  // The durable conversation is main, but a closed speech session cannot be
  // reused. Each explicit join gets its own room/dispatch lifecycle.
  const room = `orchestrator-main-${randomUUID()}`;
  const voiceTuning = voiceTuningSchema.parse(requested);
  const speaker = `phone-${randomUUID()}`;
  const metadata = JSON.stringify({ conversationId: 'main', voiceTuning, room, speaker });
  const token = new AccessToken(config.livekitKey, config.livekitSecret, { identity: speaker, ttl: '1h', metadata });
  token.addGrant({ roomJoin: true, room, canPublish: true, canSubscribe: true, canPublishData: true });
  token.roomConfig = new RoomConfiguration({ agents: [new RoomAgentDispatch({ agentName: 'orchestrator-voice', metadata })] });
  return { url: config.livekitUrl!, token: await token.toJwt(), room, speaker, voiceTuning };
}
