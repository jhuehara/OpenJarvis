import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchSpeechHealth, transcribeAudio } from '../lib/api';

// Minimal typings for the Web Speech API (not in lib.dom for every TS target).
interface SpeechRecognitionResultLike {
  isFinal: boolean;
  0: { transcript: string };
}
interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<SpeechRecognitionResultLike>;
}
interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}
type RecognitionCtor = new () => SpeechRecognitionLike;

function recognitionCtor(): RecognitionCtor | null {
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export type ListenState = 'idle' | 'listening' | 'transcribing';

interface Options {
  lang: string;
  /** Called with the final transcript of an utterance. */
  onFinal: (text: string) => void;
  /** Microphone loudness 0..1 while listening. */
  onLevel?: (level: number) => void;
  /** Called for each spoken word while Jarvis talks. */
  onWord?: () => void;
  onSpeakEnd?: () => void;
}

/**
 * Voice in and out for the drone show: browser speech recognition when
 * available (Chrome, Edge, Safari), otherwise record audio and use the
 * OpenJarvis server's transcription endpoint; replies are spoken with the
 * browser's speech synthesis.
 */
export function useVoice({ lang, onFinal, onLevel, onWord, onSpeakEnd }: Options) {
  const [state, setState] = useState<ListenState>('idle');
  const [interim, setInterim] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [serverStt, setServerStt] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const rafRef = useRef(0);
  const callbacks = useRef({ onFinal, onLevel, onWord, onSpeakEnd });
  callbacks.current = { onFinal, onLevel, onWord, onSpeakEnd };

  const browserStt = typeof window !== 'undefined' && recognitionCtor() !== null;
  const canListen = browserStt || serverStt;
  const canSpeak = typeof window !== 'undefined' && 'speechSynthesis' in window;

  useEffect(() => {
    if (browserStt) return;
    fetchSpeechHealth()
      .then((h) => setServerStt(h.available))
      .catch(() => setServerStt(false));
  }, [browserStt]);

  const stopMeter = useCallback(() => {
    cancelAnimationFrame(rafRef.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    void audioCtxRef.current?.close().catch(() => {});
    audioCtxRef.current = null;
    callbacks.current.onLevel?.(0);
  }, []);

  const startMeter = useCallback(async (): Promise<MediaStream | null> => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const ctx = new AudioContext();
      audioCtxRef.current = ctx;
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      ctx.createMediaStreamSource(stream).connect(analyser);
      const buf = new Uint8Array(analyser.fftSize);
      const tick = () => {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (const v of buf) sum += ((v - 128) / 128) ** 2;
        callbacks.current.onLevel?.(Math.min(1, Math.sqrt(sum / buf.length) * 4));
        rafRef.current = requestAnimationFrame(tick);
      };
      tick();
      return stream;
    } catch {
      return null;
    }
  }, []);

  const stop = useCallback(() => {
    recognitionRef.current?.stop();
    const recorder = recorderRef.current;
    if (recorder && recorder.state === 'recording') recorder.stop();
    else stopMeter();
  }, [stopMeter]);

  const listen = useCallback(async () => {
    setError(null);
    setInterim('');
    if (canSpeak) window.speechSynthesis.cancel();

    const Ctor = recognitionCtor();
    if (Ctor) {
      const rec = new Ctor();
      rec.lang = lang;
      rec.continuous = false;
      rec.interimResults = true;
      let finalText = '';
      rec.onresult = (e) => {
        let partial = '';
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const r = e.results[i];
          if (r.isFinal) finalText += r[0].transcript;
          else partial += r[0].transcript;
        }
        setInterim((finalText + ' ' + partial).trim());
      };
      rec.onerror = (e) => {
        if (e.error !== 'no-speech' && e.error !== 'aborted') {
          setError(e.error === 'not-allowed' ? 'Microfone bloqueado pelo navegador' : `Erro de voz: ${e.error}`);
        }
      };
      rec.onend = () => {
        recognitionRef.current = null;
        stopMeter();
        setState('idle');
        setInterim('');
        const text = finalText.trim();
        if (text) callbacks.current.onFinal(text);
      };
      recognitionRef.current = rec;
      setState('listening');
      void startMeter();
      rec.start();
      return;
    }

    if (!serverStt) {
      setError('Reconhecimento de voz indisponível neste navegador');
      return;
    }
    const stream = await startMeter();
    if (!stream) {
      setError('Microfone bloqueado pelo navegador');
      return;
    }
    const recorder = new MediaRecorder(stream);
    const chunks: Blob[] = [];
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.onstop = async () => {
      stopMeter();
      setState('transcribing');
      try {
        const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
        const { text } = await transcribeAudio(blob);
        if (text.trim()) callbacks.current.onFinal(text.trim());
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Falha na transcrição');
      } finally {
        setState('idle');
      }
    };
    recorderRef.current = recorder;
    recorder.start();
    setState('listening');
  }, [canSpeak, lang, serverStt, startMeter, stopMeter]);

  const speak = useCallback(
    (text: string) => {
      if (!canSpeak || !text.trim()) {
        callbacks.current.onSpeakEnd?.();
        return;
      }
      const synth = window.speechSynthesis;
      synth.cancel();
      const utter = new SpeechSynthesisUtterance(text);
      utter.lang = lang;
      const voices = synth.getVoices().filter((v) => v.lang.replace('_', '-').startsWith(lang.slice(0, 2)));
      const preferred =
        voices.find((v) => v.lang === lang && /natural|neural|google|online/i.test(v.name)) ??
        voices.find((v) => v.lang === lang) ??
        voices[0];
      if (preferred) utter.voice = preferred;
      utter.rate = 1.03;
      utter.onstart = () => setSpeaking(true);
      utter.onboundary = () => callbacks.current.onWord?.();
      const done = () => {
        setSpeaking(false);
        callbacks.current.onSpeakEnd?.();
      };
      utter.onend = done;
      utter.onerror = done;
      synth.speak(utter);
    },
    [canSpeak, lang],
  );

  const stopSpeaking = useCallback(() => {
    if (canSpeak) window.speechSynthesis.cancel();
    setSpeaking(false);
  }, [canSpeak]);

  useEffect(
    () => () => {
      recognitionRef.current?.abort();
      stopMeter();
      if (canSpeak) window.speechSynthesis.cancel();
    },
    [canSpeak, stopMeter],
  );

  return { state, interim, error, canListen, canSpeak, speaking, listen, stop, speak, stopSpeaking };
}
