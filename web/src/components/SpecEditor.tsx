import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { yaml } from "@codemirror/lang-yaml";
import { bracketMatching, HighlightStyle, indentOnInput, syntaxHighlighting } from "@codemirror/language";
import { linter, lintGutter, type Diagnostic } from "@codemirror/lint";
import { EditorState } from "@codemirror/state";
import { drawSelection, EditorView, highlightActiveLine, highlightActiveLineGutter, keymap, lineNumbers } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";
import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import type { SpecIssue } from "../lib/types";

const highlight = HighlightStyle.define([
  { tag: t.propertyName, color: "var(--accent)" },
  { tag: [t.string, t.special(t.string)], color: "var(--stable)" },
  { tag: [t.number, t.bool, t.null], color: "var(--slow)" },
  { tag: t.comment, color: "var(--ink-3)", fontStyle: "italic" },
  { tag: [t.punctuation, t.separator, t.bracket], color: "var(--ink-3)" },
]);

export interface SpecEditorHandle {
  setValue(text: string): void;
  getValue(): string;
}

export const SpecEditor = forwardRef<
  SpecEditorHandle,
  { value: string; onChange: (text: string) => void; issues: SpecIssue[]; label: string }
>(function SpecEditor({ value, onChange, issues, label }, ref) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const issuesRef = useRef<SpecIssue[]>(issues);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useImperativeHandle(ref, () => ({
    setValue(text: string) {
      const v = view.current;
      if (!v) return;
      v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: text } });
    },
    getValue: () => view.current?.state.doc.toString() ?? "",
  }));

  useEffect(() => {
    if (!host.current) return;
    const serverLint = linter(
      (v) => {
        const doc = v.state.doc;
        return issuesRef.current.map<Diagnostic>((iss) => {
          const line = doc.line(Math.min(Math.max(iss.line ?? 1, 1), doc.lines));
          return { from: line.from, to: line.to, severity: "error", message: iss.path ? `${iss.path}: ${iss.message}` : iss.message };
        });
      },
      { delay: 0 },
    );
    const v = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          highlightActiveLineGutter(),
          history(),
          drawSelection(),
          indentOnInput(),
          bracketMatching(),
          highlightActiveLine(),
          yaml(),
          syntaxHighlighting(highlight),
          lintGutter(),
          serverLint,
          keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
          EditorView.contentAttributes.of({ "aria-label": label }),
          EditorView.updateListener.of((u) => {
            if (u.docChanged) onChangeRef.current(u.state.doc.toString());
          }),
        ],
      }),
    });
    view.current = v;
    return () => {
      v.destroy();
      view.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    issuesRef.current = issues;
    const v = view.current;
    if (v) v.dispatch({});
  }, [issues]);

  return <div className="editor-host" ref={host} />;
});
