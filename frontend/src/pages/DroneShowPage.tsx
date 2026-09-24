import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import {
  ArrowLeft,
  Maximize2,
  Mic,
  MicOff,
  Repeat,
  Send,
  Square,
  Volume2,
  VolumeX,
} from 'lucide-react';
import { DroneSwarm, type Mood } from '../components/DroneShow/DroneSwarm';
import {
  SHOW_SYSTEM_PROMPT,
  extractActions,
  intentFromUserText,
  stripForDisplay,
  toSpeakable,
  type ShowAction,
} from '../components/DroneShow/actions';
import { useVoice } from '../hooks/useVoice';
import { streamChat } from '../lib/sse';
import { useAppStore } from '../lib/store';

const LANG = 'pt-BR';
const PREFS_KEY = 'oj-drone-show-prefs';
const DRONE_COUNTS = [800, 1500, 2500, 4000];

const QUICK: Array<{ label: string; text: string }> = [
  { label: '💖 Coração', text: 'Mostre um coração vermelho' },
  { label: '✍️ JARVIS', text: 'Escreva JARVIS' },
  { label: '🪐 Planeta', text: 'Mostre um planeta' },
  { label: '🎆 Fogos', text: 'Faça fogos de artifício' },
  { label: '🧬 DNA', text: 'Mostre uma hélice de DNA' },
  { label: '🌌 Galáxia', text: 'Mostre uma galáxia' },
  { label: '🚀 Foguete', text: 'Desenhe um foguete 🚀' },
  { label: '🕒 Hora', text: 'Que horas são?' },
];

const MOOD_LABEL: Record<Mood, string> = {
  idle: 'Pronto',
  listening: 'Ouvindo…',
  thinking: 'Pensando…',
  speaking: 'Falando…',
};

interface Prefs {
  voiceOut: boolean;
  handsFree: boolean;
  count: number;
}

