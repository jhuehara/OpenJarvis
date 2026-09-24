# Usando o Claude Code ou o Codex do seu computador no OpenJarvis

O OpenJarvis pode usar como "cérebro" o **Claude Code** ou o **Codex CLI** que
você já tem instalado e logado. Ele chama o CLI (`claude -p` / `codex exec`) e
reaproveita o seu login — assinatura Claude Pro/Max ou ChatGPT Plus/Pro —, então
**não é preciso API key**.

> O processamento continua nos servidores da Anthropic/OpenAI (é o mesmo que
> acontece quando você usa o Claude Code ou o Codex). Se quiser um modelo 100%
> offline, veja [Modelos offline](#modelos-offline-ollama-lm-studio-llamacpp).

## 1. Deixe o CLI instalado e logado

**Claude Code** — se `claude --version` não funcionar no terminal (o app desktop sozinho não basta), instale o CLI:

```bash
# macOS / Linux / WSL
curl -fsSL https://claude.ai/install.sh | bash
# Windows (PowerShell)
irm https://claude.ai/install.ps1 | iex

claude          # abra uma vez e faça login com sua conta Claude
```

**Codex**:

```bash
npm install -g @openai/codex
codex login     # login com sua conta ChatGPT
```

Confira: `claude --version` ou `codex --version` precisa funcionar no terminal.

> ⚠️ Se a variável `ANTHROPIC_API_KEY` (ou `OPENAI_API_KEY` para o Codex)
> estiver definida, o CLI pode cobrar por essa chave em vez da sua assinatura.
> Remova-a com `unset ANTHROPIC_API_KEY` se quiser usar só a assinatura.

## 2. Instale o OpenJarvis

```bash
uv sync
source .venv/bin/activate
```

## 3. Inicie com um comando

```bash
./scripts/launch-local.sh                    # usa o Claude Code (ou o Codex, se só ele existir)
./scripts/launch-local.sh claudecode:opus    # Claude Code com um modelo específico
./scripts/launch-local.sh codex              # força o Codex
./scripts/launch-local.sh -- serve           # sobe a API (http://127.0.0.1:8000) p/ o app web/desktop
./scripts/launch-local.sh -- ask "Olá!"      # pergunta única
```

O script procura, nesta ordem: `claude` → `codex` → Ollama → LM Studio →
llama.cpp, grava `~/.openjarvis/local-llm.toml` e roda o `jarvis` com ele.
Para escolher o backend: `LOCAL_LLM_ENGINE=codex ./scripts/launch-local.sh`.

## Configuração permanente (sem o script)

```bash
jarvis init --preset claude-code --force   # ou: --preset codex
jarvis                                     # chat
```

Isso grava `~/.openjarvis/config.toml`. Opções principais:

```toml
[engine]
default = "claude_code"          # ou "codex"

[engine.claude_code]
binary = "claude"                # caminho do CLI, se não estiver no PATH
timeout = 300                    # segundos por resposta
tools = ""                       # "" = só conversa; ex.: "WebSearch,WebFetch"

[intelligence]
default_model = "claudecode"     # claudecode | claudecode:sonnet | :opus | :haiku
                                 # codex | codex:<modelo>
```

Você também pode trocar na hora: `jarvis chat --engine codex --model codex`.

## Como funciona

- Engines novos em `src/openjarvis/engine/cli_agents.py`: `claude_code` e `codex`.
- Cada mensagem vira uma chamada ao CLI; o histórico da conversa e o prompt de
  sistema do Jarvis são enviados juntos. O Claude Code transmite a resposta em
  tempo real (streaming).
- O Claude Code roda sem ferramentas por padrão (`tools = ""`) e o Codex roda com
  sandbox somente leitura, ambos na pasta `~/.openjarvis/cli_workspace`.
- Funciona com `jarvis ask`, `jarvis chat` e com a API `jarvis serve`
  (compatível com OpenAI), usada pelo frontend e pelo app desktop.
- Use o agente `simple` (padrão dos presets). Os agentes com ferramentas do
  OpenJarvis (`orchestrator`, etc.) dependem de *function calling*, que os CLIs
  não expõem.
- Cada resposta leva alguns segundos a mais que uma API direta, porque o CLI é
  iniciado a cada mensagem.

## Interface visual: Jarvis Sky Show (voz + show de drones)

Com o servidor rodando, abra **http://127.0.0.1:8000/show** (ou "Drone Show" na
barra lateral do app). É uma tela cheia com um show de drones 3D sobre uma
cidade à noite, que reage ao que você pede:

```bash
./scripts/launch-local.sh -- serve     # depois abra http://127.0.0.1:8000/show
```

- **Digite ou fale**: clique no microfone (ou aperte a barra de espaço) e fale
  em português. A resposta do Jarvis aparece como legenda e é falada em voz alta.
- **Os drones executam o pedido**: “mostre um coração azul”, “escreva OLÁ
  MUNDO”, “faça fogos de artifício”, “que horas são?”, “desenhe um gato 🐱”…
  O modelo escolhe a formação (coração, estrela, planeta, galáxia, DNA, cubo,
  toro, onda, texto, relógio, fogos ou **qualquer emoji**, desenhado com as
  cores dele) e comandos simples reagem na hora, antes mesmo da resposta.
- **Estados visíveis**: anéis do logo pulsando com a sua voz enquanto ouve,
  giro acelerado enquanto pensa e batidas no ritmo da fala enquanto responde.
- **Controles**: modelo, quantidade de drones (800 a 4000), resposta falada
  liga/desliga, conversa contínua (volta a ouvir sozinho após cada resposta) e
  tela cheia. Arraste para girar a câmera e use a roda do mouse para zoom.

Requisitos: navegador com WebGL. O reconhecimento de voz usa o do navegador
(Chrome, Edge ou Safari); em outros navegadores, usa a transcrição do servidor
OpenJarvis, se estiver configurada. Para desenvolver a interface:
`cd frontend && npm ci && npm run dev` (abre em http://localhost:5173/show).

## Modelos offline (Ollama, LM Studio, llama.cpp)

Para rodar sem internet, instale o [Ollama](https://ollama.com/download) (ou
inicie o servidor do LM Studio na porta 1234 / `llama-server` na porta 8080) e
use `./scripts/launch-local.sh qwen3.5:4b` ou `jarvis init --preset local-llm --force`.
Se não houver modelo no Ollama, o script baixa um adequado à sua RAM.

## Problemas comuns

- **`'claude' CLI not found`** — o CLI não está no PATH; instale conforme o
  passo 1 ou defina `binary = "/caminho/para/claude"` na config.
- **`Not logged in` / erro de autenticação** — rode `claude` (ou `codex login`)
  no terminal uma vez e faça login.
- **Timeout** — aumente `timeout` em `[engine.claude_code]` / `[engine.codex]`.
- `jarvis doctor` mostra quais engines estão disponíveis.
