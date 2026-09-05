# Contributing

Thanks for looking at **media-processing-error-handoff**. It's a small, focused python example, so contributions stay simple.

## Ground rules

- Keep the example runnable with a single `INFRAI_API_KEY` and no other setup.
- The thin client is the only place that knows the base URL and auth — new calls go through it.
- Don't hard-code secrets; read them from the environment.

## Workflow

1. Fork & branch.
2. Make the change; keep it compiling.
3. Open a PR describing what you added and how to verify it.
