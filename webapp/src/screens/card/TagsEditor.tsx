/** Tags of a card: chips with ✕ and a field to add more (saved at once). */
import { useState } from "react";
import { normalizeTags } from "../../lib/format";

export const TAGS_MAX = 20;

/** `tags` plus what was typed (normalized, no repeats, at most TAGS_MAX). */
export function addTags(tags: string[], typed: string): string[] {
  return [...new Set([...tags, ...normalizeTags(typed)])].slice(0, TAGS_MAX);
}

export function TagsEditor({
  tags,
  disabled,
  onChange,
}: {
  tags: string[];
  disabled: boolean;
  onChange: (tags: string[]) => void;
}) {
  const [text, setText] = useState("");
  const commit = (typed = text) => {
    const next = addTags(tags, typed);
    setText("");
    if (next.join(" ") !== tags.join(" ")) onChange(next);
  };
  return (
    <div className="chips">
      {tags.map((tag) => (
        <button
          key={tag}
          type="button"
          className="chip chip--tag"
          disabled={disabled}
          onClick={() => onChange(tags.filter((t) => t !== tag))}
          aria-label={`Убрать тег ${tag}`}
        >
          #{tag} <span aria-hidden="true">✕</span>
        </button>
      ))}
      {tags.length < TAGS_MAX && (
        <input
          className="chip-input"
          value={text}
          disabled={disabled}
          placeholder={tags.length ? "ещё тег" : "добавить тег"}
          aria-label="Новый тег"
          autoCapitalize="none"
          autoCorrect="off"
          enterKeyHint="done"
          onChange={(e) => {
            const value = e.target.value;
            if (/[\s,]$/.test(value)) commit(value);
            else setText(value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit();
            }
          }}
          onBlur={() => text.trim() && commit()}
        />
      )}
    </div>
  );
}
