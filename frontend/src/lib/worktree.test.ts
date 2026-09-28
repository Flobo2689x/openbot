import { describe, expect, it } from "vitest";
import { ApiError } from "../api/errors";
import type { WorktreeStatus } from "../api/types";
import { discardConfirmText, lossText, worktreeConflict } from "./worktree";

const status = (over: Partial<WorktreeStatus> = {}): WorktreeStatus => ({
  path: "/wt/project-1234abcd", branch: "openbot/1234abcd", exists: true, branch_exists: true,
  uncommitted: 0, unpushed: 0, unpushed_commits: [], clean: true, ...over,
});

describe("worktreeConflict", () => {
  it("reads the worktree from a 409 and ignores every other error", () => {
    const body = JSON.stringify({ detail: { message: "has 2 unpushed commits", worktree: status({ unpushed: 2, clean: false }) } });
    expect(worktreeConflict(new ApiError(409, body))?.worktree.unpushed).toBe(2);
    expect(worktreeConflict(new ApiError(409, '{"detail":"conflict"}'))).toBeNull();
    expect(worktreeConflict(new ApiError(500, body))).toBeNull();
    expect(worktreeConflict(new Error("x"))).toBeNull();
  });
});

describe("what a discard throws away", () => {
  it("names unpushed commits and uncommitted changes", () => {
    expect(lossText(status({ unpushed: 1, uncommitted: 3 }))).toBe("1 unpushed commit on openbot/1234abcd and uncommitted changes in 3 files");
    expect(lossText(status())).toBe("");
  });

  it("says so plainly in the confirmation, or that nothing is lost", () => {
    expect(discardConfirmText(status({ unpushed: 2, clean: false })))
      .toBe("Discard the worktree at /wt/project-1234abcd? This permanently deletes 2 unpushed commits on openbot/1234abcd.");
    expect(discardConfirmText(status())).toContain("Nothing would be lost");
  });
});