function loadPrefs(): Prefs {
  const fallback: Prefs = { voiceOut: true, handsFree: false, count: 1500 };
  try {
    return { ...fallback, ...JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') };
  } catch {
    return fallback;
  }
}

function describe(action: ShowAction): string {
  if (action.shape === 'text') return `“${action.text}”`;
  if (action.shape === 'emoji') return action.emoji ?? '';
  return action.shape;
}

export function DroneShowPage() {
  const navigate = useNavigate();
  const models = useAppStore((s) => s.models);
  const selectedModel = useAppStore((s) => s.selectedModel);
  const setSelectedModel = useAppStore((s) => s.setSelectedModel);

  const stageRef = useRef<HTMLDivElement>(null);
  const swarmRef = useRef<DroneSwarm | null>(null);
  const historyRef = useRef<Array<{ role: string; content: string }>>([]);
  const abortRef = useRef<AbortController | null>(null);

  const [prefs, setPrefs] = useState<Prefs>(loadPrefs);
  const [mood, setMoodState] = useState<Mood>('idle');
  const [input, setInput] = useState('');
  const [userLine, setUserLine] = useState('');
  const [reply, setReply] = useState('');
  const [nowShowing, setNowShowing] = useState('jarvis');
  const [fatal, setFatal] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const model = selectedModel || models[0]?.id || '';

  const setMood = useCallback((m: Mood) => {
    setMoodState(m);
    swarmRef.current?.setMood(m);
  }, []);

  const show = useCallback((actions: ShowAction[]) => {
    const swarm = swarmRef.current;
    if (!swarm || !actions.length) return;
    swarm.sequence(actions);
    setNowShowing(actions.map(describe).join(' → '));
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      /* private mode: preferences just won't persist */
    }
  }, [prefs]);

  // Mount the 3D stage once.
  useEffect(() => {
    if (!stageRef.current) return;
    try {
      swarmRef.current = new DroneSwarm(stageRef.current, loadPrefs().count);
    } catch (err) {
      setFatal(err instanceof Error ? err.message : 'WebGL indisponível');
    }
    return () => {
      abortRef.current?.abort();
      swarmRef.current?.dispose();
      swarmRef.current = null;
    };
  }, []);

  useEffect(() => {
    swarmRef.current?.setCount(prefs.count);
  }, [prefs.count]);

  const sendRef = useRef<(text: string) => void>(() => {});

  const voice = useVoice({
    lang: LANG,
    onFinal: (text) => sendRef.current(text),
    onLevel: (level) => swarmRef.current?.setLevel(level),
    onWord: () => swarmRef.current?.pulse(0.6),
    onSpeakEnd: () => {
      setMood('idle');
      if (prefs.handsFree) void voice.listen();
    },
  });

  useEffect(() => {
    if (voice.state === 'listening') {
      if (swarmRef.current?.action.shape !== 'jarvis') show([{ shape: 'jarvis' }]);
      setMood('listening');
    } else if (voice.state === 'transcribing') {
      setMood('thinking');
    } else if (mood === 'listening') {
      setMood('idle');
    }
  }, [voice.state]); // eslint-disable-line react-hooks/exhaustive-deps

  const send = useCallback(
    async (raw: string) => {
      const text = raw.trim();
      if (!text) return;
      abortRef.current?.abort();
      voice.stopSpeaking();
      setInput('');
      setUserLine(text);
      setReply('');

      // React instantly to the user's own words; the model may refine it.
      const intent = intentFromUserText(text);
      if (intent) show([intent]);
      else if (swarmRef.current?.action.shape !== 'jarvis') show([{ shape: 'jarvis' }]);
      setMood('thinking');

      if (!model) {
        setReply('Nenhum modelo disponível. Inicie o servidor com “jarvis serve”.');
        setMood('idle');
        return;
      }

      const controller = new AbortController();
      abortRef.current = controller;
      setBusy(true);
      let full = '';
      try {
        const messages = [
          { role: 'system', content: SHOW_SYSTEM_PROMPT },
          ...historyRef.current.slice(-12),
          { role: 'user', content: text },
        ];
        for await (const evt of streamChat({ model, messages, stream: true }, controller.signal)) {
          let delta = '';
          try {
            delta = JSON.parse(evt.data)?.choices?.[0]?.delta?.content ?? '';
          } catch {
            continue;
          }
          if (!delta) continue;
          full += delta;
          setReply(stripForDisplay(full));
        }
        const { text: clean, actions } = extractActions(full);
        historyRef.current.push({ role: 'user', content: text }, { role: 'assistant', content: full });
        setReply(clean);
        if (actions.length) show(actions);
        if (prefs.voiceOut && voice.canSpeak && clean) {
          setMood('speaking');
          voice.speak(toSpeakable(clean));
        } else {
          setMood('idle');
          if (prefs.handsFree) void voice.listen();
        }
      } catch (err) {
        if (controller.signal.aborted) return;
        setReply(`Não consegui falar com o OpenJarvis: ${err instanceof Error ? err.message : String(err)}`);
        setMood('idle');
      } finally {
        if (abortRef.current === controller) setBusy(false);
      }
    },
    [model, prefs.handsFree, prefs.voiceOut, setMood, show, voice],
  );
  sendRef.current = send;

  // Space bar toggles the microphone when the text box isn't focused.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = document.activeElement;
      if (e.code !== 'Space' || el instanceof HTMLInputElement || el instanceof HTMLSelectElement) return;
      e.preventDefault();
      if (voice.state === 'listening') voice.stop();
      else if (voice.canListen) void voice.listen();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [voice]);

  const listening = voice.state === 'listening';
  const caption = voice.interim || reply;

  return (
    <div className="fixed inset-0 overflow-hidden bg-black text-white select-none" style={{ fontFamily: '"Geist Variable", system-ui, sans-serif' }}>
      <div ref={stageRef} className="absolute inset-0" />

      {fatal && (
        <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-white/70">
          Não foi possível iniciar o show 3D ({fatal}). Verifique se o WebGL está ativo no navegador.
        </div>
      )}

      {/* top bar */}
      <div className="absolute inset-x-0 top-0 flex flex-wrap items-center gap-2 p-3 sm:p-4 bg-gradient-to-b from-black/60 to-transparent">
        <button
          onClick={() => navigate('/')}
          className="rounded-full p-2 text-white/70 hover:text-white hover:bg-white/10"
          title="Voltar ao chat"
        >
          <ArrowLeft size={18} />
        </button>
        <div className="flex items-center gap-2 mr-auto">
          <span className="text-sm font-semibold tracking-[0.35em]">JARVIS</span>
          <span className="hidden sm:inline text-xs text-white/40 tracking-widest">SKY SHOW</span>
          <span
            className="ml-2 h-2 w-2 rounded-full"
            style={{
              background: { idle: '#22e5ff', listening: '#22e07a', thinking: '#9b5cff', speaking: '#ffd23f' }[mood],
              boxShadow: '0 0 10px currentColor',
            }}
          />
          <span className="text-xs text-white/60">{MOOD_LABEL[mood]}</span>
        </div>

        <select
          value={model}
          onChange={(e) => setSelectedModel(e.target.value)}
          className="max-w-[46vw] sm:max-w-xs rounded-md bg-white/10 border border-white/10 px-2 py-1 text-xs text-white/80 outline-none"
          title="Modelo"
        >
          {models.length === 0 && <option value="">sem modelos</option>}
          {models.map((m) => (
            <option key={m.id} value={m.id} className="bg-zinc-900">
              {m.id}
            </option>
          ))}
        </select>
        <select
          value={prefs.count}
          onChange={(e) => setPrefs((p) => ({ ...p, count: Number(e.target.value) }))}
          className="rounded-md bg-white/10 border border-white/10 px-2 py-1 text-xs text-white/80 outline-none"
          title="Quantidade de drones"
        >
          {DRONE_COUNTS.map((c) => (
            <option key={c} value={c} className="bg-zinc-900">
              {c} drones
            </option>
          ))}
        </select>
        <button
          onClick={() => setPrefs((p) => ({ ...p, voiceOut: !p.voiceOut }))}
          className="rounded-full p-2 text-white/70 hover:text-white hover:bg-white/10"
          title={prefs.voiceOut ? 'Resposta falada: ligada' : 'Resposta falada: desligada'}
        >
          {prefs.voiceOut ? <Volume2 size={17} /> : <VolumeX size={17} />}
        </button>
        <button
          onClick={() => setPrefs((p) => ({ ...p, handsFree: !p.handsFree }))}
          className={`rounded-full p-2 hover:bg-white/10 ${prefs.handsFree ? 'text-emerald-300' : 'text-white/70 hover:text-white'}`}
          title="Conversa contínua (volta a ouvir após cada resposta)"
        >
          <Repeat size={17} />
        </button>
        <button
          onClick={() => void document.documentElement.requestFullscreen?.().catch(() => {})}
          className="hidden sm:block rounded-full p-2 text-white/70 hover:text-white hover:bg-white/10"
          title="Tela cheia"
        >
          <Maximize2 size={17} />
        </button>
      </div>

      {/* captions */}
      <div className="pointer-events-none absolute inset-x-0 bottom-40 sm:bottom-44 flex flex-col items-center gap-2 px-4 text-center">
        {userLine && !voice.interim && <div className="max-w-2xl text-sm text-white/45">{userLine}</div>}
        {caption && (
          <div
            className="max-w-3xl text-base sm:text-xl leading-relaxed text-white/95"
            style={{ textShadow: '0 0 18px rgba(34,229,255,0.35), 0 2px 8px rgba(0,0,0,0.9)' }}
          >
            {caption}
          </div>
        )}
        <div className="text-[10px] uppercase tracking-[0.3em] text-white/30">formação: {nowShowing}</div>
      </div>

      {/* input */}
      <div className="absolute inset-x-0 bottom-0 flex flex-col items-center gap-3 p-3 sm:p-5 bg-gradient-to-t from-black/70 to-transparent">
        <div className="flex max-w-full gap-2 overflow-x-auto pb-1 [scrollbar-width:none]">
          {QUICK.map((q) => (
            <button
              key={q.label}
              onClick={() => void send(q.text)}
              className="shrink-0 rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-white/75 backdrop-blur-md hover:bg-white/15 hover:text-white"
            >
              {q.label}
            </button>
          ))}
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void send(input);
          }}
          className="flex w-full max-w-2xl items-center gap-2 rounded-full border border-white/15 bg-white/[0.07] p-1.5 pl-2 backdrop-blur-xl"
          style={{ boxShadow: '0 0 40px rgba(34,229,255,0.08)' }}
        >
          <button
            type="button"
            onClick={() => (listening ? voice.stop() : void voice.listen())}
            disabled={!voice.canListen || voice.state === 'transcribing'}
            className={`relative flex h-11 w-11 shrink-0 items-center justify-center rounded-full transition ${
              listening ? 'bg-emerald-400 text-black' : 'bg-white/10 text-white hover:bg-white/20'
            } disabled:opacity-40`}
            title={voice.canListen ? 'Falar (barra de espaço)' : 'Reconhecimento de voz indisponível'}
          >
            {listening && <span className="absolute inset-0 animate-ping rounded-full bg-emerald-400/40" />}
            {voice.canListen ? <Mic size={19} /> : <MicOff size={19} />}
          </button>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={listening ? 'Ouvindo… fale com o Jarvis' : 'Peça algo: “mostre um coração azul”, “escreva OLÁ”…'}
            className="min-w-0 flex-1 bg-transparent px-2 text-sm sm:text-base text-white placeholder:text-white/35 outline-none"
          />
          {busy || voice.speaking ? (
            <button
              type="button"
              onClick={() => {
                abortRef.current?.abort();
                voice.stopSpeaking();
                setBusy(false);
                setMood('idle');
              }}
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-white/10 hover:bg-white/20"
              title="Parar"
            >
              <Square size={16} />
            </button>
          ) : (
            <button
              type="submit"
              disabled={!input.trim()}
              className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-cyan-400 text-black hover:bg-cyan-300 disabled:opacity-30"
              title="Enviar"
            >
              <Send size={17} />
            </button>
          )}
        </form>
        {voice.error && <div className="text-xs text-rose-300/90">{voice.error}</div>}
      </div>
    </div>
  );
}

export default DroneShowPage;
