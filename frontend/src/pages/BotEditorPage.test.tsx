// @vitest-environment jsdom
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Api } from "../api/client";
import type { Bot } from "../api/types";
import BotEditorPage from "./BotEditorPage";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let el: HTMLDivElement;
let qc: QueryClient;
const bot = (id: string, name: string): Bot => ({ id, name, handle: id, description: "", icon: "bot", instructions: "", provider: "auto", model: "", model_settings: {}, tool_names: [], approval_tools: [], memory_enabled: true, enabled: true, active: false, created_at: "", updated_at: "" });
const nameInput = () => el.querySelector<HTMLInputElement>("input[required]")?.value;
async function until(condition: () => boolean) {
  const deadline = Date.now() + 2000;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error(`Timed out: name=${nameInput()}`);
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  }
}
// Navigates inside the router, so the editor instance is reused like when switching bots in the app.
function Go({ to }: { to: string }) {
  const nav = useNavigate();
  useEffect(() => { nav(to); }, [nav, to]);
  return null;
}
async function show(path: string) {
  await act(async () => root.render(
    <QueryClientProvider client={qc}><MemoryRouter initialEntries={["/edit/b1"]}>
      <Go to={path} />
      <Routes><Route path="/edit/:id" element={<BotEditorPage />} /></Routes>
    </MemoryRouter></QueryClientProvider>,
  ));
}
beforeEach(() => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  vi.spyOn(Api, "getProviders").mockResolvedValue({ providers: [], embedding_model: "none", embeddings_configured: false });
  vi.spyOn(Api, "listTools").mockResolvedValue({ tools: [], errors: [] });
  vi.spyOn(Api, "getBot").mockImplementation((id: string) => Promise.resolve(bot(id, `Bot ${id}`)));
  el = document.createElement("div"); document.body.append(el); root = createRoot(el);
});
afterEach(async () => {
  await act(async () => root.unmount());
  el.remove(); qc.clear(); vi.restoreAllMocks();
});

it("reloads the form when switching to another bot on the same page", async () => {
  await show("/edit/b1");
  await until(() => nameInput() === "Bot b1");

  await show("/edit/b2");
  await until(() => nameInput() === "Bot b2");

  // Back to a bot that is already cached: its settings come back, not the ones just shown.
  await show("/edit/b1");
  await until(() => nameInput() === "Bot b1");
  expect(Api.getBot).toHaveBeenCalledTimes(2);
});
