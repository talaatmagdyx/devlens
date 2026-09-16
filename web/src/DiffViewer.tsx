/**
 * Unified-diff viewer.
 *
 * Self-contained on purpose. The previous implementation loaded the Monaco
 * editor from a public CDN at runtime, which meant the review surface did not
 * work offline or on an air-gapped network and executed third-party script in
 * the operator's session. A read-only diff needs none of that.
 */

import { useMemo } from "react";

type Line = {
  kind: "meta" | "hunk" | "add" | "remove" | "context";
  text: string;
  oldNumber: number | null;
  newNumber: number | null;
};

const MAX_LINES = 4000;

export function parseDiff(diff: string): { file: string; lines: Line[] }[] {
  const files: { file: string; lines: Line[] }[] = [];
  let current: { file: string; lines: Line[] } | null = null;
  let oldNumber = 0;
  let newNumber = 0;
  let emitted = 0;

  for (const raw of diff.split("\n")) {
    if (emitted >= MAX_LINES) break;
    if (raw.startsWith("diff --git")) {
      const match = /b\/(.+)$/.exec(raw);
      current = { file: match?.[1] ?? raw, lines: [] };
      files.push(current);
      continue;
    }
    if (!current) {
      current = { file: "(diff)", lines: [] };
      files.push(current);
    }
    if (raw.startsWith("@@")) {
      const match = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
      oldNumber = Number(match?.[1] ?? 1);
      newNumber = Number(match?.[2] ?? 1);
      current.lines.push({ kind: "hunk", text: raw, oldNumber: null, newNumber: null });
      emitted += 1;
      continue;
    }
    if (
      raw.startsWith("index ") ||
      raw.startsWith("--- ") ||
      raw.startsWith("+++ ") ||
      raw.startsWith("new file") ||
      raw.startsWith("deleted file") ||
      raw.startsWith("similarity index") ||
      raw.startsWith("rename ") ||
      raw.startsWith("Binary files")
    ) {
      current.lines.push({ kind: "meta", text: raw, oldNumber: null, newNumber: null });
      emitted += 1;
      continue;
    }
    if (raw.startsWith("+")) {
      current.lines.push({
        kind: "add",
        text: raw.slice(1),
        oldNumber: null,
        newNumber: newNumber++,
      });
    } else if (raw.startsWith("-")) {
      current.lines.push({
        kind: "remove",
        text: raw.slice(1),
        oldNumber: oldNumber++,
        newNumber: null,
      });
    } else if (raw.startsWith("\\")) {
      current.lines.push({ kind: "meta", text: raw, oldNumber: null, newNumber: null });
    } else {
      current.lines.push({
        kind: "context",
        text: raw.slice(1),
        oldNumber: oldNumber++,
        newNumber: newNumber++,
      });
    }
    emitted += 1;
  }
  return files;
}

export function DiffViewer({
  value,
  selected,
}: {
  value: string;
  selected?: string;
}) {
  const files = useMemo(() => parseDiff(value), [value]);
  const shown = selected ? files.filter((item) => item.file === selected) : files;
  const truncated = value.split("\n").length > MAX_LINES;

  if (shown.length === 0) {
    return <p className="empty">No diff for this selection.</p>;
  }
  return (
    <div className="diff" role="region" aria-label="Unified diff">
      {shown.map((file) => (
        <section key={file.file} className="diff-file">
          <h4 className="diff-name">{file.file}</h4>
          <table>
            <caption className="visually-hidden">Changes to {file.file}</caption>
            <tbody>
              {file.lines.map((line, index) => (
                <tr key={index} className={`diff-${line.kind}`}>
                  <td className="gutter" aria-hidden="true">
                    {line.oldNumber ?? ""}
                  </td>
                  <td className="gutter" aria-hidden="true">
                    {line.newNumber ?? ""}
                  </td>
                  <td className="marker" aria-hidden="true">
                    {line.kind === "add" ? "+" : line.kind === "remove" ? "−" : " "}
                  </td>
                  <td className="code">
                    <span className="visually-hidden">
                      {line.kind === "add"
                        ? "added: "
                        : line.kind === "remove"
                          ? "removed: "
                          : ""}
                    </span>
                    {line.text || " "}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
      {truncated && (
        <p className="meta">
          Diff truncated at {MAX_LINES} lines. Export the run for the full text.
        </p>
      )}
    </div>
  );
}
