import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Api } from "../api/client";
import { Badge, Button, Spinner } from "./ui";

/** Login command and live status for the isolated Claude Code profile (claude_code_own_profile), shown
 *  under that setting. Status comes only from `claude auth status`'s exit code (see the backend), never
 *  from reading a file, and is fetched on demand rather than polled. */
export default function ClaudeCodeProfilePanel() {
  const profile = useQuery({ queryKey: ["claude-code-profile"], queryFn: Api.getClaudeCodeProfile, retry: false });
  const [copied, setCopied] = useState(false);
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be denied (permissions, insecure context); the command is still selectable text.
    }
  };
  if (profile.isLoading) return <div className="py-2"><Spinner /></div>;
  if (!profile.data) return null;
  const p = profile.data;
  return (
    <div className="space-y-2 py-2 font-sans text-xs">
      <div className="flex items-center gap-2">
        <span className="text-muted">Isolated profile:</span>
        {p.checked ? (
          <Badge tone={p.logged_in ? "green" : "amber"}>{p.logged_in ? "logged in" : "not logged in"}</Badge>
        ) : (
          <span className="text-faint">could not check{p.error ? `: ${p.error}` : ""}</span>
        )}
        <Button type="button" variant="secondary" size="sm" onClick={() => profile.refetch()}>Check again</Button>
      </div>
      <p className="text-muted">
        {p.logged_in ? "Runs once; run it again only if you log out or move the profile." : "Run once in PowerShell to log in:"}
      </p>
      {!p.logged_in && (
        <div className="flex items-center gap-2">
          <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded-ui border border-line bg-sunken px-2 py-1">{p.commands.powershell}</code>
          <Button type="button" variant="secondary" size="sm" onClick={() => copy(p.commands.powershell)}>{copied ? "Copied" : "Copy"}</Button>
        </div>
      )}
      <p className="text-faint">Profile directory: <code>{p.profile_dir}</code>. cmd.exe: <code>{p.commands.cmd}</code>. macOS/Linux: <code>{p.commands.posix}</code>.</p>
    </div>
  );
}
