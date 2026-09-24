// Show-control protocol between the model and the drone swarm.
//
// The model ends its reply with one or more tags such as
//   [[show:{"shape":"heart","color":"#ff3366"}]]
// which the page strips from the caption and plays on the swarm. A local
// keyword parser (intentFromUserText) reacts instantly to the user's own
// words, so the drones move even before — or without — a model tag.

export const SHAPES = [
  'jarvis',
  'sphere',
  'heart',
  'star',
  'planet',
  'galaxy',
  'dna',
  'cube',
  'torus',
  'wave',
  'text',
  'emoji',
  'clock',
  'fireworks',
] as const;

export type ShapeName = (typeof SHAPES)[number];

export interface ShowAction {
  shape: ShapeName;
  /** For shape "text": the word(s) the drones spell. */
  text?: string;
  /** For shape "emoji": a single emoji to draw in its own colors. */
  emoji?: string;
  /** Hex color, "rainbow", or omitted for the shape's own palette. */
  color?: string;
  /** Rotation speed in rad/s (0 keeps the shape facing the audience). */
  spin?: number;
}

export const SHOW_SYSTEM_PROMPT = `Você é o JARVIS e controla um show de drones de luz 3D ultra-realista no céu noturno, visto pelo usuário em tempo real.
Responda de forma breve (no máximo 2 frases curtas), pois a resposta é falada em voz alta.
SEMPRE termine a resposta com uma linha de comando para os drones, exatamente neste formato:
[[show:{"shape":"<forma>","color":"<cor>"}]]
Formas disponíveis: ${SHAPES.join(', ')}.
- "text": inclua "text" com até 12 caracteres (ex.: {"shape":"text","text":"OLÁ"}).
- "emoji": inclua "emoji" com um único emoji para desenhar qualquer objeto, animal ou símbolo (ex.: {"shape":"emoji","emoji":"🚀"}).
- "clock": mostra a hora atual. "fireworks": fogos de artifício. "jarvis": o logotipo em anéis (padrão).
- "color": hex como "#ff3366", ou "rainbow"; omita para a cor natural da forma.
- "spin" (opcional): velocidade de rotação, ex.: 0.4.
Escolha a forma que melhor ilustra o pedido ou a resposta. Você pode encadear até 3 comandos para uma sequência.
Nunca explique o comando nem use outro formato.`;

const SHAPE_ALIASES: Record<string, ShapeName> = {
  logo: 'jarvis',
  rings: 'jarvis',
  aneis: 'jarvis',
  ball: 'sphere',
  bola: 'sphere',
  esfera: 'sphere',
  globo: 'sphere',
  coracao: 'heart',
  love: 'heart',
  estrela: 'star',
  planeta: 'planet',
  saturno: 'planet',
  saturn: 'planet',
  galaxia: 'galaxy',
  espiral: 'galaxy',
  spiral: 'galaxy',
  helix: 'dna',
  cubo: 'cube',
  box: 'cube',
  toro: 'torus',
  donut: 'torus',
  rosquinha: 'torus',
  onda: 'wave',
  ondas: 'wave',
  bandeira: 'wave',
  flag: 'wave',
  texto: 'text',
  palavra: 'text',
  word: 'text',
  relogio: 'clock',
  hora: 'clock',
  time: 'clock',
  fogos: 'fireworks',
  firework: 'fireworks',
};

export const COLOR_NAMES: Record<string, string> = {
  vermelho: '#ff2a3d',
  red: '#ff2a3d',
  azul: '#2f7bff',
  blue: '#2f7bff',
  verde: '#22e07a',
  green: '#22e07a',
  amarelo: '#ffd23f',
  yellow: '#ffd23f',
  laranja: '#ff8a1f',
  orange: '#ff8a1f',
  roxo: '#9b5cff',
  purple: '#9b5cff',
  violeta: '#9b5cff',
  rosa: '#ff5fb7',
  pink: '#ff5fb7',
  branco: '#f4f7ff',
  white: '#f4f7ff',
  dourado: '#ffc247',
  ouro: '#ffc247',
  gold: '#ffc247',
  ciano: '#22e5ff',
  cyan: '#22e5ff',
  prata: '#c9d3e6',
  silver: '#c9d3e6',
  arcoiris: 'rainbow',
  'arco-iris': 'rainbow',
  rainbow: 'rainbow',
  colorido: 'rainbow',
};

