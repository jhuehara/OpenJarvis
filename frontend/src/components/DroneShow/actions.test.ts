import { describe, expect, it } from 'vitest';
import {
  extractActions,
  intentFromUserText,
  resolveColor,
  stripForDisplay,
  toSpeakable,
} from './actions';
import { buildFormation, fitToCount, hilbertIndex, sphere } from './formations';

describe('extractActions', () => {
  it('parses a JSON tag and strips it from the text', () => {
    const { text, actions } = extractActions(
      'Aqui está um coração para você.\n[[show:{"shape":"heart","color":"#FF3366"}]]',
    );
    expect(text).toBe('Aqui está um coração para você.');
    expect(actions).toEqual([{ shape: 'heart', color: '#ff3366' }]);
  });

  it('accepts shorthand, aliases and several tags', () => {
    const { actions } = extractActions('Ok! [[show:coração]] e [[show:{"shape":"text","text":"OLÁ","color":"azul"}]]');
    expect(actions).toEqual([
      { shape: 'heart' },
      { shape: 'text', text: 'OLÁ', color: '#2f7bff' },
    ]);
  });

  it('drops malformed or incomplete actions', () => {
    const { text, actions } = extractActions('Oi [[show:{"shape":"text"}]] [[show:{nope]] [[show:unicornio]]');
    expect(actions).toEqual([]);
    expect(text).toBe('Oi');
  });

  it('clamps spin and keeps emoji short', () => {
    const { actions } = extractActions('[[show:{"shape":"emoji","emoji":"🚀🚀🚀🚀🚀🚀","spin":99}]]');
    expect(actions[0].spin).toBe(3);
    expect(Array.from(actions[0].emoji!)).toHaveLength(4);
  });
});

describe('stripForDisplay', () => {
  it('hides a tag that is still streaming in', () => {
    expect(stripForDisplay('Claro! [[show:{"sha')).toBe('Claro!');
    expect(stripForDisplay('Claro! [[show:heart]] Pronto')).toBe('Claro!  Pronto');
  });
});

describe('intentFromUserText', () => {
  it('understands Portuguese shape requests with colours', () => {
    expect(intentFromUserText('Mostre um coração azul')).toEqual({ shape: 'heart', color: '#2f7bff' });
    expect(intentFromUserText('faça fogos de artifício')).toEqual({ shape: 'fireworks' });
    expect(intentFromUserText('Que horas são?')).toEqual({ shape: 'clock' });
  });

  it('spells words and draws emoji', () => {
    expect(intentFromUserText('Escreva Jarvis em dourado')).toEqual({ shape: 'text', text: 'JARVIS', color: '#ffc247' });
    expect(intentFromUserText('desenhe um foguete 🚀')).toEqual({ shape: 'emoji', emoji: '🚀' });
  });

  it('returns null for ordinary questions', () => {
    expect(intentFromUserText('Qual a capital da França?')).toBeNull();
  });
});

describe('helpers', () => {
  it('resolves colours', () => {
    expect(resolveColor('Vermelho')).toBe('#ff2a3d');
    expect(resolveColor('#ABC')).toBe('#abc');
    expect(resolveColor('arco-íris')).toBe('rainbow');
    expect(resolveColor('xyz')).toBeUndefined();
  });

  it('makes markdown speakable', () => {
    expect(toSpeakable('**Olá**, veja [isto](http://x) `code`')).toBe('Olá, veja isto code');
  });
});

describe('formations', () => {
  it('fits any cloud to exactly n drones and turns spare lights off', () => {
    const cloud = sphere(100);
    const { positions, colors } = fitToCount(cloud, 150);
    expect(positions).toHaveLength(450);
    const lit = Array.from({ length: 150 }, (_, i) => colors[i * 3] + colors[i * 3 + 1] + colors[i * 3 + 2] > 0);
    expect(lit.filter(Boolean)).toHaveLength(100);
  });

  it('thins dense clouds evenly', () => {
    const { colors } = fitToCount(sphere(1000), 200);
    for (let i = 0; i < 200; i++) expect(colors[i * 3] + colors[i * 3 + 1] + colors[i * 3 + 2]).toBeGreaterThan(0);
  });

  it('builds every geometric shape with the right size and colour', () => {
    for (const shape of ['jarvis', 'sphere', 'heart', 'star', 'planet', 'galaxy', 'dna', 'cube', 'torus', 'wave', 'fireworks'] as const) {
      const f = buildFormation({ shape, color: '#00ff00' }, 500);
      expect(f.positions).toHaveLength(1500);
      expect(Number.isFinite(f.halfWidth)).toBe(true);
      expect(f.halfWidth).toBeGreaterThan(10);
      expect(f.colors[1]).toBeGreaterThan(f.colors[0]);
    }
  });

  it('hilbert index is a bijection on the grid', () => {
    const seen = new Set<number>();
    for (let x = 0; x < 16; x++) for (let y = 0; y < 16; y++) seen.add(hilbertIndex(x, y, 16));
    expect(seen.size).toBe(256);
  });
});
