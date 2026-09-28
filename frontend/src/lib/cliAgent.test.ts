import { describe, expect, it } from "vitest";
import { CLAUDE_CODE, CLI_CUSTOM, CLI_DEFAULT, cliModelForSelectValue, cliModelSelectValue, offeredProviders, parseAllowedTools, providerLabel } from "./cliAgent";

const p = (id: string, configured: boolean) => ({ id, configured, models: [], default_model: "" });

describe("offeredProviders", () => {
  const all = [p("auto", true), p("openai", false), p(CLAUDE_CODE, false)];

  it("hides claude-code until the CLI is found", () => {
    expect(offeredProviders(all, "auto").map((x) => x.id)).toEqual(["openai"]);
    expect(offeredProviders([...all.slice(0, 2), p(CLAUDE_CODE, true)], "auto").map((x) => x.id)).toEqual(["openai", CLAUDE_CODE]);
  });

  it("keeps it for a bot that already uses it", () => {
    expect(offeredProviders(all, CLAUDE_CODE).map((x) => x.id)).toEqual(["openai", CLAUDE_CODE]);
    expect(providerLabel(p(CLAUDE_CODE, false))).toBe("Claude Code CLI (local) (claude not found)");
  });
});

describe("parseAllowedTools", () => {
  it("splits on commas and lines but not inside a rule", () => {
    expect(parseAllowedTools("Read, Edit\nBash(git diff *), Bash(npm run test:*)")).toEqual(["Read", "Edit", "Bash(git diff *)", "Bash(npm run test:*)"]);
    expect(parseAllowedTools("Bash(a, b)")).toEqual(["Bash(a, b)"]);
    expect(parseAllowedTools("  \n , ")).toEqual([]);
  });
});

describe("the claude-code model select", () => {
  const models = ["sonnet", "opus", "haiku"];

  it("maps a model to the select value: default, a known model, or custom", () => {
    expect(cliModelSelectValue("", models)).toBe(CLI_DEFAULT);
    expect(cliModelSelectValue("opus", models)).toBe("opus");
    expect(cliModelSelectValue("claude-3-7-sonnet-20250219", models)).toBe(CLI_CUSTOM);
  });

  it("maps a select change back to the model to store", () => {
    expect(cliModelForSelectValue(CLI_DEFAULT, "opus", models)).toBe("");
    expect(cliModelForSelectValue("haiku", "", models)).toBe("haiku");
    // Picking Custom from a known model clears the field instead of pre-filling it with that model's name.
    expect(cliModelForSelectValue(CLI_CUSTOM, "opus", models)).toBe("");
    // Picking Custom while already on a custom model keeps it.
    expect(cliModelForSelectValue(CLI_CUSTOM, "my-fine-tune", models)).toBe("my-fine-tune");
  });
});