/** Lowercase and strip accents so "Coração" matches "coracao". */
export function normalize(word: string): string {
  return word
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .trim();
}

export function resolveShape(name: unknown): ShapeName | null {
  if (typeof name !== 'string') return null;
  const key = normalize(name);
  if ((SHAPES as readonly string[]).includes(key)) return key as ShapeName;
  return SHAPE_ALIASES[key] ?? null;
}

export function resolveColor(name: unknown): string | undefined {
  if (typeof name !== 'string' || !name.trim()) return undefined;
  const raw = name.trim();
  if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(raw)) return raw.toLowerCase();
  return COLOR_NAMES[normalize(raw)];
}

function sanitizeAction(value: unknown): ShowAction | null {
  if (typeof value === 'string') {
    const shape = resolveShape(value);
    return shape ? { shape } : null;
  }
  if (!value || typeof value !== 'object') return null;
  const obj = value as Record<string, unknown>;
  const shape = resolveShape(obj.shape);
  if (!shape) return null;
  const action: ShowAction = { shape };
  if (shape === 'text') {
    const text = typeof obj.text === 'string' ? obj.text.trim() : '';
    if (!text) return null;
    action.text = text.slice(0, 24);
  }
  if (shape === 'emoji') {
    const emoji = typeof obj.emoji === 'string' ? obj.emoji.trim() : '';
    if (!emoji) return null;
    action.emoji = Array.from(emoji).slice(0, 4).join('');
  }
  const color = resolveColor(obj.color);
  if (color) action.color = color;
  if (typeof obj.spin === 'number' && Number.isFinite(obj.spin)) {
    action.spin = Math.max(-3, Math.min(3, obj.spin));
  }
  return action;
}

const TAG_RE = /\[\[\s*show\s*:\s*([\s\S]*?)\]\]/gi;

/** Pull every [[show:...]] tag out of a finished reply. */
export function extractActions(reply: string): { text: string; actions: ShowAction[] } {
  const actions: ShowAction[] = [];
  const text = reply.replace(TAG_RE, (_match, body: string) => {
    const payload = body.trim();
    let parsed: unknown = payload;
    if (payload.startsWith('{')) {
      try {
        parsed = JSON.parse(payload);
      } catch {
        parsed = null;
      }
    }
    const action = sanitizeAction(parsed);
    if (action) actions.push(action);
    return '';
  });
  return { text: text.replace(/\n{3,}/g, '\n\n').trim(), actions };
}

/** Caption text while streaming: hide tags, including a half-received one. */
export function stripForDisplay(partial: string): string {
  const withoutTags = partial.replace(TAG_RE, '');
  const open = withoutTags.lastIndexOf('[[');
  return (open >= 0 ? withoutTags.slice(0, open) : withoutTags).trim();
}

const EMOJI_RE = /\p{Extended_Pictographic}(?:️|‍\p{Extended_Pictographic})*/u;

/** Instant local reaction to the user's words (Portuguese and English). */
export function intentFromUserText(input: string): ShowAction | null {
  const text = input.trim();
  if (!text) return null;

  const color = Object.keys(COLOR_NAMES)
    .filter((name) => new RegExp(`\\b${name}\\b`).test(normalize(text)))
    .map((name) => COLOR_NAMES[name])[0];
  const withColor = (action: ShowAction): ShowAction =>
    color ? { ...action, color } : action;

  const write = text.match(
    /(?:escrev[ae]|escrever|soletr[ae]|mostr[ae] (?:o nome|a palavra)|write|spell)\s*[:\-]?\s*["“']?([^"”'\n]{1,24}?)["”']?\s*(?:em\s+\S+|in\s+\S+)?\s*[.!?]?$/i,
  );
  if (write?.[1]?.trim()) {
    return withColor({ shape: 'text', text: write[1].trim().toUpperCase() });
  }

  const emoji = text.match(EMOJI_RE);
  if (emoji) return withColor({ shape: 'emoji', emoji: emoji[0] });

  const words = normalize(text).split(/[^a-z0-9-]+/).filter(Boolean);
  for (const word of words) {
    const shape = resolveShape(word);
    if (shape && shape !== 'text') return withColor({ shape });
  }
  if (/\bque horas\b|\bhoras sao\b/.test(normalize(text))) return { shape: 'clock' };
  return null;
}

/** Plain text suitable for speech synthesis. */
export function toSpeakable(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[*_`#>~]/g, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/\s+/g, ' ')
    .trim();
}
