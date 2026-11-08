#!/usr/bin/env bash
# Refresh the model dropdowns in Settings from the AI CLIs installed on this machine.
# Codex: reads the model list the Codex CLI itself downloaded (~/.codex/models_cache.json),
# keeping only models it shows in its own picker. Claude: the CLI's tier aliases.
# Writes local-data/models.json, which the app reads on every Settings load (no restart).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=local-data/models.json
CODEX_CACHE="$HOME/.codex/models_cache.json"

if command -v codex >/dev/null && [ -f "$CODEX_CACHE" ]; then
  codex_models=$(jq -c '[.models[] | select(.visibility == "list") | .slug]' "$CODEX_CACHE")
  codex_efforts=$(jq -c '[.models[] | select(.visibility == "list")
    | {key: .slug, value: [.supported_reasoning_levels[]? | (.effort // .)]}] | from_entries' "$CODEX_CACHE")
else
  codex_models='[]'
  codex_efforts='{}'
  echo "Codex model list not found (run codex once to create it). Leaving Codex on its default."
fi

claude_models='["haiku","sonnet","opus","fable"]'

jq -n --argjson claude "$claude_models" --argjson codex "$codex_models" --argjson ce "$codex_efforts" \
  '{claude: $claude, codex: $codex, gemini: [],
    efforts: {claude: {"*": ["low","medium","high","xhigh","max"]}, codex: $ce},
    refreshed_at: (now | todate)}' > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
echo "Updated $OUT:"
jq -r '"  claude: \(.claude | join(", "))  [effort: \(.efforts.claude["*"] | join("/"))]",
  (.efforts.codex | to_entries[] | "  codex:  \(.key)  [effort: \(.value | join("/"))]")' "$OUT"
