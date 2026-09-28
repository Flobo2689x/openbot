import type { ProviderInfo } from "../api/types";

/** Bots on this provider run their turns through the local Claude Code CLI instead of an OpenBot model. */
export const CLAUDE_CODE = "claude-code";

/** Sentinel <select> values for the bot editor's claude-code model field: distinct from any real
 *  model string so they can't collide with one, and from each other (empty model = CLI default). */
export const CLI_DEFAULT = "__cli_default__";
export const CLI_CUSTOM = "__cli_custom__";

/** The <select>'s value for a bot's current model: one of the known models, CLI_DEFAULT for "" (the
 *  CLI's own default), or CLI_CUSTOM for anything else (a model id typed by hand). */
export function cliModelSelectValue(model: string, knownModels: string[]): string {
  if (model === "") return CLI_DEFAULT;
  return knownModels.includes(model) ? model : CLI_CUSTOM;
}

/** The model to store for a change on that <select>. Picking "Custom…" starts the free-text field
 *  empty unless the current model is already a custom one (not in knownModels), so switching to
 *  custom from a known model doesn't leave its name sitting in the text box unexplained. */
export function cliModelForSelectValue(value: string, currentModel: string, knownModels: string[]): string {
  if (value === CLI_DEFAULT) return "";
  if (value === CLI_CUSTOM) return knownModels.includes(currentModel) ? "" : currentModel;
  return value;
}

/** Permission modes the backend accepts, most restrictive first; `dontAsk` is its default. */
export const PERMISSION_MODES: { value: string; label: string }[] = [
  { value: "dontAsk", label: "No prompts (dontAsk: reads and allowed tools run, anything else is denied)" },
  { value: "acceptEdits", label: "Edit files (acceptEdits)" },
  { value: "auto", label: "Auto (a classifier approves most actions)" },
];

/** Providers to offer in the bot editor. claude-code only appears once the CLI was found on the server,
 *  unless this bot already uses it: hiding the current value would make the select lie. */
export function offeredProviders(providers: ProviderInfo[], current: string): ProviderInfo[] {
  return providers.filter((p) => p.id !== "auto" && (p.id !== CLAUDE_CODE || p.configured || current === CLAUDE_CODE));
}

export function providerLabel(p: ProviderInfo): string {
  if (p.id === CLAUDE_CODE) return `Claude Code CLI (local)${p.configured ? "" : " (claude not found)"}`;
  return `${p.id}${p.configured ? "" : " (not configured)"}`;
}

/** The allowed-tools field is one rule per line or comma-separated, e.g. `Read, Edit, Bash(git diff *)`.
 *  Commas inside parentheses belong to the rule. */
export function parseAllowedTools(text: string): string[] {
  const rules: string[] = [];
  let depth = 0;
  let cur = "";
  for (const ch of text) {
    if (ch === "(") depth++;
    if (ch === ")") depth = Math.max(0, depth - 1);
    if ((ch === "," && depth === 0) || ch === "\n") {
      if (cur.trim()) rules.push(cur.trim());
      cur = "";
    } else cur += ch;
  }
  if (cur.trim()) rules.push(cur.trim());
  return rules;
}
