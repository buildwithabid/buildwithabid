# Hi, I'm Abid 👋

**I build production MCP servers — the kind where every write is previewed before it happens, confirmed when it matters, and recorded in a chain you can verify.**

Not as a demo. I run one over live invoices, client prices and statutory filing deadlines every working day, which is how I learned most of what's below the hard way.

---

## The worked example

### 🏠 [housewarden](https://github.com/buildwithabid/housewarden) — a guarded household-operations MCP server

31 tools. Reads are free; **every mutating tool goes through one guard** — a dry-run preview of exactly what will change, a human ask when the action is risky, execute-exactly-once, then an append to a hash-chained audit log. The web console uses the same path, so an assistant can never reach a weaker one than a person can.

`npm run e2e` drives it with a real MCP client and prints a pass/fail table, so you don't have to take my word for any of it. Streamable HTTP · self-hosted · MIT

[Three-minute demo](https://youtu.be/3zZOKYgpXVw) — real console, real MCP client, nothing mocked.

---

## The tooling I built around MCP

Because running these in production surfaces problems nobody has tools for yet.

| | |
|---|---|
| 🛡️ [**mcp-shield**](https://github.com/buildwithabid/mcp-shield) | Security scanner for MCP servers — finds what your tool surface exposes before someone else does |
| 🧪 [**mcp-testkit**](https://github.com/buildwithabid/mcp-testkit) | Testing framework for MCP servers · [npm](https://www.npmjs.com/package/mcp-testkit) |
| 🚪 [**mcpgate**](https://github.com/buildwithabid/mcpgate) | Open-source MCP gateway — reverse proxy for MCP servers |
| 🔎 [**mcp-audit**](https://github.com/buildwithabid/mcp-audit) | Python security scanner for MCP servers |
| 🛒 [**shopify-mcp**](https://github.com/buildwithabid/shopify-mcp) | MCP server for the Shopify Admin API |

---

## Something I got wrong, written up

**[Every 21 minutes: my self-healing monitor took my business phone line down for two days](https://gist.github.com/buildwithabid/161d26e90196c4df2953dc7f954bcd54)**

A repair loop that couldn't tell *broken* from *a human is part-way through fixing it*, a monitor that trusted its own cache over the service, and `active (running)` answering a question I wasn't asking. Four bugs, one shape: a system confidently answering something slightly different from what was asked.

---

## Available for MCP work

Tool surface reviews, production builds, and keeping them running afterwards.

Scope and fixed prices: **[The Write Path](https://claude.ai/artifact/F1w4szMDEa6e4NonRyFqp6)**

📬 **support@bizfilo.com**

---

## Also built

LLM developer tooling — [llm-cost-profiler](https://github.com/buildwithabid/llm-cost-profiler) (spend visibility in two lines, `pip install llm-spend-profiler`), [llm-bench](https://github.com/buildwithabid/llm-bench) (race providers in the terminal), [ai-stability](https://github.com/buildwithabid/ai-stability) (measure output consistency, `pipx install ai-stability`).

`Python` `TypeScript` `MCP` `Claude Code` `Anthropic API` `OpenAI API` `CLI tooling`