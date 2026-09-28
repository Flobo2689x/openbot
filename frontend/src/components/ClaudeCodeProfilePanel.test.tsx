// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Api } from "../api/client";
import type { ClaudeCodeProfile } from "../api/types";
import ClaudeCodeProfilePanel from "./ClaudeCodeProfilePanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root;
let el: HTMLDivElement;
let qc: QueryClient;

const profile = (over: Partial<ClaudeCodeProfile> = {}): ClaudeCodeProfile => ({
  enabled: true, profile_dir: "C:\\data\\claude-profile",
  commands: { powershell: 'PS COMMAND', cmd: "CMD COMMAND", posix: "POSIX COMMAND" },
  checked: true, logged_in: false, ...over,
});

async function until(condition: () => boolean, what: string) {
  const deadline = Date.now() + 2000;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error(`Timed out waiting for: ${what}`);
    await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  }
}

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

it("shows the login command when not logged in, and nothing sensitive when logged in", async () => {
  vi.spyOn(Api, "getClaudeCodeProfile").mockResolvedValue(profile({ logged_in: false }));
  act(() => root.render(<QueryClientProvider client={qc}><ClaudeCodeProfilePanel /></QueryClientProvider>));
  await until(() => el.textContent?.includes("not logged in") === true, "the not-logged-in badge");
  expect(el.textContent).toContain("PS COMMAND");
  expect(el.textContent).toContain("claude-profile");

  qc.clear();
  vi.spyOn(Api, "getClaudeCodeProfile").mockResolvedValue(profile({ logged_in: true }));
  act(() => root.render(<QueryClientProvider client={qc}><ClaudeCodeProfilePanel /></QueryClientProvider>));
  await until(() => el.textContent?.includes("logged in") === true, "the logged-in badge");
  expect(el.textContent).not.toContain("PS COMMAND");
});

it("shows why the check failed instead of a status it couldn't confirm", async () => {
  vi.spyOn(Api, "getClaudeCodeProfile").mockResolvedValue(profile({ checked: false, logged_in: null, error: "claude was not found" }));
  act(() => root.render(<QueryClientProvider client={qc}><ClaudeCodeProfilePanel /></QueryClientProvider>));
  await until(() => el.textContent?.includes("could not check") === true, "the could-not-check message");
  expect(el.textContent).toContain("claude was not found");
});
