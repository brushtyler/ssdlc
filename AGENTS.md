# SSDLC Project Guidelines

Istruzioni valide per qualsiasi agent che lavora su questo repository (Claude Code, Codex, Antigravity o altri).

## Intestazione di Issue e Pull Request

Ogni testo pubblicato su GitHub per questo repository deve iniziare con l'intestazione qui sotto. Vale per issue, commenti alle issue, descrizioni di PR, commenti e review delle PR.

```markdown
> [!NOTE]
> **Agent:** <AGENT_NAME> (CLI v<CLI_VERSION>)
> **Model:** <MODEL_NAME>
> **Reasoning Level:** <REASONING_LEVEL>
```

Esempio:

> [!NOTE]
> **Agent:** Claude Code (CLI v2.1.322)
> **Model:** Claude Opus 5.5
> **Reasoning Level:** High

### Come ricavare i valori

Usare i valori **effettivi della sessione corrente** al momento della pubblicazione, non quelli di default o di una sessione precedente:

| Agent | `CLI_VERSION` | `MODEL_NAME` e `REASONING_LEVEL` |
|---|---|---|
| Claude Code | `claude --version` | modello ed effort attivi nella sessione (`/model`) |
| Codex | `codex --version` | modello e `model_reasoning_effort` effettivi della sessione (`/status`); `~/.codex/config.toml` fornisce solo i default, sovrascrivibili con `-m`, `-c` o profili |
| Antigravity | `agy --version` | campo `"model"` in `~/.gemini/antigravity-cli/settings.json`: nome del modello e livello di reasoning indicato tra parentesi |
| Altri | comando `--version` della CLI | impostazioni attive della sessione |

### Regole

- Indicare il livello di reasoning in forma leggibile (`Low`, `Medium`, `High`, …).
- Non indovinare. Se un valore non è determinabile, scrivere `non noto` e, se possibile, chiederlo all'operatore.
- Se il livello cambia durante la sessione, usare quello attivo al momento della pubblicazione.
- Se si modifica un testo già pubblicato, l'intestazione deve riflettere l'agent che lo ha scritto in origine. Se il contenuto viene riscritto in modo sostanziale, aggiungere una seconda intestazione per l'agent che lo ha rivisto.
- L'intestazione non contiene segreti, percorsi privati o dati personali.
