# Hi, I'm Abid 👋

I build developer tooling for the AI era — CLIs, profilers, and benchmarks that make working with LLMs less painful and more predictable. Vibe coder using Claude Code and OpenAI Codex.

---

## What I'm building

### 🔍 [llm-cost-profiler](https://github.com/BuildWithAbid/llm-cost-profiler) — Find the money you're burning on LLM APIs
Two lines of code, zero config. Wraps your OpenAI or Anthropic client and gives you instant visibility into spend by feature, hotspots by file, retry waste, context bloat, and model downgrade opportunities. Found **$1,240/month in waste** on my own project in the first run.

```python
from llm_cost_profiler import wrap
from openai import OpenAI

client = wrap(OpenAI())  # that's it
```

`pip install llm-spend-profiler` · Python · MIT

---

### ⚡ [llm-bench](https://github.com/BuildWithAbid/llm-bench) — Race your LLMs in the terminal
CLI that sends the same prompt to OpenAI, Anthropic, Gemini, and Groq in parallel, streams a live terminal race, scores on speed/cost/quality, and generates shareable result cards.

```bash
llm-bench run "Explain vector databases" --models gpt-4o,claude-sonnet-4-6,gemini-2.5-flash
```

`npm install -g llm-bench` · TypeScript · MIT

---

### 📊 [ai-stability](https://github.com/BuildWithAbid/ai-stability) — Measure LLM output consistency
CLI-first stability analyzer. Runs your prompt N times, scores pairwise similarity, gives you a stability label (High / Medium / Low), inline diffs, and a local JSON artifact. Published on PyPI.

```bash
ai-stability run prompt.txt --n 5 --provider openai --model gpt-4.1-mini
```

`pipx install ai-stability` · Python · MIT · [PyPI](https://pypi.org/project/ai-stability/)

---

## Stack & tools

`Python` `TypeScript` `Claude Code` `OpenAI Codex` `OpenAI API` `Anthropic API` `Gemini` `Groq` `CLI tooling` `SQLite` `Ink/React`

---

## What I'm interested in

- LLM observability and cost tooling
- Developer-facing AI infrastructure
- Fast, zero-dependency CLI tools
- Prompt reliability and evals

---

## Get in touch

Open to collaborations, contract work, and conversations about LLM tooling. If you're building in this space, let's talk.

- **X/Twitter:** [@BuildWithAbid](https://twitter.com/BuildWithAbid)
- **GitHub:** [github.com/BuildWithAbid](https://github.com/BuildWithAbid)
