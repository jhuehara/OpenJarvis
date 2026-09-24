# Rodando o OpenJarvis com o LLM local do computador

Este guia configura o OpenJarvis para usar **somente** um modelo rodando na sua
máquina — nenhuma chamada a APIs na nuvem, nenhuma chave de API necessária.

## 1. Instale um servidor de LLM local

Escolha **um** dos servidores abaixo:

| Servidor | Como iniciar | Endereço padrão |
|---|---|---|
| **Ollama** (recomendado) | instale em <https://ollama.com/download> e rode `ollama serve` | `http://localhost:11434` |
| **LM Studio** | aba *Developer / Local Server* → *Start Server* com um modelo carregado | `http://localhost:1234` |
| **llama.cpp** | `llama-server -m modelo.gguf --port 8080` | `http://localhost:8080` |

## 2. Instale o OpenJarvis

```bash
uv sync
source .venv/bin/activate
```

## 3. Inicie com um comando

```bash
./scripts/launch-local.sh                 # detecta o servidor e o modelo e abre o chat
./scripts/launch-local.sh qwen3.5:9b      # escolhe o modelo
./scripts/launch-local.sh -- serve        # sobe a API (http://127.0.0.1:8000) p/ o app web/desktop
./scripts/launch-local.sh -- ask "Olá!"   # pergunta única
```

O script:

1. Procura um servidor local rodando (Ollama → LM Studio → llama.cpp). Se nenhum
   estiver ativo mas o `ollama` estiver instalado, ele inicia o `ollama serve`.
2. Usa o primeiro modelo de chat já instalado. No Ollama, se não houver nenhum,
   baixa um modelo inicial conforme a RAM: `qwen3.5:2b` (<12 GB),
   `qwen3.5:4b` (12–23 GB) ou `qwen3.5:9b` (24 GB+).
3. Cria `~/.openjarvis/local-llm.toml` (só na primeira vez) apontando para o
   servidor local e roda o `jarvis` com essa configuração.

Variáveis opcionais: `LOCAL_LLM_ENGINE` (`ollama`, `lmstudio`, `llamacpp`),
`OLLAMA_HOST`, `LMSTUDIO_HOST`, `LLAMACPP_HOST` e `OPENJARVIS_CONFIG`.

## Alternativa: configuração permanente

Para que o `jarvis` puro (sem o script) use o LLM local:

```bash
jarvis init --preset local-llm --force   # grava ~/.openjarvis/config.toml
ollama pull qwen3.5:4b                   # o modelo definido no preset
jarvis                                   # chat
```

Edite `~/.openjarvis/config.toml` para trocar o servidor (`[engine] default`) ou
o modelo (`[intelligence] default_model`). O modelo precisa estar carregado no
servidor (`ollama list` mostra os instalados).

## Dicas

- `jarvis doctor` verifica se o servidor e o modelo estão acessíveis.
- O agente padrão do preset é `simple` (funciona com qualquer modelo). Para usar
  ferramentas, troque para `orchestrator` em `[agent] default_agent` — exige um
  modelo com suporte a *function calling* (ex.: `qwen3.5`, `llama3.1`).
- A API fica em `127.0.0.1` (só acessível desta máquina).
