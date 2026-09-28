// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Api } from "../api/client";
import type { ProvidersOut, SetupStatus } from "../api/types";
import SetupWizard from "./SetupWizard";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let el: HTMLDivElement;
let qc: QueryClient;

const status: SetupStatus = { complete: false, chat: { ok: false, providers: [] }, embeddings: { ok: true, model: "", reason: null }, missing: ["chat"] };
const providers = (claudeCodeConfigured: boolean): ProvidersOut => ({
  providers: [
    { id: "auto", configured: false, models: [], default_model: "" },
    { id: "openrouter", configured: false, models: [], default_model: "" },
    { id: "claude-code", configured: claudeCodeConfigured, models: ["sonnet", "opus", "haiku"], default_model: "" },
  ],
  embedding_model: "none", embeddings_configured: false,
});

async function until(condition: () => boolean, what: string) {
  const deadline = Date.now() + 2000;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error(`Timed out waiting for: ${what}`);
    await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  }
}

function render() {
  act(() => root.render(<QueryClientProvider client={qc}><SetupWizard status={status} onDone={() => {}} /></QueryClientProvider>));
}

const buttonText = () => [...el.querySelectorAll("button")].map((b) => b.textContent ?? "");

beforeEach(() => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  el = document.createElement("div");
  document.body.append(el);
  root = createRoot(el);
});
afterEach(async () => {
  await act(async () => root.unmount());
  el.remove();
  qc.clear();
  vi.restoreAllMocks();
});

it("hides Claude Code until GET /providers reports the CLI found", async () => {
  vi.spyOn(Api, "getProviders").mockResolvedValue(providers(false));
  render();
  await until(() => buttonText().some((t) => t.includes("OpenRouter")), "the OpenRouter choice to render");
  expect(buttonText().some((t) => t.includes("Claude Code"))).toBe(false);
});

it("offers Claude Code once found, with only a model choice, and saves the explicit selection", async () => {
  vi.spyOn(Api, "getProviders").mockResolvedValue(providers(true));
  const patch = vi.spyOn(Api, "patchSettings").mockResolvedValue([]);
  render();
  await until(() => buttonText().some((t) => t.includes("Claude Code")), "the Claude Code choice to render");

  const claudeButton = [...el.querySelectorAll("button")].find((b) => b.textContent?.includes("Claude Code"))!;
  await act(async () => { claudeButton.click(); });
  // No API key field, no Ollama URL field: just the model select with the CLI's reported models.
  expect(el.querySelector("input[type='password']")).toBeNull();
  const modelOptions = [...el.querySelectorAll<HTMLOptionElement>("select option")].map((o) => o.value);
  expect(modelOptions).toEqual(["sonnet", "opus", "haiku"]);

  const continueButton = [...el.querySelectorAll("button")].find((b) => b.textContent === "Continue")!;
  await act(async () => { continueButton.click(); });
  const noSearch = [...el.querySelectorAll("button")].find((b) => b.textContent?.includes("No semantic search"))!;
  await act(async () => { noSearch.click(); });
  const finish = [...el.querySelectorAll("button")].find((b) => b.textContent?.startsWith("Finish"))!;
  await act(async () => { finish.click(); });

  await until(() => patch.mock.calls.length > 0, "Api.patchSettings to be called");
  expect(patch).toHaveBeenCalledWith({ claude_code_selected: true, claude_code_model: "sonnet", embedding_model: "" });
});
