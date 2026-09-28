import { ApiError } from "../api/errors";
import type { WorktreeStatus } from "../api/types";

/** The worktree details of a 409 from deleting a thread or its worktree, or null for any other error. */
export function worktreeConflict(error: unknown): { message: string; worktree: WorktreeStatus } | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  try {
    const detail = JSON.parse(error.message)?.detail;
    return detail && typeof detail === "object" && detail.worktree ? { message: String(detail.message ?? ""), worktree: detail.worktree } : null;
  } catch {
    return null;
  }
}

/** What removing the worktree would lose, in words, for the confirmation dialog. Empty when nothing. */
export function lossText(s: WorktreeStatus): string {
  const parts: string[] = [];
  if (s.unpushed) parts.push(`${s.unpushed} unpushed commit${s.unpushed === 1 ? "" : "s"} on ${s.branch}`);
  if (s.uncommitted) parts.push(`uncommitted changes in ${s.uncommitted} file${s.uncommitted === 1 ? "" : "s"}`);
  return parts.join(" and ");
}

/** The confirmation text for discarding: names exactly what goes, since nothing can bring it back. */
export function discardConfirmText(s: WorktreeStatus): string {
  const loss = lossText(s);
  return loss
    ? `Discard the worktree at ${s.path}? This permanently deletes ${loss}.`
    : `Remove the worktree at ${s.path} and its branch ${s.branch}? Nothing would be lost.`;
}
