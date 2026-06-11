# Privacy & Disclaimer

## Data Collection

This plugin collects no telemetry and tracks no users. When triggered, your prompt text is sent to a read-only API endpoint to retrieve relevant n8n official and community-sourced content. Queries are not logged or retained server-side.

## Local Transparency Log

So you can see exactly what the knowledge base returns to your model, the plugin writes a recall summary to a local debug log on **your machine only** (`/tmp/n8n-knowledge-debug.log`). Nothing in this log ever leaves your computer — it exists so you can inspect, in real time, what context is being injected and why. Set the `debugRecall` plugin option to `off` to disable it, or to `full` for complete recall payloads.

## Content Disclaimer

The knowledge base contains community-sourced content from n8n's official docs, GitHub issues, and community forum posts. This content is developed and maintained by its respective authors, not by this plugin's maintainer. Although we have included confidence rankings based on trust signals and prompt injection guardrails in every context injection, the accuracy, completeness, and security of all content cannot be guaranteed. By using this plugin, you acknowledge that you do so at your own discretion and risk.

## LLM Context Injection

This plugin injects third-party content into your LLM's context window. Community-sourced content may contain outdated patterns, inaccurate information, or unexpected instructions. Each injection includes confidence scores based on source type and engagement signals, and a safety warning directing the model to verify content before acting on it. Always review LLM outputs critically. See the MIT [LICENSE](LICENSE) for full warranty and liability terms.
