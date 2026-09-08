import { randomUUID } from 'node:crypto';
import { AccessToken, RoomAgentDispatch, RoomConfiguration } from 'livekit-server-sdk';
import type { Config } from './config.js';
import { HttpError } from './types.js';
export function voiceConfigured(config: Config): boolean { return Boolean(config.livekitUrl && config.livekitKey && config.livekitSecret); }
export async function voiceToken(config: Config): Promise<{ url: string; token: string; room: string }> {
  if (!voiceConfigured(config)) throw new HttpError(503, 'voice_not_configured', 'LiveKit URL and credentials are not configured');
  const room = 'orchestrator-main';
  const token = new AccessToken(config.livekitKey, config.livekitSecret, { identity: `phone-${randomUUID()}`, ttl: '1h', metadata: JSON.stringify({ conversationId: 'main' }) });
  token.addGrant({ roomJoin: true, room, canPublish: true, canSubscribe: true, canPublishData: true });
  token.roomConfig = new RoomConfiguration({ agents: [new RoomAgentDispatch({ agentName: 'orchestrator-voice', metadata: JSON.stringify({ conversationId: 'main' }) })] });
  return { url: config.livekitUrl!, token: await token.toJwt(), room };
}
